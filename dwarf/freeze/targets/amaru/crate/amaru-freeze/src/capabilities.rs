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

//! What the build of this node was given, and so what the node may say it can do.
//!
//! `DWARF_FREEZE_CAPABILITIES` is a comma-separated list written by whoever built the node. When
//! it is set and names at least one capability, the `features` of the node are narrowed to the
//! allowed ones and the list itself is told to debuggers as `capabilities`. When it is unset or
//! empty, nothing changes.

use serde_json::{Value as Json, json};

/// The capabilities named in a comma-separated list, in order, each once.
pub fn parse(list: &str) -> Vec<String> {
    let mut allowed: Vec<String> = Vec::new();
    for entry in list.split(',').map(str::trim).filter(|entry| !entry.is_empty()) {
        if !allowed.iter().any(|known| known == entry) {
            allowed.push(entry.to_string());
        }
    }
    allowed
}

/// Whether `feature` is kept when only `allowed` was built in.
///
/// A feature is kept if `allowed` has an entry equal to it, or an entry it refines: `feature`
/// starts with that entry followed by `:`. So `state` keeps `state:chain` and
/// `state:chain:write`, `state:chain` keeps `state:chain:write` but not `state:ledger`, and
/// `probes:consensus` does not keep the plain `probes`.
pub fn allows(allowed: &[String], feature: &str) -> bool {
    allowed
        .iter()
        .any(|entry| feature.strip_prefix(entry.as_str()).is_some_and(|rest| rest.is_empty() || rest.starts_with(':')))
}

/// Narrow the `features` of a node's description to the allowed ones and add the allowed list
/// to it as `capabilities`.
pub fn restrict(info: &mut Json, allowed: &[String]) {
    if let Some(features) = info.get_mut("features").and_then(Json::as_array_mut) {
        features.retain(|feature| feature.as_str().is_some_and(|feature| allows(allowed, feature)));
    }
    if let Some(info) = info.as_object_mut() {
        info.insert("capabilities".to_string(), json!(allowed));
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_feature_is_kept_when_it_or_what_it_refines_is_allowed() {
        let allowed = parse(" cause, state:chain ,probes:consensus,,trace , cause");
        assert_eq!(allowed, ["cause", "state:chain", "probes:consensus", "trace"]);
        for kept in ["cause", "state:chain", "state:chain:write", "probes:consensus", "trace", "trace:hold"] {
            assert!(allows(&allowed, kept), "{kept}");
        }
        for left_out in ["state", "state:ledger", "state:chains", "probes", "probes:ledger", "tracer", "events:filter"]
        {
            assert!(!allows(&allowed, left_out), "{left_out}");
        }
        assert!(parse(" , ").is_empty());
    }

    #[test]
    fn the_description_of_the_node_is_narrowed_and_names_what_was_allowed() {
        let mut info = json!({
            "implementation": "amaru",
            "features": ["cause", "state:chain", "state:ledger", "probes", "probes:ledger", "trace", "trace:hold"],
        });
        restrict(&mut info, &parse("state,probes:ledger,trace:hold,unknown"));
        assert_eq!(
            info,
            json!({
                "implementation": "amaru",
                "features": ["state:chain", "state:ledger", "probes:ledger", "trace:hold"],
                "capabilities": ["state", "probes:ledger", "trace:hold", "unknown"],
            })
        );
    }
}
