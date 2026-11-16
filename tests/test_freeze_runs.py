import json
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "dwarf"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from freeze import conditions, repeater, runs  # noqa: E402
from freeze.client import FreezeClient  # noqa: E402
from freeze.session import Session  # noqa: E402
from freeze.testing.fake_agent import FakeAgent  # noqa: E402
from freeze.views import item_view, resolve_field  # noqa: E402
from freeze import cbor_tree, render  # noqa: E402
from test_freeze_tui import roll_forward  # noqa: E402


def header() -> dict:
    return {"kind": "proto_in", "from": "peer:10.0.0.12:3001", "to": "stage:chainsync-39", "label": "chain sync",
            "type": "ouroboros/2", "raw": roll_forward().hex(), "payload": [2], "editable": True, "droppable": True}


def pay(fee: int) -> dict:
    return {"kind": "internal_input", "to": "stage:validate_block-6", "label": "Pay", "payload": {"txs": [{"fee": fee}]},
            "editable": True, "droppable": True}


TRAFFIC = [pay(10), header(), pay(20), header(), pay(30), header(), pay(40)]


def node(agent: FakeAgent, items: list[dict]) -> list:
    results: list = []

    def run() -> None:
        for item in items:
            results.append(agent.offer(dict(item)))

    threading.Thread(target=run, daemon=True).start()
    return results


def wait_for(results: list, count: int) -> None:
    deadline = time.monotonic() + 10
    while len(results) < count and time.monotonic() < deadline:
        time.sleep(0.02)
    assert len(results) == count


def record_a_run(tmp_path: Path) -> tuple[Path, list]:
    """A session in which the second header's slot is edited, the third payment dropped, and a fee changed."""
    agent = FakeAgent(info={"implementation": "amaru", "version": "10.11.0", "network": "testnet_42"})
    agent.start()
    client = FreezeClient("127.0.0.1", agent.port, timeout=5)
    client.connect()
    directory = tmp_path / "sessions" / "20261112-120000"
    session = Session(directory)
    (directory / "session.json").write_text(json.dumps({"node": agent.info}), encoding="utf-8")
    client.on_event(session.record)
    client.set_hold(conditions.compile_condition("kind=proto_in or kind=input"))
    results = node(agent, TRAFFIC)

    def next_held() -> dict:
        return client.wait_held(timeout=5)

    client.forward(next_held()["seq"], then_run=True)            # pay 10: untouched
    client.forward(next_held()["seq"], then_run=True)            # header 1: untouched
    item = next_held()                                            # pay 20: fee -> 1
    client.replace(item["seq"], payload=render.set_at(item["payload"], ("txs", 0, "fee"), 1))
    client.thaw()
    item = next_held()                                            # header 2: slot + 1
    payload, names, _raw = item_view(item)
    path = resolve_field(payload, names, "header_body.slot", loose_bytes=False)
    client.replace(item["seq"], raw=cbor_tree.edit(bytes.fromhex(item["raw"]), path, render.get_at(payload, path) + 1))
    client.thaw()
    client.drop(next_held()["seq"])                               # pay 30: dropped
    client.thaw()
    client.forward(next_held()["seq"], then_run=True)            # header 3
    client.forward(next_held()["seq"], then_run=True)            # pay 40
    wait_for(results, 7)
    time.sleep(0.3)
    client.close()
    session.close()
    agent.stop()
    return directory, results


def test_a_run_is_exported_imported_and_repeated_on_a_fresh_node(tmp_path):
    directory, original = record_a_run(tmp_path)
    assert [verdict for _seq, verdict, _new in original] == ["forward", "forward", "replace", "replace", "drop", "forward", "forward"]

    archive = tmp_path / "out" / "run.tar.gz"
    manifest = runs.export_run(directory, archive, note="three tampers")
    assert (manifest["items"], manifest["interventions"], manifest["node"]["network"]) == (7, 3, "testnet_42")

    stored = runs.import_run(archive, tmp_path / "runs")
    loaded, steps, where = runs.load_run(stored)
    assert where == stored and loaded["note"] == "three tampers"
    assert [(step["action"], step["after"], step["what"]) for step in steps] == [
        ("replace", 1, "Pay"), ("replace", 1, "RollForward"), ("drop", 0, "Pay")]
    assert steps[0]["edits"] == [{"path": ["txs", 0, "fee"], "field": "txs[0].fee", "value": 1}]
    assert steps[1]["edits"][0]["field"] == "header.header.header_body.slot"
    assert steps[1]["edits"][0]["value"] == 138801636 and "$embed" in steps[1]["edits"][0]["path"]
    assert runs.describe(steps[2]).startswith("drop the next Pay")
    assert runs.describe(steps[0]).startswith("after letting 1 through, in the next Pay set txs[0].fee = 1")

    # a fresh node producing the same kind of traffic gets the same treatment
    agent = FakeAgent(start_frozen=True)
    agent.start()
    client = FreezeClient("127.0.0.1", agent.port, timeout=5)
    client.connect()
    results = node(agent, TRAFFIC)
    done = runs.Replayer(client, steps, log=lambda _line: None, stale_negative_control=True).run(timeout=30)
    assert [entry["outcome"].split()[0] for entry in done] == ["edited", "edited", "dropped"]
    assert agent.frozen, "the node is left frozen where the recorded run stopped"
    client.thaw()
    wait_for(results, 7)
    assert [verdict for _seq, verdict, _new in results] == [verdict for _seq, verdict, _new in original]
    assert results[2][2] == original[2][2]
    assert results[3][2] == {**original[3][2], "$stale_negative_control": True}, \
        "the same bytes were sent under the replay's explicit negative-control policy"
    assert agent.conditions == {}
    client.close()
    agent.stop()


def test_run_replay_refuses_a_protected_header_edit_before_replace():
    agent = FakeAgent()
    agent.start()
    item = {**header(), "seq": 41}
    edited = repeater.edited(item, [("header_body.slot", "add:1")])[0]
    assert edited is not None
    step = {
        "action": "replace",
        "edits": [],
        "whole": {"raw": edited.hex()},
    }
    try:
        with FreezeClient("127.0.0.1", agent.port, timeout=5) as client:
            with pytest.raises(runs.RunError, match="Praos VRF proof"):
                runs.Replayer(client, [step], log=lambda _line: None)._apply(step, item)
        assert agent.items == {}, "the refused edit never reached item.replace"
    finally:
        agent.stop()


def test_export_up_to_a_point_keeps_only_what_happened_before_it(tmp_path):
    directory, original = record_a_run(tmp_path)
    second_tamper_item = original[3][0]
    manifest = runs.export_run(directory, tmp_path / "part.tar.gz", until=second_tamper_item)
    assert manifest["last_item"] == second_tamper_item and manifest["interventions"] == 2
    _manifest, steps, _where = runs.load_run(tmp_path / "part.tar.gz", tmp_path / "runs")
    assert [step["action"] for step in steps] == ["replace", "replace"]
    listed = runs.sessions(tmp_path / "sessions")
    assert [(entry["name"], entry["items"], entry["tampers"]) for entry in listed] == [("20261112-120000", 7, 3)]


def test_bad_archives_are_refused(tmp_path):
    (tmp_path / "junk.tar.gz").write_bytes(b"not a tarball")
    with pytest.raises(Exception):
        runs.import_run(tmp_path / "junk.tar.gz", tmp_path / "runs")
    with pytest.raises(runs.RunError):
        runs.export_run(tmp_path / "nothing", tmp_path / "x.tar.gz")
    with pytest.raises(runs.RunError):
        runs.load_run(tmp_path)


def test_run_commands_parse_their_argument():
    from freeze import cli

    parser = cli.build_parser()
    for command in ("import", "open", "ingest"):
        args = parser.parse_args([command, "some-run"])
        assert args.archive == "some-run" and callable(args.run), command
    for argv in (["export", "--until", "5"], ["sessions"], ["payloads", "list"], ["scenarios", "list"],
                 ["template", "t.yaml"], ["devnet", "up"], ["attach"], ["watch"], ["demo"]):
        assert callable(parser.parse_args(argv).run), argv
    assert parser.parse_args(["devnet", "up"]).resign is None
    assert parser.parse_args(["devnet", "up", "--no-resign"]).resign is False


def test_devnet_ingest_uses_the_persisted_restart_configuration(tmp_path, monkeypatch):
    from freeze import cli, devnet

    restarted = []
    monkeypatch.setattr(cli, "_load_run", lambda _archive: ({}, [], tmp_path))
    monkeypatch.setattr(cli, "data_root", lambda: tmp_path)
    monkeypatch.setattr(
        devnet,
        "restart_target",
        lambda root, frozen: restarted.append((root, frozen))
        or {"host": "127.0.0.1", "port": 7373, "token": "secret", "resign": False},
    )

    class Client:
        def __init__(self, *_args, **_kwargs):
            pass

        def connect(self):
            pass

        def close(self):
            pass

    class Replayer:
        def __init__(self, _client, steps):
            assert steps == []

        def run(self, timeout):
            assert timeout == 5
            return []

    monkeypatch.setattr(cli, "FreezeClient", Client)
    monkeypatch.setattr(runs, "Replayer", Replayer)
    args = SimpleNamespace(
        archive="recorded-run", no_restart=False, network="devnet", snapshot=None,
        timeout=3, wait=5, no_attach=True,
    )

    assert cli.cmd_ingest(args) == 0
    assert restarted == [(tmp_path, True)]
