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
//! `#[trace_fn]` on every shape of function the node has, and what a debugger then sees.

#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

use std::{
    fmt::Debug,
    future::Future,
    sync::{Arc, LazyLock, Mutex, MutexGuard},
};

use amaru_freeze::{
    Agent, calls,
    serde_json::{Value as Json, json},
    trace_fn,
};
use tokio::sync::broadcast::Receiver;

/// Functions the attribute must accept. Most are only compiled, never called.
#[allow(
    dead_code,
    clippy::extra_unused_lifetimes,
    clippy::needless_lifetimes,
    clippy::unused_async,
    clippy::needless_arbitrary_self_type,
    clippy::missing_safety_doc,
    clippy::boxed_local,
    clippy::redundant_pattern,
    clippy::toplevel_ref_arg
)]
mod shapes {
    use super::*;

    pub struct Opaque;

    #[derive(Debug, Default)]
    pub struct Counter {
        pub count: u64,
    }

    pub trait Shape {
        #[trace_fn(in = "Shape")]
        fn declared(&self, by: u64) -> u64;

        #[trace_fn(in = "Shape")]
        fn provided(&self, by: u64) -> u64 {
            self.declared(by) + 1
        }
    }

    impl Shape for Counter {
        #[trace_fn(in = "Counter")]
        fn declared(&self, by: u64) -> u64 {
            self.count + by
        }
    }

    impl Counter {
        #[trace_fn(in = "Counter")]
        pub fn by_value(self) -> u64 {
            self.count
        }

        #[trace_fn(in = "Counter")]
        pub fn by_ref(&self, label: &str) -> String {
            format!("{label}{}", self.count)
        }

        #[trace_fn(in = "Counter")]
        pub fn by_mut(&mut self, by: u64) {
            self.count += by;
        }

        #[trace_fn(in = "Counter")]
        pub fn boxed(self: Box<Self>) -> u64 {
            self.count
        }

        #[trace_fn(in = "Counter")]
        pub fn shared(self: Arc<Self>, _unused: ()) -> u64 {
            self.count
        }

        #[trace_fn(in = "Counter")]
        pub fn explicit(self: &Self) -> u64 {
            self.count
        }

        #[trace_fn(in = "Counter")]
        pub fn associated(count: u64) -> Self {
            Self { count }
        }

        #[trace_fn(in = "Counter")]
        pub async fn waits(&mut self, by: u64) -> u64 {
            tokio::task::yield_now().await;
            self.count += by;
            self.count
        }

        #[trace_fn(in = "Counter")]
        pub const fn constant(&self) -> u64 {
            self.count
        }
    }

    unsafe extern "C" {
        #[trace_fn]
        pub fn abs(value: i32) -> i32;
    }

    #[trace_fn]
    pub const fn free_constant(value: u8) -> u8 {
        value
    }

    #[trace_fn]
    pub fn generic<'a, T: Clone, U>(first: &'a T, second: U, opaque: Opaque) -> (T, U)
    where
        U: Debug,
    {
        let Opaque = opaque;
        (first.clone(), second)
    }

    #[trace_fn]
    pub fn impl_argument(items: impl Iterator<Item = u8>, convert: impl Fn(u8) -> u16) -> u16 {
        items.map(convert).sum()
    }

    #[trace_fn]
    pub fn patterns((a, b): (u8, u8), _: u8, mut c: u8, [d, ..]: [u8; 2], whole @ _: u8, ref e: u8) -> u8 {
        c += 1;
        a + b + c + d + whole + *e
    }

    #[trace_fn]
    pub unsafe fn not_safe(pointer: *const u8) -> u8 {
        unsafe { *pointer }
    }

    #[trace_fn]
    pub extern "C" fn foreign_abi(value: i32) -> i32 {
        value + 1
    }

    #[trace_fn]
    pub async fn waits(value: u8, text: &str) -> usize {
        tokio::task::yield_now().await;
        usize::from(value) + text.len()
    }

    #[trace_fn]
    pub async fn calls_others(value: u64) -> u64 {
        let before = inner(value);
        let waited = waits(1, "ab").await;
        tokio::task::yield_now().await;
        before + inner(value) + u64::try_from(waited).unwrap()
    }

    #[trace_fn]
    pub async fn opaque(upto: u8) -> impl Iterator<Item = u8> + Send {
        tokio::task::yield_now().await;
        0..inner(u64::from(upto)) as u8
    }

    #[trace_fn]
    pub async fn coerced(value: Option<u8>) -> Result<Box<dyn Debug + Send>, Box<dyn std::error::Error + Send + Sync>> {
        let text = value.ok_or("nothing")?.to_string();
        if text.is_empty() {
            return Err(std::fmt::Error.into());
        }
        let parsed: u8 = text.parse()?;
        Ok(Box::new(parsed))
    }

    #[trace_fn]
    pub async fn borrows<'a, T: Debug + Sync>(first: &'a T, second: &mut Vec<String>, mut times: u8) -> &'a T {
        while times > 0 {
            tokio::task::yield_now().await;
            second.push(format!("{first:?}"));
            times -= 1;
        }
        first
    }

    #[trace_fn]
    pub async fn not_send(value: u8) -> u8 {
        let shared = std::rc::Rc::new(value);
        tokio::task::yield_now().await;
        *shared
    }

    #[trace_fn]
    pub async fn recursive(depth: u8) -> u8 {
        if depth == 0 { 0 } else { 1 + Box::pin(recursive(depth - 1)).await }
    }

    #[trace_fn]
    pub async fn unit(_ignored: u8) {}

    pub trait Waits {
        #[trace_fn(in = "Waits")]
        fn declared(&self, by: u64) -> impl Future<Output = u64> + Send;

        #[trace_fn(in = "Waits")]
        fn provided(&self, by: u64) -> impl Future<Output = u64> + Send
        where
            Self: Sync,
        {
            async move { self.declared(by).await + 1 }
        }
    }

    impl Waits for Counter {
        #[trace_fn(in = "Counter")]
        async fn declared(&self, by: u64) -> u64 {
            tokio::task::yield_now().await;
            inner(self.count + by)
        }
    }

    pub fn must_be_send<T: Send>(value: T) -> T {
        value
    }

    #[trace_fn]
    pub fn returns_future(value: u8) -> impl Future<Output = u8> + Send {
        async move { value }
    }

    #[trace_fn]
    pub fn never_returns(message: &str) -> ! {
        panic!("{message}")
    }

    #[trace_fn]
    pub fn conditional(
        always: u8,
        #[cfg(test)] in_tests: u8,
        #[cfg(not(test))] elsewhere: (),
        #[cfg(any())] never: u8,
    ) -> u8 {
        #[cfg(test)]
        return always + in_tests;
        #[cfg(not(test))]
        {
            drop(elsewhere);
            always
        }
    }

    #[inline]
    #[trace_fn]
    #[must_use]
    pub fn inlined(r#type: u8) -> u8 {
        r#type
    }

    #[trace_fn]
    pub fn r#match(value: u8) -> u8 {
        value
    }

    #[trace_fn]
    pub fn outer(value: u64) -> u64 {
        #[trace_fn(in = "outer")]
        fn nested(value: u64) -> u64 {
            value * 2
        }
        nested(value) + inner(value)
    }

    #[trace_fn]
    pub fn inner(value: u64) -> u64 {
        value + 1
    }

    #[trace_fn]
    pub fn early_return(value: Option<u8>) -> Result<u8, String> {
        let value = value.ok_or("nothing")?;
        if value == 0 {
            return Err("zero".to_string());
        }
        Ok(value)
    }

    #[trace_fn]
    pub fn opaque_only(opaque: Opaque, long: Vec<u64>) -> usize {
        let Opaque = opaque;
        long.len()
    }
}

use shapes::{Counter, Shape};

static AGENT: LazyLock<Arc<Agent>> = LazyLock::new(|| {
    let agent = Agent::new(json!({ "kind": "test" }), None, false);
    assert!(amaru_freeze::install(agent.clone()));
    agent
});

static ONE_AT_A_TIME: Mutex<()> = Mutex::new(());

/// The process-wide agent with nothing enabled, for one test at a time.
fn debugger() -> (MutexGuard<'static, ()>, &'static Arc<Agent>, Receiver<Arc<str>>) {
    let guard = ONE_AT_A_TIME.lock().unwrap_or_else(|poisoned| poisoned.into_inner());
    calls::disable("all");
    calls::unhold("all");
    AGENT.thaw();
    (guard, &AGENT, AGENT.subscribe())
}

fn next(events: &mut Receiver<Arc<str>>) -> Json {
    amaru_freeze::serde_json::from_str(&events.try_recv().expect("an event")).unwrap()
}

fn site(path: &str) -> Json {
    let list = calls::list();
    list["sites"].as_array().unwrap().iter().find(|site| site["name"] == path).cloned().unwrap_or(Json::Null)
}

#[test]
fn every_shape_runs_as_before_and_reports_nothing_while_off() {
    let (_guard, agent, mut events) = debugger();
    let before = agent.status()["seq"].clone();

    let mut counter = Counter::associated(1);
    counter.by_mut(2);
    assert_eq!(counter.by_ref("n"), "n3");
    assert_eq!((counter.explicit(), counter.constant(), counter.declared(1), counter.provided(1)), (3, 3, 4, 5));
    assert_eq!(Box::new(Counter { count: 4 }).boxed(), 4);
    assert_eq!(Arc::new(Counter { count: 5 }).shared(()), 5);
    assert_eq!(counter.by_value(), 3);
    assert_eq!(unsafe { shapes::abs(-3) }, 3);
    assert_eq!(shapes::free_constant(1), 1);
    assert_eq!(shapes::generic(&1_u8, "two", shapes::Opaque), (1, "two"));
    assert_eq!(shapes::impl_argument([1, 2].into_iter(), u16::from), 3);
    assert_eq!(shapes::patterns((1, 2), 0, 3, [4, 0], 5, 6), 22);
    assert_eq!(unsafe { shapes::not_safe(&7) }, 7);
    assert_eq!(shapes::foreign_abi(1), 2);
    assert_eq!(shapes::conditional(1, 2), 3);
    assert_eq!((shapes::inlined(1), shapes::r#match(2), shapes::outer(3)), (1, 2, 10));
    assert_eq!(shapes::early_return(None), Err("nothing".to_string()));
    assert_eq!(shapes::opaque_only(shapes::Opaque, vec![0; 3]), 3);
    assert!(std::panic::catch_unwind(|| shapes::never_returns("expected by this test")).is_err());
    let runtime = tokio::runtime::Builder::new_current_thread().build().unwrap();
    assert_eq!(runtime.block_on(shapes::waits(1, "ab")), 3);
    assert_eq!(runtime.block_on(shapes::returns_future(9)), 9);
    assert_eq!(runtime.block_on(Counter::default().waits(2)), 2);
    assert_eq!(runtime.block_on(shapes::must_be_send(shapes::calls_others(1))), 7);
    assert_eq!(runtime.block_on(shapes::must_be_send(shapes::opaque(2))).collect::<Vec<_>>(), [0, 1, 2]);
    assert_eq!(format!("{:?}", runtime.block_on(shapes::must_be_send(shapes::coerced(Some(7)))).unwrap()), "7");
    assert!(runtime.block_on(shapes::coerced(None)).is_err());
    let mut seen = Vec::new();
    assert_eq!(runtime.block_on(shapes::must_be_send(shapes::borrows(&5_u8, &mut seen, 2))), &5);
    assert_eq!(seen, ["5", "5"]);
    assert_eq!(runtime.block_on(shapes::not_send(3)), 3);
    assert_eq!(runtime.block_on(shapes::must_be_send(shapes::recursive(3))), 3);
    runtime.block_on(shapes::must_be_send(shapes::unit(0)));
    let counter = Counter { count: 1 };
    assert_eq!(runtime.block_on(shapes::must_be_send(shapes::Waits::declared(&counter, 1))), 3);
    assert_eq!(runtime.block_on(shapes::must_be_send(shapes::Waits::provided(&counter, 1))), 4);

    assert!(events.try_recv().is_err(), "a function that is off reports nothing");
    assert_eq!(agent.status()["seq"], before);
    assert_eq!(
        site("trace_fn::shapes::Counter::by_ref"),
        json!({ "name": "trace_fn::shapes::Counter::by_ref", "hits": 0, "enabled": false, "held": false })
    );
    assert_eq!(site("trace_fn::shapes::Counter::constant"), Json::Null, "a const fn is left alone");
}

#[test]
fn enabled_functions_report_arguments_depth_and_caller() {
    let (_guard, _agent, mut events) = debugger();
    calls::enable("trace_fn::shapes::outer");
    calls::enable("trace_fn::shapes::inner");
    let hits_before = site("trace_fn::shapes::inner")["hits"].as_u64().unwrap_or(0);
    assert_eq!(shapes::outer(3), 10);
    assert_eq!(shapes::inlined(1), 1);

    let outer = next(&mut events)["data"].clone();
    assert_eq!((outer["kind"].clone(), outer["label"].clone()), (json!("call"), json!("outer")));
    assert_eq!(outer["type"], "trace_fn::shapes::outer");
    assert_eq!(outer["payload"], json!({ "args": { "value": "3" }, "depth": 0, "caller": null, "async": false }));

    let nested = next(&mut events)["data"].clone();
    assert_eq!(
        (nested["label"].clone(), nested["type"].clone()),
        (json!("nested"), json!("trace_fn::shapes::outer::nested"))
    );
    assert_eq!(nested["payload"]["caller"], "trace_fn::shapes::outer");
    assert_eq!(next(&mut events)["data"]["seq"], nested["seq"]);

    let inner = next(&mut events)["data"].clone();
    assert_eq!(
        inner["payload"],
        json!({ "args": { "value": "3" }, "depth": 1, "caller": "trace_fn::shapes::outer", "async": false })
    );
    assert_eq!(next(&mut events)["data"]["seq"], inner["seq"]);

    let done = next(&mut events);
    assert_eq!((done["event"].clone(), done["data"]["seq"].clone()), (json!("item.done"), outer["seq"].clone()));
    assert_eq!(done["data"]["outcome"]["unwound"], false);
    assert!(events.try_recv().is_err(), "`inlined` is not enabled");
    assert_eq!(site("trace_fn::shapes::inner")["hits"], hits_before + 1);
}

#[test]
fn arguments_without_debug_and_patterns_are_handled() {
    let (_guard, _agent, mut events) = debugger();
    calls::enable("trace_fn::shapes");

    shapes::generic(&1_u8, "two", shapes::Opaque);
    assert_eq!(
        next(&mut events)["data"]["payload"]["args"],
        json!({ "first": "…", "second": "\"two\"", "opaque": "…" }),
        "`first` is of a generic type that is not bound to be `Debug`"
    );
    next(&mut events);

    shapes::patterns((1, 2), 0, 3, [4, 0], 5, 6);
    assert_eq!(next(&mut events)["data"]["payload"]["args"], json!({ "c": "3", "e": "6" }));
    next(&mut events);

    shapes::conditional(1, 2);
    assert_eq!(next(&mut events)["data"]["payload"]["args"], json!({ "always": "1", "in_tests": "2" }));
    next(&mut events);

    assert_eq!(shapes::inlined(1), 1);
    assert_eq!(next(&mut events)["data"]["payload"]["args"], json!({ "type": "1" }));
    next(&mut events);

    shapes::opaque_only(shapes::Opaque, vec![7; 500]);
    let long = next(&mut events)["data"]["payload"]["args"]["long"].as_str().unwrap().to_string();
    assert_eq!(long.chars().count(), calls::ARGUMENT_LIMIT + 1);
    next(&mut events);

    let mut counter = Counter { count: 1 };
    counter.by_mut(2);
    let method = next(&mut events)["data"].clone();
    assert_eq!(method["type"], "trace_fn::shapes::Counter::by_mut");
    assert_eq!(method["payload"]["args"], json!({ "by": "2" }), "`self` is not reported");
    next(&mut events);

    assert_eq!(counter.provided(1), 5);
    assert_eq!(next(&mut events)["data"]["type"], "trace_fn::shapes::Shape::provided");
    let declared = next(&mut events)["data"].clone();
    assert_eq!(declared["type"], "trace_fn::shapes::Counter::declared");
    assert_eq!(declared["payload"]["caller"], "trace_fn::shapes::Shape::provided");
}

#[test]
fn a_panicking_function_reports_that_it_unwound() {
    let (_guard, _agent, mut events) = debugger();
    calls::enable("trace_fn::shapes::never_returns");
    assert!(std::panic::catch_unwind(|| shapes::never_returns("expected by this test")).is_err());
    assert_eq!(next(&mut events)["data"]["label"], "never_returns");
    assert_eq!(next(&mut events)["data"]["outcome"]["unwound"], true);
}

#[test]
fn a_suspended_async_function_is_not_the_caller_of_what_runs_beside_it() {
    let (_guard, _agent, mut events) = debugger();
    calls::enable("trace_fn::shapes::waits");
    calls::enable("trace_fn::shapes::inner");
    let runtime = tokio::runtime::Builder::new_current_thread().build().unwrap();

    let future = shapes::waits(1, "ab");
    assert!(events.try_recv().is_err(), "nothing runs before the first poll");
    let result = runtime.block_on(async {
        let (result, inner) = tokio::join!(future, async { shapes::inner(1) });
        result + usize::try_from(inner).unwrap()
    });
    assert_eq!(result, 5);

    let start = next(&mut events)["data"].clone();
    assert_eq!(start["label"], "waits");
    assert_eq!(
        start["payload"],
        json!({ "args": { "value": "1", "text": "\"ab\"" }, "depth": 0, "caller": null, "async": true })
    );
    let inner = next(&mut events)["data"].clone();
    assert_eq!(inner["payload"]["caller"], Json::Null, "the suspended async function is not its caller");
    assert_eq!(inner["payload"]["depth"], 0);
    assert_eq!(next(&mut events)["data"]["seq"], inner["seq"]);
    let done = next(&mut events);
    assert_eq!((done["event"].clone(), done["data"]["seq"].clone()), (json!("item.done"), start["seq"].clone()));

    let spawned = tokio::runtime::Builder::new_multi_thread().worker_threads(2).build().unwrap();
    assert_eq!(spawned.block_on(async { tokio::spawn(shapes::waits(2, "c")).await.unwrap() }), 3);
    assert_eq!(next(&mut events)["data"]["payload"]["async"], true);
    assert_eq!(next(&mut events)["event"], "item.done");
}

fn calls_seen(events: &mut Receiver<Arc<str>>) -> Vec<(String, u64, Json)> {
    let mut seen = Vec::new();
    while let Ok(line) = events.try_recv() {
        let event: Json = amaru_freeze::serde_json::from_str(&line).unwrap();
        if event["event"] == "item" {
            let item = &event["data"];
            let label = item["label"].as_str().unwrap().to_string();
            seen.push((label, item["payload"]["depth"].as_u64().unwrap(), item["payload"]["caller"].clone()));
        }
    }
    seen
}

#[test]
fn what_an_async_function_calls_reports_it_as_caller_across_awaits() {
    let (_guard, _agent, mut events) = debugger();
    calls::enable("trace_fn::shapes");
    let runtime = tokio::runtime::Builder::new_current_thread().build().unwrap();
    let outer = json!("trace_fn::shapes::calls_others");

    let (result, beside) =
        runtime.block_on(async { tokio::join!(shapes::calls_others(1), async { shapes::inlined(9) }) });
    assert_eq!((result, beside), (7, 9));
    assert_eq!(
        calls_seen(&mut events),
        [
            ("calls_others".to_string(), 0, Json::Null),
            ("inner".to_string(), 1, outer.clone()),
            ("waits".to_string(), 1, outer.clone()),
            ("inlined".to_string(), 0, Json::Null),
            ("inner".to_string(), 1, outer.clone()),
        ],
        "before and after its awaits, and not what runs beside it"
    );

    let spawned = tokio::runtime::Builder::new_multi_thread().worker_threads(2).build().unwrap();
    assert_eq!(spawned.block_on(async { tokio::spawn(shapes::calls_others(1)).await.unwrap() }), 7);
    assert_eq!(calls_seen(&mut events).last().unwrap(), &("inner".to_string(), 1, outer));

    let counter = Counter { count: 1 };
    assert_eq!(runtime.block_on(shapes::Waits::provided(&counter, 1)), 4);
    assert_eq!(
        calls_seen(&mut events),
        [
            ("provided".to_string(), 0, Json::Null),
            ("declared".to_string(), 0, Json::Null),
            ("inner".to_string(), 1, json!("trace_fn::shapes::Counter::declared")),
        ],
        "a function that returns a future has returned by the time the future runs"
    );

    assert_eq!(runtime.block_on(shapes::opaque(2)).count(), 3);
    assert_eq!(
        calls_seen(&mut events),
        [("opaque".to_string(), 0, Json::Null), ("inner".to_string(), 0, Json::Null)],
        "an async function returning `impl Trait` is not on the stack"
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
fn a_held_function_waits_at_its_start_where_it_can() {
    let (_guard, agent, _events) = debugger();
    assert_eq!(calls::list()["holding"], json!([]));
    calls::hold("trace_fn::shapes::inner");
    calls::hold("trace_fn::shapes::waits");
    calls::hold("trace_fn::shapes::opaque");
    let runtime = tokio::runtime::Builder::new_multi_thread().worker_threads(2).build().unwrap();
    let payload = |seq: u64| agent.item(seq).unwrap()["payload"].clone();

    let plain = std::thread::spawn(|| shapes::inner(1));
    let seq = held_seq(agent);
    assert!(agent.is_frozen() && !plain.is_finished());
    assert_eq!(payload(seq), json!({ "args": { "value": "1" }, "depth": 0, "caller": null, "async": false }));
    agent.resolve(seq, amaru_freeze::Verdict::Forward).unwrap();
    assert_eq!(plain.join().unwrap(), 2);
    agent.thaw();

    for staged in [true, false] {
        let waits = runtime.spawn(amaru_freeze::trace::in_part("trace-fn-stage", async move {
            if staged { shapes::waits(1, "ab").await } else { shapes::opaque(2).await.count() }
        }));
        let seq = held_seq(agent);
        assert!(agent.is_frozen() && !waits.is_finished());
        assert_eq!(agent.item(seq).unwrap()["held"], true);
        assert_eq!(payload(seq)["not_held"], Json::Null);
        agent.resolve(seq, amaru_freeze::Verdict::Forward).unwrap();
        assert_eq!(runtime.block_on(waits).unwrap(), 3);
        agent.thaw();
    }

    let before = agent.status()["seq"].as_u64().unwrap();
    assert_eq!(runtime.block_on(async { (shapes::inner(1), shapes::waits(1, "ab").await) }), (2, 3));
    assert!(!agent.is_frozen(), "neither call could wait where it ran");
    assert_eq!(
        payload(before + 1),
        json!({ "args": { "value": "1" }, "depth": 0, "caller": null, "async": false, "not_held": "would block the runtime" })
    );
    assert_eq!(payload(before + 2)["not_held"], "would block the runtime");
    assert_eq!(site("trace_fn::shapes::inner")["held"], true);
    assert_eq!(calls::unhold("all"), 3);
}
