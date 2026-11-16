//! Which step caused which.
//!
//! A node is many parts handing messages to each other. To say "this rejection happened because
//! of that header", every item carries a `cause`: the sequence number of the item at the root of
//! its chain, usually a message from a peer. Hooks keep two small tables up to date:
//!
//! - per part, the input it is handling and that input's cause;
//! - per message in flight, the step that sent it and that step's cause. A message is known by
//!   the address of its heap allocation, which does not change while it sits in a mailbox.

use std::{collections::BTreeMap, future::Future, sync::LazyLock};

use parking_lot::Mutex;

/// More messages in flight than this means entries were leaked by messages that never arrived.
const IN_FLIGHT_LIMIT: usize = 65_536;

static CONTEXT: LazyLock<Mutex<BTreeMap<String, (u64, u64)>>> = LazyLock::new(Default::default);
static IN_FLIGHT: LazyLock<Mutex<BTreeMap<usize, (u64, u64)>>> = LazyLock::new(Default::default);

tokio::task_local! {
    /// The part whose code the current task is running.
    static PART: String;
}

/// Run `future` as the code of `part`, so that [`current`] can tell what it is handling.
pub fn in_part<F: Future>(part: &str, future: F) -> impl Future<Output = F::Output> + use<F> {
    PART.scope(part.to_string(), future)
}

/// The input the part running on this task is handling and the root of its chain; zeroes when
/// the task belongs to no part or the part is handling nothing known.
pub fn current() -> (u64, u64) {
    PART.try_with(|part| context(part)).unwrap_or_default()
}

/// Whether the current task is running the code of a part, as [`in_part`] arranges.
pub fn in_stage() -> bool {
    PART.try_with(|_| ()).is_ok()
}

/// Record that `part` is now handling input `input`, whose chain is rooted at `cause`.
pub fn set_context(part: &str, input: u64, cause: u64) {
    CONTEXT.lock().insert(part.to_string(), (input, cause));
}

/// The input `part` is handling and the root of its chain; zeroes when unknown.
pub fn context(part: &str) -> (u64, u64) {
    CONTEXT.lock().get(part).copied().unwrap_or_default()
}

/// Forget a part that has stopped.
pub fn forget(part: &str) {
    CONTEXT.lock().remove(part);
}

/// Remember that the message at `address` was sent by step `sender`, in the chain rooted at `cause`.
pub fn tag(address: usize, sender: u64, cause: u64) {
    let mut in_flight = IN_FLIGHT.lock();
    if in_flight.len() >= IN_FLIGHT_LIMIT {
        in_flight.clear();
    }
    in_flight.insert(address, (sender, cause));
}

/// The step that sent the message at `address` and the root of its chain, once.
pub fn claim(address: usize) -> Option<(u64, u64)> {
    IN_FLIGHT.lock().remove(&address)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_message_is_claimed_once_and_parts_remember_their_input() {
        tag(0x1000, 7, 3);
        assert_eq!(claim(0x1000), Some((7, 3)));
        assert_eq!(claim(0x1000), None);

        assert_eq!(context("trace-test"), (0, 0));
        set_context("trace-test", 9, 3);
        assert_eq!(context("trace-test"), (9, 3));
        forget("trace-test");
        assert_eq!(context("trace-test"), (0, 0));
    }
}
