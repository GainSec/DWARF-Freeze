// Copyright 2026 PRAGMA
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.
//! Offering byte strings (network traffic) to the agent.

use cbor4ii::core::{
    Value as Cbor,
    dec::Decode,
    enc::Encode,
    utils::{BufWriter, SliceReader},
};
use serde_json::Value as Json;

use crate::{
    agent::{Agent, Draft, Verdict},
    value,
};

/// What the debugger decided for some bytes.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum BytesVerdict {
    Forward,
    Drop,
    Replace(Vec<u8>),
    ReplaceStale(Vec<u8>),
}

/// Some bytes on their way into or out of the node.
#[derive(Debug, Clone)]
pub struct BytesItem<'a> {
    pub kind: &'static str,
    pub from: String,
    pub to: String,
    pub type_name: String,
    pub label: String,
    pub cold_spots: Vec<&'static str>,
    pub bytes: &'a [u8],
    /// Whether the bytes are exactly one CBOR item, to be shown decoded as well as raw.
    pub is_cbor: bool,
    /// The step that led to these bytes, and the root of its chain; zero when unknown.
    pub parent: u64,
    pub cause: u64,
}

/// The bytes as a payload, if they start with a well-formed CBOR item.
pub fn cbor_payload(bytes: &[u8]) -> Option<Json> {
    let mut reader = SliceReader::new(bytes);
    let decoded = Cbor::decode(&mut reader).ok()?;
    Some(value::cbor_to_json(&decoded))
}

/// Encode a payload as one CBOR item, with definite lengths and shortest-form integers.
pub fn payload_to_cbor(payload: &Json) -> Result<Vec<u8>, String> {
    let cbor = value::json_to_cbor(payload)?;
    let mut writer = BufWriter::new(Vec::new());
    cbor.encode(&mut writer).map_err(|e| format!("{e:?}"))?;
    Ok(writer.into_inner())
}

/// Offer bytes to the agent and wait for the debugger if they are held.
///
/// An empty replacement counts as a drop.
pub async fn gate_bytes(agent: &Agent, item: BytesItem<'_>) -> (u64, BytesVerdict) {
    let payload = if item.is_cbor { cbor_payload(item.bytes) } else { None };
    let draft = Draft {
        kind: item.kind,
        from: item.from,
        to: item.to,
        type_name: item.type_name,
        label: item.label,
        payload: payload.unwrap_or(Json::Null),
        raw: Some(item.bytes.to_vec()),
        cold_spots: item.cold_spots,
        parent: (item.parent != 0).then_some(item.parent),
        cause: (item.cause != 0).then_some(item.cause),
        editable: true,
        droppable: true,
    };
    let (seq, verdict) = agent.gate(draft).await;
    let (replaced, stale) = match verdict {
        Verdict::Forward => return (seq, BytesVerdict::Forward),
        Verdict::Drop => return (seq, BytesVerdict::Drop),
        Verdict::ReplaceRaw(raw) => (raw, false),
        Verdict::ReplaceRawStale(raw) => (raw, true),
        Verdict::Replace(payload) => match payload_to_cbor(&payload) {
            Ok(raw) => (raw, false),
            Err(error) => {
                agent.log("error", &format!("item {seq}: edit rejected, original kept: {error}"));
                return (seq, BytesVerdict::Forward);
            }
        },
    };
    if replaced.is_empty() {
        (seq, BytesVerdict::Drop)
    } else if stale {
        (seq, BytesVerdict::ReplaceStale(replaced))
    } else {
        (seq, BytesVerdict::Replace(replaced))
    }
}

#[cfg(test)]
mod tests {
    use serde_json::json;

    use super::*;

    /// `[4, 24(h'0102')]`: the shape of a block-fetch block message.
    const BLOCK_MESSAGE: [u8; 7] = [0x82, 0x04, 0xd8, 0x18, 0x42, 0x01, 0x02];

    #[test]
    fn cbor_messages_are_shown_decoded_and_survive_a_round_trip() {
        let payload = cbor_payload(&BLOCK_MESSAGE).unwrap();
        assert_eq!(payload, json!([4, { "$tag": 24, "value": { "$bytes": "0102" } }]));
        assert_eq!(payload_to_cbor(&payload).unwrap(), BLOCK_MESSAGE);
        assert_eq!(cbor_payload(&[0xff, 0x00]), None);
    }

    #[tokio::test]
    async fn held_bytes_can_be_replaced_as_payload_or_raw() {
        let agent = Agent::new(json!({}), None, true);
        let item = || BytesItem {
            kind: "proto_in",
            from: "peer:10.0.0.12:3001".into(),
            to: "stage:blockfetch-1".into(),
            type_name: "ouroboros/3".into(),
            label: "protocol 3".into(),
            cold_spots: vec!["cbor-decode"],
            bytes: &BLOCK_MESSAGE,
            is_cbor: true,
            parent: 0,
            cause: 0,
        };

        let as_payload = tokio::spawn({
            let agent = agent.clone();
            async move { gate_bytes(&agent, item()).await.1 }
        });
        tokio::time::sleep(std::time::Duration::from_millis(20)).await;
        let held = agent.item(1).unwrap();
        assert_eq!(held["raw"], "8204d818420102");
        assert_eq!(held["cold_spots"], json!(["cbor-decode"]));
        agent.resolve(1, Verdict::Replace(json!([4, { "$tag": 24, "value": { "$bytes": "ff" } }]))).unwrap();
        assert_eq!(as_payload.await.unwrap(), BytesVerdict::Replace(vec![0x82, 0x04, 0xd8, 0x18, 0x41, 0xff]));

        let as_raw = tokio::spawn({
            let agent = agent.clone();
            async move { gate_bytes(&agent, item()).await.1 }
        });
        tokio::time::sleep(std::time::Duration::from_millis(20)).await;
        agent.resolve(2, Verdict::ReplaceRaw(vec![])).unwrap();
        assert_eq!(as_raw.await.unwrap(), BytesVerdict::Drop, "an empty replacement is a drop");

        let as_stale = tokio::spawn({
            let agent = agent.clone();
            async move { gate_bytes(&agent, item()).await.1 }
        });
        tokio::time::sleep(std::time::Duration::from_millis(20)).await;
        agent.resolve(3, Verdict::ReplaceRawStale(vec![0x01])).unwrap();
        assert_eq!(as_stale.await.unwrap(), BytesVerdict::ReplaceStale(vec![0x01]));
    }
}
