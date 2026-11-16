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

//! Lossless conversion between CBOR values and the tagged JSON used on the control socket.
//!
//! JSON cannot represent byte strings, large integers, tags or maps with non-text keys, so
//! those are wrapped: `{"$bytes": "<hex>"}`, `{"$int": "<decimal>"}`,
//! `{"$tag": n, "value": v}`, `{"$map": [[k, v], ...]}` and `{"$float": "nan" | "inf" | "-inf"}`.
//! A map whose keys are all text and do not start with `$` is a plain JSON object.

use cbor4ii::core::Value as Cbor;
use serde_json::{Map, Number, Value as Json, json};

const SAFE_INT: i128 = 9_007_199_254_740_991;

pub fn cbor_to_json(value: &Cbor) -> Json {
    match value {
        Cbor::Null => Json::Null,
        Cbor::Bool(b) => Json::Bool(*b),
        Cbor::Integer(i) => {
            if (-SAFE_INT..=SAFE_INT).contains(i) {
                Json::Number(Number::from(*i as i64))
            } else {
                json!({ "$int": i.to_string() })
            }
        }
        Cbor::Float(f) => match Number::from_f64(*f) {
            Some(n) => Json::Number(n),
            None if f.is_nan() => json!({ "$float": "nan" }),
            None if *f > 0.0 => json!({ "$float": "inf" }),
            None => json!({ "$float": "-inf" }),
        },
        Cbor::Bytes(bytes) => json!({ "$bytes": hex::encode(bytes) }),
        Cbor::Text(text) => Json::String(text.clone()),
        Cbor::Array(items) => Json::Array(items.iter().map(cbor_to_json).collect()),
        Cbor::Map(entries) => {
            let plain = entries.iter().all(|(k, _)| matches!(k, Cbor::Text(t) if !t.starts_with('$')));
            if plain {
                let mut object = Map::new();
                let mut unique = true;
                for (k, v) in entries {
                    if let Cbor::Text(t) = k {
                        unique &= object.insert(t.clone(), cbor_to_json(v)).is_none();
                    }
                }
                if unique {
                    return Json::Object(object);
                }
            }
            let pairs: Vec<Json> =
                entries.iter().map(|(k, v)| Json::Array(vec![cbor_to_json(k), cbor_to_json(v)])).collect();
            json!({ "$map": pairs })
        }
        Cbor::Tag(tag, inner) => json!({ "$tag": tag, "value": cbor_to_json(inner) }),
        other => json!({ "$unsupported": format!("{other:?}") }),
    }
}

pub fn json_to_cbor(value: &Json) -> Result<Cbor, String> {
    Ok(match value {
        Json::Null => Cbor::Null,
        Json::Bool(b) => Cbor::Bool(*b),
        Json::Number(n) => {
            if let Some(i) = n.as_i64() {
                Cbor::Integer(i128::from(i))
            } else if let Some(u) = n.as_u64() {
                Cbor::Integer(i128::from(u))
            } else {
                Cbor::Float(n.as_f64().ok_or_else(|| format!("unrepresentable number {n}"))?)
            }
        }
        Json::String(s) => Cbor::Text(s.clone()),
        Json::Array(items) => Cbor::Array(items.iter().map(json_to_cbor).collect::<Result<_, _>>()?),
        Json::Object(object) => {
            if let Some(wrapped) = unwrap_special(object)? {
                return Ok(wrapped);
            }
            let mut entries = Vec::with_capacity(object.len());
            for (k, v) in object {
                entries.push((Cbor::Text(k.clone()), json_to_cbor(v)?));
            }
            Cbor::Map(entries)
        }
    })
}

fn unwrap_special(object: &Map<String, Json>) -> Result<Option<Cbor>, String> {
    if let Some(Json::String(hex_text)) = object.get("$bytes") {
        return hex::decode(hex_text).map(|b| Some(Cbor::Bytes(b))).map_err(|e| format!("bad $bytes: {e}"));
    }
    if let Some(Json::String(decimal)) = object.get("$int") {
        return decimal.parse::<i128>().map(|i| Some(Cbor::Integer(i))).map_err(|e| format!("bad $int: {e}"));
    }
    if let Some(Json::String(name)) = object.get("$float") {
        return match name.as_str() {
            "nan" => Ok(Some(Cbor::Float(f64::NAN))),
            "inf" => Ok(Some(Cbor::Float(f64::INFINITY))),
            "-inf" => Ok(Some(Cbor::Float(f64::NEG_INFINITY))),
            other => Err(format!("bad $float: {other}")),
        };
    }
    if let Some(tag) = object.get("$tag") {
        let tag = tag.as_u64().ok_or("bad $tag: not an unsigned integer")?;
        let inner = object.get("value").ok_or("bad $tag: missing value")?;
        return Ok(Some(Cbor::Tag(tag, Box::new(json_to_cbor(inner)?))));
    }
    if let Some(Json::Array(pairs)) = object.get("$map") {
        let mut entries = Vec::with_capacity(pairs.len());
        for pair in pairs {
            match pair.as_array().map(Vec::as_slice) {
                Some([k, v]) => entries.push((json_to_cbor(k)?, json_to_cbor(v)?)),
                _ => return Err("bad $map: every entry must be a [key, value] pair".to_string()),
            }
        }
        return Ok(Some(Cbor::Map(entries)));
    }
    Ok(None)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn round_trip(value: Cbor) {
        let json = cbor_to_json(&value);
        let text = serde_json::to_string(&json).unwrap();
        let back = json_to_cbor(&serde_json::from_str(&text).unwrap()).unwrap();
        assert_eq!(format!("{value:?}"), format!("{back:?}"), "via {text}");
    }

    #[test]
    fn scalars_and_wrappers_round_trip() {
        round_trip(Cbor::Null);
        round_trip(Cbor::Bool(true));
        round_trip(Cbor::Integer(301_204));
        round_trip(Cbor::Integer(-1));
        round_trip(Cbor::Integer(i128::from(u64::MAX)));
        round_trip(Cbor::Integer(-i128::from(u64::MAX)));
        round_trip(Cbor::Float(1.5));
        round_trip(Cbor::Float(f64::INFINITY));
        round_trip(Cbor::Bytes(vec![0, 255, 16]));
        round_trip(Cbor::Text("addr1".into()));
        round_trip(Cbor::Tag(24, Box::new(Cbor::Bytes(vec![1, 2]))));
    }

    #[test]
    fn maps_keep_their_shape() {
        let plain = Cbor::Map(vec![(Cbor::Text("fee".into()), Cbor::Integer(1))]);
        assert_eq!(cbor_to_json(&plain), json!({ "fee": 1 }));
        round_trip(plain);

        let keyed = Cbor::Map(vec![(Cbor::Integer(0), Cbor::Array(vec![])), (Cbor::Integer(2), Cbor::Integer(7))]);
        assert_eq!(cbor_to_json(&keyed), json!({ "$map": [[0, []], [2, 7]] }));
        round_trip(keyed);

        round_trip(Cbor::Map(vec![(Cbor::Text("$bytes".into()), Cbor::Integer(1))]));
        round_trip(Cbor::Map(vec![
            (Cbor::Text("a".into()), Cbor::Integer(1)),
            (Cbor::Text("a".into()), Cbor::Integer(2)),
        ]));
    }

    #[test]
    fn malformed_wrappers_are_rejected() {
        assert!(json_to_cbor(&json!({ "$bytes": "zz" })).is_err());
        assert!(json_to_cbor(&json!({ "$int": "12x" })).is_err());
        assert!(json_to_cbor(&json!({ "$map": [[1]] })).is_err());
        assert!(json_to_cbor(&json!({ "$tag": -1, "value": 0 })).is_err());
    }
}
