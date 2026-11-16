import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "dwarf"))

from freeze import conditions, glossary, render  # noqa: E402
from freeze.client import FreezeClient, FreezeError  # noqa: E402
from freeze.session import Session  # noqa: E402
from freeze.testing.fake_agent import FakeAgent, load_capture, replay  # noqa: E402

CAPTURE = Path(__file__).resolve().parents[1] / "dwarf" / "freeze" / "demo" / "amaru-preprod-sync.jsonl.gz"


@pytest.fixture
def agent():
    fake = FakeAgent(token="icy")
    fake.start()
    yield fake
    fake.stop()


def connect(agent: FakeAgent) -> FreezeClient:
    client = FreezeClient("127.0.0.1", agent.port, token="icy", timeout=5)
    client.connect()
    return client


def offer_async(agent: FakeAgent, item: dict) -> list:
    result: list = []
    threading.Thread(target=lambda: result.append(agent.offer(item)), daemon=True).start()
    return result


def settled(result: list, timeout: float = 5.0) -> list:
    """The offer's outcome, once the node side has received the verdict."""
    deadline = time.monotonic() + timeout
    while not result and time.monotonic() < deadline:
        time.sleep(0.01)
    return result


def payment(fee: int) -> dict:
    return {"kind": "internal_input", "to": "stage:validate-1", "label": "Pay", "payload": {"fee": fee},
            "editable": True, "droppable": True}


def test_wrong_token_is_refused(agent):
    client = FreezeClient("127.0.0.1", agent.port, token="nope", timeout=5)
    with pytest.raises(FreezeError) as refused:
        client.connect()
    assert refused.value.code == "bad_token"


def test_resigning_toggle(agent):
    agent.resigning = {"enabled": True, "available": True}
    with connect(agent) as client:
        assert client.resigning() == {"enabled": True, "available": True}
        assert client.set_resigning(False) == {"enabled": False, "available": True}
        assert agent.resigning["enabled"] is False


def test_hold_edit_and_release(agent):
    with connect(agent) as client:
        client.set_hold(conditions.compile_condition("fee>100"))
        assert agent.offer(payment(5))[1] == "forward"
        result = offer_async(agent, payment(301204))
        held = client.wait_held(timeout=5)
        assert held["payload"] == {"fee": 301204}
        assert client.status()["frozen"] is True
        client.replace(held["seq"], payload={"fee": 1})
        client.wait_for("released", lambda data: data["seq"] == held["seq"], timeout=5)
        assert settled(result) == [(held["seq"], "replace", {"fee": 1})]
        client.thaw()
        assert client.status() == {"frozen": False, "seq": 2, "held": [], "hold": client.status()["hold"],
                                   "conditions": 0, "frozen_ms": 0, "deferred": [],
                                   "resigning": {"enabled": False, "available": False,
                                                  "error": "not a controlled Freeze devnet"}}


def test_raw_stale_negative_control_is_bound_to_that_replacement(agent):
    with connect(agent) as client:
        client.freeze()
        result = offer_async(agent, {**payment(1), "raw": "01"})
        held = client.wait_held(timeout=5)
        client.replace(held["seq"], raw=b"\x02", stale_negative_control=True)
        assert settled(result) == [
            (held["seq"], "replace", {"$raw": "02", "$stale_negative_control": True}),
        ]


def test_drop_needs_a_droppable_item(agent):
    with connect(agent) as client:
        client.freeze()
        offer_async(agent, {**payment(1), "droppable": False})
        held = client.wait_held(timeout=5)
        with pytest.raises(FreezeError):
            client.drop(held["seq"])
        assert client.step() == held["seq"]


def test_run_until_stops_at_the_first_match(agent):
    with connect(agent) as client:
        client.freeze()
        first = offer_async(agent, payment(1))
        client.wait_held(timeout=5)
        client.run_until(conditions.compile_condition("label=Target"))
        assert settled(first)[0][1] == "forward"
        agent.offer(payment(2))
        offer_async(agent, {**payment(3), "label": "Target"})
        frozen = client.wait_for("frozen", timeout=5)
        assert client.item(frozen["seq"])["label"] == "Target"
        assert client.conditions() == []


def test_session_follows_a_real_capture(agent):
    events = load_capture(CAPTURE)
    with connect(agent) as client:
        session = Session()
        client.on_event(session.record)
        replay(agent, events).join(timeout=60)
        items = [e for e in events if e["event"] == "item"]
        deadline = time.monotonic() + 10
        while len(session.items) < len(items) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert len(session.items) == len(items)
        with_reaction = [seq for seq in session.items if session.reaction(seq)]
        assert len(with_reaction) > 100
        labels = {item["label"] for item in session.items.values()}
        assert {"ValidateHeaderEffect", "StoreBlockEffect", "send"} <= labels


def test_conditions_round_trip_and_match():
    item = {"seq": 9, "kind": "internal_input", "to": "stage:validate_block-6", "from": "", "type": "x::Msg",
            "label": "Msg", "cold_spots": ["block-check"],
            "payload": {"slot": 138758377, "txs": [{"fee": 1}], "hash": {"$bytes": "8f29cc"}}}
    for text, expected in [
        ("kind=input and to~validate", True), ("kind=action", False), ("slot>=138758377", True),
        ("slot>138758377", False), ("txs[0].fee=1", True), ("txs#>1", False), ("hash~29cc", True),
        ("spot=block-check and not label=clock", True), ("missing?", False), ("label=Nope or seq>=9", True),
    ]:
        predicate = conditions.compile_condition(text)
        assert conditions.matches(predicate, item) is expected, text
        assert conditions.compile_condition(conditions.describe(predicate)) == predicate, text
    with pytest.raises(conditions.ConditionError):
        conditions.compile_condition("what is this")


def test_glossary_gives_plain_names():
    words = glossary.default()
    assert words.lookup("vrf_proof").label == "vrf proof"
    assert words.lookup("amaru_consensus::effects::ledger_effects::ValidateBlockEffect").label == "check block"
    assert words.party("stage:validate_block-6").label == "validate block"
    assert words.party("stage:0-85.235.64.182:6000-15").label == "connection 85.235.64.182:6000"
    unknown = words.lookup("some_new_field")
    assert (unknown.label, unknown.known) == ("some new field", False)
    assert all(entry.what for entry in words._entries.values())


def test_render_and_edit_payloads():
    payload = {"hash": {"$bytes": "8f29cc02e26fd3f6853013f815f16dd86c744b65dd38145ca20b503bf577f2e4"},
               "body": {"slot": 138758377, "seed": list(range(10, 20))}, "txs": [{"fee": 301204}]}
    lines = {node.name: node.summary for node in render.walk(render.build(payload)) if node.name}
    assert lines["hash"] == "8f29cc02…f577f2e4  (32 bytes)"
    assert lines["slot"] == "138,758,377"
    assert lines["seed"].endswith("(10 bytes)")
    edited = render.set_at(payload, ("txs", 0, "fee"), render.parse_edit("1", 301204))
    assert edited["txs"] == [{"fee": 1}] and payload["txs"] == [{"fee": 301204}]
    assert render.parse_edit("00ff", {"$bytes": "aa"}) == {"$bytes": "00ff"}
    assert render.parse_edit("99999999999999999999", 5) == {"$int": "99999999999999999999"}
    with pytest.raises(ValueError):
        render.parse_edit("zz", {"$bytes": "aa"})
    assert render.one_line("Initialize") == "Initialize"
