import json
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "dwarf"))

from freeze import cli, scripting  # noqa: E402
from freeze.client import FreezeClient, FreezeError  # noqa: E402
from freeze.conditions import compile_condition  # noqa: E402
from freeze.session import Session  # noqa: E402
from freeze.testing.fake_agent import FakeAgent  # noqa: E402

EXAMPLE = Path(__file__).resolve().parents[1] / "dwarf" / "freeze" / "examples" / "drop_large_fee.py"


@pytest.fixture
def agent():
    fake = FakeAgent()
    yield fake
    fake.stop()


class DirectClient(FreezeClient):
    """FreezeClient surface over FakeAgent without a socket (the sandbox forbids binds)."""

    def __init__(self, agent):
        super().__init__("in-process", 0, timeout=2)
        self.agent = agent

        class Wire:
            def send(wire, line):
                message = json.loads(line)
                self._events.put(message)
                for listener in list(self._listeners):
                    listener(message["event"], message.get("data"))

        self.wire = Wire()
        agent._connections.append(self.wire)
        self.hello = agent.handle("hello", {})

    def call(self, method, **params):
        return self.agent.handle(method, params)

    def close(self):
        if self.wire in self.agent._connections:
            self.agent._connections.remove(self.wire)


class FailingRestoreClient(DirectClient):
    def __init__(self, agent, failure):
        super().__init__(agent)
        self.failure = failure
        self.hold_calls = 0

    def set_hold(self, when):
        self.hold_calls += 1
        if self.failure == "hold" and self.hold_calls == 2:
            raise FreezeError("restore hold failed")
        return super().set_hold(when)

    def thaw(self, keep=None):
        if self.failure == "thaw":
            raise FreezeError("restore thaw failed")
        return super().thaw(keep)


class HoldSnapshotRaceClient(DirectClient):
    def __init__(self, agent):
        super().__init__(agent)
        self.hold_calls = 0

    def set_hold(self, when):
        self.hold_calls += 1
        snapshot = super().set_hold(when)
        if self.hold_calls == 1:
            threading.Thread(
                target=lambda: self.agent.offer({"kind": "internal_input", "label": "raced"}),
                daemon=True,
            ).start()
            wait_until(lambda: bool(self.agent.held))
        return snapshot


class StartupOrderRaceClient(DirectClient):
    def __init__(self, agent):
        super().__init__(agent)
        self.hold_calls = 0

    def set_hold(self, when):
        self.hold_calls += 1
        snapshot = super().set_hold(when)
        if self.hold_calls == 1:
            threading.Thread(
                target=lambda: self.agent.offer(
                    {"kind": "internal_input", "label": "queued-after-set-hold"}
                ),
                daemon=True,
            ).start()
            wait_until(lambda: sorted(self.agent.held) == [1, 2])
        return snapshot


class StaleStartupSnapshotClient(DirectClient):
    def __init__(self, agent):
        super().__init__(agent)
        self.hold_calls = 0

    def set_hold(self, when):
        self.hold_calls += 1
        snapshot = super().set_hold(when)
        if self.hold_calls == 1:
            self.agent._release(1, "forward")
            threading.Thread(
                target=lambda: self.agent.offer(
                    {"kind": "internal_input", "label": "currently-held"}
                ),
                daemon=True,
            ).start()
            wait_until(lambda: list(self.agent.held) == [2])
        return snapshot


class EventsLostRaceClient(DirectClient):
    def __init__(self, agent):
        super().__init__(agent)
        self.hold_calls = 0

    def set_hold(self, when):
        self.hold_calls += 1
        snapshot = super().set_hold(when)
        if self.hold_calls == 1:
            self.agent._connections.remove(self.wire)
            threading.Thread(
                target=lambda: self.agent.offer(
                    {"kind": "internal_input", "label": "missed-held-item"}
                ),
                daemon=True,
            ).start()
            wait_until(lambda: list(self.agent.held) == [1])
            self.agent._connections.append(self.wire)
            self.agent._emit("error", {"code": "events_lost", "missed": 1})
            threading.Thread(
                target=lambda: self.agent.offer(
                    {"kind": "internal_input", "label": "visible-held-item"}
                ),
                daemon=True,
            ).start()
            wait_until(lambda: sorted(self.agent.held) == [1, 2])
        return snapshot


class FailingForwardClient(DirectClient):
    def forward(self, seq, then_run=False):
        raise FreezeError(f"item {seq} was already released", "failed")


class FailingFirstThawClient(DirectClient):
    def __init__(self, agent):
        super().__init__(agent)
        self.thaw_calls = 0

    def thaw(self, keep=None):
        self.thaw_calls += 1
        if self.thaw_calls == 1:
            raise FreezeError("first processing thaw failed")
        return super().thaw(keep)


class FailingCleanupStatusClient(FailingFirstThawClient):
    def __init__(self, agent):
        super().__init__(agent)
        self.status_calls = 0

    def status(self):
        self.status_calls += 1
        if self.status_calls >= 3:
            raise FreezeError("cleanup status failed")
        return super().status()


class FailingCloseClient(DirectClient):
    def __init__(self, agent):
        super().__init__(agent)
        self.close_calls = 0

    def close(self):
        self.close_calls += 1
        raise OSError("client close failed")


def connect(agent):
    return DirectClient(agent)


def connect_socket(agent):
    try:
        port = agent.start()
    except OSError as error:
        pytest.skip(f"loopback socket binding is unavailable: {error}")
    client = FreezeClient("127.0.0.1", port, timeout=2)
    client.connect()
    return client


def wait_until(check, timeout=3):
    deadline = time.monotonic() + timeout
    while not check() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert check()


def run_async(client, callback, session, **kwargs):
    result, errors = [], []

    def target():
        try:
            result.append(scripting.run(client, callback, session, **kwargs))
        except BaseException as error:
            errors.append(error)

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    return thread, result, errors


def hold_two_startup_items(agent):
    agent.frozen = True
    for label in ("first", "second"):
        threading.Thread(
            target=lambda item_label=label: agent.offer(
                {"kind": "internal_input", "label": item_label}
            ),
            daemon=True,
        ).start()
        wait_until(lambda: len(agent.held) == (1 if label == "first" else 2))


def test_loads_only_the_named_python_file(tmp_path):
    script = tmp_path / "decision.py"
    script.write_text("MARKER = 'loaded'\ndef on_item(item):\n    return MARKER, item['seq']\n", encoding="utf-8")

    callback = scripting.load_callback(script)

    assert callback({"seq": 7}) == ("loaded", 7)


@pytest.mark.parametrize(
    ("name", "source", "message"),
    [
        ("decision.txt", "def on_item(item): return None\n", "must name a local .py file"),
        ("decision.py", "VALUE = 1\n", "must define callable `on_item`"),
        ("decision.py", "on_item = 3\n", "must define callable `on_item`"),
    ],
)
def test_rejects_invalid_script_files(tmp_path, name, source, message):
    script = tmp_path / name
    script.write_text(source, encoding="utf-8")

    with pytest.raises(scripting.ScriptError, match=message):
        scripting.load_callback(script)


def test_missing_script_is_reported_as_a_script_error(tmp_path):
    with pytest.raises(scripting.ScriptError, match="does not exist"):
        scripting.load_callback(tmp_path / "missing.py")


def test_script_loading_error_is_reported(tmp_path):
    script = tmp_path / "broken.py"
    script.write_text("raise RuntimeError('load exploded')\n", encoding="utf-8")

    with pytest.raises(scripting.ScriptError) as failed:
        scripting.load_callback(script)

    assert str(failed.value) == f"failed while loading {script}: RuntimeError: load exploded"


def test_shipped_example_is_loadable_and_drops_only_large_fees():
    callback = scripting.load_callback(EXAMPLE)

    assert callback({"droppable": True, "payload": {"fee": 2_000_000}}) == "drop"
    assert callback({"droppable": True, "payload": {"fee": 5}}) is None
    assert callback({"droppable": False, "payload": {"fee": 2_000_000}}) is None


@pytest.mark.parametrize(
    ("result", "action", "value"),
    [
        (None, "forward", None),
        ("forward", "forward", None),
        ("drop", "drop", None),
        ("hold", "hold", None),
        ({"action": "replace", "payload": {"fee": 1}}, "replace", {"fee": 1}),
        ({"action": "replace_raw", "raw": "00fF"}, "replace_raw", b"\x00\xff"),
    ],
)
def test_validates_every_supported_decision(result, action, value):
    item = {"seq": 9, "editable": True, "droppable": True}

    assert scripting.validate_decision(result, item) == scripting.Decision(action, value)


@pytest.mark.parametrize(
    ("result", "message"),
    [
        ("bogus", "unsupported decision 'bogus'"),
        (3, "unsupported decision result of type int"),
        ({}, "decision mapping requires an `action`"),
        ({"action": "forward"}, "unsupported mapping action 'forward'"),
        ({"action": "replace"}, "replace decision must contain exactly `action` and `payload`"),
        ({"action": "replace", "payload": 1, "extra": 2}, "replace decision must contain exactly `action` and `payload`"),
        ({"action": "replace_raw"}, "replace_raw decision must contain exactly `action` and `raw`"),
        ({"action": "replace_raw", "raw": 12}, "replace_raw `raw` must be an even-length hexadecimal string"),
        ({"action": "replace_raw", "raw": "0"}, "replace_raw `raw` must be an even-length hexadecimal string"),
        ({"action": "replace_raw", "raw": "no"}, "replace_raw `raw` must be an even-length hexadecimal string"),
    ],
)
def test_rejects_all_other_decision_results_precisely(result, message):
    with pytest.raises(scripting.ScriptError) as rejected:
        scripting.validate_decision(result, {"seq": 9, "editable": True, "droppable": True})

    assert str(rejected.value) == message


def test_rejects_actions_the_item_does_not_allow():
    with pytest.raises(scripting.ScriptError, match="item 9 is not droppable"):
        scripting.validate_decision("drop", {"seq": 9, "editable": True, "droppable": False})
    with pytest.raises(scripting.ScriptError, match="item 9 is not editable"):
        scripting.validate_decision(
            {"action": "replace", "payload": None},
            {"seq": 9, "editable": False, "droppable": True},
        )
    with pytest.raises(scripting.ScriptError, match="item 9 is not editable"):
        scripting.validate_decision(
            {"action": "replace_raw", "raw": "00"},
            {"seq": 9, "editable": False, "droppable": True},
        )


def test_rejects_a_non_json_replace_payload():
    with pytest.raises(scripting.ScriptError, match="replace payload violates protocol section 4"):
        scripting.validate_decision(
            {"action": "replace", "payload": object()},
            {"seq": 9, "editable": True, "droppable": True},
        )


@pytest.mark.parametrize(
    "payload",
    [
        None,
        False,
        True,
        "text",
        -9_007_199_254_740_991,
        9_007_199_254_740_991,
        -1.5,
        [None, {"nested": [1, {"$bytes": "00ff"}]}],
        {"ordinary": {"map": "value"}},
        {"$int": "9007199254740992"},
        {"$int": "-9007199254740992"},
        {"$float": "nan"},
        {"$float": "inf"},
        {"$float": "-inf"},
        {"$bytes": ""},
        {"$bytes": "00ff"},
        {"$map": [[1, "integer-key"], [{"$bytes": "aa"}, {"nested": True}]]},
        {"$map": [["duplicate", 1], ["duplicate", 2]]},
        {"$tag": 24, "value": {"$bytes": "a100"}},
    ],
)
def test_accepts_protocol_section_4_replacement_payloads(payload):
    decision = scripting.validate_decision(
        {"action": "replace", "payload": payload},
        {"seq": 9, "editable": True, "droppable": True},
    )

    assert decision == scripting.Decision("replace", payload)


@pytest.mark.parametrize(
    ("payload", "problem"),
    [
        (9_007_199_254_740_992, "unsafe bare integer"),
        (-9_007_199_254_740_992, "unsafe bare integer"),
        ({1: "integer-key"}, "object key 1 is not text"),
        ({"$evil": "reserved"}, "reserved key '$evil'"),
        ({"nested": [{"$evil": True}]}, "reserved key '$evil'"),
        ({"$int": 9_007_199_254_740_992}, "`$int` must be a canonical decimal string"),
        ({"$int": "1"}, "`$int` must encode an unsafe integer"),
        ({"$int": "+9007199254740992"}, "`$int` must be a canonical decimal string"),
        ({"$int": "09007199254740992"}, "`$int` must be a canonical decimal string"),
        ({"$int": "9007199254740992", "extra": 1}, "malformed `$int` form"),
        ({"$bytes": 12}, "`$bytes` must be lowercase even-length hexadecimal text"),
        ({"$bytes": "0"}, "`$bytes` must be lowercase even-length hexadecimal text"),
        ({"$bytes": "AA"}, "`$bytes` must be lowercase even-length hexadecimal text"),
        ({"$bytes": "00", "extra": 1}, "malformed `$bytes` form"),
        ({"$float": "infinity"}, "`$float` must be 'nan', 'inf', or '-inf'"),
        ({"$map": "pairs"}, "`$map` must contain an array of pairs"),
        ({"$map": [[1]]}, "`$map` entry 0 must be a two-element array"),
        ({"$map": [[1, {"$bytes": "GG"}]]}, "lowercase even-length hexadecimal"),
        ({"$map": [["ordinary", 1]]}, "must use an ordinary object"),
        ({"$map": [[1, 2]], "extra": 1}, "malformed `$map` form"),
        ({"$tag": 24}, "malformed `$tag` form"),
        ({"$tag": -1, "value": None}, "`$tag` must be a non-negative JSON-safe integer"),
        ({"$tag": True, "value": None}, "`$tag` must be a non-negative JSON-safe integer"),
        ({"$tag": 9_007_199_254_740_992, "value": None}, "non-negative JSON-safe integer"),
        ({"$tag": 24, "value": {"$int": "1"}}, "`$int` must encode an unsafe integer"),
        ({"$unserializable": "reason"}, "reserved key '$unserializable'"),
        (float("nan"), "non-finite float must use `$float`"),
        ((1, 2), "unsupported value type tuple"),
    ],
)
def test_rejects_payloads_outside_protocol_section_4(payload, problem):
    with pytest.raises(scripting.ScriptError) as rejected:
        scripting.validate_decision(
            {"action": "replace", "payload": payload},
            {"seq": 9, "editable": True, "droppable": True},
        )

    assert "replace payload violates protocol section 4" in str(rejected.value)
    assert problem in str(rejected.value)


def test_rejects_objects_without_invoking_their_equality_code():
    class HostileEquality:
        def __eq__(self, _other):
            raise RuntimeError("equality code ran")

    item = {"seq": 9, "editable": True, "droppable": True}
    with pytest.raises(scripting.ScriptError) as rejected_result:
        scripting.validate_decision(HostileEquality(), item)
    with pytest.raises(scripting.ScriptError) as rejected_action:
        scripting.validate_decision({"action": HostileEquality()}, item)

    assert str(rejected_result.value) == "unsupported decision result of type HostileEquality"
    assert str(rejected_action.value) == "decision mapping `action` must be a string"


def test_fake_agent_runner_applies_every_decision_and_stops_held(agent):
    decisions = iter(
        [
            None,
            "drop",
            {"action": "replace", "payload": {"fee": 2}},
            {"action": "replace_raw", "raw": "00ff"},
            "hold",
        ]
    )
    client, session = connect(agent), Session()
    thread, result, errors = run_async(client, lambda _item: next(decisions), session)
    wait_until(lambda: agent.hold == {"op": "true"})

    assert agent.offer({"kind": "internal_input"})[1:] == ("forward", None)
    assert agent.offer({"kind": "internal_input", "droppable": True})[1:] == ("drop", None)
    assert agent.offer({"kind": "internal_input", "editable": True})[1:] == ("replace", {"fee": 2})
    assert agent.offer({"kind": "wire_in", "editable": True})[1:] == ("replace", {"$raw": "00ff"})
    held_result = []
    threading.Thread(
        target=lambda: held_result.append(agent.offer({"kind": "internal_input"})), daemon=True
    ).start()

    thread.join(timeout=3)
    assert not thread.is_alive() and not errors
    assert result[0].actions == {"forward": 1, "drop": 1, "replace": 1, "replace_raw": 1, "hold": 1}
    assert result[0].held_seq == 5 and result[0].kept_frozen_for_safety is True
    assert agent.hold is None, "the old hold predicate is restored"
    assert list(agent.held) == [5] and not held_result, "hold never releases the current item"
    client.forward(5)
    client.thaw()
    client.close()


def test_failed_release_is_not_counted_as_an_applied_action(agent):
    client, session = FailingForwardClient(agent), Session()
    summary = scripting.ScriptSummary()
    producer = threading.Thread(
        target=lambda: (
            wait_until(lambda: agent.hold == {"op": "true"}),
            agent.offer({"kind": "internal_input"}),
        ),
        daemon=True,
    )
    producer.start()

    with pytest.raises(FreezeError, match="item 1 was already released"):
        scripting.run(client, lambda _item: "forward", session, summary=summary)

    assert summary.actions["forward"] == 0
    assert summary.held_seq == 1 and list(agent.held) == [1]
    agent._release(1, "forward")
    client.close()


def test_only_filter_forwards_without_calling_the_callback(agent):
    seen = []
    client, session = connect(agent), Session()
    thread, result, errors = run_async(
        client,
        lambda item: seen.append(item["label"]) or "hold",
        session,
        only=compile_condition("label=Target"),
    )
    wait_until(lambda: agent.hold == {"op": "true"})

    assert agent.offer({"kind": "internal_input", "label": "Other"})[1] == "forward"
    threading.Thread(target=lambda: agent.offer({"kind": "internal_input", "label": "Target"}), daemon=True).start()

    thread.join(timeout=3)
    assert not errors and seen == ["Target"]
    assert result[0].filtered == 1
    client.forward(2)
    client.thaw()
    client.close()


def test_existing_held_item_is_processed(agent):
    agent.frozen = True
    offered = []
    threading.Thread(target=lambda: offered.append(agent.offer({"kind": "internal_input", "label": "waiting"})), daemon=True).start()
    wait_until(lambda: bool(agent.held))
    client, session = connect(agent), Session()

    summary = scripting.run(client, lambda _item: "hold", session)

    assert summary.held_seq == 1 and session.items[1]["label"] == "waiting"
    assert agent.hold is None and agent.frozen is True and not offered
    client.forward(1)
    client.close()


def test_non_holdable_items_are_recorded_but_not_intercepted(agent):
    seen = []
    client, session = connect(agent), Session()
    thread, _result, errors = run_async(client, lambda item: seen.append(item["kind"]) or "hold", session)
    wait_until(lambda: agent.hold == {"op": "true"})

    assert agent.offer({"kind": "internal_state", "label": "state"}, holdable=False)[1] == "forward"
    threading.Thread(target=lambda: agent.offer({"kind": "internal_input"}), daemon=True).start()

    thread.join(timeout=3)
    wait_until(lambda: 1 in session.items)
    assert not errors and seen == ["internal_input"]
    assert session.items[1]["kind"] == "internal_state"
    client.forward(2)
    client.thaw()
    client.close()


def test_events_queued_before_runner_registration_are_recorded(agent):
    client, session = connect(agent), Session()
    assert agent.offer({"kind": "internal_state", "label": "early"}, holdable=False)[1] == "forward"
    thread, _result, errors = run_async(client, lambda _item: "hold", session)
    wait_until(lambda: agent.hold == {"op": "true"})
    threading.Thread(target=lambda: agent.offer({"kind": "internal_input"}), daemon=True).start()

    thread.join(timeout=3)
    assert not errors
    assert session.items[1]["label"] == "early"
    assert list(session.items) == [1, 2]
    client.forward(2)
    client.thaw()
    client.close()


def test_pre_run_backlog_is_recorded_in_protocol_order(agent):
    client, session = connect(agent), Session()
    assert agent.offer({"kind": "internal_state", "label": "first"}, holdable=False)[1] == "forward"
    agent.frozen = True
    threading.Thread(
        target=lambda: agent.offer({"kind": "internal_input", "label": "second"}), daemon=True
    ).start()
    wait_until(lambda: bool(agent.held))

    summary = scripting.run(client, lambda _item: "hold", session)

    assert list(session.items) == [1, 2]
    assert summary.held_seq == 2
    client.forward(2)
    client.close()


def test_item_held_after_hold_snapshot_is_not_lost_from_drained_queue(agent):
    client, session = HoldSnapshotRaceClient(agent), Session()
    seen = []
    thread, result, errors = run_async(
        client,
        lambda item: seen.append(item["label"]) or "hold",
        session,
    )

    deadline = time.monotonic() + 0.25
    while not seen and time.monotonic() < deadline:
        time.sleep(0.01)
    if not seen:
        client._events.put({"event": "closed", "data": {}})
        thread.join(timeout=1)
        agent._release(1, "forward")
    assert seen == ["raced"]
    thread.join(timeout=1)
    assert not errors and result[0].held_seq == 1
    agent._release(1, "forward")
    client.close()


def test_startup_candidates_are_processed_in_sequence_order(agent):
    agent.frozen = True
    threading.Thread(
        target=lambda: agent.offer({"kind": "internal_input", "label": "held-before-connect"}),
        daemon=True,
    ).start()
    wait_until(lambda: list(agent.held) == [1])
    client, session = StartupOrderRaceClient(agent), Session()
    seen = []

    summary = scripting.run(
        client,
        lambda item: seen.append(item["seq"]) or ("forward" if len(seen) == 1 else "hold"),
        session,
    )

    assert seen == [1, 2]
    assert summary.held_seq == 2
    client.forward(2)
    client.close()


def test_first_thaw_failure_resynchronizes_second_startup_held_item(agent):
    hold_two_startup_items(agent)
    client, session = FailingFirstThawClient(agent), Session()
    summary = scripting.ScriptSummary()

    with pytest.raises(FreezeError, match="first processing thaw failed"):
        scripting.run(client, lambda _item: "forward", session, summary=summary)

    assert summary.actions["forward"] == 1
    assert summary.held_seq == 2
    assert agent.frozen is True and list(agent.held) == [2]
    client.forward(2)
    client.close()


def test_synchronize_held_replaces_a_stale_lower_sequence(agent):
    hold_two_startup_items(agent)
    agent._release(1, "forward")
    client, session = connect(agent), Session()
    summary = scripting.ScriptSummary(held_seq=1)

    assert scripting._synchronize_held(client, session, summary) == [2]
    assert summary.held_seq == 2

    client.forward(2)
    client.close()


def test_synchronize_held_clears_a_sequence_absent_from_authoritative_status(agent):
    client, session = connect(agent), Session()
    summary = scripting.ScriptSummary(held_seq=1)

    assert scripting._synchronize_held(client, session, summary) == []
    assert summary.held_seq is None

    client.close()


def test_first_thaw_and_cleanup_status_fail_closed_without_masking_thaw(agent):
    hold_two_startup_items(agent)
    client, session = FailingCleanupStatusClient(agent), Session()
    summary = scripting.ScriptSummary()

    with pytest.raises(FreezeError, match="first processing thaw failed"):
        scripting.run(client, lambda _item: "forward", session, summary=summary)

    assert summary.held_seq is None
    assert summary.kept_frozen_for_safety is True
    assert agent.frozen is True and list(agent.held) == [2]
    client.forward(2)
    client.close()


def test_startup_reconciliation_drops_items_absent_from_synchronized_status(agent):
    agent.frozen = True
    threading.Thread(
        target=lambda: agent.offer({"kind": "internal_input", "label": "stale-snapshot"}),
        daemon=True,
    ).start()
    wait_until(lambda: list(agent.held) == [1])
    client, session = StaleStartupSnapshotClient(agent), Session()
    seen = []

    summary = scripting.run(
        client,
        lambda item: seen.append(item["seq"]) or "hold",
        session,
    )

    assert seen == [2]
    assert summary.held_seq == 2
    client.forward(2)
    client.close()


def test_events_lost_resynchronizes_actual_held_item_and_fails_closed(agent):
    client, session = EventsLostRaceClient(agent), Session()
    summary = scripting.ScriptSummary()

    with pytest.raises(scripting.ScriptError) as failed:
        scripting.run(client, lambda _item: pytest.fail("callback must not run"), session, summary=summary)

    assert str(failed.value) == (
        "agent reported `events_lost` (missed 1 event); "
        "resynchronized held item 1; event stream is incomplete"
    )
    assert summary.held_seq == 1
    assert session.items[1]["label"] == "missed-held-item"
    assert agent.frozen is True and sorted(agent.held) == [1, 2]
    client.forward(1)
    client.forward(2)
    client.close()


def test_real_socket_runs_full_forward_and_hold_flow(agent):
    client, session = connect_socket(agent), Session()
    decisions = iter(["forward", "hold"])
    thread, result, errors = run_async(client, lambda _item: next(decisions), session)
    wait_until(lambda: agent.hold == {"op": "true"})

    assert agent.offer({"kind": "internal_input", "label": "over-socket"})[1] == "forward"
    threading.Thread(
        target=lambda: agent.offer({"kind": "internal_input", "label": "stop-over-socket"}),
        daemon=True,
    ).start()

    thread.join(timeout=3)
    assert not thread.is_alive() and not errors
    assert result[0].actions["forward"] == 1
    assert result[0].actions["hold"] == 1 and result[0].held_seq == 2
    assert session.items[1]["label"] == "over-socket"
    client.forward(2)
    client.close()


def test_real_socket_events_lost_resynchronizes_missed_held_item(agent):
    client, session = connect_socket(agent), Session()
    summary = scripting.ScriptSummary()
    thread, _result, errors = run_async(
        client,
        lambda _item: pytest.fail("callback must not run after events_lost"),
        session,
        summary=summary,
    )
    wait_until(lambda: agent.hold == {"op": "true"})

    with agent._lock:
        connections = list(agent._connections)
        agent._connections.clear()
    threading.Thread(
        target=lambda: agent.offer({"kind": "internal_input", "label": "missed-over-socket"}),
        daemon=True,
    ).start()
    wait_until(lambda: list(agent.held) == [1])
    with agent._lock:
        agent._connections.extend(connections)
    agent._emit("error", {"code": "events_lost", "missed": 1})

    thread.join(timeout=3)
    assert not thread.is_alive()
    assert len(errors) == 1 and isinstance(errors[0], scripting.ScriptError)
    assert str(errors[0]) == (
        "agent reported `events_lost` (missed 1 event); "
        "resynchronized held item 1; event stream is incomplete"
    )
    assert summary.held_seq == 1 and session.items[1]["label"] == "missed-over-socket"
    assert agent.frozen is True and list(agent.held) == [1]
    client.forward(1)
    client.close()


def test_real_socket_connection_close_stops_waiting_runner(agent):
    client, session = connect_socket(agent), Session()
    thread, _result, errors = run_async(client, lambda _item: "forward", session)
    wait_until(lambda: agent.hold == {"op": "true"})

    client.close()

    thread.join(timeout=3)
    assert not thread.is_alive()
    assert len(errors) == 1 and isinstance(errors[0], FreezeError)
    assert errors[0].code == "closed" and str(errors[0]) == "connection closed"


def test_recording_failure_does_not_mask_ctrl_c_or_skip_restoration(agent):
    class FailingSession(Session):
        def record(self, event, data):
            if event == "log":
                raise OSError("session disk full")
            return super().record(event, data)

    client, session = connect(agent), FailingSession()
    summary = scripting.ScriptSummary()

    def interrupt(_item):
        agent._emit("log", {"message": "queued before Ctrl-C"})
        raise KeyboardInterrupt

    thread, _result, errors = run_async(client, interrupt, session, summary=summary)
    wait_until(lambda: agent.hold == {"op": "true"})
    threading.Thread(target=lambda: agent.offer({"kind": "internal_input"}), daemon=True).start()

    thread.join(timeout=3)
    assert len(errors) == 1 and isinstance(errors[0], KeyboardInterrupt)
    assert agent.hold is None and list(agent.held) == [1]
    assert summary.record_errors == ["could not record `log` event: session disk full"]
    client.forward(1)
    client.thaw()
    client.close()


def test_events_lost_during_cleanup_does_not_mask_ctrl_c(agent):
    client, session = connect(agent), Session()
    summary = scripting.ScriptSummary()

    def interrupt(_item):
        agent._emit("error", {"code": "events_lost", "missed": 2})
        raise KeyboardInterrupt

    thread, _result, errors = run_async(client, interrupt, session, summary=summary)
    wait_until(lambda: agent.hold == {"op": "true"})
    threading.Thread(target=lambda: agent.offer({"kind": "internal_input"}), daemon=True).start()

    thread.join(timeout=3)
    assert len(errors) == 1 and isinstance(errors[0], KeyboardInterrupt)
    assert summary.held_seq == 1
    assert agent.hold is None and agent.frozen is True and list(agent.held) == [1]
    client.forward(1)
    client.close()


def test_recording_failure_on_intercepted_item_retains_held_item(agent):
    class FailingSession(Session):
        def record(self, event, data):
            if event == "item" and data.get("held"):
                raise OSError("session disk full")
            return super().record(event, data)

    client, session = connect(agent), FailingSession()
    summary = scripting.ScriptSummary()
    producer = threading.Thread(
        target=lambda: (
            wait_until(lambda: agent.hold == {"op": "true"}),
            agent.offer({"kind": "internal_input"}),
        ),
        daemon=True,
    )
    producer.start()

    with pytest.raises(scripting.ScriptError) as failed:
        scripting.run(client, lambda _item: pytest.fail("callback must not run"), session, summary=summary)

    problem = "could not record `item` event for held item 1: session disk full"
    assert str(failed.value) == problem
    assert summary.record_errors == [problem]
    assert summary.held_seq == 1 and summary.kept_frozen_for_safety is True
    assert agent.frozen is True and list(agent.held) == [1]
    client.forward(1)
    client.close()


def test_callback_error_leaves_the_current_item_held(agent):
    def fail(_item):
        raise RuntimeError("callback exploded")

    client, session = connect(agent), Session()
    summary = scripting.ScriptSummary()
    thread, _result, errors = run_async(client, fail, session, summary=summary)
    wait_until(lambda: agent.hold == {"op": "true"})
    threading.Thread(target=lambda: agent.offer({"kind": "internal_input"}), daemon=True).start()

    thread.join(timeout=3)
    assert len(errors) == 1 and isinstance(errors[0], scripting.ScriptError)
    assert str(errors[0]) == "on_item failed for item 1: RuntimeError: callback exploded"
    assert list(agent.held) == [1] and agent.hold is None
    assert summary.held_seq == 1 and summary.kept_frozen_for_safety is True
    client.forward(1)
    client.thaw()
    client.close()


def test_ctrl_c_leaves_the_current_item_held(agent):
    agent.frozen = True
    threading.Thread(target=lambda: agent.offer({"kind": "internal_input"}), daemon=True).start()
    wait_until(lambda: bool(agent.held))
    client, session = connect(agent), Session()

    with pytest.raises(KeyboardInterrupt):
        scripting.run(client, lambda _item: (_ for _ in ()).throw(KeyboardInterrupt()), session)

    assert list(agent.held) == [1] and agent.hold is None
    client.forward(1)
    client.close()


def test_old_matching_hold_predicate_is_restored_without_releasing_item(agent):
    old_hold = compile_condition("label=Keep")
    agent.hold = old_hold
    client, session = connect(agent), Session()
    thread, result, errors = run_async(client, lambda _item: "hold", session)
    wait_until(lambda: agent.hold == {"op": "true"})
    threading.Thread(
        target=lambda: agent.offer({"kind": "internal_input", "label": "Keep"}), daemon=True
    ).start()

    thread.join(timeout=3)
    assert not errors and result[0].running_restored is True
    assert result[0].kept_frozen_for_safety is False
    assert agent.hold == old_hold and list(agent.held) == [1]
    agent._release(1, "forward")
    client.close()


@pytest.mark.parametrize(
    ("failure", "message"),
    [
        ("hold", "could not restore hold predicate: restore hold failed"),
        ("thaw", "could not restore running state: restore thaw failed"),
    ],
)
def test_restoration_failure_is_reported_and_remains_safely_frozen(agent, failure, message):
    old_hold = compile_condition("label=Keep")
    agent.hold = old_hold
    client, session = FailingRestoreClient(agent, failure), Session()
    summary = scripting.ScriptSummary()
    thread, result, errors = run_async(
        client, lambda _item: "hold", session, summary=summary
    )
    wait_until(lambda: agent.hold == {"op": "true"})
    threading.Thread(
        target=lambda: agent.offer({"kind": "internal_input", "label": "Keep"}), daemon=True
    ).start()

    thread.join(timeout=3)
    assert not result
    assert len(errors) == 1 and isinstance(errors[0], scripting.ScriptError)
    assert str(errors[0]) == message
    assert summary.restore_errors == [message]
    assert summary.kept_frozen_for_safety is True
    assert agent.frozen is True and list(agent.held) == [1]
    agent._release(1, "forward")
    client.close()


def test_script_parser_has_connection_filter_and_recording_options():
    args = cli.build_parser().parse_args(
        [
            "script",
            "decide.py",
            "--host",
            "node.test",
            "--port",
            "7474",
            "--token",
            "secret",
            "--timeout",
            "4",
            "--network",
            "preview",
            "--only",
            "kind=input",
            "--no-record",
        ]
    )

    assert args.run is cli.cmd_script
    assert (args.script, args.host, args.port, args.token, args.timeout, args.network) == (
        "decide.py", "node.test", 7474, "secret", 4, "preview"
    )
    assert args.only == "kind=input" and args.no_record is True


def test_script_help_prominently_warns_about_code_and_non_holdable_items(capsys):
    with pytest.raises(SystemExit) as stopped:
        cli.build_parser().parse_args(["script", "--help"])

    assert stopped.value.code == 0
    help_text = capsys.readouterr().out
    assert "WARNING" in help_text and "NOT SANDBOXED" in help_text
    assert "internal_state" in help_text and "cannot be intercepted" in help_text


def held_before_cli(agent, item=None):
    agent.frozen = True
    threading.Thread(
        target=lambda: agent.offer(item or {"kind": "internal_input"}), daemon=True
    ).start()
    wait_until(lambda: bool(agent.held))


def test_cli_runs_script_without_recording_and_prints_summary(tmp_path, capsys, monkeypatch, agent):
    script = tmp_path / "decide.py"
    script.write_text("def on_item(item):\n    return 'hold'\n", encoding="utf-8")
    held_before_cli(agent)
    client = connect(agent)
    monkeypatch.setattr(cli, "_connect", lambda _args: client)

    assert cli.main(["script", str(script), "--no-record"]) == 0

    output = capsys.readouterr()
    assert "WARNING" in output.err and "NOT SANDBOXED" in output.err
    assert "actions: forward=0 drop=0 replace=0 replace_raw=0 hold=1" in output.out
    assert "session: not recorded" in output.out
    assert list(agent.held) == [1]
    agent._release(1, "forward")


def test_cli_records_protocol_events_and_reports_the_session(tmp_path, capsys, monkeypatch, agent):
    script = tmp_path / "decide.py"
    script.write_text("def on_item(item):\n    return 'hold'\n", encoding="utf-8")
    held_before_cli(agent, {"kind": "internal_input", "label": "existing"})
    client = connect(agent)
    monkeypatch.setattr(cli, "_connect", lambda _args: client)
    monkeypatch.setenv("DWARF_DATA_ROOT", str(tmp_path / "data"))

    assert cli.main(["script", str(script)]) == 0

    output = capsys.readouterr().out
    assert "session recorded in" in output
    event_files = list((tmp_path / "data" / "freeze" / "sessions").glob("*/events.jsonl"))
    assert len(event_files) == 1 and '"event":"item"' in event_files[0].read_text(encoding="utf-8")
    agent._release(1, "forward")


def test_cli_reports_callback_error_and_keeps_item_held(tmp_path, capsys, monkeypatch, agent):
    script = tmp_path / "broken.py"
    script.write_text("def on_item(item):\n    raise ValueError('bad decision')\n", encoding="utf-8")
    held_before_cli(agent)
    monkeypatch.setattr(cli, "_connect", lambda _args: connect(agent))

    assert cli.main(["script", str(script), "--no-record"]) == 1

    output = capsys.readouterr()
    assert "on_item failed for item 1: ValueError: bad decision" in output.err
    assert "left frozen" in output.err and list(agent.held) == [1]
    agent._release(1, "forward")


def test_cli_reports_intercepted_item_recording_failure_and_safety_warning(
    tmp_path, capsys, monkeypatch, agent
):
    class FailingSession(Session):
        def record(self, event, data):
            if event == "item" and data.get("held"):
                raise OSError("session disk full")
            return super().record(event, data)

    script = tmp_path / "decide.py"
    script.write_text("def on_item(item):\n    return None\n", encoding="utf-8")
    client, session = connect(agent), FailingSession()
    monkeypatch.setattr(cli, "_connect", lambda _args: client)
    monkeypatch.setattr(cli, "_recorded_session", lambda *_args, **_kwargs: (session, None))
    producer = threading.Thread(
        target=lambda: (
            wait_until(lambda: agent.hold == {"op": "true"}),
            agent.offer({"kind": "internal_input"}),
        ),
        daemon=True,
    )
    producer.start()

    assert cli.main(["script", str(script)]) == 1

    output = capsys.readouterr()
    problem = "could not record `item` event for held item 1: session disk full"
    assert problem in output.err
    assert "safety: item 1 remains held; node left frozen" in output.err
    assert list(agent.held) == [1]
    agent._release(1, "forward")


def test_cli_first_thaw_failure_warns_about_second_startup_held_item(
    tmp_path, capsys, monkeypatch, agent
):
    script = tmp_path / "decide.py"
    script.write_text("def on_item(item):\n    return 'forward'\n", encoding="utf-8")
    hold_two_startup_items(agent)
    client = FailingFirstThawClient(agent)
    monkeypatch.setattr(cli, "_connect", lambda _args: client)

    assert cli.main(["script", str(script), "--no-record"]) == 1

    output = capsys.readouterr()
    assert "first processing thaw failed" in output.err
    assert "safety: item 2 remains held; node left frozen" in output.err
    assert agent.frozen is True and list(agent.held) == [2]
    client.forward(2)


def test_cli_cleanup_status_failure_prints_generic_safety_warning(
    tmp_path, capsys, monkeypatch, agent
):
    script = tmp_path / "decide.py"
    script.write_text("def on_item(item):\n    return 'forward'\n", encoding="utf-8")
    hold_two_startup_items(agent)
    client = FailingCleanupStatusClient(agent)
    monkeypatch.setattr(cli, "_connect", lambda _args: client)

    assert cli.main(["script", str(script), "--no-record"]) == 1

    output = capsys.readouterr()
    assert "first processing thaw failed" in output.err
    assert "safety: node left frozen; held-item status could not be confirmed" in output.err
    assert agent.frozen is True and list(agent.held) == [2]
    client.forward(2)


def test_cli_events_lost_reports_resynchronized_held_item(
    tmp_path, capsys, monkeypatch, agent
):
    script = tmp_path / "decide.py"
    script.write_text(
        "def on_item(item):\n    raise AssertionError('callback must not run')\n",
        encoding="utf-8",
    )
    client = EventsLostRaceClient(agent)
    monkeypatch.setattr(cli, "_connect", lambda _args: client)

    assert cli.main(["script", str(script), "--no-record"]) == 1

    output = capsys.readouterr()
    assert (
        "agent reported `events_lost` (missed 1 event); "
        "resynchronized held item 1; event stream is incomplete"
    ) in output.err
    assert "safety: item 1 remains held; node left frozen" in output.err
    assert agent.frozen is True and sorted(agent.held) == [1, 2]
    agent._release(1, "forward")
    agent._release(2, "forward")


@pytest.mark.parametrize(
    ("failure", "message"),
    [
        ("hold", "could not restore hold predicate: restore hold failed"),
        ("thaw", "could not restore running state: restore thaw failed"),
    ],
)
def test_cli_restoration_failure_is_nonzero(
    tmp_path, capsys, monkeypatch, agent, failure, message
):
    script = tmp_path / "decide.py"
    script.write_text("def on_item(item):\n    return 'hold'\n", encoding="utf-8")
    agent.hold = compile_condition("label=Keep")
    client = FailingRestoreClient(agent, failure)
    monkeypatch.setattr(cli, "_connect", lambda _args: client)
    producer = threading.Thread(
        target=lambda: (
            wait_until(lambda: agent.hold == {"op": "true"}),
            agent.offer({"kind": "internal_input", "label": "Keep"}),
        ),
        daemon=True,
    )
    producer.start()

    assert cli.main(["script", str(script), "--no-record"]) == 1

    output = capsys.readouterr()
    assert message in output.err
    assert "safety: item 1 remains held; node left frozen" in output.err
    assert agent.frozen is True and list(agent.held) == [1]
    agent._release(1, "forward")


def test_cli_ctrl_c_exits_130_and_keeps_item_held(tmp_path, capsys, monkeypatch, agent):
    script = tmp_path / "interrupt.py"
    script.write_text("def on_item(item):\n    raise KeyboardInterrupt\n", encoding="utf-8")
    held_before_cli(agent)
    monkeypatch.setattr(cli, "_connect", lambda _args: connect(agent))

    assert cli.main(["script", str(script), "--no-record"]) == 130

    output = capsys.readouterr()
    assert "interrupted" in output.err and "left frozen" in output.err
    assert list(agent.held) == [1]
    agent._release(1, "forward")


def test_cli_session_close_failure_still_closes_client_and_reports_safety(
    tmp_path, capsys, monkeypatch, agent
):
    class FailingCloseSession(Session):
        def close(self):
            raise OSError("session close failed")

    script = tmp_path / "decide.py"
    script.write_text("def on_item(item): return 'hold'\n", encoding="utf-8")
    held_before_cli(agent)
    client, session = connect(agent), FailingCloseSession()
    monkeypatch.setattr(cli, "_connect", lambda _args: client)
    monkeypatch.setattr(cli, "_recorded_session", lambda *_args, **_kwargs: (session, None))

    assert cli.main(["script", str(script), "--no-record"]) == 1

    output = capsys.readouterr()
    assert "freeze-debugger script: cleanup failed while closing session: session close failed" in output.err
    assert "cannot reach the node" not in output.err
    assert "safety: item 1 remains held; node left frozen" in output.err
    assert "actions: forward=0 drop=0 replace=0 replace_raw=0 hold=1" in output.out
    assert client.wire not in agent._connections
    agent._release(1, "forward")


def test_cli_client_close_failure_still_reports_summary_and_safety(
    tmp_path, capsys, monkeypatch, agent
):
    script = tmp_path / "decide.py"
    script.write_text("def on_item(item): return 'hold'\n", encoding="utf-8")
    held_before_cli(agent)
    client = FailingCloseClient(agent)
    monkeypatch.setattr(cli, "_connect", lambda _args: client)

    assert cli.main(["script", str(script), "--no-record"]) == 1

    output = capsys.readouterr()
    assert "freeze-debugger script: cleanup failed while closing client: client close failed" in output.err
    assert "cannot reach the node" not in output.err
    assert "safety: item 1 remains held; node left frozen" in output.err
    assert "actions: forward=0 drop=0 replace=0 replace_raw=0 hold=1" in output.out
    assert client.close_calls == 1
    agent._connections.remove(client.wire)
    agent._release(1, "forward")


def test_cli_ctrl_c_keeps_130_when_cleanup_fails(tmp_path, capsys, monkeypatch, agent):
    script = tmp_path / "interrupt.py"
    script.write_text("def on_item(item): raise KeyboardInterrupt\n", encoding="utf-8")
    held_before_cli(agent)
    client = FailingCloseClient(agent)
    monkeypatch.setattr(cli, "_connect", lambda _args: client)

    assert cli.main(["script", str(script), "--no-record"]) == 130

    output = capsys.readouterr()
    assert "freeze-debugger script: interrupted" in output.err
    assert "freeze-debugger script: cleanup failed while closing client: client close failed" in output.err
    assert "safety: item 1 remains held; node left frozen" in output.err
    assert client.close_calls == 1
    agent._connections.remove(client.wire)
    agent._release(1, "forward")


def test_cli_closes_connection_when_session_setup_fails(tmp_path, monkeypatch, agent):
    script = tmp_path / "decide.py"
    script.write_text("def on_item(item): return 'hold'\n", encoding="utf-8")
    client = connect(agent)
    monkeypatch.setattr(cli, "_connect", lambda _args: client)
    monkeypatch.setattr(cli, "_recorded_session", lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("disk full")))

    assert cli.main(["script", str(script)]) == 1

    assert client.wire not in agent._connections


@pytest.mark.parametrize(
    ("extra", "expected"),
    [
        (["missing.py"], "script does not exist"),
        (["unused.py", "--only", "not a condition"], "cannot understand"),
    ],
)
def test_cli_rejects_script_and_filter_errors_before_connecting(tmp_path, capsys, monkeypatch, extra, expected):
    if extra[0] == "unused.py":
        (tmp_path / extra[0]).write_text("def on_item(item): return None\n", encoding="utf-8")
    extra[0] = str(tmp_path / extra[0])
    monkeypatch.setattr(cli, "_connect", lambda _args: pytest.fail("must not connect"))

    assert cli.main(["script", *extra, "--no-record"]) == 1

    assert expected in capsys.readouterr().err


def test_node_found_frozen_is_frozen_again_after_the_script_let_it_run(agent):
    agent.frozen = True
    threading.Thread(target=lambda: agent.offer({"kind": "internal_input", "label": "first"}), daemon=True).start()
    wait_until(lambda: bool(agent.held))
    client, session = connect(agent), Session()
    seen = []

    def on_item(item):
        seen.append(item["label"])
        if len(seen) == 2:
            raise RuntimeError("stop here")
        return None

    thread, _result, errors = run_async(client, on_item, session)
    wait_until(lambda: seen == ["first"] and not agent.held)
    assert agent.frozen is False, "forwarding the first item lets the node run"
    threading.Thread(target=lambda: agent.offer({"kind": "internal_input", "label": "second"}), daemon=True).start()

    thread.join(timeout=3)
    assert not thread.is_alive() and errors and seen == ["first", "second"]
    assert agent.hold is None and agent.frozen is True and list(agent.held) == [2]
    client.forward(2)
    client.close()
