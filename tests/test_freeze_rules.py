import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "dwarf"))

from freeze import payloads, rules  # noqa: E402
from freeze.client import FreezeClient  # noqa: E402
from freeze.session import Session  # noqa: E402
from freeze.testing.fake_agent import FakeAgent  # noqa: E402
from freeze.views import item_view, path_text, resolve_field  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_freeze_tui import roll_forward  # noqa: E402


@pytest.fixture
def agent():
    fake = FakeAgent()
    fake.start()
    yield fake
    fake.stop()


def header(agent: FakeAgent) -> dict:
    return {"kind": "proto_in", "from": "peer:10.0.0.12:3001", "to": "stage:chainsync-39", "label": "chain sync",
            "type": "ouroboros/2", "raw": roll_forward().hex(), "payload": [2], "editable": True, "droppable": True}


def offer_all(agent: FakeAgent, items: list[dict]) -> list:
    results: list = []

    def node() -> None:
        for item in items:
            results.append(agent.offer(dict(item)))

    threading.Thread(target=node, daemon=True).start()
    return results


def test_values_and_transforms():
    assert payloads.apply("add:1", 2778) == 2779
    assert payloads.apply("mul:2", {"$int": "18446744073709551615"}) == {"$int": "36893488147419103230"}
    assert payloads.apply(-1, 5) == -1 and payloads.apply("18446744073709551616", 5) == {"$int": "18446744073709551616"}
    data = {"$bytes": "aabbccdd"}
    assert payloads.apply("empty", data) == {"$bytes": ""}
    assert payloads.apply("trunc:1", data) == {"$bytes": "aa"}
    assert payloads.apply("append:00", data) == {"$bytes": "aabbccdd00"}
    assert payloads.apply("flip:0", data) == {"$bytes": "abbbccdd"}
    assert payloads.apply("zero", data) == {"$bytes": "00000000"}
    assert payloads.apply("0xdead", data) == {"$bytes": "dead"}
    assert payloads.apply("ff", data) == {"$bytes": "ff"}, "hex text fits a bytes field"
    assert payloads.apply("hello", "x") == "hello" and payloads.apply("empty", "x") == ""
    with pytest.raises(payloads.PayloadError):
        payloads.apply("add:1", data)
    with pytest.raises(payloads.PayloadError):
        payloads.apply("flip:99", data)


def test_lists_are_stored_imported_and_layered_over_the_shipped_ones(tmp_path):
    store = payloads.Store(tmp_path / "payloads")
    shipped = {item.name for item in store.all()}
    assert {"integer boundaries", "byte string damage"} <= shipped and all(item.builtin for item in store.all())

    (tmp_path / "mine.txt").write_text("# my values\n1\n2\n\nadd:5\n", encoding="utf-8")
    (tmp_path / "two.yaml").write_text("lists:\n  a: [1, 2]\n  b: ['0xff']\n", encoding="utf-8")
    assert [item.values for item in store.import_file(tmp_path / "mine.txt")] == [["1", "2", "add:5"]]
    assert [item.name for item in store.import_file(tmp_path / "two.yaml")] == ["a", "b"]
    assert store.get("mine").builtin is False

    store.save(payloads.PayloadList("integer boundaries", [7]))
    assert store.get("integer boundaries").values == [7]
    assert store.delete("integer boundaries") and len(store.get("integer boundaries").values) > 5, "the shipped list is back"
    store.export("a", tmp_path / "out.yaml")
    assert payloads.read_file(tmp_path / "out.yaml")[0].values == [1, 2]
    with pytest.raises(payloads.PayloadError):
        store.get("nope")


def test_fields_are_found_by_name_chain_or_path():
    payload, names, raw = item_view({"kind": "proto_in", "type": "ouroboros/2", "raw": roll_forward().hex()})
    in_header = resolve_field(payload, names, "header_body.slot", loose_bytes=False)
    in_tip = resolve_field(payload, names, "tip.point.slot", loose_bytes=False)
    assert "$embed" in in_header and "$embed" not in in_tip and in_header != in_tip
    assert resolve_field(payload, names, "slot", loose_bytes=False) == in_header, "the first match in reading order"
    assert path_text(in_header, names) == "header.header.header_body.slot"
    assert resolve_field({"txs": [{"fee": 1}]}, {}, "txs[0].fee", loose_bytes=True) == ("txs", 0, "fee")
    assert resolve_field({"txs": [{"fee": 1}]}, {}, "fee", loose_bytes=True) == ("txs", 0, "fee")
    with pytest.raises(ValueError):
        resolve_field(payload, names, "no_such_field", loose_bytes=False)


def test_a_list_is_run_against_one_field_one_value_per_matching_item(agent):
    client = FreezeClient("127.0.0.1", agent.port, timeout=5)
    client.connect()
    session = Session()
    engine = rules.Engine(client, session)
    rule = rules.Rule(name="slot", when="kind=proto_in and type=ouroboros/2 and [0]=2", field="header_body.slot",
                      values=["add:1", 0, "18446744073709551615"], stale_negative_control=True)
    engine.arm(rule)
    results = offer_all(agent, [header(agent)] * 4 + [{"kind": "internal_input", "to": "stage:x", "label": "Other"}])
    report = engine.run(timeout=20, settle=0.5)

    deadline = time.monotonic() + 5
    while len(results) < 5 and time.monotonic() < deadline:
        time.sleep(0.02)
    assert [verdict for _seq, verdict, _new in results] == ["replace", "replace", "replace", "forward", "forward"]
    original = roll_forward()
    sent = [bytes.fromhex(new["$raw"]) for _seq, verdict, new in results if verdict == "replace"]
    slots = [item_view({"kind": "proto_in", "type": "ouroboros/2", "raw": raw.hex()})[0] for raw in sent]
    path = resolve_field(slots[0], {}, list(resolve_field(*item_view(header(agent))[:2], "header_body.slot", loose_bytes=False)), False)
    from freeze import render

    assert [render.get_at(payload, path) for payload in slots] == [138801636, 0, {"$int": "18446744073709551615"}]
    assert len(sent[0]) == len(original) and sum(a != b for a, b in zip(sent[0], original)) == 1
    assert [row["value"] for row in report] == ["add:1", "0", "18446744073709551615"]
    assert [row["before"] for row in report] == ["138,801,635"] * 3 and report[0]["after"] == "138,801,636"
    assert not rule.armed and agent.conditions == {} and agent.frozen is False, "the rule cleans up after its last value"
    client.close()


def test_network_rules_require_an_explicit_stale_negative_control(agent):
    client = FreezeClient("127.0.0.1", agent.port, timeout=5)
    client.connect()
    engine = rules.Engine(client, Session())
    refused = rules.Rule(
        name="protected slot",
        when="kind=proto_in and type=ouroboros/2 and [0]=2",
        field="header_body.slot",
        values=["add:1"],
    )
    engine.arm(refused)
    first = offer_all(agent, [header(agent)])
    report = engine.run(timeout=10, settle=0.2)
    deadline = time.monotonic() + 5
    while not first and time.monotonic() < deadline:
        time.sleep(0.02)
    assert first[0][1] == "forward"
    assert report[0]["node_said"].startswith("not applied: edit refused:")
    assert "Praos VRF proof" in report[0]["node_said"]

    allowed = rules.Rule(
        name="deliberately stale slot",
        when="kind=proto_in and type=ouroboros/2 and [0]=2",
        field="header_body.slot",
        values=["add:1"],
        stale_negative_control=True,
    )
    engine.arm(allowed)
    second = offer_all(agent, [header(agent)])
    engine.run(timeout=10, settle=0.2)
    deadline = time.monotonic() + 5
    while not second and time.monotonic() < deadline:
        time.sleep(0.02)
    assert second[0][1] == "replace"
    client.close()


def test_template_with_several_rules_and_a_reaction(agent, tmp_path):
    (tmp_path / "t.yaml").write_text(
        "name: demo\n"
        "lists:\n  tiny: [1]\n"
        "rules:\n"
        "  - name: fee\n    when: kind=input and to~validate\n    field: txs[0].fee\n    values: tiny\n"
        "  - name: starve\n    when: kind=input and to~select_chain\n    action: drop\n    count: 2\n"
        "  - name: sizes\n    when: label=Sized\n    field: body\n    values: byte string damage\n    mode: cycle\n    count: 2\n",
        encoding="utf-8",
    )
    template = rules.load_template(tmp_path / "t.yaml", payloads.Store(tmp_path / "none"))
    assert [(rule.name, rule.action, rule.total) for rule in template.rules] == [("fee", "replace", 1), ("starve", "drop", 2), ("sizes", "replace", 2)]
    rules.save_template(template, tmp_path / "again.yaml")
    assert rules.load_template(tmp_path / "again.yaml", payloads.Store(tmp_path / "none")).rules[2].values[0] == "empty"

    client = FreezeClient("127.0.0.1", agent.port, timeout=5)
    client.connect()
    session = Session()
    engine = rules.Engine(client, session)
    engine.arm_template(template)
    pay = {"kind": "internal_input", "to": "stage:validate_block-6", "label": "Pay", "payload": {"txs": [{"fee": 301204}]},
           "editable": True, "droppable": True}
    tip = {"kind": "internal_input", "to": "stage:select_chain-4", "label": "NewTip", "payload": 1, "editable": True, "droppable": True}
    sized = {"kind": "internal_input", "to": "stage:x-1", "label": "Sized", "payload": {"body": {"$bytes": "aabb"}},
             "editable": True, "droppable": True}
    error = {"kind": "internal_result", "to": "stage:validate_block-6", "label": "Result",
             "payload": {"Err": {"Assert": {"VrfProof": {"InvalidProof": ["VerificationFailed", 1]}}}}}
    rejected = {"kind": "internal_action", "from": "stage:track_peers-3", "label": "RecordHeaderRejectedEffect", "payload": {"outcome": "InvalidHeader"}}
    hostile = {"kind": "internal_action", "from": "stage:peer_selection-2", "label": "PeerAdversarialEffect", "payload": {"peer": {}}}
    results = offer_all(agent, [pay, error, rejected, hostile, tip, tip, tip, sized, sized, sized])
    report = engine.run(timeout=20, settle=0.5)
    deadline = time.monotonic() + 5
    while len(results) < 10 and time.monotonic() < deadline:
        time.sleep(0.02)
    verdicts = [verdict for _seq, verdict, _new in results]
    assert verdicts == ["replace", "forward", "forward", "forward", "drop", "drop", "forward", "replace", "replace", "forward"]
    assert results[0][2] == {"txs": [{"fee": 1}]}
    assert [results[7][2], results[8][2]] == [{"body": {"$bytes": ""}}, {"body": {"$bytes": "aa"}}]
    by_rule = {row["rule"]: row for row in report}
    assert by_rule["fee"]["node_said"] == "error: Assert › VrfProof › InvalidProof › VerificationFailed; rejected (InvalidHeader); marked the peer hostile"
    assert by_rule["starve"]["node_said"] == "dropped; no reaction seen"
    assert not engine.active
    client.close()


def test_a_value_that_does_not_fit_is_reported_and_the_item_goes_through(agent):
    client = FreezeClient("127.0.0.1", agent.port, timeout=5)
    client.connect()
    engine = rules.Engine(client, Session())
    engine.arm(rules.Rule(name="bad", when="label=Pay", field="fee", values=["flip:0"]))
    results = offer_all(agent, [{"kind": "internal_input", "to": "stage:v-1", "label": "Pay", "payload": {"fee": 5}, "editable": True}])
    report = engine.run(timeout=10, settle=0.2)
    deadline = time.monotonic() + 5
    while not results and time.monotonic() < deadline:
        time.sleep(0.02)
    assert results[0][1] == "forward"
    assert report[0]["node_said"].startswith("not applied: `flip` needs bytes")
    with pytest.raises(rules.RuleError):
        rules.Rule(name="x", when="what is this", values=[1])
    with pytest.raises(rules.RuleError):
        rules.Rule(name="x", when="label=a")
    client.close()


def test_rule_made_from_a_selected_field():
    item = header(None)
    payload, names, _raw = item_view(item)
    path = resolve_field(payload, names, "header_body.slot", loose_bytes=False)
    rule = rules.rule_for_item(item, path, ["add:1"], "nearby numbers")
    assert rule.when == "kind=proto_in and type=ouroboros/2 and [0]=2"
    assert rule.name == "header.header.header_body.slot ← nearby numbers"
    inner = {"kind": "internal_input", "to": "stage:validate_block-6", "label": "ValidateBlockMsg", "payload": {"a": 1}}
    assert rules.rule_for_item(inner, ("a",), [1]).when == "kind=internal_input and label=ValidateBlockMsg and to~validate_block"


def test_skip_then_replay_an_earlier_message_and_judge_the_reaction(agent):
    client = FreezeClient("127.0.0.1", agent.port, timeout=5)
    client.connect()
    engine = rules.Engine(client, Session())
    template = rules.parse_template({"rules": [
        {"name": "old header again", "when": "kind=proto_in and type=ouroboros/2", "action": "replay", "after": 2,
         "expect": ["rejected", "hung up"], "stale_negative_control": True},
        {"name": "damage", "when": "kind=proto_in and type=ouroboros/3", "field": "@message", "values": ["xorlast:01", "0x8105"],
         "expect": ["rejected"], "stale_negative_control": True},
    ]})
    engine.arm_template(template)

    def message(protocol: int, raw: str) -> dict:
        return {"kind": "proto_in", "from": "peer:10.0.0.12:3001", "to": "stage:x-1", "type": f"ouroboros/{protocol}",
                "label": "m", "raw": raw, "payload": None, "editable": True, "droppable": True}

    rejected = {"kind": "internal_action", "from": "stage:track_peers-3", "label": "RecordHeaderRejectedEffect", "payload": {"outcome": "InvalidHeader"}}
    results = offer_all(agent, [message(2, "820201"), message(2, "820202"), message(2, "820203"), rejected,
                                message(3, "8204aa"), message(3, "8204bb"), message(2, "820204")])
    report = engine.run(timeout=20, settle=0.5)
    deadline = time.monotonic() + 5
    while len(results) < 7 and time.monotonic() < deadline:
        time.sleep(0.02)
    assert [verdict for _seq, verdict, _new in results] == ["forward", "forward", "replace", "forward", "replace", "replace", "forward"]
    assert results[2][2] == {"$raw": "820201", "$stale_negative_control": True}, \
        "the third header was replaced by the first under the explicit negative-control policy"
    assert [results[4][2], results[5][2]] == [
        {"$raw": "8204ab", "$stale_negative_control": True},
        {"$raw": "8105", "$stale_negative_control": True},
    ]
    by_rule = {row["rule"]: row for row in report}
    assert by_rule["old header again"]["as_expected"] is True and "rejected (InvalidHeader)" in by_rule["old header again"]["node_said"]
    assert [row["as_expected"] for row in report if row["rule"] == "damage"] == [False, False]
    assert rules.parse_template(template.to_document()).rules[0].after == 2
    client.close()


def test_reactions_follow_the_cause_the_node_reports_not_the_order_of_arrival():
    """Two tampered messages a moment apart: each reaction lands on its own message."""
    session = Session()

    def item(seq, kind, label, cause=None, to="", payload=None):
        data = {"seq": seq, "kind": kind, "label": label, "from": "", "to": to, "payload": payload}
        if cause:
            data["cause"] = cause
        session.record("item", data)

    item(10, "proto_in", "chain sync")
    item(11, "proto_in", "chain sync")
    # The node's work on the two messages interleaves; the second is rejected first.
    item(12, "internal_input", "Msg", cause=11, to="stage:validate_header-4")
    item(13, "internal_input", "Msg", cause=10, to="stage:validate_header-4")
    item(14, "internal_action", "HeaderRejected", cause=11, payload={"outcome": "InvalidKesSignature"})
    item(15, "internal_action", "PeerAdversarialEffect", cause=11)
    item(16, "internal_input", "Msg", cause=10, to="stage:select_chain-5")

    assert session.traced
    assert rules.reaction(session, 11) == ["rejected (InvalidKesSignature)", "marked the peer hostile"]
    assert rules.reaction(session, 10) == ["no error in 2 steps, reached validate_header, select_chain"]
    assert rules.reaction(session, 99) == []


def test_reaction_tells_a_parked_header_and_a_duplicate_from_a_rejection():
    session = Session()
    for data in (
        {"seq": 30, "kind": "proto_in", "label": "chain sync", "from": "peer:10.0.0.3:3001", "to": "stage:chainsync-1", "payload": None},
        # Amaru sets a header aside when it is far ahead of the ledger, and rechecks on a timer.
        {"seq": 31, "kind": "internal_action", "label": "schedule", "from": "stage:track_peers-3", "to": "",
         "payload": {"at": "37.5s", "message": "RecheckLedgerHeight"}, "cause": 30},
        {"seq": 32, "kind": "internal_action", "label": "schedule", "from": "stage:track_peers-3", "to": "",
         "payload": {"at": "37.7s", "message": "RecheckLedgerHeight"}, "cause": 30},
        {"seq": 40, "kind": "proto_in", "label": "chain sync", "from": "peer:10.0.0.4:3001", "to": "stage:chainsync-2", "payload": None},
        # The same header had already arrived from another peer.
        {"seq": 41, "kind": "internal_action", "label": "RecordHeaderRejectedEffect", "from": "stage:track_peers-3", "to": "node",
         "payload": {"outcome": "DuplicateHeader"}, "cause": 40},
    ):
        session.record("item", data)

    assert rules.reaction(session, 30) == ["set aside by the node, undecided (waiting for its ledger or clock to catch up)"]
    assert rules.reaction(session, 40) == ["already had it (duplicate, no penalty)"]
    assert not any("rejected" in phrase for phrase in rules.reaction(session, 40)), "a duplicate must not satisfy `expect: [rejected]`"


def test_reaction_reports_fail_closed_resigning_errors():
    session = Session()
    session.record("item", {"seq": 20, "kind": "proto_in", "label": "chain sync", "from": "", "to": "", "payload": None})
    session.record("item", {
        "seq": 21,
        "kind": "resign_error",
        "label": "edited header re-signing failed",
        "from": "freeze:resigner",
        "to": "stage:chainsync",
        "payload": {"error": "mounted VRF key does not match the header identity"},
        "cause": 20,
    })

    assert rules.reaction(session, 20) == ["re-signing failed: mounted VRF key does not match the header identity"]


def test_history_shows_the_whole_chain_under_the_message_that_caused_it():
    session = Session()
    for data in (
        {"seq": 1, "kind": "internal_input", "label": "Bytes"},
        {"seq": 2, "kind": "proto_in", "label": "chain sync", "parent": 1},
        {"seq": 3, "kind": "internal_action", "label": "send", "parent": 1, "cause": 2},
        {"seq": 4, "kind": "internal_input", "label": "Msg", "parent": 3, "cause": 2},
        {"seq": 5, "kind": "internal_action", "label": "CloseEffect", "parent": 4, "cause": 2},
    ):
        session.record("item", {"from": "", "to": "", "payload": None, **data})
    assert [(depth, item["seq"]) for depth, item in session.reaction(2)] == [(0, 3), (1, 4), (2, 5)]


def test_a_value_captured_from_one_message_is_put_into_another(agent):
    """Paired edits: keep the slot of a header, then write it into the block number of the next."""
    client = FreezeClient("127.0.0.1", agent.port, timeout=5)
    client.connect()
    engine = rules.Engine(client, Session())
    template = rules.parse_template({"rules": [
        {"name": "keep", "when": "kind=proto_in and type=ouroboros/2 and [0]=2", "action": "capture",
         "field": "header_body.slot", "as": "slot"},
        {"name": "use", "when": "kind=proto_in and type=ouroboros/2 and [0]=2",
         "field": "header_body.block_number", "values": ["$slot"], "stale_negative_control": True},
    ]})
    engine.arm_template(template)
    results = offer_all(agent, [header(agent)] * 3)
    report = engine.run(timeout=20, settle=0.5)
    deadline = time.monotonic() + 5
    while len(results) < 3 and time.monotonic() < deadline:
        time.sleep(0.02)
    assert engine.variables == {"slot": 138801635}
    assert [row["rule"] for row in report] == ["keep", "use"]
    assert report[0]["value"] == "kept as $slot" and report[1]["after"] == "138,801,635"
    assert [verdict for _seq, verdict, _new in results] == ["forward", "replace", "forward"]
    for wrong in ({"action": "capture", "when": "kind=proto_in"}, {"action": "delay", "when": "kind=proto_in"}):
        with pytest.raises(rules.RuleError):
            rules.parse_template({"rules": [wrong]})
    client.close()


def test_delay_and_overtake_set_an_item_aside_while_the_node_runs_on(agent):
    client = FreezeClient("127.0.0.1", agent.port, timeout=5)
    client.connect()
    session = Session()
    engine = rules.Engine(client, session)
    engine.arm_template(rules.parse_template({"rules": [
        {"name": "late", "when": "kind=proto_in and type=ouroboros/2", "action": "delay", "delay_ms": 400},
        {"name": "second first", "when": "label=First", "action": "overtake", "until": "label=Second"},
    ]}))
    order: list = []

    def node(item: dict, started: float) -> None:
        agent.offer(dict(item))
        order.append((item["label"], time.monotonic() - started))

    began = time.monotonic()
    threading.Thread(target=node, args=(header(agent), began), daemon=True).start()
    threading.Thread(target=node, args=({"kind": "internal_input", "to": "stage:a-1", "label": "First"}, began), daemon=True).start()
    runner = threading.Thread(target=lambda: engine.run(timeout=10, settle=1.5), daemon=True)
    runner.start()
    time.sleep(0.15)
    assert agent.status()["frozen"] is False and len(agent.status()["deferred"]) == 2, "both are set aside, the node runs"
    assert order == []
    node({"kind": "internal_input", "to": "stage:b-2", "label": "Second"}, began)
    runner.join(timeout=15)
    by_label = {item["label"]: seq for seq, item in session.items.items()}
    release = session.releases[by_label["First"]]
    assert release["deferred"] and release["overtaken_by"] == by_label["Second"], "First waited until Second had gone by"
    waited = dict(order)["chain sync"]
    assert 0.35 <= waited < 3, f"the delayed message waited {waited:.2f}s"
    assert agent.status()["deferred"] == [] and session.deferred == {}
    said = {row["rule"]: row["node_said"] for row in engine.report()}
    assert said["second first"].startswith(f"went on after item {by_label['Second']} passed it")
    assert said["late"].startswith("went on when its time was up")
    client.close()
