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
//! An in-process agent that lets an external debugger hold, inspect, edit and release every
//! step a node takes.
//!
//! The agent does nothing unless it has been installed, which a node does at start-up when
//! `DWARF_FREEZE_LISTEN` is set. Hooks elsewhere in the node call [`agent()`] and skip all work
//! when it returns `None`.

use std::sync::{Arc, OnceLock};

use serde_json::Value as Json;

pub mod agent;
pub mod calls;
pub mod capabilities;
pub mod predicate;
pub mod probes;
pub mod server;
pub mod trace;
pub mod value;
pub mod wire;

pub use agent::{Action, Agent, Draft, Event, Injector, Pending, StateProvider, Verdict};
/// Report every call of the function this is placed on while a debugger has it enabled; see
/// [`calls`]. `#[trace_fn]` on a free function, `#[trace_fn(in = "Type")]` on a method, whose
/// path is then `crate::module::Type::method`.
pub use amaru_freeze_macros::trace as trace_fn;
pub use serde_json;

static AGENT: OnceLock<Arc<Agent>> = OnceLock::new();

/// The installed agent, if this process is being debugged.
#[inline]
pub fn agent() -> Option<&'static Arc<Agent>> {
    AGENT.get()
}

/// Make `agent` the process-wide agent. Returns false if one was already installed.
pub fn install(agent: Arc<Agent>) -> bool {
    AGENT.set(agent).is_ok()
}

/// Install an agent and open its control socket if `DWARF_FREEZE_LISTEN` is set.
///
/// `DWARF_FREEZE_TOKEN` is the shared secret debuggers must present. `DWARF_FREEZE_START=frozen`
/// holds the node at its very first step until a debugger releases it. `DWARF_FREEZE_PROBES`
/// switches probes on from the start: `all`, or names and `prefix*` patterns separated by commas.
/// `DWARF_FREEZE_TRACE` traces functions from the start: path prefixes separated by commas, as
/// [`calls::enable`] takes them. `DWARF_FREEZE_CAPABILITIES` narrows what the node says it can do
/// to what its build was given: see [`capabilities`].
pub fn init_from_env(mut info: Json) -> Option<&'static Arc<Agent>> {
    let listen = std::env::var("DWARF_FREEZE_LISTEN").ok().filter(|v| !v.is_empty())?;
    let Some(token) = std::env::var("DWARF_FREEZE_TOKEN").ok().filter(|value| !value.trim().is_empty()) else {
        tracing::error!("freeze agent disabled: DWARF_FREEZE_TOKEN is missing or empty");
        return None;
    };
    let start_frozen = std::env::var("DWARF_FREEZE_START").is_ok_and(|v| v.eq_ignore_ascii_case("frozen"));
    let configured = std::env::var("DWARF_FREEZE_DEVNET_CONFIG").ok().filter(|value| !value.is_empty());
    let available = configured.as_deref().is_some_and(|path| std::path::Path::new(path).is_dir());
    let requested = std::env::var("DWARF_FREEZE_RESIGN")
        .is_ok_and(|value| matches!(value.to_ascii_lowercase().as_str(), "1" | "true" | "yes" | "on"));
    let resigning_error =
        (requested && !available).then(|| "controlled devnet configuration mount is missing".to_string());
    let allowed = std::env::var("DWARF_FREEZE_CAPABILITIES").ok().map(|list| capabilities::parse(&list));
    let allowed = allowed.filter(|allowed| !allowed.is_empty());
    if let Some(allowed) = &allowed {
        capabilities::restrict(&mut info, allowed);
    }
    let new_agent = Agent::new(info, Some(token), start_frozen);
    if let Some(allowed) = &allowed {
        new_agent.probes().restrict(allowed);
    }
    new_agent.configure_resigning(available, requested, resigning_error);
    if let Ok(listed) = std::env::var("DWARF_FREEZE_PROBES") {
        new_agent.probes().enable_listed(&listed);
    }
    if let Ok(listed) = std::env::var("DWARF_FREEZE_TRACE") {
        calls::registry().enable_listed(&listed);
    }
    let prepared = match server::prepare(new_agent.clone(), &listen) {
        Ok(prepared) => prepared,
        Err(error) => {
            tracing::error!(%error, %listen, "freeze agent could not open its control socket");
            return None;
        }
    };
    if !install(new_agent) {
        return agent();
    }
    let installed = agent()?;
    let address = prepared.activate();
    tracing::warn!(%address, start_frozen, "freeze agent listening: this node can be controlled remotely");
    Some(installed)
}

#[cfg(test)]
mod tests {
    use std::{net::TcpListener, process::Command};

    use serde_json::json;

    use super::*;

    #[test]
    fn occupied_listen_address_does_not_install_a_frozen_agent() {
        const CHILD: &str = "DWARF_FREEZE_OCCUPIED_LISTEN_CHILD";
        if std::env::var_os(CHILD).is_some() {
            assert!(agent().is_none());
            assert!(init_from_env(json!({ "kind": "test" })).is_none());
            assert!(agent().is_none(), "failed socket startup must not expose an unreachable agent");
            return;
        }

        let occupied = TcpListener::bind("127.0.0.1:0").unwrap();
        let output = Command::new(std::env::current_exe().unwrap())
            .args(["--exact", "tests::occupied_listen_address_does_not_install_a_frozen_agent", "--nocapture"])
            .env(CHILD, "1")
            .env("DWARF_FREEZE_LISTEN", occupied.local_addr().unwrap().to_string())
            .env("DWARF_FREEZE_TOKEN", "secret")
            .env("DWARF_FREEZE_START", "frozen")
            .output()
            .unwrap();

        assert!(
            output.status.success(),
            "child failed:\nstdout:\n{}\nstderr:\n{}",
            String::from_utf8_lossy(&output.stdout),
            String::from_utf8_lossy(&output.stderr),
        );
    }
}
