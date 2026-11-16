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
//! Reports of the calls made to functions carrying [`trace_fn`](crate::trace_fn).
//!
//! Every such function owns a [`Site`] and starts by calling [`enter`]. While the function is
//! not selected by an enabled prefix, that call is a single atomic load. While it is selected
//! and an agent is installed, the call is reported as an item of kind `call` and the returned
//! [`Guard`] reports the end of the call when it is dropped.
//!
//! Functions are selected by the prefix of their full path, `crate::module::Type::function`:
//! see [`enable`]. A call is only reported, unless its function was also selected with [`hold`]:
//! then the call waits at its start for a debugger to forward it, wherever waiting is possible.
//!
//! The calls running on a thread form a stack, from which each call takes its depth and its
//! caller. An ordinary function is on the stack from its start to its end. An `async fn` is on
//! it whenever it is being polled, through [`framed`], so that what it calls between two of its
//! awaits is reported as called by it.

use std::{
    cell::RefCell,
    collections::{BTreeMap, BTreeSet},
    fmt::{self, Write},
    future::Future,
    pin::Pin,
    sync::{
        Arc, LazyLock,
        atomic::{AtomicU8, AtomicU64, Ordering},
    },
    task::{Context, Poll},
    time::Instant,
};

use parking_lot::Mutex;
use serde_json::{Map, Value as Json, json};
use tokio::sync::oneshot;

use crate::agent::{Agent, Draft, Pending, Verdict};

/// Longest text reported for one argument, in characters.
pub const ARGUMENT_LIMIT: usize = 300;

/// Most sites one [`list`] answers with, whatever was asked for.
pub const LIST_LIMIT: usize = 5_000;

const UNSEEN: u8 = 0;
const OFF: u8 = 1;
const ON: u8 = 2;
const HELD: u8 = 3;

/// Why the node froze when a call was held; the reason its `frozen` event gives.
pub const HOLD_REASON: &str = "trace.hold";

/// What a call says in `not_held` when its function is selected for holding but waiting where
/// it runs would stop a thread the runtime needs.
pub const NOT_HELD: &str = "would block the runtime";

/// One function that can be traced.
pub struct Site {
    path: &'static str,
    name: &'static str,
    is_async: bool,
    state: AtomicU8,
    hits: AtomicU64,
}

impl Site {
    /// A site for the function at `path` (`crate::module::Type::function`) whose own name is
    /// `name`.
    pub const fn new(path: &'static str, name: &'static str, is_async: bool) -> Self {
        Self { path, name, is_async, state: AtomicU8::new(UNSEEN), hits: AtomicU64::new(0) }
    }

    /// The full path of the function.
    pub fn path(&self) -> &'static str {
        self.path
    }
}

/// Whether `prefix` selects the function at `path`.
///
/// `all` selects every function. `text*` selects the paths starting with `text`. Anything else
/// selects the path equal to it and the paths below it, so `amaru_ledger::rules` selects
/// `amaru_ledger::rules::block::execute` but not `amaru_ledger::rules_of_thumb`.
fn selects(prefix: &str, path: &str) -> bool {
    if prefix == "all" {
        return true;
    }
    match prefix.strip_suffix('*') {
        Some(start) => path.starts_with(start),
        None => path.strip_prefix(prefix).is_some_and(|rest| rest.is_empty() || rest.starts_with("::")),
    }
}

/// A prefix as it is stored: trimmed, `*` and the empty text meaning `all`, and the dashes of a
/// crate name written the way paths have them.
fn normalise(prefix: &str) -> String {
    let prefix = prefix.trim();
    if prefix.is_empty() || prefix == "*" { "all".to_string() } else { prefix.replace('-', "_") }
}

#[derive(Default)]
struct Inner {
    prefixes: BTreeSet<String>,
    holds: BTreeSet<String>,
    sites: BTreeMap<&'static str, Vec<&'static Site>>,
}

impl Inner {
    /// The state the function at `path` should be in: held if selected for holding, which
    /// counts as enabled, else on or off as the enabled prefixes say.
    fn wanted(&self, path: &str) -> u8 {
        if self.holds.iter().any(|prefix| selects(prefix, path)) {
            HELD
        } else if self.prefixes.iter().any(|prefix| selects(prefix, path)) {
            ON
        } else {
            OFF
        }
    }

    /// Bring every known site in line with the prefixes. Returns how many changed state.
    fn refresh(&self) -> usize {
        let mut changed = 0;
        for (path, sites) in &self.sites {
            let state = self.wanted(path);
            for site in sites {
                if site.state.swap(state, Ordering::Relaxed) != state {
                    changed += 1;
                }
            }
        }
        changed
    }
}

/// The enabled prefixes and the functions seen so far.
#[derive(Default)]
pub struct Registry {
    inner: Mutex<Inner>,
}

impl Registry {
    /// Trace the functions selected by `prefix` from now on, including ones not called yet.
    /// Returns how many functions seen so far changed state.
    pub fn enable(&self, prefix: &str) -> usize {
        let mut inner = self.inner.lock();
        inner.prefixes.insert(normalise(prefix));
        inner.refresh()
    }

    /// Stop tracing the functions selected by `prefix`: every enabled prefix it selects is
    /// removed, so `all` removes them all. Returns how many functions seen so far changed state.
    pub fn disable(&self, prefix: &str) -> usize {
        let prefix = normalise(prefix);
        let mut inner = self.inner.lock();
        inner.prefixes.retain(|enabled| *enabled != prefix && !selects(&prefix, enabled.trim_end_matches('*')));
        inner.refresh()
    }

    /// Hold the calls of the functions selected by `prefix` at their start from now on, which
    /// also traces them. Returns how many functions seen so far changed state.
    pub fn hold(&self, prefix: &str) -> usize {
        let mut inner = self.inner.lock();
        inner.holds.insert(normalise(prefix));
        inner.refresh()
    }

    /// Stop holding the functions selected by `prefix`, as [`Registry::disable`] stops tracing
    /// them. A function that is also enabled stays traced. Returns how many functions seen so
    /// far changed state.
    pub fn unhold(&self, prefix: &str) -> usize {
        let prefix = normalise(prefix);
        let mut inner = self.inner.lock();
        inner.holds.retain(|held| *held != prefix && !selects(&prefix, held.trim_end_matches('*')));
        inner.refresh()
    }

    /// Enable every prefix of a comma-separated list. Returns how many functions changed state.
    pub fn enable_listed(&self, list: &str) -> usize {
        list.split(',').map(str::trim).filter(|prefix| !prefix.is_empty()).map(|prefix| self.enable(prefix)).sum()
    }

    /// The enabled prefixes and the functions called at least once, in path order:
    /// `{"enabled": [..], "holding": [..], "sites": [{"name", "hits", "enabled", "held"}], "total"}`.
    ///
    /// Only the functions selected by `prefix` are listed, `limit` of them at most (and never
    /// more than [`LIST_LIMIT`]) after skipping `offset`; `total` counts them all. `hits` counts
    /// the calls made while the function was enabled. `holding` is the prefixes selected for
    /// holding; a function they select is `held` and, for that reason alone, `enabled`.
    pub fn list(&self, prefix: Option<&str>, offset: usize, limit: usize) -> Json {
        let prefix = prefix.map(normalise);
        let inner = self.inner.lock();
        let selected =
            || inner.sites.iter().filter(|(path, _)| prefix.as_deref().is_none_or(|prefix| selects(prefix, path)));
        let sites: Vec<Json> = selected()
            .skip(offset)
            .take(limit.min(LIST_LIMIT))
            .map(|(path, sites)| {
                json!({
                    "name": path,
                    "hits": sites.iter().map(|site| site.hits.load(Ordering::Relaxed)).sum::<u64>(),
                    "enabled": sites.iter().any(|site| matches!(site.state.load(Ordering::Relaxed), ON | HELD)),
                    "held": sites.iter().any(|site| site.state.load(Ordering::Relaxed) == HELD),
                })
            })
            .collect();
        json!({ "enabled": inner.prefixes, "holding": inner.holds, "sites": sites, "total": selected().count() })
    }

    /// The state of `site`: off, on or held. Makes it known first if this is its first call.
    fn observe(&self, site: &'static Site) -> u8 {
        match site.state.load(Ordering::Relaxed) {
            UNSEEN => {
                let mut inner = self.inner.lock();
                let state = inner.wanted(site.path);
                if site.state.swap(state, Ordering::Relaxed) == UNSEEN {
                    inner.sites.entry(site.path).or_default().push(site);
                }
                state
            }
            state => state,
        }
    }
}

static REGISTRY: LazyLock<Registry> = LazyLock::new(Registry::default);

/// The registry the traced functions of this process report to.
pub fn registry() -> &'static Registry {
    &REGISTRY
}

/// [`Registry::enable`] on the registry of this process.
pub fn enable(prefix: &str) -> usize {
    REGISTRY.enable(prefix)
}

/// [`Registry::disable`] on the registry of this process.
pub fn disable(prefix: &str) -> usize {
    REGISTRY.disable(prefix)
}

/// [`Registry::hold`] on the registry of this process.
pub fn hold(prefix: &str) -> usize {
    REGISTRY.hold(prefix)
}

/// [`Registry::unhold`] on the registry of this process.
pub fn unhold(prefix: &str) -> usize {
    REGISTRY.unhold(prefix)
}

/// [`Registry::list`] on the registry of this process, unfiltered.
pub fn list() -> Json {
    REGISTRY.list(None, 0, LIST_LIMIT)
}

/// The arguments of one call, by name.
#[derive(Default)]
pub struct Args(Map<String, Json>);

impl Args {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn push(&mut self, name: &str, text: String) {
        self.0.insert(name.to_string(), Json::String(text));
    }
}

struct Clipped {
    text: String,
    room: usize,
}

impl Write for Clipped {
    fn write_str(&mut self, part: &str) -> fmt::Result {
        for character in part.chars() {
            if self.room == 0 {
                return Err(fmt::Error);
            }
            self.text.push(character);
            self.room -= 1;
        }
        Ok(())
    }
}

/// The `{:?}` text of `value`, cut after [`ARGUMENT_LIMIT`] characters without formatting the rest.
pub fn debug_text<T: fmt::Debug + ?Sized>(value: &T) -> String {
    let mut clipped = Clipped { text: String::new(), room: ARGUMENT_LIMIT };
    if write!(clipped, "{value:?}").is_err() {
        clipped.text.push('…');
    }
    clipped.text
}

/// An argument about to be shown: `(&Shown(&argument)).freeze_text()` is its `{:?}` text when
/// its type implements `Debug` and `"…"` otherwise, provided [`ShowDebug`] and [`ShowOpaque`]
/// are both in scope. Method lookup prefers [`ShowDebug`] because it needs one reference less.
pub struct Shown<'a, T>(pub &'a T);

pub trait ShowDebug {
    fn freeze_text(&self) -> String;
}

impl<T: fmt::Debug> ShowDebug for Shown<'_, T> {
    fn freeze_text(&self) -> String {
        debug_text(self.0)
    }
}

pub trait ShowOpaque {
    fn freeze_text(&self) -> String;
}

impl<T> ShowOpaque for &Shown<'_, T> {
    fn freeze_text(&self) -> String {
        "…".to_string()
    }
}

thread_local! {
    /// The enabled functions running on this thread, outermost first: ordinary functions from
    /// start to end, `async fn`s while they are being polled.
    static STACK: RefCell<Vec<&'static str>> = const { RefCell::new(Vec::new()) };
}

struct Reported {
    agent: Arc<Agent>,
    seq: u64,
    started: Instant,
    on_stack: bool,
    /// The path of the function if it is an `async fn`, which joins the stack when polled.
    frame: Option<&'static str>,
    /// The verdict a held call of an `async fn` has yet to wait for.
    pending: Option<oneshot::Receiver<Verdict>>,
}

/// Reports the end of a call when dropped. Does nothing for a call that was not reported.
#[must_use = "the call ends when the guard is dropped"]
pub struct Guard(Option<Reported>);

impl Guard {
    /// The sequence number of the `call` item, if the call was reported.
    pub fn seq(&self) -> Option<u64> {
        self.0.as_ref().map(|reported| reported.seq)
    }

    /// What [`framed`] puts on the stack while the body of this call is polled: the path of the
    /// function, if the call was reported and the function is an `async fn`.
    pub fn frame(&self) -> Option<&'static str> {
        self.0.as_ref().and_then(|reported| reported.frame)
    }
}

impl Drop for Guard {
    fn drop(&mut self) {
        let Some(reported) = self.0.take() else {
            return;
        };
        if reported.on_stack {
            let _ = STACK.try_with(|stack| stack.borrow_mut().pop());
        }
        let ms = reported.started.elapsed().as_secs_f64() * 1000.0;
        reported.agent.done(reported.seq, json!({ "ms": ms, "unwound": std::thread::panicking() }));
    }
}

/// Start a call of the function `site` belongs to. `args` is evaluated only if the call is
/// reported, which takes an enabled prefix selecting the function and an installed agent.
///
/// A call of a function selected for holding waits here, on a thread that no runtime needs,
/// until a debugger forwards it. A held call of an `async fn` running in a stage is admitted
/// here and waits in [`held`], which the function must await next.
#[inline]
pub fn enter(site: &'static Site, args: impl FnOnce() -> Args) -> Guard {
    if site.state.load(Ordering::Relaxed) == OFF {
        return Guard(None);
    }
    enter_in(&REGISTRY, crate::agent(), site, args)
}

/// [`enter`] against a given registry and agent.
#[cold]
pub fn enter_in(
    registry: &Registry,
    agent: Option<&Arc<Agent>>,
    site: &'static Site,
    args: impl FnOnce() -> Args,
) -> Guard {
    let state = registry.observe(site);
    if state == OFF {
        return Guard(None);
    }
    site.hits.fetch_add(1, Ordering::Relaxed);
    let Some(agent) = agent else {
        return Guard(None);
    };
    let args = args();
    let position = STACK.try_with(|stack| {
        let mut stack = stack.borrow_mut();
        let position = (stack.len(), stack.last().copied());
        if !site.is_async {
            stack.push(site.path);
        }
        position
    });
    let on_stack = position.is_ok() && !site.is_async;
    let (depth, caller) = position.unwrap_or_default();
    let (parent, cause) = crate::trace::current();
    let can_wait =
        if site.is_async { crate::trace::in_stage() } else { tokio::runtime::Handle::try_current().is_err() };
    let mut payload = json!({ "args": args.0, "depth": depth, "caller": caller, "async": site.is_async });
    if state == HELD && !can_wait {
        payload["not_held"] = json!(NOT_HELD);
    }
    let draft = Draft {
        kind: "call",
        from: format!("call:{}", site.path),
        label: site.name.to_string(),
        type_name: site.path.to_string(),
        payload,
        parent: (parent != 0).then_some(parent),
        cause: (cause != 0).then_some(cause),
        ..Draft::default()
    };
    let (seq, pending) = if state == HELD && can_wait {
        match agent.admit_held(draft, !site.is_async, HOLD_REASON) {
            (seq, Some(Pending::Blocking(verdict))) => {
                drop(verdict.recv());
                (seq, None)
            }
            (seq, Some(Pending::Async(verdict))) => (seq, Some(verdict)),
            (seq, None) => (seq, None),
        }
    } else {
        (agent.note(draft), None)
    };
    let frame = site.is_async.then_some(site.path);
    Guard(Some(Reported { agent: agent.clone(), seq, started: Instant::now(), on_stack, frame, pending }))
}

/// Wait until a debugger forwards the call `guard` stands for, if [`enter`] found it held.
/// Whatever the verdict, the call goes on. Returns at once for every other call.
#[inline]
pub async fn held(guard: &mut Guard) {
    if let Some(verdict) = guard.0.as_mut().and_then(|reported| reported.pending.take()) {
        drop(verdict.await);
    }
}

/// The body of an `async fn`, on the stack of calls whenever it is polled.
pub struct Framed<'a, F> {
    frame: Option<&'static str>,
    body: Pin<&'a mut F>,
}

/// Run `body` with `frame` on the stack of calls during each of its polls, so that the calls it
/// makes report `frame` as their caller and a depth below it. With no frame, `body` runs as is.
#[inline]
pub fn framed<'a, F: Future>(frame: Option<&'static str>, body: Pin<&'a mut F>) -> Framed<'a, F> {
    Framed { frame, body }
}

struct OnStack(bool);

impl Drop for OnStack {
    fn drop(&mut self) {
        if self.0 {
            let _ = STACK.try_with(|stack| stack.borrow_mut().pop());
        }
    }
}

impl<F: Future> Future for Framed<'_, F> {
    type Output = F::Output;

    #[inline]
    fn poll(mut self: Pin<&mut Self>, context: &mut Context<'_>) -> Poll<Self::Output> {
        let Some(frame) = self.frame else {
            return self.body.as_mut().poll(context);
        };
        let _on_stack = OnStack(STACK.try_with(|stack| stack.borrow_mut().push(frame)).is_ok());
        self.body.as_mut().poll(context)
    }
}

#[cfg(test)]
mod tests {
    use tokio::sync::broadcast::Receiver;

    use super::*;

    fn agent() -> Arc<Agent> {
        Agent::new(json!({}), None, false)
    }

    fn never() -> Args {
        panic!("arguments were evaluated although nothing reports them")
    }

    fn next(events: &mut Receiver<Arc<str>>) -> Json {
        serde_json::from_str(&events.try_recv().unwrap()).unwrap()
    }

    fn entry(registry: &Registry, path: &str) -> Json {
        let list = registry.list(None, 0, LIST_LIMIT);
        list["sites"].as_array().unwrap().iter().find(|site| site["name"] == path).cloned().unwrap_or(Json::Null)
    }

    #[test]
    fn prefixes_select_whole_path_segments() {
        let path = "amaru_ledger::rules::block::execute";
        assert!(selects("all", path));
        assert!(selects("amaru_ledger", path));
        assert!(selects("amaru_ledger::rules", path));
        assert!(selects(path, path));
        assert!(!selects("amaru_ledger::rule", path));
        assert!(selects("amaru_ledger::rule*", path));
        assert!(!selects("amaru_ledger::rules::block::execute::inner", path));
        assert!(!selects("ledger", path));
        assert_eq!(normalise(" amaru-ledger::rules "), "amaru_ledger::rules");
        assert_eq!((normalise(""), normalise("*")), ("all".to_string(), "all".to_string()));
    }

    #[test]
    fn a_function_is_known_after_its_first_call_and_follows_the_prefixes() {
        static ONE: Site = Site::new("known_test::alpha::one", "one", false);
        static TWO: Site = Site::new("known_test::alpha::two", "two", false);
        static OTHER: Site = Site::new("known_test::beta::one", "one", false);
        let registry = Registry::default();
        let on = |path: &str| entry(&registry, path)["enabled"] == true;

        assert_eq!(registry.enable("known_test::alpha"), 0, "nothing was called yet");
        for site in [&ONE, &TWO, &OTHER] {
            assert!(enter_in(&registry, None, site, never).seq().is_none());
        }
        assert!(on(ONE.path()) && on(TWO.path()) && !on(OTHER.path()));
        assert_eq!(
            entry(&registry, ONE.path()),
            json!({ "name": ONE.path(), "hits": 1, "enabled": true, "held": false })
        );
        assert_eq!(entry(&registry, OTHER.path())["hits"], 0, "calls are counted while enabled");

        assert_eq!(registry.enable("known_test::alpha"), 0, "nothing changes the second time");
        assert_eq!(registry.enable("known-test::beta::one"), 1);
        assert_eq!(registry.disable("known_test::alpha::one"), 0, "still selected by the wider prefix");
        assert_eq!(registry.disable("known_test::alpha"), 2);
        assert!(!on(ONE.path()) && !on(TWO.path()) && on(OTHER.path()));

        assert_eq!(registry.enable_listed(" known_test::alpha::two , known_test::beta* ,,"), 1);
        let list = registry.list(Some("known_test::alpha"), 0, LIST_LIMIT);
        assert_eq!(list["enabled"], json!(["known_test::alpha::two", "known_test::beta*", "known_test::beta::one"]));
        assert_eq!(list["total"], 2);
        assert_eq!(list["sites"].as_array().unwrap().len(), 2);
        let page = registry.list(None, 1, 1);
        assert_eq!((page["total"].clone(), page["sites"][0]["name"].clone()), (json!(3), json!(TWO.path())));

        assert_eq!(registry.disable("all"), 2);
        assert_eq!(registry.list(None, 0, 0), json!({ "enabled": [], "holding": [], "sites": [], "total": 3 }));
    }

    #[test]
    fn a_disabled_function_reports_nothing() {
        static QUIET: Site = Site::new("quiet_test::quiet", "quiet", false);
        let registry = Registry::default();
        let agent = agent();
        let mut events = agent.subscribe();
        registry.enable("quiet_test::other");
        for _ in 0..3 {
            drop(enter_in(&registry, Some(&agent), &QUIET, never));
            drop(enter(&QUIET, never));
        }
        assert!(events.try_recv().is_err());
        assert_eq!(agent.status()["seq"], 0);
        assert_eq!(
            entry(&registry, QUIET.path()),
            json!({ "name": QUIET.path(), "hits": 0, "enabled": false, "held": false })
        );
    }

    #[test]
    fn calls_report_their_arguments_depth_and_caller() {
        static OUTER: Site = Site::new("stack_test::Outer::run", "run", false);
        static INNER: Site = Site::new("stack_test::inner", "inner", false);
        let registry = Registry::default();
        let agent = Agent::new(json!({}), None, true);
        let mut events = agent.subscribe();
        registry.enable("stack_test");

        let outer = enter_in(&registry, Some(&agent), &OUTER, || {
            let mut args = Args::new();
            args.push("slot", debug_text(&42_u64));
            args
        });
        let inner = enter_in(&registry, Some(&agent), &INNER, Args::new);
        assert_eq!((outer.seq(), inner.seq()), (Some(1), Some(2)));

        let item = next(&mut events)["data"].clone();
        assert_eq!((item["kind"].clone(), item["label"].clone()), (json!("call"), json!("run")));
        assert_eq!(
            (item["type"].clone(), item["from"].clone()),
            (json!(OUTER.path()), json!("call:stack_test::Outer::run"))
        );
        assert_eq!(item["payload"], json!({ "args": { "slot": "42" }, "depth": 0, "caller": null, "async": false }));
        assert_eq!(item["held"], false, "a call never waits, even while the node is frozen");
        assert_eq!(
            next(&mut events)["data"]["payload"],
            json!({ "args": {}, "depth": 1, "caller": OUTER.path(), "async": false })
        );

        drop(inner);
        let done = next(&mut events);
        assert_eq!((done["event"].clone(), done["data"]["seq"].clone()), (json!("item.done"), json!(2)));
        assert_eq!(done["data"]["outcome"]["unwound"], false);
        assert!(done["data"]["outcome"]["ms"].as_f64().unwrap() >= 0.0);

        let sibling = enter_in(&registry, Some(&agent), &INNER, Args::new);
        assert_eq!(next(&mut events)["data"]["payload"]["depth"], 1, "the first inner call left the stack");
        drop(sibling);
        drop(outer);
        assert_eq!(next(&mut events)["data"]["seq"], 3);
        assert_eq!(next(&mut events)["data"]["seq"], 1);
        assert_eq!(entry(&registry, INNER.path())["hits"], 2);
    }

    #[test]
    fn a_call_that_unwinds_says_so() {
        static FAILING: Site = Site::new("unwind_test::failing", "failing", false);
        let registry = Registry::default();
        let agent = agent();
        let mut events = agent.subscribe();
        registry.enable("unwind_test");
        let unwound = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
            let _guard = enter_in(&registry, Some(&agent), &FAILING, Args::new);
            panic!("expected by this test");
        }));
        assert!(unwound.is_err());
        assert_eq!(next(&mut events)["event"], "item");
        assert_eq!(next(&mut events)["data"]["outcome"]["unwound"], true);
        let again = enter_in(&registry, Some(&agent), &FAILING, Args::new);
        assert_eq!(next(&mut events)["data"]["payload"]["depth"], 0, "the unwound call left the stack");
        drop(again);
    }

    #[test]
    fn an_async_function_is_reported_but_not_put_on_the_stack() {
        static WAITING: Site = Site::new("async_test::waiting", "waiting", true);
        static PLAIN: Site = Site::new("async_test::plain", "plain", false);
        let registry = Registry::default();
        let agent = agent();
        let mut events = agent.subscribe();
        registry.enable("async_test");

        let waiting = enter_in(&registry, Some(&agent), &WAITING, Args::new);
        assert_eq!(
            next(&mut events)["data"]["payload"],
            json!({ "args": {}, "depth": 0, "caller": null, "async": true })
        );
        let plain = enter_in(&registry, Some(&agent), &PLAIN, Args::new);
        assert_eq!(next(&mut events)["data"]["payload"]["caller"], Json::Null);
        drop(plain);
        std::thread::scope(|scope| scope.spawn(move || drop(waiting)).join()).unwrap();
        assert_eq!(next(&mut events)["data"]["seq"], 2);
        let done = next(&mut events);
        assert_eq!((done["event"].clone(), done["data"]["seq"].clone()), (json!("item.done"), json!(1)));
    }

    #[test]
    fn an_async_function_is_on_the_stack_while_its_body_is_polled() {
        static OUTER: Site = Site::new("framed_test::outer", "outer", true);
        static WAITING: Site = Site::new("framed_test::waiting", "waiting", true);
        static PLAIN: Site = Site::new("framed_test::plain", "plain", false);
        let registry = Registry::default();
        let agent = agent();
        let mut events = agent.subscribe();
        registry.enable("framed_test");
        let plain = || drop(enter_in(&registry, Some(&agent), &PLAIN, Args::new));

        let outer = async {
            let guard = enter_in(&registry, Some(&agent), &OUTER, Args::new);
            let body = std::pin::pin!(async {
                plain();
                let guard = enter_in(&registry, Some(&agent), &WAITING, Args::new);
                let body = std::pin::pin!(async {
                    tokio::task::yield_now().await;
                    plain();
                });
                framed(guard.frame(), body).await;
                drop(guard);
                plain();
            });
            framed(guard.frame(), body).await;
        };
        let beside = async { plain() };
        let runtime = tokio::runtime::Builder::new_current_thread().build().unwrap();
        runtime.block_on(async { tokio::join!(outer, beside) });
        assert_eq!(framed(None, std::pin::pin!(async { 7 })).frame, None);

        let mut seen = Vec::new();
        while let Ok(line) = events.try_recv() {
            let event: Json = serde_json::from_str(&line).unwrap();
            if event["event"] == "item" {
                let item = &event["data"];
                seen.push((item["label"].clone(), item["payload"]["depth"].clone(), item["payload"]["caller"].clone()));
            }
        }
        assert_eq!(
            seen,
            [
                (json!("outer"), json!(0), Json::Null),
                (json!("plain"), json!(1), json!(OUTER.path())),
                (json!("waiting"), json!(1), json!(OUTER.path())),
                (json!("plain"), json!(0), Json::Null),
                (json!("plain"), json!(2), json!(WAITING.path())),
                (json!("plain"), json!(1), json!(OUTER.path())),
            ],
            "what runs beside a suspended function is not called by it"
        );
    }

    #[test]
    fn functions_selected_for_holding_are_enabled_and_listed_as_held() {
        static ONE: Site = Site::new("holding_test::alpha::one", "one", false);
        static TWO: Site = Site::new("holding_test::beta::two", "two", false);
        let registry = Registry::default();
        for site in [&ONE, &TWO] {
            drop(enter_in(&registry, None, site, never));
        }
        assert_eq!(registry.hold("holding-test::alpha"), 1);
        assert_eq!(registry.hold("holding_test::alpha"), 0);
        assert_eq!(registry.enable("holding_test"), 1, "the held function was enabled already");
        let list = registry.list(None, 0, LIST_LIMIT);
        assert_eq!(
            (list["enabled"].clone(), list["holding"].clone()),
            (json!(["holding_test"]), json!(["holding_test::alpha"]))
        );
        assert_eq!(
            list["sites"],
            json!([
                { "name": ONE.path(), "hits": 0, "enabled": true, "held": true },
                { "name": TWO.path(), "hits": 0, "enabled": true, "held": false },
            ])
        );

        assert_eq!(registry.disable("all"), 1, "a held function stays enabled");
        assert_eq!(entry(&registry, ONE.path())["enabled"], true);
        assert_eq!(registry.unhold("holding_test::beta"), 0);
        assert_eq!(registry.unhold("all"), 1);
        assert_eq!(registry.list(None, 0, 0)["holding"], json!([]));
        assert_eq!(
            entry(&registry, ONE.path()),
            json!({ "name": ONE.path(), "hits": 0, "enabled": false, "held": false })
        );
    }

    fn held_seq(agent: &Agent) -> u64 {
        loop {
            if let Some(seq) = agent.status()["held"][0].as_u64() {
                return seq;
            }
            std::thread::sleep(std::time::Duration::from_millis(5));
        }
    }

    #[test]
    fn a_held_call_blocks_a_plain_thread_until_it_is_forwarded() {
        static BLOCKING: Site = Site::new("hold_thread_test::blocking", "blocking", false);
        static REGISTRY: LazyLock<Registry> = LazyLock::new(Registry::default);
        let agent = agent();
        let mut events = agent.subscribe();
        REGISTRY.hold("hold_thread_test");

        let worker = std::thread::spawn({
            let agent = agent.clone();
            move || {
                let guard = enter_in(&REGISTRY, Some(&agent), &BLOCKING, || {
                    let mut args = Args::new();
                    args.push("slot", debug_text(&42_u64));
                    args
                });
                (guard.seq(), agent.is_frozen())
            }
        });
        let seq = held_seq(&agent);
        assert!(agent.is_frozen(), "a held call freezes the node");
        assert!(!worker.is_finished());
        let item = agent.item(seq).unwrap();
        assert_eq!(
            (item["kind"].clone(), item["held"].clone(), item["editable"].clone(), item["droppable"].clone()),
            (json!("call"), json!(true), json!(false), json!(false))
        );
        assert_eq!(item["payload"], json!({ "args": { "slot": "42" }, "depth": 0, "caller": null, "async": false }));
        assert_eq!(next(&mut events)["event"], "item");
        assert_eq!(next(&mut events), json!({ "event": "frozen", "data": { "seq": seq, "reasons": [HOLD_REASON] } }));
        assert!(agent.resolve(seq, Verdict::Drop).is_err(), "a call cannot be skipped");
        assert!(agent.resolve(seq, Verdict::Replace(json!({}))).is_err(), "nor its arguments swapped");
        std::thread::sleep(std::time::Duration::from_millis(20));
        assert!(!worker.is_finished(), "it is still waiting");

        agent.resolve(seq, Verdict::Forward).unwrap();
        assert_eq!(worker.join().unwrap(), (Some(seq), true), "the node stays frozen after one forward");
        agent.thaw();
        assert!(!agent.is_frozen());
    }

    #[test]
    fn a_call_is_not_held_where_waiting_would_block_the_runtime() {
        static ON_WORKER: Site = Site::new("hold_refused_test::on_worker", "on_worker", false);
        static NO_STAGE: Site = Site::new("hold_refused_test::no_stage", "no_stage", true);
        let registry = Registry::default();
        let agent = agent();
        registry.hold("hold_refused_test");
        let runtime = tokio::runtime::Builder::new_current_thread().build().unwrap();

        let (sync_call, async_call) = runtime.block_on(async {
            let on_worker = enter_in(&registry, Some(&agent), &ON_WORKER, Args::new).seq();
            let mut no_stage = enter_in(&registry, Some(&agent), &NO_STAGE, Args::new);
            held(&mut no_stage).await;
            (on_worker.unwrap(), no_stage.seq().unwrap())
        });
        let outside = std::thread::scope(|scope| {
            scope.spawn(|| enter_in(&registry, Some(&agent), &NO_STAGE, Args::new).seq().unwrap()).join().unwrap()
        });
        for (seq, is_async) in [(sync_call, false), (async_call, true), (outside, true)] {
            let item = agent.item(seq).unwrap();
            assert_eq!(item["held"], false);
            assert_eq!(
                item["payload"],
                json!({ "args": {}, "depth": 0, "caller": null, "async": is_async, "not_held": NOT_HELD })
            );
        }
        assert!(!agent.is_frozen());
        assert_eq!(agent.status()["held"], json!([]));
    }

    #[tokio::test]
    async fn a_held_call_of_an_async_function_waits_in_its_stage() {
        static STAGED: Site = Site::new("hold_stage_test::staged", "staged", true);
        static REGISTRY: LazyLock<Registry> = LazyLock::new(Registry::default);
        let agent = agent();
        REGISTRY.hold("hold_stage_test::staged");
        crate::trace::set_context("hold-test-stage", 7, 3);

        let stage = tokio::spawn(crate::trace::in_part("hold-test-stage", {
            let agent = agent.clone();
            async move {
                let mut guard = enter_in(&REGISTRY, Some(&agent), &STAGED, Args::new);
                held(&mut guard).await;
                guard.seq().unwrap()
            }
        }));
        while agent.status()["held"] == json!([]) {
            tokio::time::sleep(std::time::Duration::from_millis(5)).await;
        }
        let seq = held_seq(&agent);
        assert!(agent.is_frozen() && !stage.is_finished());
        let item = agent.item(seq).unwrap();
        assert_eq!(
            (item["held"].clone(), item["parent"].clone(), item["cause"].clone()),
            (json!(true), json!(7), json!(3))
        );
        assert_eq!(item["payload"], json!({ "args": {}, "depth": 0, "caller": null, "async": true }));

        tokio::time::sleep(std::time::Duration::from_millis(20)).await;
        assert!(!stage.is_finished(), "it is still waiting");
        assert_eq!(agent.step(None), Ok(seq));
        assert_eq!(stage.await.unwrap(), seq);
        crate::trace::forget("hold-test-stage");
    }

    #[tokio::test]
    async fn a_call_names_the_step_its_stage_is_handling() {
        static STAGED: Site = Site::new("staged_test::staged", "staged", false);
        let registry = Registry::default();
        let agent = agent();
        registry.enable("staged_test");
        crate::trace::set_context("call-test-stage", 7, 3);
        let seq = crate::trace::in_part("call-test-stage", async {
            enter_in(&registry, Some(&agent), &STAGED, Args::new).seq()
        })
        .await
        .unwrap();
        crate::trace::forget("call-test-stage");
        let item = agent.item(seq).unwrap();
        assert_eq!((item["parent"].clone(), item["cause"].clone()), (json!(7), json!(3)));
    }

    #[test]
    #[allow(clippy::needless_borrow)]
    fn arguments_are_shown_when_they_can_be_and_cut_when_long() {
        struct Opaque;
        let (number, text, opaque, long) = (7_u8, "seven", Opaque, vec![1_u64; 1_000]);
        assert_eq!((&Shown(&number)).freeze_text(), "7");
        assert_eq!((&Shown(&text)).freeze_text(), "\"seven\"");
        assert_eq!((&Shown(&opaque)).freeze_text(), "…");
        let shown = (&Shown(&long)).freeze_text();
        assert_eq!(shown.chars().count(), ARGUMENT_LIMIT + 1);
        assert!(shown.starts_with("[1, 1,") && shown.ends_with('…'));
    }
}
