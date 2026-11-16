import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "dwarf"))

from freeze import rules, scenarios  # noqa: E402
from freeze.client import FreezeClient  # noqa: E402
from freeze.session import Session  # noqa: E402
from freeze.testing.fake_agent import FakeAgent  # noqa: E402


def test_every_scenario_is_listed_and_classified_with_a_reason():
    entries = scenarios.catalog()
    assert len(entries) >= 350
    counts = scenarios.summary(entries)
    assert counts[scenarios.READY] >= 30 and counts[scenarios.NO] > counts[scenarios.READY]
    for entry in entries:
        assert entry.id and entry.title and entry.implementation and entry.subject
        if entry.status == scenarios.NO:
            assert entry.reason, entry.id
        else:
            assert scenarios.template_for(entry).rules, entry.id
    library = [entry for entry in entries if entry.runtime == "library"]
    assert library and all(entry.status == scenarios.NO for entry in library)


def test_dwarfs_own_test_values_are_read_without_importing_dwarf():
    lists = scenarios.value_lists()
    assert lists["dwarf handshake cases"]["unsupported-version-999"] == "8200a11903e782182af4"
    assert lists["dwarf tx submission cases"]["trailing-bytes-after-done"] == "810400"
    assert len(lists["dwarf block fetch cases"]) == 5 and len(lists["dwarf invalid cbor cases"]) == 3
    assert lists["dwarf chainsync seeds"]["seed-001-request-next"] == "8100"
    assert "profile_manager" not in sys.modules, "DWARF's own code is read as text, never imported"


def test_proxy_scenarios_become_rules_that_do_the_same_tampering():
    slot = scenarios.template_for(scenarios.find("runtime-substrate-chainsync-nonmonotonic-slot-example-smoke")).rules
    assert [(rule.when, rule.action, rule.after) for rule in slot] == [("kind=proto_in and type=ouroboros/2 and [0]=2", "replay", 3)]
    assert "rejected" in slot[0].expect

    header = scenarios.template_for(scenarios.find("runtime-substrate-praos-header-assertion-example-smoke")).rules[0]
    assert (header.field, header.values, header.after) == ("kes_signature", ["xorlast:01"], 2)

    block = scenarios.template_for(scenarios.find("runtime-substrate-blockfetch-invalid-block-cbor-example-smoke")).rules[0]
    assert (block.when, block.field, block.mode) == ("kind=proto_in and type=ouroboros/3 and [0]=4", rules.WHOLE, "cycle")

    request = scenarios.template_for(scenarios.find("runtime-substrate-blockfetch-invalid-range-example-smoke")).rules[0]
    assert request.when == "kind=proto_out and type=ouroboros/3 and [0]=0" and request.expect == []

    shim = scenarios.template_for(scenarios.find("phase3-runtime-generated-haskell3-node1-malformed-handshake")).rules
    assert [(rule.when, rule.values) for rule in shim] == [("kind=proto_in and type=ouroboros/0", ["0x83010b82182af4"])]

    amaru = scenarios.find("client-example-invalid-mini-protocol-amaru")
    assert amaru.status == scenarios.READY and len(scenarios.template_for(amaru).rules) >= 2

    with pytest.raises(rules.RuleError, match="cannot be carried out"):
        scenarios.template_for(scenarios.find("amaru-cbor-tx-body-fuzz-structured"))
    with pytest.raises(KeyError):
        scenarios.find("no-such-scenario")


def test_a_converted_scenario_runs_and_is_judged(tmp_path):
    agent = FakeAgent()
    agent.start()
    client = FreezeClient("127.0.0.1", agent.port, timeout=5)
    client.connect()
    engine = rules.Engine(client, Session())
    template = scenarios.template_for(scenarios.find("runtime-substrate-chainsync-nonmonotonic-slot-example-smoke"))
    rules.save_template(template, tmp_path / "t.yaml")
    assert rules.load_template(tmp_path / "t.yaml").rules[0].action == "replay"
    engine.arm_template(template)
    results: list = []

    def node() -> None:
        for number in range(1, 6):
            results.append(agent.offer({"kind": "proto_in", "from": "peer:10.0.0.3:3001", "to": "stage:chainsync-27", "label": "chain sync",
                                        "type": "ouroboros/2", "raw": f"82020{number}", "payload": [2, number], "editable": True, "droppable": True}))
            if number == 4:
                agent.offer({"kind": "internal_action", "from": "stage:track_peers-3", "label": "RecordHeaderRejectedEffect",
                             "payload": {"outcome": "InvalidHeader"}})

    threading.Thread(target=node, daemon=True).start()
    report = engine.run(timeout=20, settle=0.5)
    deadline = time.monotonic() + 5
    while len(results) < 5 and time.monotonic() < deadline:
        time.sleep(0.02)
    assert [verdict for _seq, verdict, _new in results] == ["forward", "forward", "forward", "replace", "forward"]
    assert results[3][2] == {"$raw": "820201", "$stale_negative_control": True}, \
        "the fourth header was explicitly replayed as a stale negative control, as DWARF's proxy does"
    assert report[0]["as_expected"] is True and "rejected" in report[0]["node_said"]
    client.close()
    agent.stop()
