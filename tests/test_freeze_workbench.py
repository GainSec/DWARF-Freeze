"""Search, coverage, comparison, marks and findings, repeating an item, and resource figures."""

import json
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "dwarf"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from freeze import analysis, cli, notes, repeater, resources  # noqa: E402
from freeze.client import FreezeClient  # noqa: E402
from freeze.session import Session  # noqa: E402
from freeze.testing.fake_agent import FakeAgent  # noqa: E402
from freeze.views import item_view  # noqa: E402
from test_freeze_tui import roll_forward  # noqa: E402

HEADER = {"kind": "proto_in", "from": "peer:10.0.0.12:3001", "to": "stage:chainsync-39", "label": "chain sync",
          "type": "ouroboros/2", "payload": [2], "editable": True, "droppable": True}


def recorded(directory: Path) -> Session:
    """A small session on disk: a header edited by the debugger, rejected by the node, and some other work."""
    session = Session(directory)
    raw = roll_forward()
    edited = repeater.edited({**HEADER, "raw": raw.hex(), "seq": 2}, [("header_body.slot", "add:1")])[0]
    events = [
        ("item", {"seq": 1, "kind": "internal_input", "label": "Bytes", "from": "", "to": "stage:chainsync-39", "payload": None}),
        ("item", {**HEADER, "seq": 2, "raw": raw.hex(), "held": True, "parent": 1}),
        ("released", {"seq": 2, "verdict": "replace", "raw": edited.hex()}),
        ("item", {"seq": 3, "kind": "internal_action", "label": "send", "from": "stage:chainsync-39", "to": "stage:validate_header-4",
                  "payload": None, "parent": 1, "cause": 2}),
        ("item", {"seq": 4, "kind": "internal_input", "label": "Msg", "from": "", "to": "stage:validate_header-4", "payload": None,
                  "parent": 3, "cause": 2}),
        ("item", {"seq": 5, "kind": "internal_action", "label": "ValidateHeaderEffect", "from": "stage:validate_header-4", "to": "node",
                  "payload": None, "parent": 4, "cause": 2}),
        ("item", {"seq": 6, "kind": "internal_result", "label": "Result", "from": "node", "to": "stage:validate_header-4",
                  "payload": {"Err": {"Assert": {"VrfProof": "InvalidProof"}}}, "parent": 5, "cause": 2}),
        ("item", {"seq": 7, "kind": "internal_action", "label": "HeaderRejected", "from": "stage:validate_header-4", "to": "node",
                  "payload": {"outcome": "InvalidHeader"}, "parent": 4, "cause": 2}),
        ("item", {"seq": 8, "kind": "internal_action", "label": "send", "from": "stage:validate_header-4", "to": "stage:select_chain-5",
                  "payload": None, "parent": 4, "cause": 2}),
        ("item", {**HEADER, "seq": 9, "raw": raw.hex()}),
    ]
    for event, data in events:
        session.record(event, data)
    session.close()
    return session


def test_search_coverage_and_queues(tmp_path):
    session = recorded(tmp_path / "a")
    assert [item["seq"] for item in analysis.search(session, "kind=proto_in")] == [2, 9]
    assert [item["seq"] for item in analysis.search(session, "invalidproof")] == [6], "plain text falls back to a text search"
    reached = analysis.coverage(session)
    assert dict(reached["parts"]) == {"chainsync": 1, "validate_header": 1}
    assert dict(reached["errors"]) == {"Assert › VrfProof › InvalidProof": 1}
    assert dict(reached["rejections"]) == {"HeaderRejected: InvalidHeader": 1}
    assert reached["effects"]["ValidateHeaderEffect"] == 1 and reached["messages"]["← RollForward"] == 2
    assert resources.backlog(session) == [("stage:select_chain-5", 1)], "sent to chain selection and never taken"

    quiet = Session()
    quiet.record("item", {**HEADER, "seq": 1, "raw": roll_forward().hex()})
    differences = analysis.compare_coverage(session, quiet)
    assert differences["errors"] == {"only_first": ["Assert › VrfProof › InvalidProof"], "only_second": []}


def test_results_and_items_are_compared(tmp_path):
    first = [{"rule": "slot", "number": 1, "value": "add:1", "node_said": "rejected (InvalidHeader)"},
             {"rule": "slot", "number": 2, "value": "0", "node_said": "no error in 83 steps"}]
    second = [{"rule": "slot", "number": 1, "value": "add:1", "node_said": "no error in 12 steps"},
              {"rule": "slot", "number": 2, "value": "0", "node_said": "no error in 90 steps"},
              {"rule": "extra", "number": 1, "value": "", "node_said": "hung up"}]
    rows = analysis.compare_results(first, second)
    assert [row["same"] for row in rows] == [False, True, False], "step counts do not make two reactions different"
    assert rows[2]["first"] == "(not attempted)"

    session = recorded(tmp_path / "a")
    session.items[9] = {**session.items[9], "raw": session.releases[2]["raw"]}
    result = analysis.compare_items(session, 2, 9)
    assert result["fields"] == [{"field": "header.header.header_body.slot", "first": "138,801,635", "second": "138,801,636"}]
    assert result["first"][0].startswith("error: Assert") and result["second"] == []


def test_marks_and_a_findings_report(tmp_path):
    directory = tmp_path / "a"
    recorded(directory)
    marks = notes.Notes(directory)
    assert marks.toggle_bookmark(9) and marks.badge(9) == "*"
    marks.comment(2, "slot moved one ahead")
    marks.finding(2, "Header with a moved slot is rejected only at the VRF check", "low", "Expected; recorded as a baseline.")
    with pytest.raises(notes.NotesError):
        marks.finding(2, "x", "catastrophic")
    assert notes.Notes(directory).badge(2) == "!#", "marks are kept beside the session"
    assert not marks.toggle_bookmark(9) and 9 not in notes.Notes(directory).marks

    output = tmp_path / "report.md"
    assert notes.report(directory, output, "Slot edits") == 1
    text = output.read_text(encoding="utf-8")
    assert "# Slot edits" in text and "(low)" in text and "slot moved one ahead" in text
    assert "138,801,635 → 138,801,636" in text, "the report shows what was changed"
    assert "error: Assert › VrfProof › InvalidProof; rejected (InvalidHeader)" in text
    assert "HeaderRejected" in text, "and the chain of steps that followed"
    bundle = json.loads(output.with_suffix(".evidence.json").read_text(encoding="utf-8"))
    assert bundle[0]["seq"] == 2 and bundle[0]["changes"][0]["after"] == "138,801,636"


def test_commands_work_on_a_recorded_session(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("DWARF_DATA_ROOT", str(tmp_path))
    recorded(tmp_path / "freeze" / "sessions" / "s1")
    assert cli.main(["search", "kind=proto_in", "--session", "s1"]) == 0
    assert "2 item(s)" in capsys.readouterr().out
    assert cli.main(["mark", "2", "--session", "s1", "--finding", "Odd", "--severity", "high", "--bookmark"]) == 0
    assert cli.main(["findings", "--session", "s1", "-o", str(tmp_path / "r.md")]) == 0
    assert "## 1. Odd  (high)" in (tmp_path / "r.md").read_text(encoding="utf-8")
    assert cli.main(["coverage", "--session", "s1"]) == 0
    out = capsys.readouterr().out
    assert "Assert › VrfProof › InvalidProof" in out and "select_chain" in out
    assert cli.main(["compare", "--items", "2", "9", "--session", "s1"]) == 0
    assert cli.main(["mark", "77", "--session", "s1", "--bookmark"]) == 1


def test_an_old_message_takes_the_place_of_the_next_one_and_an_input_is_delivered_again(tmp_path):
    agent = FakeAgent()
    agent.start()
    try:
        client = FreezeClient("127.0.0.1", agent.port, timeout=5)
        client.connect()
        from freeze import rules

        past = {**HEADER, "seq": 2, "raw": roll_forward().hex()}
        with pytest.raises(repeater.RepeatError, match="Praos VRF proof"):
            repeater.plan(client, past, [("header_body.slot", "add:5")], times=1)
        step = repeater.plan(
            client,
            past,
            [("header_body.slot", "add:5")],
            times=1,
            stale_negative_control=True,
        )
        assert step["how"] == "next"
        assert step["impact"]["state"] == "stale-negative-control"
        engine = rules.Engine(client, Session())
        engine.arm(step["rule"])
        results: list = []
        threading.Thread(target=lambda: results.append(agent.offer({**HEADER, "raw": roll_forward().hex()})), daemon=True).start()
        engine.run(timeout=10, settle=0.3)
        deadline = time.monotonic() + 5
        while not results and time.monotonic() < deadline:
            time.sleep(0.02)
        sent = bytes.fromhex(results[0][2]["$raw"])
        assert sent == repeater.edited(past, [("header_body.slot", "add:5")])[0] and sent != roll_forward()
        assert item_view({"kind": "proto_in", "type": "ouroboros/2", "raw": sent.hex()})[2]

        again = repeater.plan(client, {"seq": 1, "kind": "internal_input", "to": "stage:fake", "payload": {"fee": 5}}, [("fee", "add:1")], 2)
        assert again["how"] == "inject" and repeater.send(client, again) == 2
        assert agent.injected == [("stage:fake", {"fee": 6})] * 2
        with pytest.raises(repeater.RepeatError):
            repeater.plan(client, {"seq": 1, "kind": "internal_input", "to": "stage:gone", "payload": 1})
        client.close()
    finally:
        agent.stop()


def test_resource_figures():
    line = json.dumps({"CPUPerc": "12.50%", "MemUsage": "512MiB / 31.2GiB", "PIDs": "41"})
    parsed = resources.parse_stats(line)
    assert (parsed["cpu_percent"], parsed["memory_bytes"], parsed["pids"]) == (12.5, 512 * 1024**2, 41)
    steady = [{"memory_bytes": 500_000_000, "cpu_percent": 10}, {"memory_bytes": 520_000_000, "cpu_percent": 30}]
    assert resources.verdict(steady).startswith("memory steady") and "peak CPU 30%" in resources.verdict(steady)
    assert resources.verdict(steady + [{"memory_bytes": 900_000_000, "cpu_percent": 5}]).startswith("MEMORY GREW")
