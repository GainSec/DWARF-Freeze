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
//! Named points inside functions of the node that a debugger can switch on.
//!
//! A probe is one line at a place worth knowing about: a check that failed, a decision that was
//! taken. Every probe counts how often it was reached. A probe that is switched on also reports
//! an item of kind `probe` each time, carrying a few small fields. Probes never hold the node.
//!
//! Without an installed agent [`hit`] returns after a single load and evaluates nothing.

use std::{
    collections::BTreeMap,
    sync::{
        Arc,
        atomic::{AtomicBool, AtomicU64, Ordering},
    },
};

use parking_lot::{Mutex, RwLock};
use serde_json::{Value as Json, json};

use crate::agent::{Agent, Draft};

/// Every probe placed in the node: its name and where it sits, as
/// `<file relative to the repository root>::<function>`.
///
/// All of them are listed from the start, so a debugger can see probes that were never reached.
pub const KNOWN: &[(&str, &str)] = &[
    ("blockfetch.block_received", "crates/amaru-consensus/src/stages/fetch_blocks/mod.rs::FetchBlocks::block"),
    ("blockfetch.request", "crates/amaru-consensus/src/stages/fetch_blocks/mod.rs::FetchBlocks::request_blocks"),
    ("chain_store.roll_forward", "crates/amaru-protocols/src/store_effects.rs::RollForwardChainEffect::run"),
    ("chain_store.store_block", "crates/amaru-protocols/src/store_effects.rs::StoreBlockEffect::run"),
    ("chain_store.store_header", "crates/amaru-protocols/src/store_effects.rs::StoreValidatedHeaderEffect::run"),
    ("chain_store.switch_to_fork", "crates/amaru-protocols/src/store_effects.rs::SwitchToForkEffect::run"),
    ("connection.handshake.accepted", "crates/amaru-protocols/src/connection.rs::do_handshake"),
    ("connection.handshake.refused", "crates/amaru-protocols/src/connection.rs::do_handshake"),
    ("connection.initialize", "crates/amaru-protocols/src/connection.rs::do_initialize"),
    ("connection.teardown", "crates/amaru-protocols/src/connection.rs::teardown"),
    ("consensus.adopt_chain.extend", "crates/amaru-consensus/src/stages/adopt_chain/mod.rs::adopt_tip"),
    ("consensus.adopt_chain.switch_fork", "crates/amaru-consensus/src/stages/adopt_chain/mod.rs::adopt_tip"),
    (
        "consensus.select_chain.best_tip_invalidated",
        "crates/amaru-consensus/src/stages/select_chain/mod.rs::SelectChain::handle_block_validation_result",
    ),
    (
        "consensus.select_chain.extend",
        "crates/amaru-consensus/src/stages/select_chain/mod.rs::SelectChain::handle_tip_from_upstream",
    ),
    (
        "consensus.select_chain.fork",
        "crates/amaru-consensus/src/stages/select_chain/mod.rs::SelectChain::handle_tip_from_upstream",
    ),
    (
        "consensus.select_chain.new_best_tip",
        "crates/amaru-consensus/src/stages/select_chain/mod.rs::SelectChain::handle_tip_from_upstream",
    ),
    (
        "consensus.select_chain.reject",
        "crates/amaru-consensus/src/stages/select_chain/mod.rs::SelectChain::handle_tip_from_upstream",
    ),
    (
        "consensus.track_peers.adversarial",
        "crates/amaru-consensus/src/stages/track_peers/mod.rs::TrackPeers::try_roll_forward",
    ),
    (
        "consensus.track_peers.defer.clock_skew",
        "crates/amaru-consensus/src/stages/track_peers/mod.rs::TrackPeers::try_roll_forward",
    ),
    (
        "consensus.track_peers.defer.ledger_height",
        "crates/amaru-consensus/src/stages/track_peers/mod.rs::TrackPeers::handle_from_upstream",
    ),
    (
        "consensus.track_peers.defer.stake_distribution",
        "crates/amaru-consensus/src/stages/track_peers/mod.rs::TrackPeers::try_roll_forward",
    ),
    (
        "consensus.track_peers.duplicate_header",
        "crates/amaru-consensus/src/stages/track_peers/mod.rs::TrackPeers::try_roll_forward",
    ),
    (
        "consensus.track_peers.invalid_height",
        "crates/amaru-consensus/src/stages/track_peers/mod.rs::TrackPeers::validate_header",
    ),
    (
        "consensus.track_peers.invalid_parent",
        "crates/amaru-consensus/src/stages/track_peers/mod.rs::TrackPeers::validate_header",
    ),
    (
        "consensus.track_peers.roll_backward",
        "crates/amaru-consensus/src/stages/track_peers/mod.rs::TrackPeers::handle_from_upstream",
    ),
    ("consensus.validate_header", "crates/amaru-consensus/src/effects/ledger_effects.rs::ValidateHeaderEffect::run"),
    ("consensus.validate_header.kes", "crates/amaru-ouroboros/src/praos/header.rs::assert_all"),
    ("consensus.validate_header.leader_stake", "crates/amaru-ouroboros/src/praos/header.rs::assert_all"),
    ("consensus.validate_header.opcert", "crates/amaru-ouroboros/src/praos/header.rs::assert_all"),
    ("consensus.validate_header.vrf", "crates/amaru-ouroboros/src/praos/header.rs::assert_all"),
    ("ledger.block.invalid", "crates/amaru-consensus/src/stages/validate_block/mod.rs::ValidateBlock::error"),
    ("ledger.block.valid", "crates/amaru-consensus/src/stages/validate_block/mod.rs::ValidateBlock::completed"),
    ("ledger.block_rule.body_size", "crates/amaru-ledger/src/rules/block.rs::execute"),
    ("ledger.block_rule.ex_units", "crates/amaru-ledger/src/rules/block.rs::execute"),
    ("ledger.block_rule.header_size", "crates/amaru-ledger/src/rules/block.rs::execute"),
    ("ledger.block_rule.ref_scripts_size", "crates/amaru-ledger/src/rules/block.rs::execute"),
    ("ledger.epoch_transition", "crates/amaru-ledger/src/state.rs::State::try_epoch_transition"),
    ("ledger.rule.certificates", "crates/amaru-ledger/src/rules/transaction/phase_one/mod.rs::when_valid"),
    ("ledger.rule.collateral", "crates/amaru-ledger/src/rules/transaction/phase_one/mod.rs::execute"),
    ("ledger.rule.collateral_return", "crates/amaru-ledger/src/rules/transaction/phase_one/mod.rs::execute"),
    ("ledger.rule.fees", "crates/amaru-ledger/src/rules/transaction/phase_one/mod.rs::when_valid"),
    ("ledger.rule.inputs", "crates/amaru-ledger/src/rules/transaction/phase_one/mod.rs::when_valid"),
    ("ledger.rule.metadata", "crates/amaru-ledger/src/rules/transaction/phase_one/mod.rs::when_valid"),
    ("ledger.rule.mint", "crates/amaru-ledger/src/rules/transaction/phase_one/mod.rs::when_valid"),
    ("ledger.rule.native_scripts", "crates/amaru-ledger/src/rules/transaction/phase_one/scripts.rs::execute"),
    ("ledger.rule.network", "crates/amaru-ledger/src/rules/transaction/phase_one/mod.rs::when_valid"),
    ("ledger.rule.outputs", "crates/amaru-ledger/src/rules/transaction/phase_one/mod.rs::when_valid"),
    ("ledger.rule.proposals", "crates/amaru-ledger/src/rules/transaction/phase_one/mod.rs::when_valid"),
    ("ledger.rule.scripts", "crates/amaru-ledger/src/rules/transaction/phase_one/mod.rs::when_valid"),
    ("ledger.rule.size", "crates/amaru-ledger/src/rules/transaction/phase_one/mod.rs::when_valid"),
    ("ledger.rule.treasury_value", "crates/amaru-ledger/src/rules/transaction/phase_one/mod.rs::when_valid"),
    ("ledger.rule.validity_interval", "crates/amaru-ledger/src/rules/transaction/phase_one/mod.rs::when_valid"),
    ("ledger.rule.value_preserved", "crates/amaru-ledger/src/rules/transaction/phase_one/mod.rs::when_valid"),
    ("ledger.rule.verification_key_witness", "crates/amaru-ledger/src/rules/transaction/phase_one/mod.rs::when_valid"),
    ("ledger.rule.voting_procedures", "crates/amaru-ledger/src/rules/transaction/phase_one/mod.rs::when_valid"),
    ("ledger.rule.withdrawals", "crates/amaru-ledger/src/rules/transaction/phase_one/mod.rs::when_valid"),
    ("ledger.script.eval", "crates/amaru-ledger/src/rules/transaction/phase_two/mod.rs::execute"),
    ("ledger.snapshot.taken", "crates/amaru-ledger/src/state/volatile/overlay.rs::StateOverlay::apply"),
    ("ledger.verdict.block", "crates/amaru-ledger/src/state.rs::State::roll_forward"),
    ("ledger.verdict.transaction", "crates/amaru-ledger/src/rules/block.rs::validate_transaction"),
    ("mempool.accepted", "crates/amaru-consensus/src/stages/mempool/stage.rs::validate_and_insert"),
    ("mempool.rejected", "crates/amaru-consensus/src/stages/mempool/stage.rs::validate_and_insert"),
    ("mux.error", "crates/amaru-protocols/src/mux.rs::stage"),
    ("mux.read_error", "crates/amaru-protocols/src/mux.rs::read_segment"),
    ("peer_selection.added", "crates/amaru-consensus/src/stages/peer_selection/mod.rs::stage"),
    ("peer_selection.adversarial", "crates/amaru-consensus/src/stages/peer_selection/mod.rs::stage"),
    ("peer_selection.cooldown", "crates/amaru-consensus/src/stages/peer_selection/mod.rs::PeerSelection::ban_peer"),
    ("peer_selection.removed", "crates/amaru-consensus/src/stages/peer_selection/mod.rs::PeerSelection::ban_peer"),
    ("protocol.error", "crates/amaru-protocols/src/protocol/miniprotocol.rs::miniprotocol"),
];

/// The group a probe belongs to, by the crate it sits in. A build that leaves a group out has
/// none of its probes.
fn group(location: &str) -> &'static str {
    const GROUPS: [(&str, &str); 3] = [
        ("crates/amaru-consensus/", "consensus"),
        ("crates/amaru-ouroboros/", "header"),
        ("crates/amaru-ledger/", "ledger"),
    ];
    GROUPS.iter().find(|(directory, _)| location.contains(directory)).map_or(NODE_GROUP, |(_, group)| group)
}

/// The group of the probes that sit in none of the crates with a group of their own.
const NODE_GROUP: &str = "node";

struct Probe {
    location: String,
    group: &'static str,
    enabled: AtomicBool,
    hits: AtomicU64,
}

/// Whether `pattern` selects the probe called `name`.
///
/// `all` selects every probe, `prefix*` those whose name starts with `prefix`, anything else the
/// probe of exactly that name.
fn selects(pattern: &str, name: &str) -> bool {
    match pattern.strip_suffix('*') {
        Some(prefix) => name.starts_with(prefix),
        None => pattern == "all" || pattern == name,
    }
}

/// The probes of one agent.
pub struct Registry {
    probes: RwLock<BTreeMap<String, Arc<Probe>>>,
    /// What was asked for so far, in order, so that a probe first seen later starts in the
    /// state it would have had if it had been known from the start.
    rules: Mutex<Vec<(String, bool)>>,
    /// The groups a debugger may list and switch; `None` while every probe is covered.
    groups: RwLock<Option<Vec<String>>>,
}

impl Default for Registry {
    fn default() -> Self {
        Self::new()
    }
}

impl Registry {
    /// A registry holding every probe of [`KNOWN`], switched off and never reached.
    pub fn new() -> Self {
        let registry =
            Self { probes: RwLock::new(BTreeMap::new()), rules: Mutex::new(Vec::new()), groups: RwLock::new(None) };
        for (name, location) in KNOWN {
            registry.register(name, location);
        }
        registry
    }

    fn register(&self, name: &str, location: &str) -> Arc<Probe> {
        let mut probes = self.probes.write();
        if let Some(probe) = probes.get(name) {
            return probe.clone();
        }
        let group = group(location);
        let enabled = self.covers(group)
            && self.rules.lock().iter().rev().find(|(pattern, _)| selects(pattern, name)).is_some_and(|(_, on)| *on);
        let probe = Arc::new(Probe {
            location: location.to_string(),
            group,
            enabled: AtomicBool::new(enabled),
            hits: AtomicU64::new(0),
        });
        probes.insert(name.to_string(), probe.clone());
        probe
    }

    fn covers(&self, group: &str) -> bool {
        self.groups.read().as_ref().is_none_or(|groups| groups.iter().any(|covered| covered == group))
    }

    /// Narrow the probes a debugger can list and switch to what the build of this node has,
    /// given its capabilities (see [`crate::capabilities`]).
    ///
    /// `probes` covers every probe. Otherwise each `probes:<group>` entry covers one group:
    /// `consensus`, `header` and `ledger` are the probes sitting in `crates/amaru-consensus`,
    /// `crates/amaru-ouroboros` and `crates/amaru-ledger`. Probes sitting anywhere else form the
    /// group `node`, which is covered as soon as any group is. With neither kind of entry no
    /// probe is covered. A probe that is not covered is not listed, cannot be switched on, and
    /// is switched off if it was on.
    pub fn restrict(&self, capabilities: &[String]) {
        let mut groups: Vec<String> =
            capabilities.iter().filter_map(|entry| entry.strip_prefix("probes:")).map(str::to_string).collect();
        if !groups.is_empty() {
            groups.push(NODE_GROUP.to_string());
        }
        let everything = capabilities.iter().any(|entry| entry == "probes");
        let probes = self.probes.write();
        *self.groups.write() = (!everything).then_some(groups);
        for probe in probes.values().filter(|probe| !self.covers(probe.group)) {
            probe.enabled.store(false, Ordering::Relaxed);
        }
    }

    /// Switch the probes selected by `pattern` on or off, among those covered (see
    /// [`Registry::restrict`]). Returns how many changed state.
    pub fn set(&self, pattern: &str, enabled: bool) -> usize {
        let pattern = pattern.trim();
        // Taken before the rule is recorded and kept until every probe is updated, so that a
        // probe registering itself meanwhile sees either all of this change or none of it.
        let probes = self.probes.write();
        {
            let mut rules = self.rules.lock();
            if pattern == "all" || pattern == "*" {
                rules.clear();
            } else {
                rules.retain(|(earlier, _)| earlier != pattern);
            }
            rules.push((pattern.to_string(), enabled));
        }
        probes
            .iter()
            .filter(|(name, probe)| selects(pattern, name) && self.covers(probe.group))
            .filter(|(_, probe)| probe.enabled.swap(enabled, Ordering::Relaxed) != enabled)
            .count()
    }

    /// Switch on what a `DWARF_FREEZE_PROBES` value asks for: `all`, or names and `prefix*`
    /// patterns separated by commas. Returns how many probes changed state.
    pub fn enable_listed(&self, list: &str) -> usize {
        list.split(',')
            .map(str::trim)
            .filter(|pattern| !pattern.is_empty())
            .map(|pattern| self.set(pattern, true))
            .sum()
    }

    /// Every covered probe in name order, as `{"name", "where", "enabled", "hits"}`.
    pub fn list(&self) -> Json {
        Json::Array(
            self.probes
                .read()
                .iter()
                .filter(|(_, probe)| self.covers(probe.group))
                .map(|(name, probe)| {
                    json!({
                        "name": name,
                        "where": probe.location,
                        "enabled": probe.enabled.load(Ordering::Relaxed),
                        "hits": probe.hits.load(Ordering::Relaxed),
                    })
                })
                .collect(),
        )
    }

    /// Count one more arrival at `name`. Returns where the probe sits if it is switched on.
    fn count(&self, name: &str, location: &str) -> Option<String> {
        let known = self.probes.read().get(name).cloned();
        let probe = known.unwrap_or_else(|| self.register(name, location));
        probe.hits.fetch_add(1, Ordering::Relaxed);
        probe.enabled.load(Ordering::Relaxed).then(|| probe.location.clone())
    }
}

impl Agent {
    /// The probes of this agent.
    pub fn probes(&self) -> &Registry {
        &self.probes
    }

    /// Count an arrival at probe `name` and, if the probe is switched on, report it as an item.
    ///
    /// `location` is only used for a name that is not in [`KNOWN`]. `fields` is evaluated only
    /// when the item is reported. Returns the item's sequence number, if there is one.
    pub fn probe_hit(&self, name: &str, location: &str, fields: impl FnOnce() -> Json) -> Option<u64> {
        let location = self.probes.count(name, location)?;
        let (parent, cause) = crate::trace::current();
        Some(self.note(Draft {
            kind: "probe",
            from: format!("probe:{name}"),
            label: name.to_string(),
            type_name: location,
            payload: fields(),
            parent: (parent != 0).then_some(parent),
            cause: (cause != 0).then_some(cause),
            ..Draft::default()
        }))
    }
}

/// The text of the error in `result`, for a probe field; `None` when there is none.
pub fn error_text<T, E: std::fmt::Display>(result: &Result<T, E>) -> Option<String> {
    result.as_ref().err().map(ToString::to_string)
}

/// Report that the node reached the probe called `name`.
///
/// Does nothing unless an agent is installed. `fields` is evaluated only if the probe is
/// switched on; keep what it returns small.
#[inline]
pub fn hit(name: &'static str, fields: impl FnOnce() -> Json) {
    hit_at(name, "", fields);
}

/// Like [`hit`], naming the place in the source for a probe that is not in [`KNOWN`].
#[inline]
pub fn hit_at(name: &'static str, location: &'static str, fields: impl FnOnce() -> Json) {
    if let Some(agent) = crate::agent() {
        agent.probe_hit(name, location, fields);
    }
}

/// Mark a place in a function as a probe: `probe!("ledger.block.valid")`, or with a few small
/// fields in the style of `json!`: `probe!("ledger.block.valid", { "slot": slot })`.
///
/// The fields are only evaluated while a debugger has the probe switched on.
#[macro_export]
macro_rules! probe {
    ($name:expr) => {
        $crate::probes::hit_at($name, concat!(file!(), ":", line!()), || $crate::serde_json::json!({}))
    };
    ($name:expr, { $($fields:tt)* }) => {
        $crate::probes::hit_at($name, concat!(file!(), ":", line!()), || $crate::serde_json::json!({ $($fields)* }))
    };
}

#[cfg(test)]
mod tests {
    use super::*;

    fn agent() -> Arc<Agent> {
        Agent::new(json!({}), None, false)
    }

    fn never() -> Json {
        panic!("fields were evaluated although nothing reports them")
    }

    fn entry(agent: &Agent, name: &str) -> Json {
        let list = agent.probes().list();
        list.as_array().unwrap().iter().find(|probe| probe["name"] == name).cloned().unwrap_or(Json::Null)
    }

    #[test]
    fn capabilities_narrow_the_probes_to_the_groups_that_were_built() {
        let names = |agent: &Agent| -> Vec<String> {
            let list = agent.probes().list();
            list.as_array().unwrap().iter().map(|probe| probe["name"].as_str().unwrap().to_string()).collect()
        };
        let allowed = |list: &str| crate::capabilities::parse(list);

        let all = agent();
        all.probes().restrict(&allowed("trace,probes,probes:ledger"));
        assert_eq!(names(&all).len(), KNOWN.len(), "`probes` covers every group");

        let none = agent();
        assert_eq!(none.probes().set("all", true), KNOWN.len());
        none.probes().restrict(&allowed("trace,state:ledger"));
        assert_eq!(none.probes().list(), json!([]));
        assert_eq!(none.probes().set("all", true), 0);
        assert_eq!(none.probe_hit(KNOWN[0].0, "", never), None, "it was switched off");

        let sits_in = |name: &str, directory: &str| {
            KNOWN.iter().any(|(known, location)| *known == name && location.starts_with(directory))
        };
        let expected = |left_out: &[&str]| -> Vec<String> {
            let mut names: Vec<String> = KNOWN
                .iter()
                .filter(|(name, _)| !left_out.iter().any(|directory| sits_in(name, directory)))
                .map(|(name, _)| name.to_string())
                .collect();
            names.sort();
            names
        };

        let some = agent();
        some.probes().restrict(&allowed("probes:consensus,probes:header"));
        let without_ledger = expected(&["crates/amaru-ledger/"]);
        assert!(without_ledger.len() < KNOWN.len());
        assert_eq!(names(&some), without_ledger);
        assert_eq!(some.probes().set("all", true), without_ledger.len());
        assert_eq!(some.probe_hit("fresh.ledger", "crates/amaru-ledger/src/new.rs:1", never), None);
        assert_eq!(entry(&some, "fresh.ledger"), Json::Null);
        assert!(some.probe_hit("fresh.node", "crates/amaru-node/src/new.rs:1", || json!({})).is_some());

        let ledger = agent();
        ledger.probes().restrict(&allowed("probes:ledger"));
        assert_eq!(names(&ledger), expected(&["crates/amaru-consensus/", "crates/amaru-ouroboros/"]));
        assert_eq!(
            (group("crates/amaru-protocols/src/mux.rs::stage"), group("crates/amaru-ouroboros/src/x.rs:1")),
            ("node", "header")
        );
    }

    #[test]
    fn every_known_probe_is_listed_before_it_is_reached() {
        let list = agent().probes().list();
        let listed: Vec<(&str, &str)> = list
            .as_array()
            .unwrap()
            .iter()
            .map(|probe| {
                assert_eq!(probe["enabled"], false);
                assert_eq!(probe["hits"], 0);
                (probe["name"].as_str().unwrap(), probe["where"].as_str().unwrap())
            })
            .collect();
        let mut known = KNOWN.to_vec();
        known.sort();
        assert_eq!(listed, known, "listed in name order, each known name exactly once");
    }

    #[test]
    fn known_probes_say_where_they_are() {
        for (name, location) in KNOWN {
            assert!(!name.is_empty() && name.split('.').all(|part| !part.is_empty()), "bad name `{name}`");
            let (file, function) = location.split_once("::").unwrap_or_else(|| panic!("`{name}` at `{location}`"));
            assert!(file.starts_with("crates/") && file.ends_with(".rs"), "`{name}` at `{location}`");
            assert!(!function.is_empty(), "`{name}` at `{location}`");
        }
    }

    /// The names of the probes placed in each source file of the node, by path from the
    /// repository root. Tests of other crates, which place probes of their own, are left out.
    fn placed() -> BTreeMap<String, Vec<String>> {
        fn visit(dir: &std::path::Path, root: &std::path::Path, found: &mut BTreeMap<String, Vec<String>>) {
            for entry in std::fs::read_dir(dir).unwrap() {
                let path = entry.unwrap().path();
                if path.is_dir() {
                    visit(&path, root, found);
                } else if path.extension().is_some_and(|extension| extension == "rs") {
                    let source = std::fs::read_to_string(&path).unwrap();
                    let names: Vec<String> = source
                        .split("probe!(")
                        .skip(1)
                        .filter_map(|rest| rest.trim_start().strip_prefix('"'))
                        .filter_map(|rest| rest.split_once('"'))
                        .map(|(name, _)| name.to_string())
                        .collect();
                    if !names.is_empty() {
                        let file = path.strip_prefix(root).unwrap().to_string_lossy().replace('\\', "/");
                        found.insert(file, names);
                    }
                }
            }
        }

        let this_crate = std::path::Path::new(env!("CARGO_MANIFEST_DIR"));
        let root = this_crate.parent().and_then(std::path::Path::parent).unwrap();
        let mut found = BTreeMap::new();
        for entry in std::fs::read_dir(root.join("crates")).unwrap() {
            let path = entry.unwrap().path();
            if path != this_crate && path.join("src").is_dir() {
                visit(&path.join("src"), root, &mut found);
            }
        }
        found
    }

    #[test]
    fn known_lists_exactly_the_probes_placed_in_the_node() {
        let placed = placed();
        let mut names: Vec<&str> = placed.values().flatten().map(String::as_str).collect();
        names.sort();
        names.dedup();
        let mut known: Vec<&str> = KNOWN.iter().map(|(name, _)| *name).collect();
        known.sort();
        assert_eq!(names, known, "left: probes placed in the node, right: KNOWN");

        for (name, location) in KNOWN {
            let (file, function) = location.split_once("::").unwrap();
            let in_file = placed.get(file).is_some_and(|names| names.iter().any(|placed| placed == name));
            assert!(in_file, "`{name}` is not placed in `{file}`");
            let function = function.rsplit("::").next().unwrap();
            let root = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).parent().and_then(std::path::Path::parent);
            let source = std::fs::read_to_string(root.unwrap().join(file)).unwrap();
            assert!(source.contains(&format!("fn {function}")), "`{file}` has no function `{function}`");
            for (other, names) in &placed {
                assert!(other == file || !names.contains(&name.to_string()), "`{name}` is also placed in `{other}`");
            }
        }
    }

    #[test]
    fn patterns_switch_probes_on_and_off() {
        let registry = Registry::new();
        for name in ["alpha.one", "alpha.two", "beta.one"] {
            registry.register(name, "crates/x/src/lib.rs::f");
        }
        let on = |name: &str| {
            let list = registry.list();
            list.as_array().unwrap().iter().any(|probe| probe["name"] == name && probe["enabled"] == true)
        };

        assert_eq!(registry.set("alpha.*", true), 2);
        assert!(on("alpha.one") && on("alpha.two") && !on("beta.one"));
        assert_eq!(registry.set("alpha.*", true), 0, "nothing changes the second time");
        assert_eq!(registry.set("alpha.one", false), 1);
        assert!(!on("alpha.one") && on("alpha.two"));
        assert_eq!(registry.set("alpha", true), 0, "a name without `*` is not a prefix");

        assert_eq!(registry.set("all", true), KNOWN.len() + 2);
        assert!(on("alpha.one") && on("beta.one"));
        assert_eq!(registry.set("all", false), KNOWN.len() + 3);
        assert!(!on("alpha.two"));

        assert_eq!(registry.enable_listed(" alpha.one , beta.* ,,"), 2);
        assert!(on("alpha.one") && !on("alpha.two") && on("beta.one"));
    }

    #[test]
    fn hits_are_counted_while_the_probe_is_off() {
        let agent = agent();
        let mut events = agent.subscribe();
        for _ in 0..3 {
            let reported = agent.probe_hit("test.quiet", "crates/x/src/lib.rs:1", never);
            assert_eq!(reported, None);
        }
        assert_eq!(
            entry(&agent, "test.quiet"),
            json!({ "name": "test.quiet", "where": "crates/x/src/lib.rs:1", "enabled": false, "hits": 3 })
        );
        assert!(events.try_recv().is_err(), "a probe that is off reports nothing");
        assert_eq!(agent.status()["seq"], 0);
    }

    #[test]
    fn a_probe_that_is_on_reports_an_item_that_is_never_held() {
        let agent = Agent::new(json!({}), None, true);
        let mut events = agent.subscribe();
        assert_eq!(agent.probe_hit("test.loud", "crates/x/src/lib.rs:2", || json!({ "slot": 1 })), None);
        assert_eq!(agent.probes().set("test.*", true), 1);

        assert_eq!(agent.probe_hit("test.loud", "", || json!({ "slot": 42, "peer": "10.0.0.12:3001" })), Some(1));
        assert_eq!(entry(&agent, "test.loud")["hits"], 2);

        let event: Json = serde_json::from_str(&events.try_recv().unwrap()).unwrap();
        assert_eq!(event["event"], "item");
        let item = &event["data"];
        assert_eq!(item["kind"], "probe");
        assert_eq!(item["label"], "test.loud");
        assert_eq!(item["from"], "probe:test.loud");
        assert_eq!(item["to"], "");
        assert_eq!(item["type"], "crates/x/src/lib.rs:2");
        assert_eq!(item["payload"], json!({ "slot": 42, "peer": "10.0.0.12:3001" }));
        assert_eq!(item["held"], false, "a probe never waits, even while the node is frozen");
        assert!(item.get("parent").is_none() && item.get("cause").is_none(), "no step is known outside a stage");
        assert_eq!(agent.status()["held"], json!([]));
        assert_eq!(agent.item(1).unwrap()["label"], "test.loud");

        assert_eq!(agent.probes().set("test.loud", false), 1);
        assert_eq!(agent.probe_hit("test.loud", "", || json!({})), None);
        assert_eq!(entry(&agent, "test.loud")["hits"], 3);
    }

    #[test]
    fn a_probe_first_reached_later_follows_what_was_asked_before() {
        let agent = agent();
        agent.probes().enable_listed("late.*");
        assert_eq!(agent.probe_hit("late.one", "crates/x/src/lib.rs:3", || json!({})), Some(1));
        agent.probes().set("all", false);
        assert_eq!(agent.probe_hit("late.two", "crates/x/src/lib.rs:4", || json!({})), None);
        assert_eq!(entry(&agent, "late.two")["hits"], 1);
    }

    #[tokio::test]
    async fn a_probe_names_the_step_its_stage_is_handling() {
        let agent = agent();
        agent.probes().set("test.staged", true);
        crate::trace::set_context("probe-test-stage", 7, 3);
        let seq = crate::trace::in_part("probe-test-stage", async {
            agent.probe_hit("test.staged", "crates/x/src/lib.rs:5", || json!({}))
        })
        .await
        .unwrap();
        crate::trace::forget("probe-test-stage");
        let item = agent.item(seq).unwrap();
        assert_eq!((item["parent"].clone(), item["cause"].clone()), (json!(7), json!(3)));
    }

    #[test]
    fn without_an_agent_a_probe_does_nothing() {
        assert!(crate::agent().is_none(), "no test of this crate installs the process-wide agent");
        crate::probe!("test.no_agent", { "never": never() });
        crate::probe!("test.no_agent");
        hit("test.no_agent", never);
    }
}
