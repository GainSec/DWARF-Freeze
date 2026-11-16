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
//! The control socket: newline-delimited JSON requests, responses and events over TCP.
//!
//! The server runs on its own thread with its own runtime so that it keeps answering while the
//! node's runtime is held at a gate.

use std::{
    collections::{BTreeMap, BTreeSet},
    io,
    net::{SocketAddr, TcpListener as StdListener},
    sync::{
        Arc,
        atomic::{AtomicU8, Ordering},
    },
    thread::Thread,
    time::Duration,
};

use parking_lot::Mutex;
use serde_json::{Value as Json, json};
use tokio::{
    io::{AsyncBufReadExt, AsyncWriteExt, BufReader},
    net::{TcpListener, TcpStream},
    sync::{broadcast::error::RecvError, mpsc},
    time::MissedTickBehavior,
};

use crate::{
    agent::{Action, Agent, Event, Verdict},
    predicate::Predicate,
};

pub const PROTOCOL: &str = "fcp/1";

/// How many traced functions `trace.list` answers with when no `limit` is given.
const TRACE_LIST_DEFAULT: usize = 2_000;

/// How many items sent to a connection are remembered so that the end of each is sent too.
const SENT_ITEMS: usize = 8192;

/// How often a connection that is not sent everything is told what it missed.
const STATS_EVERY: Duration = Duration::from_secs(1);

const SERVER_PENDING: u8 = 0;
const SERVER_ACTIVE: u8 = 1;
const SERVER_CANCELLED: u8 = 2;

/// A fully initialized control server whose worker cannot accept until [`PreparedServer::activate`].
pub(crate) struct PreparedServer {
    address: SocketAddr,
    gate: Arc<AtomicU8>,
    thread: Thread,
    activated: bool,
}

impl PreparedServer {
    pub(crate) fn activate(mut self) -> SocketAddr {
        self.activated = true;
        self.gate.store(SERVER_ACTIVE, Ordering::Release);
        self.thread.unpark();
        self.address
    }
}

impl Drop for PreparedServer {
    fn drop(&mut self) {
        if !self.activated {
            self.gate.store(SERVER_CANCELLED, Ordering::Release);
            self.thread.unpark();
        }
    }
}

/// Bind the control socket and serve it from a dedicated thread. Returns the bound address.
pub fn spawn(agent: Arc<Agent>, listen: &str) -> io::Result<SocketAddr> {
    Ok(prepare(agent, listen)?.activate())
}

/// Complete every fallible start-up step, but keep the worker from accepting connections.
pub(crate) fn prepare(agent: Arc<Agent>, listen: &str) -> io::Result<PreparedServer> {
    if !agent.token.as_deref().is_some_and(|token| !token.trim().is_empty()) {
        return Err(io::Error::new(
            io::ErrorKind::PermissionDenied,
            "freeze control socket requires a nonempty DWARF_FREEZE_TOKEN",
        ));
    }
    let listener = StdListener::bind(listen)?;
    listener.set_nonblocking(true)?;
    let address = listener.local_addr()?;
    let runtime = tokio::runtime::Builder::new_current_thread().enable_all().build()?;
    let listener = {
        let _entered = runtime.enter();
        TcpListener::from_std(listener)?
    };
    let gate = Arc::new(AtomicU8::new(SERVER_PENDING));
    let worker_gate = gate.clone();
    let worker = std::thread::Builder::new().name("freeze-agent".to_string()).spawn(move || {
        loop {
            match worker_gate.load(Ordering::Acquire) {
                SERVER_PENDING => std::thread::park(),
                SERVER_ACTIVE => break,
                SERVER_CANCELLED => return,
                _ => return,
            }
        }
        runtime.block_on(async move {
            loop {
                match listener.accept().await {
                    Ok((stream, _)) => {
                        tokio::spawn(connection(agent.clone(), stream));
                    }
                    Err(error) => tracing::warn!(%error, "freeze agent accept failed"),
                }
            }
        });
    })?;
    let thread = worker.thread().clone();
    drop(worker);
    Ok(PreparedServer { address, gate, thread, activated: false })
}

/// Which items a connection asked to be sent.
struct Filter {
    kinds: Option<Vec<String>>,
    when: Option<Predicate>,
    sample: u64,
}

impl Filter {
    /// The filter `events.subscribe` asks for; `None` when it asks for everything.
    fn parse(params: &Json) -> Result<Option<Self>, String> {
        let given = |key: &str| params.get(key).filter(|value| !value.is_null());
        let kinds = match given("kinds") {
            Some(kinds) => Some(
                kinds
                    .as_array()
                    .ok_or("`kinds` must be a list of item kinds")?
                    .iter()
                    .map(|kind| kind.as_str().map(str::to_string).ok_or("`kinds` must be a list of item kinds"))
                    .collect::<Result<Vec<_>, _>>()?,
            ),
            None => None,
        };
        let when = given("when").map(Predicate::parse).transpose()?;
        let sample = match given("sample") {
            Some(sample) => sample.as_u64().filter(|n| *n >= 1).ok_or("`sample` must be a whole number, 1 or more")?,
            None => 1,
        };
        Ok((kinds.is_some() || when.is_some() || sample > 1).then_some(Self { kinds, when, sample }))
    }
}

/// What the node produced during one period, for a connection that was not sent all of it.
#[derive(Default)]
struct Period {
    seq: u64,
    items: u64,
    by_kind: BTreeMap<String, u64>,
    dropped: u64,
}

/// What one connection is sent of the events of the node.
#[derive(Default)]
struct Subscription {
    filter: Option<Filter>,
    /// How many items passed `kinds` and `when` since the filter was set.
    passed: u64,
    /// The latest item seen, sent or not.
    latest: u64,
    /// The items seen from `whole.0` to `whole.1` were all sent, having arrived while there
    /// was no filter.
    whole: (u64, u64),
    /// The most recent items sent while there was a filter.
    sent: BTreeSet<u64>,
    period: Period,
}

impl Subscription {
    fn set(&mut self, filter: Option<Filter>) {
        if self.filter.is_some() && filter.is_none() {
            self.whole = (self.latest + 1, self.latest);
            self.period = Period::default();
        }
        self.passed = 0;
        self.filter = filter;
    }

    /// Whether the connection is sent `event`. Everything is unless there is a filter. With a
    /// filter, an item is sent if it is held or passes the filter, the end of an item if the
    /// item was sent, and every other event.
    fn wants(&mut self, event: &Event) -> bool {
        match (event.name(), event.seq()) {
            ("item", Some(seq)) => {
                self.latest = self.latest.max(seq);
                let Some(filter) = &self.filter else {
                    self.whole.1 = self.latest;
                    return true;
                };
                let kind = event.item().and_then(|item| item.get("kind")).and_then(Json::as_str).unwrap_or("");
                self.period.seq = self.latest;
                self.period.items += 1;
                match self.period.by_kind.get_mut(kind) {
                    Some(count) => *count += 1,
                    None => drop(self.period.by_kind.insert(kind.to_string(), 1)),
                }
                let passes = || {
                    filter.kinds.as_ref().is_none_or(|kinds| kinds.iter().any(|wanted| wanted == kind))
                        && filter.when.as_ref().is_none_or(|when| event.item().is_some_and(|item| when.matches(item)))
                };
                let wanted = event.held() || {
                    passes() && {
                        self.passed += 1;
                        (self.passed - 1).is_multiple_of(filter.sample)
                    }
                };
                if wanted {
                    if self.sent.len() >= SENT_ITEMS {
                        self.sent.pop_first();
                    }
                    self.sent.insert(seq);
                } else {
                    self.period.dropped += 1;
                }
                wanted
            }
            ("item.done", Some(seq)) => {
                self.sent.remove(&seq) || self.filter.is_none() || (self.whole.0..=self.whole.1).contains(&seq)
            }
            _ => true,
        }
    }

    /// The `stats` event for the period that just ended, if anything was left out during it.
    fn stats(&mut self) -> Option<Json> {
        let period = std::mem::take(&mut self.period);
        (period.dropped > 0).then(|| {
            json!({
                "event": "stats",
                "data": {
                    "seq": period.seq, "items": period.items, "by_kind": period.by_kind, "dropped": period.dropped,
                },
            })
        })
    }
}

async fn connection(agent: Arc<Agent>, stream: TcpStream) {
    drop(stream.set_nodelay(true));
    let (reader, mut writer) = stream.into_split();
    let (out_tx, mut out_rx) = mpsc::unbounded_channel::<Arc<str>>();

    let write_task = tokio::spawn(async move {
        while let Some(line) = out_rx.recv().await {
            if writer.write_all(line.as_bytes()).await.is_err() || writer.write_all(b"\n").await.is_err() {
                break;
            }
        }
    });

    let mut lines = BufReader::new(reader).lines();
    let mut authenticated = false;
    let mut event_task = None;
    let subscription = Arc::new(Mutex::new(Subscription::default()));

    while let Ok(Some(line)) = lines.next_line().await {
        if line.trim().is_empty() {
            continue;
        }
        let request: Json = match serde_json::from_str(&line) {
            Ok(request) => request,
            Err(error) => {
                send(&out_tx, json!({ "id": null, "error": { "code": "bad_json", "message": error.to_string() } }));
                continue;
            }
        };
        let id = request.get("id").cloned().unwrap_or(Json::Null);
        let method = request.get("method").and_then(Json::as_str).unwrap_or("");
        let params = request.get("params").cloned().unwrap_or_else(|| json!({}));

        if !authenticated {
            if method != "hello" {
                send(&out_tx, json!({ "id": id, "error": { "code": "hello_first", "message": "send `hello` first" } }));
                continue;
            }
            let offered = params.get("token").and_then(Json::as_str);
            if !agent.token.as_deref().is_some_and(|expected| offered == Some(expected)) {
                send(
                    &out_tx,
                    json!({ "id": id, "error": { "code": "bad_token", "message": "wrong or missing token" } }),
                );
                break;
            }
            authenticated = true;
            // Subscribe before answering so that no event after the status in `hello` is missed.
            let mut events = agent.subscribe_events();
            let forward = out_tx.clone();
            let subscription = subscription.clone();
            event_task = Some(tokio::spawn(async move {
                let mut period = tokio::time::interval(STATS_EVERY);
                period.set_missed_tick_behavior(MissedTickBehavior::Delay);
                loop {
                    let line = tokio::select! {
                        received = events.recv() => match received {
                            Ok(event) => {
                                if !subscription.lock().wants(&event) {
                                    continue;
                                }
                                event.line()
                            }
                            Err(RecvError::Lagged(missed)) => Arc::from(
                                json!({ "event": "error", "data": { "code": "events_lost", "missed": missed } })
                                    .to_string(),
                            ),
                            Err(RecvError::Closed) => break,
                        },
                        _ = period.tick() => match subscription.lock().stats() {
                            Some(stats) => Arc::from(stats.to_string()),
                            None => continue,
                        },
                    };
                    if forward.send(line).is_err() {
                        break;
                    }
                }
            }));
        }

        let response = match handle(&agent, &subscription, method, &params) {
            Ok(result) => json!({ "id": id, "result": result }),
            Err(message) => {
                let code = failure_code(&message);
                json!({ "id": id, "error": { "code": code, "message": message } })
            }
        };
        send(&out_tx, response);
    }

    if let Some(task) = event_task {
        task.abort();
    }
    drop(out_tx);
    drop(write_task.await);
}

fn failure_code(message: &str) -> &str {
    const STABLE: [&str; 1] = ["typed_raw_replacement_unsupported"];
    message.split_once(':').map(|(code, _)| code).filter(|code| STABLE.contains(code)).unwrap_or("failed")
}

fn send(out: &mpsc::UnboundedSender<Arc<str>>, message: Json) {
    drop(out.send(Arc::from(message.to_string())));
}

fn handle(agent: &Arc<Agent>, subscription: &Mutex<Subscription>, method: &str, params: &Json) -> Result<Json, String> {
    let seq = || params.get("seq").and_then(Json::as_u64).ok_or("needs `seq`".to_string());
    let text = |key: &str| params.get(key).and_then(Json::as_str).ok_or(format!("needs text `{key}`"));
    let when = || params.get("when").filter(|w| !w.is_null());

    Ok(match method {
        "hello" => {
            json!({ "protocol": PROTOCOL, "node": agent.info, "status": agent.status(),
                    "namespaces": agent.namespaces(), "destinations": agent.destinations() })
        }
        "status" => agent.status(),
        "events.subscribe" => {
            let filter = Filter::parse(params)?;
            subscription.lock().set(filter);
            json!({})
        }
        "resign.get" => agent.resigning_status(),
        "resign.set" => {
            let enabled = params.get("enabled").and_then(Json::as_bool).ok_or("needs boolean `enabled`")?;
            agent.set_resigning_enabled(enabled)?;
            agent.resigning_status()
        }
        "freeze" => {
            agent.freeze();
            agent.status()
        }
        "thaw" => {
            match params.get("keep").filter(|keep| !keep.is_null()) {
                Some(keep) => agent.thaw_keeping(Some(&Predicate::parse(keep)?)),
                None => agent.thaw(),
            }
            agent.status()
        }
        "step" => json!({ "seq": agent.step(params.get("seq").and_then(Json::as_u64))? }),
        "run_until" => json!({ "condition": agent.run_until(when().ok_or("needs `when`")?)? }),
        "set_hold" => {
            agent.set_hold(when())?;
            agent.status()
        }
        "item.get" => agent.item(seq()?).ok_or("no such item (it may have aged out)")?,
        "item.forward" => {
            agent.resolve(seq()?, Verdict::Forward)?;
            if params.get("then").and_then(Json::as_str) == Some("run") {
                agent.thaw();
            }
            agent.status()
        }
        "item.defer" => {
            let seq = seq()?;
            let until = match params.get("until").filter(|until| !until.is_null()) {
                Some(until) => Some((
                    Predicate::parse(until.get("when").ok_or("`until` needs `when`")?)?,
                    until.get("count").and_then(Json::as_u64).unwrap_or(1),
                )),
                None => None,
            };
            let delay = params.get("ms").and_then(Json::as_u64);
            if until.is_none() && delay.is_none() {
                return Err("needs `ms` or `until`".to_string());
            }
            agent.defer(seq, until)?;
            if let Some(ms) = delay {
                let agent = agent.clone();
                tokio::spawn(async move {
                    tokio::time::sleep(std::time::Duration::from_millis(ms)).await;
                    agent.resume_deferred(seq);
                });
            }
            if params.get("then").and_then(Json::as_str) == Some("run") {
                agent.thaw();
            }
            agent.status()
        }
        "item.resume" => json!({ "resumed": agent.resume_deferred(seq()?) }),
        "item.drop" => {
            agent.resolve(seq()?, Verdict::Drop)?;
            agent.status()
        }
        "item.replace" => {
            let stale = match params.get("stale_negative_control") {
                None => false,
                Some(Json::Bool(value)) => *value,
                Some(_) => return Err("`stale_negative_control` must be a boolean".to_string()),
            };
            let verdict = match (params.get("payload"), params.get("raw").and_then(Json::as_str)) {
                (_, Some(raw)) if stale => {
                    Verdict::ReplaceRawStale(hex::decode(raw).map_err(|e| format!("bad `raw`: {e}"))?)
                }
                (_, Some(raw)) => Verdict::ReplaceRaw(hex::decode(raw).map_err(|e| format!("bad `raw`: {e}"))?),
                (Some(_), None) if stale => {
                    return Err("`stale_negative_control` requires exact `raw` bytes".to_string());
                }
                (Some(payload), None) => Verdict::Replace(payload.clone()),
                (None, None) => return Err("needs `payload` or `raw`".to_string()),
            };
            agent.resolve(seq()?, verdict)?;
            agent.status()
        }
        "item.inject" => {
            agent.inject(text("to")?, params.get("payload").ok_or("needs `payload`")?)?;
            json!({})
        }
        "destinations" => json!(agent.destinations()),
        "cond.add" => {
            let action = match params.get("action").and_then(Json::as_str).unwrap_or("freeze") {
                "freeze" => Action::Freeze,
                "log" => Action::Log,
                other => return Err(format!("unknown action `{other}`")),
            };
            let once = params.get("once").and_then(Json::as_bool).unwrap_or(false);
            json!({ "id": agent.add_condition(when().ok_or("needs `when`")?, action, once)? })
        }
        "cond.remove" => {
            json!({ "removed": agent.remove_condition(params.get("id").and_then(Json::as_u64).ok_or("needs `id`")?) })
        }
        "cond.list" => agent.conditions(),
        "state.namespaces" => json!(agent.namespaces()),
        "state.list" => {
            let (provider, prefix) = agent.state(text("path")?)?;
            let limit = params.get("limit").and_then(Json::as_u64).unwrap_or(100).min(10_000) as usize;
            json!(provider.list(&prefix, params.get("after").and_then(Json::as_str), limit)?)
        }
        "state.get" => {
            let (provider, key) = agent.state(text("path")?)?;
            provider.get(&key)?.unwrap_or(Json::Null)
        }
        "state.put" => {
            let (provider, key) = agent.state(text("path")?)?;
            provider.put(&key, params.get("value").ok_or("needs `value`")?)?;
            provider.receipt(&key)
        }
        "state.delete" => {
            let (provider, key) = agent.state(text("path")?)?;
            provider.delete(&key)?;
            provider.receipt(&key)
        }
        "probe.list" => agent.probes().list(),
        "probe.enable" => json!({ "changed": agent.probes().set(text("name")?, true) }),
        "probe.disable" => json!({ "changed": agent.probes().set(text("name")?, false) }),
        "trace.list" => {
            let number = |key: &str| params.get(key).and_then(Json::as_u64).and_then(|n| usize::try_from(n).ok());
            crate::calls::registry().list(
                params.get("prefix").and_then(Json::as_str),
                number("offset").unwrap_or(0),
                number("limit").unwrap_or(TRACE_LIST_DEFAULT),
            )
        }
        "trace.enable" => json!({ "changed": crate::calls::enable(text("prefix")?) }),
        "trace.disable" => {
            json!({ "changed": crate::calls::disable(params.get("prefix").and_then(Json::as_str).unwrap_or("all")) })
        }
        "trace.hold" => json!({ "changed": crate::calls::hold(text("prefix")?) }),
        "trace.unhold" => {
            json!({ "changed": crate::calls::unhold(params.get("prefix").and_then(Json::as_str).unwrap_or("all")) })
        }
        other => return Err(format!("unknown method `{other}`")),
    })
}

#[cfg(test)]
mod tests {
    use tokio::io::{AsyncReadExt, Lines};

    use super::*;
    use crate::agent::Draft;

    struct Client {
        lines: Lines<BufReader<tokio::net::tcp::OwnedReadHalf>>,
        writer: tokio::net::tcp::OwnedWriteHalf,
        next: u64,
    }

    impl Client {
        async fn connect(address: SocketAddr) -> Self {
            let (reader, writer) = TcpStream::connect(address).await.unwrap().into_split();
            Self { lines: BufReader::new(reader).lines(), writer, next: 0 }
        }

        async fn read(&mut self) -> Json {
            let line = tokio::time::timeout(Duration::from_secs(5), self.lines.next_line()).await.unwrap();
            serde_json::from_str(&line.unwrap().unwrap()).unwrap()
        }

        /// Send a request and return its response, collecting events that arrive before it.
        async fn call(&mut self, method: &str, params: Json) -> (Json, Vec<Json>) {
            self.next += 1;
            let request = json!({ "id": self.next, "method": method, "params": params });
            self.writer.write_all(format!("{request}\n").as_bytes()).await.unwrap();
            let mut events = Vec::new();
            loop {
                let message = self.read().await;
                if message.get("id") == Some(&json!(self.next)) {
                    return (message, events);
                }
                events.push(message);
            }
        }

        /// The name and item of every event up to the `log` event carrying `marker`, leaving
        /// `stats` out.
        async fn until_marker(&mut self, agent: &Agent, marker: &str) -> Vec<(String, u64)> {
            agent.log("info", marker);
            let mut events = Vec::new();
            loop {
                let message = self.read().await;
                let name = message["event"].as_str().unwrap().to_string();
                if name == "log" && message["data"]["message"] == marker {
                    return events;
                }
                if name != "stats" {
                    events.push((name, message["data"]["seq"].as_u64().unwrap_or(0)));
                }
            }
        }

        async fn event(&mut self, name: &str) -> Json {
            loop {
                let message = self.read().await;
                if message["event"] == name {
                    return message["data"].clone();
                }
            }
        }
    }

    #[tokio::test(flavor = "multi_thread")]
    async fn control_socket_requires_a_nonempty_token_before_bind() {
        for token in [None, Some(String::new())] {
            let agent = Agent::new(json!({ "kind": "test" }), token, false);
            let error = spawn(agent, "127.0.0.1:0").unwrap_err();
            assert_eq!(error.kind(), io::ErrorKind::PermissionDenied);
        }
    }

    #[tokio::test(flavor = "multi_thread")]
    async fn a_debugger_can_hold_edit_and_release_a_step_over_the_socket() {
        let agent = Agent::new(json!({ "kind": "test" }), Some("secret".into()), false);
        let address = spawn(agent.clone(), "127.0.0.1:0").unwrap();

        let mut stranger = Client::connect(address).await;
        assert_eq!(stranger.call("status", json!({})).await.0["error"]["code"], "hello_first");
        assert_eq!(stranger.call("hello", json!({ "token": "nope" })).await.0["error"]["code"], "bad_token");
        let mut rest = Vec::new();
        assert_eq!(stranger.lines.get_mut().read_to_end(&mut rest).await.unwrap(), 0, "connection is closed");

        let mut client = Client::connect(address).await;
        let (hello, _) = client.call("hello", json!({ "token": "secret" })).await;
        assert_eq!(hello["result"]["protocol"], PROTOCOL);
        assert_eq!(hello["result"]["node"]["kind"], "test");
        assert_eq!(hello["result"]["status"]["resigning"]["available"], false);

        agent.configure_resigning(true, true, None);
        let (resigning, _) = client.call("resign.get", json!({})).await;
        assert_eq!(resigning["result"], json!({ "enabled": true, "available": true }));
        let (disabled, _) = client.call("resign.set", json!({ "enabled": false })).await;
        assert_eq!(disabled["result"], json!({ "enabled": false, "available": true }));

        let (held, _) = client.call("set_hold", json!({ "when": { "op": "field", "path": "fee", "gt": 100 } })).await;
        assert_eq!(held["result"]["frozen"], false);

        let node = tokio::spawn({
            let agent = agent.clone();
            async move {
                let draft = |fee: u64| Draft {
                    kind: "internal_input",
                    to: "stage:validate".into(),
                    payload: json!({ "fee": fee }),
                    editable: true,
                    droppable: true,
                    ..Draft::default()
                };
                let first = agent.gate(draft(5)).await.1;
                let second = agent.gate(draft(301_204)).await.1;
                (first, second)
            }
        });

        let item = client.event("frozen").await;
        assert_eq!(item["seq"], 2);
        let (got, _) = client.call("item.get", json!({ "seq": 2 })).await;
        assert_eq!(got["result"]["payload"], json!({ "fee": 301_204 }));
        assert_eq!(got["result"]["held"], true);

        let (replaced, _) = client.call("item.replace", json!({ "seq": 2, "payload": { "fee": 1 } })).await;
        assert_eq!(replaced["result"]["held"], json!([]));
        assert_eq!(node.await.unwrap(), (Verdict::Forward, Verdict::Replace(json!({ "fee": 1 }))));

        let (thawed, _) = client.call("thaw", json!({})).await;
        assert_eq!(thawed["result"]["frozen"], false);
        assert_eq!(client.call("nope", json!({})).await.0["error"]["code"], "failed");
    }

    #[tokio::test(flavor = "multi_thread")]
    async fn raw_replacement_of_a_typed_item_is_rejected_before_release() {
        let agent = Agent::new(json!({ "kind": "test" }), Some("secret".into()), true);
        let address = spawn(agent.clone(), "127.0.0.1:0").unwrap();
        let mut client = Client::connect(address).await;
        client.call("hello", json!({ "token": "secret" })).await;

        let waiting = tokio::spawn({
            let agent = agent.clone();
            async move {
                agent
                    .gate(Draft {
                        kind: "internal_input",
                        to: "stage:typed".into(),
                        payload: json!({ "value": 1 }),
                        editable: true,
                        ..Draft::default()
                    })
                    .await
            }
        });
        let item = client.event("item").await;
        let (response, events) = client.call("item.replace", json!({ "seq": item["seq"], "raw": "01" })).await;
        assert_eq!(response["error"]["code"], "typed_raw_replacement_unsupported");
        assert!(response["error"]["message"].as_str().unwrap().contains("typed_raw_replacement_unsupported"));
        assert!(events.iter().all(|event| event["event"] != "released"));
        assert_eq!(agent.status()["held"], json!([item["seq"].clone()]));
        agent.step(item["seq"].as_u64()).unwrap();
        assert_eq!(waiting.await.unwrap().1, Verdict::Forward);
    }

    #[tokio::test(flavor = "multi_thread")]
    async fn a_debugger_can_list_and_switch_probes_over_the_socket() {
        let agent = Agent::new(json!({ "kind": "test" }), Some("secret".into()), false);
        let address = spawn(agent.clone(), "127.0.0.1:0").unwrap();
        let mut client = Client::connect(address).await;
        client.call("hello", json!({ "token": "secret" })).await;

        assert_eq!(agent.probe_hit("socket.one", "crates/x/src/lib.rs:1", || json!({})), None);
        assert_eq!(agent.probe_hit("socket.two", "crates/x/src/lib.rs:2", || json!({})), None);
        let listed = |response: &Json| -> Vec<Json> {
            let all = response["result"].as_array().unwrap();
            all.iter().filter(|probe| probe["name"].as_str().unwrap().starts_with("socket.")).cloned().collect()
        };

        let (list, _) = client.call("probe.list", json!({})).await;
        assert_eq!(list["result"].as_array().unwrap().len(), crate::probes::KNOWN.len() + 2);
        assert_eq!(
            listed(&list),
            vec![
                json!({ "name": "socket.one", "where": "crates/x/src/lib.rs:1", "enabled": false, "hits": 1 }),
                json!({ "name": "socket.two", "where": "crates/x/src/lib.rs:2", "enabled": false, "hits": 1 }),
            ]
        );

        let (enabled, _) = client.call("probe.enable", json!({ "name": "socket.*" })).await;
        assert_eq!(enabled["result"], json!({ "changed": 2 }));
        let (disabled, _) = client.call("probe.disable", json!({ "name": "socket.two" })).await;
        assert_eq!(disabled["result"], json!({ "changed": 1 }));
        assert_eq!(client.call("probe.enable", json!({})).await.0["error"]["code"], "failed");

        let seq = agent.probe_hit("socket.one", "", || json!({ "slot": 7 })).unwrap();
        assert_eq!(agent.probe_hit("socket.two", "", || json!({})), None);
        let item = client.event("item").await;
        assert_eq!(item["seq"], seq);
        assert_eq!((item["kind"].clone(), item["label"].clone()), (json!("probe"), json!("socket.one")));
        assert_eq!(item["payload"], json!({ "slot": 7 }));

        let (list, _) = client.call("probe.list", json!({})).await;
        let hits: Vec<(bool, u64)> =
            listed(&list).iter().map(|p| (p["enabled"].as_bool().unwrap(), p["hits"].as_u64().unwrap())).collect();
        assert_eq!(hits, vec![(true, 2), (false, 2)]);

        let (all, _) = client.call("probe.enable", json!({ "name": "all" })).await;
        assert_eq!(all["result"], json!({ "changed": crate::probes::KNOWN.len() + 1 }));
    }

    #[tokio::test(flavor = "multi_thread")]
    async fn a_debugger_can_list_and_switch_traced_functions_over_the_socket() {
        use crate::calls::{Args, Site, enter_in, registry};

        static ONE: Site = Site::new("socket_trace::one", "one", false);
        static TWO: Site = Site::new("socket_trace::two", "two", false);
        let agent = Agent::new(json!({ "kind": "test" }), Some("secret".into()), false);
        let address = spawn(agent.clone(), "127.0.0.1:0").unwrap();
        let mut client = Client::connect(address).await;
        client.call("hello", json!({ "token": "secret" })).await;
        let call = |site: &'static Site| enter_in(registry(), Some(&agent), site, Args::new).seq();

        assert_eq!((call(&ONE), call(&TWO)), (None, None));
        let (list, _) = client.call("trace.list", json!({ "prefix": "socket_trace" })).await;
        assert_eq!(
            list["result"]["sites"],
            json!([
                { "name": "socket_trace::one", "hits": 0, "enabled": false, "held": false },
                { "name": "socket_trace::two", "hits": 0, "enabled": false, "held": false },
            ])
        );
        assert_eq!(list["result"]["total"], 2);

        let (enabled, _) = client.call("trace.enable", json!({ "prefix": "socket_trace" })).await;
        assert_eq!(enabled["result"], json!({ "changed": 2 }));
        assert_eq!(client.call("trace.enable", json!({})).await.0["error"]["code"], "failed");
        let (disabled, _) = client.call("trace.disable", json!({ "prefix": "socket_trace" })).await;
        assert_eq!(disabled["result"], json!({ "changed": 2 }));
        client.call("trace.enable", json!({ "prefix": "socket_trace::one" })).await;

        let seq = call(&ONE).unwrap();
        assert_eq!(call(&TWO), None);
        let item = client.event("item").await;
        assert_eq!(item["seq"], seq);
        assert_eq!((item["kind"].clone(), item["label"].clone()), (json!("call"), json!("one")));
        assert_eq!(client.event("item.done").await["seq"], seq);

        let (list, _) = client.call("trace.list", json!({ "prefix": "socket_trace", "offset": 0, "limit": 1 })).await;
        assert_eq!(list["result"]["enabled"], json!(["socket_trace::one"]));
        assert_eq!(
            list["result"]["sites"],
            json!([{ "name": "socket_trace::one", "hits": 1, "enabled": true, "held": false }])
        );

        let (all, _) = client.call("trace.disable", json!({})).await;
        assert_eq!(all["result"], json!({ "changed": 1 }));
        assert_eq!(client.call("trace.list", json!({})).await.0["result"]["enabled"], json!([]));
    }

    fn item(kind: &'static str, fee: u64) -> Draft {
        Draft { kind, label: format!("fee {fee}"), payload: json!({ "fee": fee }), ..Draft::default() }
    }

    fn items(seqs: &[u64]) -> Vec<(String, u64)> {
        seqs.iter().map(|seq| ("item".to_string(), *seq)).collect()
    }

    #[tokio::test(flavor = "multi_thread")]
    async fn a_connection_is_sent_only_the_items_it_subscribed_to() {
        let agent = Agent::new(json!({ "kind": "test" }), Some("secret".into()), false);
        let address = spawn(agent.clone(), "127.0.0.1:0").unwrap();
        let mut client = Client::connect(address).await;
        client.call("hello", json!({ "token": "secret" })).await;
        let mut other = Client::connect(address).await;
        other.call("hello", json!({ "token": "secret" })).await;

        let before = agent.note(item("call", 1));
        assert_eq!(client.until_marker(&agent, "everything").await, items(&[before]));

        let (subscribed, _) = client.call("events.subscribe", json!({ "kinds": ["probe", "internal_input"] })).await;
        assert_eq!(subscribed["result"], json!({}));
        let call = agent.note(item("call", 2));
        let probe = agent.note(item("probe", 3));
        for seq in [before, call, probe] {
            agent.done(seq, json!({}));
        }
        assert_eq!(
            client.until_marker(&agent, "kinds").await,
            vec![("item".to_string(), probe), ("item.done".to_string(), before), ("item.done".to_string(), probe)],
            "an item is sent if its kind was asked for, the end of an item if the item was sent"
        );
        let (unsent, _) = client.call("item.get", json!({ "seq": call })).await;
        assert_eq!(unsent["result"]["payload"], json!({ "fee": 2 }), "an item that was not sent can still be fetched");

        let when = json!({ "op": "field", "path": "fee", "gt": 10 });
        client.call("events.subscribe", json!({ "when": when, "kinds": null })).await;
        let small = agent.note(item("call", 5));
        let large = agent.note(item("call", 50));
        agent.done(small, json!({}));
        assert_eq!(client.until_marker(&agent, "when").await, items(&[large]));

        client.call("events.subscribe", json!({ "kinds": ["probe"], "when": when, "sample": 3 })).await;
        let passing: Vec<u64> = (0..7)
            .map(|_| {
                agent.note(item("call", 99));
                agent.note(item("probe", 1));
                agent.note(item("probe", 99))
            })
            .collect();
        assert_eq!(
            client.until_marker(&agent, "sample").await,
            items(&[passing[0], passing[3], passing[6]]),
            "one in three of the items that pass, starting with the first"
        );

        agent.freeze();
        let waiting = tokio::spawn({
            let agent = agent.clone();
            async move { agent.gate(item("call", 0)).await }
        });
        let held = client.event("item").await;
        assert_eq!((held["held"].clone(), held["kind"].clone()), (json!(true), json!("call")), "a held item is sent");
        let seq = held["seq"].as_u64().unwrap();
        agent.resolve(seq, Verdict::Forward).unwrap();
        assert_eq!(waiting.await.unwrap(), (seq, Verdict::Forward));
        agent.done(seq, json!({}));
        agent.thaw();
        let names: Vec<String> = client.until_marker(&agent, "held").await.into_iter().map(|(name, _)| name).collect();
        assert_eq!(names, ["released", "item.done", "thawed"]);

        for bad in [json!({ "sample": 0 }), json!({ "sample": "2" }), json!({ "kinds": "probe" }), json!({ "when": 1 })]
        {
            assert_eq!(client.call("events.subscribe", bad).await.0["error"]["code"], "failed");
        }
        agent.note(item("probe", 1));
        assert_eq!(client.until_marker(&agent, "unchanged").await, vec![], "a refused filter changes nothing");

        client.call("events.subscribe", json!({})).await;
        let after = agent.note(item("call", 1));
        agent.done(passing[1], json!({}));
        agent.done(after, json!({}));
        assert_eq!(
            client.until_marker(&agent, "restored").await,
            vec![("item".to_string(), after), ("item.done".to_string(), passing[1]), ("item.done".to_string(), after)],
            "without a filter everything is sent"
        );

        let everything = other.until_marker(&agent, "restored").await;
        let sent = everything.iter().filter(|(name, _)| name == "item").count();
        assert_eq!(sent as u64, agent.status()["seq"].as_u64().unwrap(), "the other connection missed nothing");
    }

    #[tokio::test(flavor = "multi_thread")]
    async fn a_connection_is_told_each_second_what_it_was_not_sent() {
        let agent = Agent::new(json!({ "kind": "test" }), Some("secret".into()), false);
        let address = spawn(agent.clone(), "127.0.0.1:0").unwrap();
        let mut client = Client::connect(address).await;
        client.call("hello", json!({ "token": "secret" })).await;
        let mut other = Client::connect(address).await;
        other.call("hello", json!({ "token": "secret" })).await;

        client.call("events.subscribe", json!({ "kinds": ["probe"] })).await;
        for fee in 0..5 {
            agent.note(item("call", fee));
        }
        agent.note(item("internal_state", 0));
        let last = agent.note(item("probe", 0));

        let (mut total, mut calls, mut dropped) = (0, 0, 0);
        while total < 7 {
            let stats = client.event("stats").await;
            assert_eq!(stats.as_object().unwrap().len(), 4);
            total += stats["items"].as_u64().unwrap();
            calls += stats["by_kind"]["call"].as_u64().unwrap_or(0);
            dropped += stats["dropped"].as_u64().unwrap();
            if total == 7 {
                assert_eq!(stats["seq"], last);
            }
        }
        assert_eq!((total, calls, dropped), (7, 5, 6));

        client.call("events.subscribe", json!({ "kinds": ["probe"] })).await;
        tokio::time::sleep(STATS_EVERY * 2).await;
        agent.log("info", "end");
        for (connection, why) in [(&mut client, "nothing was left out"), (&mut other, "it is sent everything")] {
            loop {
                let message = connection.read().await;
                assert_ne!(message["event"], "stats", "{why}, so nothing is said");
                if message["data"]["message"] == "end" {
                    break;
                }
            }
        }
    }
}
