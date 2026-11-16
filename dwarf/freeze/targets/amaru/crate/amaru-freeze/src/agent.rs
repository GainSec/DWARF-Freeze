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
//! The freeze state machine: which items are held, who is waiting on them, and how they are released.
//!
//! Every observable step of the node is offered to the agent as an item. The agent gives it a
//! sequence number, publishes it to connected debuggers, and decides whether the step may proceed.
//! While the node is frozen every step is held; a held step waits until a debugger forwards, drops
//! or replaces it.

use std::{
    collections::{BTreeMap, VecDeque},
    sync::{
        Arc, OnceLock,
        atomic::{AtomicU64, Ordering},
        mpsc,
    },
    time::{Duration, Instant, SystemTime, UNIX_EPOCH},
};

use parking_lot::Mutex;
use serde_json::{Value as Json, json};
use tokio::sync::{Notify, broadcast, oneshot};

use crate::{predicate::Predicate, probes::Registry};

/// How many recent items stay retrievable by sequence number after they were released.
const RECENT_ITEMS: usize = 4096;
const EVENT_BACKLOG: usize = 16384;

/// What the debugger decided for a held item.
#[derive(Debug, Clone, PartialEq)]
pub enum Verdict {
    Forward,
    Drop,
    /// Proceed with this payload instead, in tagged JSON form.
    Replace(Json),
    /// Proceed with these bytes instead; only meaningful for items that are bytes.
    ReplaceRaw(Vec<u8>),
    /// Proceed with these exact bytes and bypass dependent-value repair for this one item.
    ReplaceRawStale(Vec<u8>),
}

/// A step the node is about to take, described by whoever hooked it.
#[derive(Debug, Clone, Default)]
pub struct Draft {
    pub kind: &'static str,
    pub from: String,
    pub to: String,
    pub type_name: String,
    pub label: String,
    pub payload: Json,
    pub raw: Option<Vec<u8>>,
    pub cold_spots: Vec<&'static str>,
    pub parent: Option<u64>,
    /// The item at the root of the chain of steps this one belongs to.
    pub cause: Option<u64>,
    pub editable: bool,
    pub droppable: bool,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Action {
    Freeze,
    Log,
}

struct Condition {
    predicate: Predicate,
    source: Json,
    action: Action,
    once: bool,
    hits: u64,
}

enum Waiter {
    Async(oneshot::Sender<Verdict>),
    Blocking(mpsc::SyncSender<Verdict>),
}

impl Waiter {
    fn release(self, verdict: Verdict) {
        match self {
            Waiter::Async(tx) => drop(tx.send(verdict)),
            Waiter::Blocking(tx) => drop(tx.send(verdict)),
        }
    }
}

/// One thing the agent tells the debuggers connected to it.
///
/// The line that goes on the wire is only produced when a connection first needs it, and is
/// then shared by all of them.
pub struct Event {
    name: &'static str,
    seq: Option<u64>,
    held: bool,
    body: Body,
    line: OnceLock<Arc<str>>,
}

enum Body {
    Item(Arc<Json>),
    Data(Json),
}

impl Event {
    /// The name of the event: `item`, `item.done`, `frozen` and so on.
    pub fn name(&self) -> &'static str {
        self.name
    }

    /// The item, for an `item` event.
    pub fn item(&self) -> Option<&Json> {
        match &self.body {
            Body::Item(item) => Some(item),
            Body::Data(_) => None,
        }
    }

    /// The sequence number of the item an `item` or `item.done` event is about.
    pub fn seq(&self) -> Option<u64> {
        self.seq
    }

    /// Whether this is an `item` event for an item that is waiting for a verdict.
    pub fn held(&self) -> bool {
        self.held
    }

    /// The event as one line of JSON, without the line break.
    pub fn line(&self) -> Arc<str> {
        self.line
            .get_or_init(|| {
                let data = match &self.body {
                    Body::Item(item) => &**item,
                    Body::Data(data) => data,
                };
                Arc::from(format!(r#"{{"data":{data},"event":"{}"}}"#, self.name))
            })
            .clone()
    }
}

pub enum Pending {
    Async(oneshot::Receiver<Verdict>),
    Blocking(mpsc::Receiver<Verdict>),
}

struct Inner {
    frozen: bool,
    hold: Option<(Predicate, Json)>,
    conditions: BTreeMap<u64, Condition>,
    next_condition: u64,
    held: BTreeMap<u64, (Arc<Json>, Waiter)>,
    /// Items set aside: still waiting, but no longer keeping the node frozen. Each goes on when
    /// its time comes or when enough matching items have passed it.
    deferred: BTreeMap<u64, (Waiter, Option<(Predicate, u64)>)>,
    recent: VecDeque<Arc<Json>>,
    frozen_since: Option<Instant>,
    frozen_total: Duration,
    resigning_available: bool,
    resigning_enabled: bool,
    resigning_error: Option<String>,
}

/// Sends a payload to a named destination inside the node, as if it had arrived normally.
pub type Injector = Arc<dyn Fn(&Json) -> Result<(), String> + Send + Sync>;

/// Read and write access to one family of node state, addressed by text keys.
pub trait StateProvider: Send + Sync {
    /// Entries in key order, starting after `after`, at most `limit`.
    fn list(&self, prefix: &str, after: Option<&str>, limit: usize) -> Result<Vec<Json>, String>;
    fn get(&self, key: &str) -> Result<Option<Json>, String>;
    fn put(&self, key: &str, value: &Json) -> Result<(), String>;
    fn delete(&self, key: &str) -> Result<(), String>;

    /// What a debugger is told after a `put` or `delete` on `key` was accepted: when the write
    /// takes effect and what it leaves untouched, if that is not obvious.
    fn receipt(&self, _key: &str) -> Json {
        json!({})
    }
}

pub struct Agent {
    seq: AtomicU64,
    inner: Mutex<Inner>,
    events: broadcast::Sender<Arc<Event>>,
    lines: broadcast::Sender<Arc<str>>,
    thawed: Notify,
    pub(crate) token: Option<String>,
    pub(crate) info: Json,
    injectors: Mutex<BTreeMap<String, Injector>>,
    providers: Mutex<BTreeMap<String, Arc<dyn StateProvider>>>,
    pub(crate) probes: Registry,
}

impl Agent {
    pub fn new(info: Json, token: Option<String>, start_frozen: bool) -> Arc<Self> {
        let (events, _) = broadcast::channel(EVENT_BACKLOG);
        let (lines, _) = broadcast::channel(EVENT_BACKLOG);
        Arc::new(Self {
            seq: AtomicU64::new(0),
            inner: Mutex::new(Inner {
                frozen: start_frozen,
                hold: None,
                conditions: BTreeMap::new(),
                next_condition: 1,
                held: BTreeMap::new(),
                deferred: BTreeMap::new(),
                recent: VecDeque::new(),
                frozen_since: start_frozen.then(Instant::now),
                frozen_total: Duration::ZERO,
                resigning_available: false,
                resigning_enabled: false,
                resigning_error: Some("not a controlled Freeze devnet".to_string()),
            }),
            events,
            lines,
            thawed: Notify::new(),
            token,
            info,
            injectors: Mutex::new(BTreeMap::new()),
            providers: Mutex::new(BTreeMap::new()),
            probes: Registry::new(),
        })
    }

    /// Every event from now on, each as one line of JSON.
    pub fn subscribe(&self) -> broadcast::Receiver<Arc<str>> {
        self.lines.subscribe()
    }

    /// Every event from now on, not yet turned into text, so that a receiver which leaves an
    /// event out pays nothing for it.
    pub fn subscribe_events(&self) -> broadcast::Receiver<Arc<Event>> {
        self.events.subscribe()
    }

    fn publish(&self, event: Event) {
        let event = Arc::new(event);
        if self.lines.receiver_count() > 0 {
            drop(self.lines.send(event.line()));
        }
        drop(self.events.send(event));
    }

    fn emit(&self, name: &'static str, data: Json) {
        self.publish(Event { name, seq: None, held: false, body: Body::Data(data), line: OnceLock::new() });
    }

    fn emit_item(&self, seq: u64, held: bool, item: Arc<Json>) {
        self.publish(Event { name: "item", seq: Some(seq), held, body: Body::Item(item), line: OnceLock::new() });
    }

    fn item_json(&self, seq: u64, draft: Draft) -> Json {
        let millis = SystemTime::now().duration_since(UNIX_EPOCH).map(|d| d.as_millis() as u64).unwrap_or(0);
        let mut item = json!({
            "seq": seq,
            "t_ms": millis,
            "kind": draft.kind,
            "from": draft.from,
            "to": draft.to,
            "type": draft.type_name,
            "label": draft.label,
            "cold_spots": draft.cold_spots,
            "payload": draft.payload,
            "editable": draft.editable,
            "droppable": draft.droppable,
        });
        if let (Some(raw), Some(object)) = (draft.raw, item.as_object_mut()) {
            object.insert("raw".to_string(), Json::String(hex::encode(raw)));
        }
        if let (Some(parent), Some(object)) = (draft.parent, item.as_object_mut()) {
            object.insert("parent".to_string(), json!(parent));
        }
        if let (Some(cause), Some(object)) = (draft.cause, item.as_object_mut()) {
            object.insert("cause".to_string(), json!(cause));
        }
        item
    }

    /// Record an item that is not a step and therefore never waits, such as the result of one.
    pub fn note(&self, draft: Draft) -> u64 {
        let seq = self.seq.fetch_add(1, Ordering::SeqCst) + 1;
        let mut item = self.item_json(seq, draft);
        item["held"] = Json::Bool(false);
        let item = Arc::new(item);
        self.remember(&mut self.inner.lock(), item.clone());
        self.emit_item(seq, false, item);
        seq
    }

    fn remember(&self, inner: &mut Inner, item: Arc<Json>) {
        if inner.recent.len() >= RECENT_ITEMS {
            inner.recent.pop_front();
        }
        inner.recent.push_back(item);
    }

    /// Offer a step. Returns its sequence number and, if it must wait, the handle to wait on.
    pub fn admit(&self, draft: Draft, blocking: bool) -> (u64, Option<Pending>) {
        self.admit_for(draft, blocking, None)
    }

    /// Offer a step that waits whatever the state of the node, for the reason given, which is
    /// what the `frozen` event names. The node freezes as it does for any held step.
    pub fn admit_held(&self, draft: Draft, blocking: bool, reason: &str) -> (u64, Option<Pending>) {
        self.admit_for(draft, blocking, Some(reason))
    }

    fn admit_for(&self, draft: Draft, blocking: bool, forced: Option<&str>) -> (u64, Option<Pending>) {
        let seq = self.seq.fetch_add(1, Ordering::SeqCst) + 1;
        let mut item = self.item_json(seq, draft);

        let mut inner = self.inner.lock();
        let mut reasons: Vec<Json> = Vec::new();
        if let Some(reason) = forced {
            reasons.push(json!(reason));
        }
        if inner.frozen {
            reasons.push(json!("frozen"));
        }
        if inner.hold.as_ref().is_some_and(|(predicate, _)| predicate.matches(&item)) {
            reasons.push(json!("hold"));
        }
        let mut spent = Vec::new();
        let mut logged = Vec::new();
        for (id, condition) in inner.conditions.iter_mut() {
            if condition.predicate.matches(&item) {
                condition.hits += 1;
                match condition.action {
                    Action::Freeze => reasons.push(json!({ "condition": id })),
                    Action::Log => logged.push(*id),
                }
                if condition.once {
                    spent.push(*id);
                }
            }
        }
        for id in spent {
            inner.conditions.remove(&id);
        }

        let mut overtaken = Vec::new();
        for (deferred, (_, until)) in inner.deferred.iter_mut() {
            if let Some((predicate, remaining)) = until
                && predicate.matches(&item)
            {
                *remaining = remaining.saturating_sub(1);
                if *remaining == 0 {
                    overtaken.push(*deferred);
                }
            }
        }
        let overtaken: Vec<(u64, Waiter)> = overtaken
            .into_iter()
            .filter_map(|seq| inner.deferred.remove(&seq).map(|(waiter, _)| (seq, waiter)))
            .collect();

        let hold = !reasons.is_empty();
        item["held"] = Json::Bool(hold);
        let item = Arc::new(item);
        self.remember(&mut inner, item.clone());

        let pending = if hold {
            let was_frozen = inner.frozen;
            if !was_frozen {
                inner.frozen = true;
                inner.frozen_since = Some(Instant::now());
            }
            let (waiter, pending) = if blocking {
                let (tx, rx) = mpsc::sync_channel(1);
                (Waiter::Blocking(tx), Pending::Blocking(rx))
            } else {
                let (tx, rx) = oneshot::channel();
                (Waiter::Async(tx), Pending::Async(rx))
            };
            inner.held.insert(seq, (item.clone(), waiter));
            drop(inner);
            self.emit_item(seq, true, item);
            if !was_frozen {
                self.emit("frozen", json!({ "seq": seq, "reasons": reasons }));
            }
            Some(pending)
        } else {
            drop(inner);
            self.emit_item(seq, false, item);
            None
        };
        for id in logged {
            self.emit("condition", json!({ "id": id, "seq": seq }));
        }
        for (deferred, waiter) in overtaken {
            waiter.release(Verdict::Forward);
            self.emit(
                "released",
                json!({ "seq": deferred, "verdict": "forward", "deferred": true, "overtaken_by": seq }),
            );
        }
        (seq, pending)
    }

    /// Offer a step from async code and wait for the debugger if it is held.
    pub async fn gate(&self, draft: Draft) -> (u64, Verdict) {
        match self.admit(draft, false) {
            (seq, Some(Pending::Async(rx))) => (seq, rx.await.unwrap_or(Verdict::Forward)),
            (seq, _) => (seq, Verdict::Forward),
        }
    }

    /// Offer a step from a plain thread and block it while the step is held.
    pub fn gate_blocking(&self, draft: Draft) -> (u64, Verdict) {
        match self.admit(draft, true) {
            (seq, Some(Pending::Blocking(rx))) => (seq, rx.recv().unwrap_or(Verdict::Forward)),
            (seq, _) => (seq, Verdict::Forward),
        }
    }

    /// Report how a step ended.
    pub fn done(&self, seq: u64, outcome: Json) {
        self.publish(Event {
            name: "item.done",
            seq: Some(seq),
            held: false,
            body: Body::Data(json!({ "seq": seq, "outcome": outcome })),
            line: OnceLock::new(),
        });
    }

    pub fn log(&self, level: &str, message: &str) {
        self.emit("log", json!({ "level": level, "message": message }));
    }

    pub fn is_frozen(&self) -> bool {
        self.inner.lock().frozen
    }

    /// Total time the node has spent frozen, including the current freeze.
    pub fn frozen_time(&self) -> Duration {
        let inner = self.inner.lock();
        inner.frozen_total + inner.frozen_since.map(|since| since.elapsed()).unwrap_or_default()
    }

    /// Resolves once the node is not frozen.
    pub async fn until_thawed(&self) {
        loop {
            let notified = self.thawed.notified();
            if !self.is_frozen() {
                return;
            }
            notified.await;
        }
    }

    pub fn freeze(&self) {
        let mut inner = self.inner.lock();
        if !inner.frozen {
            inner.frozen = true;
            inner.frozen_since = Some(Instant::now());
            drop(inner);
            self.emit("frozen", json!({ "reasons": ["requested"] }));
        }
    }

    /// Let the node run. Items matching the hold filter stay held, and keep the node frozen.
    pub fn thaw(&self) {
        self.thaw_keeping(None);
    }

    /// Let the node run, but keep held whatever matches the hold filter or `also`.
    ///
    /// A debugger that acts on certain items uses `also` so that one of them arriving at the
    /// very moment the node is released is not let through unseen.
    pub fn thaw_keeping(&self, also: Option<&Predicate>) {
        let mut released = Vec::new();
        let mut inner = self.inner.lock();
        let keep: Vec<u64> = inner
            .held
            .iter()
            .filter(|(_, (item, _))| {
                inner.hold.as_ref().is_some_and(|(predicate, _)| predicate.matches(item))
                    || also.is_some_and(|predicate| predicate.matches(item))
            })
            .map(|(seq, _)| *seq)
            .collect();
        let all: Vec<u64> = inner.held.keys().copied().collect();
        for seq in all {
            if !keep.contains(&seq)
                && let Some((_, waiter)) = inner.held.remove(&seq)
            {
                released.push((seq, waiter));
            }
        }
        let still_frozen = !inner.held.is_empty();
        if !still_frozen && inner.frozen {
            inner.frozen = false;
            if let Some(since) = inner.frozen_since.take() {
                inner.frozen_total += since.elapsed();
            }
        }
        drop(inner);
        for (seq, waiter) in released {
            waiter.release(Verdict::Forward);
            self.emit("released", json!({ "seq": seq, "verdict": "forward" }));
        }
        if !still_frozen {
            self.thawed.notify_waiters();
            self.emit("thawed", json!({}));
        }
    }

    /// Set a held item aside: it keeps waiting, but the rest of the node may run on past it.
    ///
    /// With `until`, it goes on once that many items matching the predicate have been seen;
    /// otherwise the caller ends the wait with [`Agent::resume_deferred`]. The part of the node
    /// that is waiting on the item does nothing else meanwhile, so messages queued behind it in
    /// the same part cannot overtake it.
    pub fn defer(&self, seq: u64, until: Option<(Predicate, u64)>) -> Result<(), String> {
        let mut inner = self.inner.lock();
        let (_, waiter) = inner.held.remove(&seq).ok_or_else(|| format!("item {seq} is not held"))?;
        inner.deferred.insert(seq, (waiter, until.filter(|(_, count)| *count > 0)));
        drop(inner);
        self.emit("deferred", json!({ "seq": seq }));
        Ok(())
    }

    /// Let an item that was set aside go on. Returns false if it already has.
    pub fn resume_deferred(&self, seq: u64) -> bool {
        let Some((waiter, _)) = self.inner.lock().deferred.remove(&seq) else {
            return false;
        };
        waiter.release(Verdict::Forward);
        self.emit("released", json!({ "seq": seq, "verdict": "forward", "deferred": true }));
        true
    }

    /// Decide one held item. The node stays frozen, so whatever that step leads to is held next.
    pub fn resolve(&self, seq: u64, verdict: Verdict) -> Result<(), String> {
        let mut inner = self.inner.lock();
        let (item, _) = inner.held.get(&seq).ok_or_else(|| format!("item {seq} is not held"))?;
        let flag = |name: &str| item.get(name).and_then(Json::as_bool).unwrap_or(false);
        let kind = item.get("kind").and_then(Json::as_str).unwrap_or_default();
        if matches!(verdict, Verdict::ReplaceRaw(_) | Verdict::ReplaceRawStale(_))
            && !matches!(kind, "wire_in" | "wire_out" | "proto_in" | "proto_out")
        {
            return Err(format!(
                "typed_raw_replacement_unsupported: item {seq} is `{kind}`; use a typed payload replacement so reconstruction can be proven"
            ));
        }
        match &verdict {
            Verdict::Forward => {}
            Verdict::Drop if flag("droppable") => {}
            Verdict::Replace(_) | Verdict::ReplaceRaw(_) | Verdict::ReplaceRawStale(_) if flag("editable") => {}
            Verdict::Drop | Verdict::Replace(_) | Verdict::ReplaceRaw(_) | Verdict::ReplaceRawStale(_) => {
                return Err(format!("item {seq} does not allow that"));
            }
        }
        let Some((_, waiter)) = inner.held.remove(&seq) else {
            return Err(format!("item {seq} is not held"));
        };
        drop(inner);
        let name = match &verdict {
            Verdict::Forward => "forward",
            Verdict::Drop => "drop",
            Verdict::Replace(_) | Verdict::ReplaceRaw(_) | Verdict::ReplaceRawStale(_) => "replace",
        };
        let mut released = json!({ "seq": seq, "verdict": name });
        if let Verdict::Replace(payload) = &verdict {
            released["payload"] = payload.clone();
        }
        if let Verdict::ReplaceRaw(raw) | Verdict::ReplaceRawStale(raw) = &verdict {
            released["raw"] = Json::String(hex::encode(raw));
        }
        if matches!(verdict, Verdict::ReplaceRawStale(_)) {
            released["stale_negative_control"] = Json::Bool(true);
        }
        waiter.release(verdict);
        self.emit("released", released);
        Ok(())
    }

    /// Forward the oldest held item, or the given one, and stay frozen.
    pub fn step(&self, seq: Option<u64>) -> Result<u64, String> {
        let seq = match seq {
            Some(seq) => seq,
            None => *self.inner.lock().held.keys().next().ok_or("nothing is held")?,
        };
        self.resolve(seq, Verdict::Forward).map(|()| seq)
    }

    pub fn set_hold(&self, predicate: Option<&Json>) -> Result<(), String> {
        let parsed = match predicate {
            Some(json) => Some((Predicate::parse(json)?, json.clone())),
            None => None,
        };
        self.inner.lock().hold = parsed;
        Ok(())
    }

    pub fn add_condition(&self, predicate: &Json, action: Action, once: bool) -> Result<u64, String> {
        let parsed = Predicate::parse(predicate)?;
        let mut inner = self.inner.lock();
        let id = inner.next_condition;
        inner.next_condition += 1;
        inner.conditions.insert(id, Condition { predicate: parsed, source: predicate.clone(), action, once, hits: 0 });
        Ok(id)
    }

    pub fn remove_condition(&self, id: u64) -> bool {
        self.inner.lock().conditions.remove(&id).is_some()
    }

    pub fn conditions(&self) -> Json {
        let inner = self.inner.lock();
        Json::Array(
            inner
                .conditions
                .iter()
                .map(|(id, c)| {
                    json!({
                        "id": id, "predicate": c.source, "once": c.once, "hits": c.hits,
                        "action": match c.action { Action::Freeze => "freeze", Action::Log => "log" },
                    })
                })
                .collect(),
        )
    }

    /// Run until an item matches, then freeze on it.
    pub fn run_until(&self, predicate: &Json) -> Result<u64, String> {
        let id = self.add_condition(predicate, Action::Freeze, true)?;
        self.thaw();
        Ok(id)
    }

    pub fn item(&self, seq: u64) -> Option<Json> {
        let inner = self.inner.lock();
        if let Some((item, _)) = inner.held.get(&seq) {
            return Some((**item).clone());
        }
        inner
            .recent
            .iter()
            .rev()
            .find(|item| item.get("seq").and_then(Json::as_u64) == Some(seq))
            .map(|i| (**i).clone())
    }

    pub fn status(&self) -> Json {
        let inner = self.inner.lock();
        let resigning = Self::resigning_status_locked(&inner);
        json!({
            "frozen": inner.frozen,
            "seq": self.seq.load(Ordering::SeqCst),
            "held": inner.held.keys().collect::<Vec<_>>(),
            "deferred": inner.deferred.keys().collect::<Vec<_>>(),
            "hold": inner.hold.as_ref().map(|(_, source)| source.clone()),
            "conditions": inner.conditions.len(),
            "frozen_ms": (inner.frozen_total + inner.frozen_since.map(|s| s.elapsed()).unwrap_or_default()).as_millis() as u64,
            "resigning": resigning,
        })
    }

    fn resigning_status_locked(inner: &Inner) -> Json {
        let mut status = json!({
            "enabled": inner.resigning_enabled,
            "available": inner.resigning_available,
        });
        if let (Some(error), Some(object)) = (&inner.resigning_error, status.as_object_mut()) {
            object.insert("error".to_string(), Json::String(error.clone()));
        }
        status
    }

    pub fn resigning_status(&self) -> Json {
        Self::resigning_status_locked(&self.inner.lock())
    }

    pub fn configure_resigning(&self, available: bool, enabled: bool, error: Option<String>) {
        let mut inner = self.inner.lock();
        inner.resigning_available = available;
        inner.resigning_enabled = available && enabled;
        inner.resigning_error =
            if available { error } else { error.or_else(|| Some("not a controlled Freeze devnet".into())) };
    }

    pub fn resigning_enabled(&self) -> bool {
        self.inner.lock().resigning_enabled
    }

    pub fn set_resigning_enabled(&self, enabled: bool) -> Result<(), String> {
        let mut inner = self.inner.lock();
        if enabled && !inner.resigning_available {
            return Err(inner
                .resigning_error
                .clone()
                .unwrap_or_else(|| "re-signing is unavailable outside a controlled Freeze devnet".to_string()));
        }
        inner.resigning_enabled = enabled;
        Ok(())
    }

    pub fn register_injector(&self, destination: &str, injector: Injector) {
        self.injectors.lock().insert(destination.to_string(), injector);
    }

    pub fn unregister_injector(&self, destination: &str) {
        self.injectors.lock().remove(destination);
    }

    pub fn destinations(&self) -> Vec<String> {
        self.injectors.lock().keys().cloned().collect()
    }

    pub fn inject(&self, destination: &str, payload: &Json) -> Result<(), String> {
        let injector = self
            .injectors
            .lock()
            .get(destination)
            .cloned()
            .ok_or_else(|| format!("unknown destination `{destination}`"))?;
        injector(payload)
    }

    pub fn register_state(&self, namespace: &str, provider: Arc<dyn StateProvider>) {
        self.providers.lock().insert(namespace.to_string(), provider);
    }

    pub fn namespaces(&self) -> Vec<String> {
        self.providers.lock().keys().cloned().collect()
    }

    /// Split `namespace/key` on the longest registered namespace.
    pub fn state(&self, path: &str) -> Result<(Arc<dyn StateProvider>, String), String> {
        let providers = self.providers.lock();
        providers
            .iter()
            .filter(|(namespace, _)| {
                path == namespace.as_str() || path.strip_prefix(namespace.as_str()).is_some_and(|r| r.starts_with('/'))
            })
            .max_by_key(|(namespace, _)| namespace.len())
            .map(|(namespace, provider)| {
                (provider.clone(), path[namespace.len()..].trim_start_matches('/').to_string())
            })
            .ok_or_else(|| format!("no state namespace for `{path}`"))
    }
}

#[cfg(test)]
mod tests {
    use std::sync::{Barrier, atomic::AtomicBool};

    use super::*;

    #[test]
    fn re_signing_is_unavailable_by_default_and_cannot_be_enabled() {
        let agent = Agent::new(json!({}), None, false);
        assert_eq!(
            agent.resigning_status(),
            json!({ "enabled": false, "available": false, "error": "not a controlled Freeze devnet" })
        );
        assert!(agent.set_resigning_enabled(true).is_err());

        agent.configure_resigning(true, true, None);
        assert!(agent.resigning_enabled());
        assert_eq!(agent.resigning_status(), json!({ "enabled": true, "available": true }));
        agent.set_resigning_enabled(false).unwrap();
        assert!(!agent.resigning_enabled());
    }

    #[tokio::test]
    async fn an_item_set_aside_lets_the_node_run_on_until_others_have_passed_it() {
        let agent = Agent::new(json!({}), None, true);
        let slow = tokio::spawn({
            let agent = agent.clone();
            async move { agent.gate(draft("slow", 1)).await }
        });
        tokio::time::sleep(Duration::from_millis(20)).await;
        let until = Predicate::parse(&json!({ "op": "label", "eq": "fast" })).unwrap();
        agent.defer(1, Some((until, 2))).unwrap();
        agent.thaw();
        assert!(!agent.is_frozen(), "an item set aside does not keep the node frozen");
        assert_eq!(agent.status()["deferred"], json!([1]));

        assert_eq!(agent.gate(draft("fast", 2)).await, (2, Verdict::Forward));
        assert!(!slow.is_finished(), "one item has passed, two were asked for");
        assert_eq!(agent.gate(draft("fast", 3)).await, (3, Verdict::Forward));
        assert_eq!(slow.await.unwrap(), (1, Verdict::Forward));
        assert_eq!(agent.status()["deferred"], json!([]));
        assert!(!agent.resume_deferred(1), "it has already gone on");
        assert!(agent.defer(9, None).is_err());
    }

    fn draft(label: &str, fee: u64) -> Draft {
        Draft {
            kind: "internal_input",
            to: "stage:validate".into(),
            label: label.into(),
            payload: json!({ "fee": fee }),
            editable: true,
            droppable: true,
            ..Draft::default()
        }
    }

    #[tokio::test]
    async fn runs_freely_until_frozen_then_holds_every_step() {
        let agent = Agent::new(json!({}), None, false);
        assert_eq!(agent.gate(draft("a", 1)).await, (1, Verdict::Forward));

        agent.freeze();
        let waiting = tokio::spawn({
            let agent = agent.clone();
            async move { agent.gate(draft("b", 2)).await }
        });
        tokio::time::sleep(Duration::from_millis(20)).await;
        assert_eq!(agent.status()["held"], json!([2]));

        agent.resolve(2, Verdict::Replace(json!({ "fee": 9 }))).unwrap();
        assert_eq!(waiting.await.unwrap(), (2, Verdict::Replace(json!({ "fee": 9 }))));
        assert!(agent.is_frozen(), "resolving one item keeps the node frozen");
    }

    #[tokio::test]
    async fn hold_filter_freezes_the_node_and_thaw_keeps_matching_items() {
        let agent = Agent::new(json!({}), None, false);
        agent.set_hold(Some(&json!({ "op": "field", "path": "fee", "gt": 100 }))).unwrap();
        assert_eq!(agent.gate(draft("small", 5)).await.1, Verdict::Forward);

        let big = tokio::spawn({
            let agent = agent.clone();
            async move { agent.gate(draft("big", 500)).await.1 }
        });
        let other = tokio::spawn({
            let agent = agent.clone();
            async move {
                tokio::time::sleep(Duration::from_millis(20)).await;
                agent.gate(draft("small again", 6)).await.1
            }
        });
        tokio::time::sleep(Duration::from_millis(60)).await;
        assert_eq!(agent.status()["held"], json!([2, 3]), "the node froze, so the unrelated step waits too");

        agent.thaw();
        assert_eq!(other.await.unwrap(), Verdict::Forward);
        assert!(agent.is_frozen(), "the matching item is still held");
        agent.resolve(2, Verdict::Drop).unwrap();
        assert_eq!(big.await.unwrap(), Verdict::Drop);
        agent.thaw();
        assert!(!agent.is_frozen());
    }

    #[tokio::test]
    async fn run_until_freezes_on_the_first_match_only_once() {
        let agent = Agent::new(json!({}), None, true);
        agent.run_until(&json!({ "op": "label", "eq": "target" })).unwrap();
        assert!(!agent.is_frozen());
        assert_eq!(agent.gate(draft("other", 1)).await.1, Verdict::Forward);
        let hit = tokio::spawn({
            let agent = agent.clone();
            async move { agent.gate(draft("target", 1)).await }
        });
        tokio::time::sleep(Duration::from_millis(20)).await;
        assert!(agent.is_frozen());
        assert_eq!(agent.conditions(), json!([]));
        agent.thaw();
        assert_eq!(hit.await.unwrap().1, Verdict::Forward);
        assert_eq!(agent.gate(draft("target", 1)).await.1, Verdict::Forward);
    }

    #[tokio::test]
    async fn verdicts_respect_what_the_item_allows() {
        let agent = Agent::new(json!({}), None, true);
        let fixed = tokio::spawn({
            let agent = agent.clone();
            async move { agent.gate(Draft { editable: false, droppable: false, ..draft("fixed", 1) }).await.1 }
        });
        tokio::time::sleep(Duration::from_millis(20)).await;
        assert!(agent.resolve(1, Verdict::Drop).is_err());
        assert!(agent.resolve(1, Verdict::Replace(json!(1))).is_err());
        assert!(agent.resolve(7, Verdict::Forward).is_err());
        assert_eq!(agent.step(None), Ok(1));
        assert_eq!(fixed.await.unwrap(), Verdict::Forward);
    }

    #[test]
    fn rejected_resolve_racing_thaw_never_leaves_a_held_item_while_running() {
        let agent = Agent::new(json!({}), None, true);
        let seq = Arc::new(AtomicU64::new(0));
        let stop = Arc::new(AtomicBool::new(false));
        let start = Arc::new(Barrier::new(3));
        let done = Arc::new(Barrier::new(3));
        let mut violation = None;

        std::thread::scope(|scope| {
            scope.spawn({
                let agent = agent.clone();
                let seq = seq.clone();
                let stop = stop.clone();
                let start = start.clone();
                let done = done.clone();
                move || loop {
                    start.wait();
                    if stop.load(Ordering::SeqCst) {
                        break;
                    }
                    drop(agent.resolve(seq.load(Ordering::SeqCst), Verdict::Drop));
                    done.wait();
                }
            });
            scope.spawn({
                let agent = agent.clone();
                let stop = stop.clone();
                let start = start.clone();
                let done = done.clone();
                move || loop {
                    start.wait();
                    if stop.load(Ordering::SeqCst) {
                        break;
                    }
                    agent.thaw();
                    done.wait();
                }
            });

            for _ in 0..20_000 {
                agent.freeze();
                let (current, pending) =
                    agent.admit(Draft { editable: false, droppable: false, ..draft("fixed", 1) }, false);
                seq.store(current, Ordering::SeqCst);
                start.wait();
                done.wait();
                let status = agent.status();
                if status["held"].as_array().is_some_and(|held| !held.is_empty()) && status["frozen"] == false {
                    violation = Some(status);
                    drop(agent.step(None));
                    drop(pending);
                    break;
                }
                drop(pending);
            }

            stop.store(true, Ordering::SeqCst);
            start.wait();
        });

        assert!(violation.is_none(), "rejected verdict was reinserted after thaw: {violation:?}");
    }

    #[tokio::test]
    async fn thaw_can_keep_chosen_items_held() {
        let agent = Agent::new(json!({}), None, true);
        let wanted = tokio::spawn({
            let agent = agent.clone();
            async move { agent.gate(draft("wanted", 1)).await.1 }
        });
        let other = tokio::spawn({
            let agent = agent.clone();
            async move { agent.gate(draft("other", 2)).await.1 }
        });
        tokio::time::sleep(Duration::from_millis(30)).await;
        let keep = Predicate::parse(&json!({ "op": "label", "eq": "wanted" })).unwrap();
        agent.thaw_keeping(Some(&keep));
        assert_eq!(other.await.unwrap(), Verdict::Forward);
        assert!(agent.is_frozen(), "the kept item still holds the node");
        assert_eq!(agent.status()["held"].as_array().map(Vec::len), Some(1));
        agent.thaw();
        assert_eq!(wanted.await.unwrap(), Verdict::Forward);
    }

    #[test]
    fn plain_threads_can_be_held() {
        let agent = Agent::new(json!({}), None, true);
        let worker = std::thread::spawn({
            let agent = agent.clone();
            move || agent.gate_blocking(draft("ledger", 3)).1
        });
        while agent.status()["held"] != json!([1]) {
            std::thread::sleep(Duration::from_millis(5));
        }
        agent.resolve(1, Verdict::Drop).unwrap();
        assert_eq!(worker.join().unwrap(), Verdict::Drop);
    }

    #[tokio::test]
    async fn frozen_time_accumulates_only_while_frozen() {
        let agent = Agent::new(json!({}), None, false);
        tokio::time::sleep(Duration::from_millis(30)).await;
        assert_eq!(agent.frozen_time(), Duration::ZERO);
        agent.freeze();
        tokio::time::sleep(Duration::from_millis(40)).await;
        agent.thaw();
        let frozen = agent.frozen_time();
        assert!(frozen >= Duration::from_millis(35), "{frozen:?}");
        tokio::time::sleep(Duration::from_millis(30)).await;
        assert_eq!(agent.frozen_time(), frozen);
        agent.until_thawed().await;
    }

    #[tokio::test]
    async fn events_describe_what_happened() {
        let agent = Agent::new(json!({}), None, false);
        let mut events = agent.subscribe();
        let (seq, _) = agent.gate(draft("a", 1)).await;
        agent.done(seq, json!({ "verdict": "accepted" }));
        let first: Json = serde_json::from_str(&events.recv().await.unwrap()).unwrap();
        assert_eq!(first["event"], "item");
        assert_eq!(first["data"]["label"], "a");
        assert_eq!(first["data"]["held"], false);
        let second: Json = serde_json::from_str(&events.recv().await.unwrap()).unwrap();
        assert_eq!(second, json!({ "event": "item.done", "data": { "seq": 1, "outcome": { "verdict": "accepted" } } }));
        assert_eq!(agent.item(1).unwrap()["payload"], json!({ "fee": 1 }));
    }
}
