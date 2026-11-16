"""Inject from the UI, the Nodes tab, claiming a session from the UI, and where a devnet node is reached."""

import asyncio
import json
import os
import sys
from pathlib import Path

import pytest

pytest.importorskip("textual")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "dwarf"))

from freeze import devnet  # noqa: E402
from freeze.client import FreezeClient  # noqa: E402
from freeze.testing.fake_agent import FakeAgent  # noqa: E402
from freeze.tui import FreezeApp, InjectScreen  # noqa: E402
from test_freeze_broker import Running  # noqa: E402

INFO = {"implementation": "amaru", "version": "10.11.0", "network": "test", "kinds": {}}


async def settle(pilot, condition, timeout: float = 20.0) -> None:
    waited = 0.0
    while not condition() and waited < timeout:
        await pilot.pause(0.05)
        waited += 0.05
    assert condition()


@pytest.fixture
def agent():
    fake = FakeAgent(info=INFO)
    fake.start()
    yield fake
    fake.stop()


def test_a_message_is_injected_from_the_ui(agent):
    asyncio.run(_a_message_is_injected_from_the_ui(agent))


async def _a_message_is_injected_from_the_ui(agent):
    client = FreezeClient("127.0.0.1", agent.port, timeout=5)
    client.connect()
    app = FreezeApp(client)
    async with app.run_test(size=(200, 60)) as pilot:
        agent.offer({"kind": "internal_input", "to": "stage:fake", "label": "Tick", "type": "Tick", "payload": {"n": 1}}, holdable=False)
        await settle(pilot, lambda: bool(app.session.items))
        await pilot.press("2")
        table = app.query_one("#history-table")
        await settle(pilot, lambda: table.row_count == 1)
        await pilot.press("n")
        await settle(pilot, lambda: isinstance(app.screen, InjectScreen))
        assert app.screen.query_one("#inject-to").value == "stage:fake", "filled in from the selected input"
        assert json.loads(app.screen.query_one("#inject-payload").value) == {"n": 1}
        app.screen.query_one("#inject-payload").value = '{"n": 2}'
        await pilot.press("enter")
        await settle(pilot, lambda: agent.injected == [("stage:fake", {"n": 2})])
        assert app.session.tampers == 1, "injecting counts as tampering"

        # A destination may be named without its `stage:` prefix; one that does not exist is refused.
        await pilot.press("n")
        await settle(pilot, lambda: isinstance(app.screen, InjectScreen))
        app.screen.query_one("#inject-to").value = "nowhere"
        await pilot.press("enter")
        await settle(pilot, lambda: "not a part of the node" in app.message)
        assert len(agent.injected) == 1
    client.close()


def test_the_nodes_tab_lists_every_node_and_moves_between_them(agent):
    other = FakeAgent(info=INFO)
    other.start()
    try:
        asyncio.run(_the_nodes_tab(agent, other))
    finally:
        other.stop()


async def _the_nodes_tab(agent, other):
    client = FreezeClient("127.0.0.1", agent.port, timeout=5)
    client.connect()
    app = FreezeApp(client)
    app.node_name = "amaru-relay-2"
    app.nodes = {"amaru-relay-2": {"host": "127.0.0.1", "port": agent.port, "token": None},
                 "amaru-relay-1": {"host": "127.0.0.1", "port": other.port, "token": None}}
    async with app.run_test(size=(200, 60)) as pilot:
        await pilot.press("8")
        table = app.query_one("#nodes-table")
        await settle(pilot, lambda: table.row_count == 2)
        assert [str(cell) for cell in table.get_row("amaru-relay-1")][2] == "running"
        other.offer({"kind": "proto_out", "to": "peer:10.0.0.9:3001", "label": "RollForward", "type": "ouroboros/32770",
                     "payload": [2]}, holdable=False)
        agent.offer({"kind": "proto_in", "from": "peer:10.0.0.8:3001", "label": "RollForward", "type": "ouroboros/2",
                     "payload": [2]}, holdable=False)
        feed = app.query_one("#nodes-feed")
        await settle(pilot, lambda: feed.row_count == 2)
        assert {str(feed.get_row_at(row)[1]) for row in range(2)} == {"amaru-relay-1", "amaru-relay-2"}

        await pilot.press("Z")
        await settle(pilot, lambda: agent.frozen and other.frozen)
        await settle(pilot, lambda: [str(cell) for cell in table.get_row("amaru-relay-1")][2] == "FROZEN")
        await pilot.press("G")
        await settle(pilot, lambda: not agent.frozen and not other.frozen)

        # What either node holds is listed, and decided from here.
        import threading

        held = app.query_one("#nodes-held")
        threading.Thread(target=lambda: other.offer({"kind": "proto_in", "from": "peer:10.0.0.9:3001", "label": "RollForward", "payload": [2]}),
                         daemon=True).start()
        other.hold = {"op": "kind", "in": ["proto_in"]}
        results = []
        threading.Thread(target=lambda: results.append(other.offer({"kind": "proto_in", "from": "peer:10.0.0.9:3001", "label": "RollBackward", "payload": [3], "droppable": True})),
                         daemon=True).start()
        await settle(pilot, lambda: held.row_count == 1 and str(held.get_row_at(0)[0]) == "amaru-relay-1")
        held.focus()
        await pilot.press("d")
        await settle(pilot, lambda: bool(results) and results[0][1] == "drop")
        await settle(pilot, lambda: held.row_count == 0)
        assert app.session.tampers == 1, "a drop on another node is tampering too"
        other.hold = None
        other.thaw()

        table.focus()
        table.move_cursor(row=1)
        await pilot.press("enter")
        await settle(pilot, lambda: app.switch_to == "amaru-relay-1")
    client.close()


def test_claiming_from_the_ui_makes_other_windows_watch_only(tmp_path):
    setup = Running(tmp_path, auto=False)
    try:
        asyncio.run(_claiming_from_the_ui(setup))
    finally:
        setup.close()


async def _claiming_from_the_ui(setup):
    mine, theirs = setup.client("mine"), setup.client("theirs")
    app = FreezeApp(mine)
    async with app.run_test(size=(200, 60)) as pilot:
        await pilot.press("O")
        await settle(pilot, lambda: app.owner == mine.broker["client"])
        assert "only this window" in str(app.query_one("#status").render())
        theirs.claim(force=True)
        await settle(pilot, lambda: app.owner == theirs.broker["client"])
        await pilot.pause(0.2)
        assert "WATCHING" in str(app.query_one("#status").render())
        await pilot.press("O")
        await settle(pilot, lambda: app.owner == mine.broker["client"])
        await pilot.press("O")
        await settle(pilot, lambda: app.owner is None)
    theirs.close()
    mine.close()


def test_a_devnet_node_is_reached_through_its_broker_when_one_runs(tmp_path):
    home = devnet.directory(tmp_path)
    home.mkdir(parents=True)
    (home / "token").write_text("secret")
    (home / "run.json").write_text(json.dumps({
        "host": "127.0.0.1", "port": 7373, "token_file": str(home / "token"), "target": "amaru-relay-2",
        "nodes": {"amaru-relay-2": 7373, "amaru-relay-1": 7374}, "brokers": {"amaru-relay-2": 7423, "amaru-relay-1": 7424}}))
    assert devnet.current(tmp_path)["port"] == 7373, "no broker process: the agent itself"
    (home / "broker-amaru-relay-2.pid").write_text(str(os.getpid()))
    reached = devnet.current(tmp_path)
    assert (reached["port"], reached["agent_port"], reached["main_port"], reached["brokered"]) == (7423, 7373, 7373, True)
    assert devnet.current(tmp_path, direct=True)["port"] == 7373
    other = devnet.current(tmp_path, "amaru-relay-1")
    assert (other["port"], other["agent_port"], other["main_port"]) == (7374, 7374, 7373), "its broker is not running"


# Rewind ------------------------------------------------------------------------------------------


def test_rewind_repeats_earlier_changes_and_stops_at_the_same_item(tmp_path):
    from freeze import runs
    from test_freeze_runs import TRAFFIC, node, record_a_run, wait_for

    directory, original = record_a_run(tmp_path)
    events = runs.read_events(directory / "events.jsonl")
    # The sixth item is the third header; before it a fee was edited, a slot edited and a payment dropped.
    sixth = original[5][0]
    goal = runs.target(events, sixth)
    assert (goal["what"], goal["before"]) == ("RollForward", 2)
    with pytest.raises(runs.RunError, match="not in this recording"):
        runs.target(events, 9999)

    agent = FakeAgent(start_frozen=True)
    agent.start()
    client = FreezeClient("127.0.0.1", agent.port, timeout=5)
    client.connect()
    results = node(agent, TRAFFIC)
    rewinder = runs.Rewinder(
        client,
        events,
        sixth,
        log=lambda _line: None,
        stale_negative_control=True,
    )
    assert len(rewinder.steps) == 3, "only what was changed before the item is repeated"
    # Every header of this traffic carries the same bytes, so position is what tells them apart.
    result = rewinder.run(timeout=30, by_count=True)
    assert result["reached"] is not None and agent.frozen
    held = client.item(result["reached"])
    assert held["kind"] == "proto_in" and result["reached"] in client.status()["held"]
    assert [verdict for _seq, verdict, _new in results] == ["forward", "forward", "replace", "replace", "drop"], \
        "everything before the item happened again, the item itself has not gone on yet"
    assert agent.conditions == {}
    client.forward(result["reached"], then_run=True)
    client.thaw()
    wait_for(results, 7)
    client.close()
    agent.stop()


def test_rewind_by_content_finds_the_item_that_carries_the_same(tmp_path):
    from freeze import runs
    from test_freeze_runs import node, pay

    recorded = [{"event": "item", "data": {**pay(fee), "seq": number, "held": False}} for number, fee in enumerate((10, 20, 30), start=1)]
    agent = FakeAgent(start_frozen=True)
    agent.start()
    client = FreezeClient("127.0.0.1", agent.port, timeout=5)
    client.connect()
    node(agent, [pay(7), pay(20), pay(30)])
    result = runs.Rewinder(client, recorded, 3, log=lambda _line: None).run(timeout=30)
    assert client.item(result["reached"])["payload"] == {"txs": [{"fee": 30}]}

    again = FakeAgent(start_frozen=True)
    again.start()
    other = FreezeClient("127.0.0.1", again.port, timeout=5)
    other.connect()
    node(again, [pay(1), pay(2)])
    missed = runs.Rewinder(other, recorded, 3, log=lambda _line: None).run(timeout=4)
    assert missed["reached"] is None and missed["seen"] == 2 and again.frozen
    for each in (client, other):
        each.close()
    agent.stop()
    again.stop()


def test_the_start_snapshot_is_due_once_per_frozen_start(tmp_path):
    home = devnet.directory(tmp_path)
    home.mkdir(parents=True)
    (home / "token").write_text("secret")
    run = {"host": "127.0.0.1", "port": 7373, "token_file": str(home / "token"), "target": "amaru-relay-1",
           "started_at": "2026-11-14T10:00:00Z", "frozen_start": True}
    (home / "run.json").write_text(json.dumps(run))
    assert devnet.start_snapshot_due(tmp_path) is True
    devnet.snapshots_dir(tmp_path).mkdir()
    (devnet.snapshots_dir(tmp_path) / "start.json").write_text(json.dumps({"name": "start", "saved_at": "2026-11-13T09:00:00Z"}))
    assert devnet.start_snapshot_due(tmp_path) is True, "a snapshot from an earlier devnet does not count"
    (devnet.snapshots_dir(tmp_path) / "start.json").write_text(json.dumps({"name": "start", "saved_at": "2026-11-14T10:20:00Z"}))
    assert devnet.start_snapshot_due(tmp_path) is False
    (home / "run.json").write_text(json.dumps({**run, "frozen_start": False}))
    (devnet.snapshots_dir(tmp_path) / "start.json").unlink()
    assert devnet.start_snapshot_due(tmp_path) is False, "a node that was not started frozen has already moved on"


def test_the_broker_keeps_clients_out_while_it_prepares_the_node(tmp_path):
    import threading
    import time

    from freeze.client import FreezeError
    from test_freeze_broker import until

    release = threading.Event()
    calls = []

    def prepare() -> bool:
        calls.append(time.time())
        release.wait(10)
        return False

    setup = Running.__new__(Running)
    setup.prepare = prepare
    Running.__init__(setup, tmp_path, auto=False, frozen=True, wait=False)
    try:
        until(lambda: bool(calls))
        waiting = FreezeClient("127.0.0.1", setup.port, token="secret", timeout=5.0)
        with pytest.raises(FreezeError) as refused:
            waiting.connect()
        assert refused.value.code == "not_ready"
        waiting.close()
        release.set()
        until(lambda: not setup.broker.preparing and setup.broker.directory is not None)
        with setup.client() as client:
            assert client.status()["frozen"] is True
    finally:
        release.set()
        setup.close()


# Source lines ------------------------------------------------------------------------------------


def fake_debugger(tmp_path):
    from freeze import source

    code = tmp_path / "decide.rs"
    code.write_text("".join(f"line {number}\n" for number in range(1, 41)))
    driver = Path(__file__).resolve().parent / "support" / "fake_lldb_driver.py"
    return source.Debugger([sys.executable, str(driver), str(code)], timeout=10)


def test_the_debugger_is_driven_line_by_line(tmp_path):
    from freeze import source

    debugger = fake_debugger(tmp_path)
    assert debugger.start()["lldb"] == "fake"
    assert debugger.attach()["pid"] == 42
    first = debugger.wait_stopped(5)
    assert first["at"]["line"] == 3 and debugger.stopped is first and not debugger.running
    assert debugger.call("break", file="decide.rs", line=10) == {"id": 1, "places": 1}
    with pytest.raises(source.SourceError, match="no code there"):
        debugger.call("break", file="decide.rs", line=999)
    debugger.call("next")
    assert debugger.wait_stopped(5)["at"]["line"] == 4
    debugger.call("continue")
    stop = debugger.wait_stopped(5)
    assert (stop["at"]["line"], stop["reason"]) == (7, "breakpoint 1.1")
    shown = source.listing(debugger.call("source", file=stop["at"]["file"], line=7, around=2), 7)
    assert shown == [(5, "line 5", False), (6, "line 6", False), (7, "line 7", True), (8, "line 8", False), (9, "line 9", False)]
    assert debugger.call("vars") == [{"name": "tip", "type": "Point", "value": "slot 7"}]
    debugger.close()
    assert not debugger.attached
    with pytest.raises(source.SourceError):
        debugger.call("where")


def test_the_tools_image_runs_beside_the_node():
    from freeze import source

    command = source.command_for("abc123", "dwarf/freeze-amaru:x-static-dbg")
    assert command[-1] == "dwarf/freeze-amaru:x-static-dbg-tools"
    assert command[command.index("--pid") + 1] == "container:abc123"
    assert command[command.index("--volumes-from") + 1] == "abc123:ro", "the node's binary is read where the node runs it from"
    assert "SYS_PTRACE" in command and command[command.index("--network") + 1] == "none"


def test_a_debug_build_keeps_full_information_for_the_nodes_own_code(tmp_path):
    from freeze import build

    (tmp_path / "crates" / "amaru-consensus").mkdir(parents=True)
    (tmp_path / "crates" / "amaru-consensus" / "Cargo.toml").write_text('[package]\nname = "amaru-consensus"\n')
    (tmp_path / "Cargo.toml").write_text('[workspace]\nmembers = ["crates/*"]\n\n[profile.dev]\nopt-level = 2\ndebug = false\n\n[profile.release]\nlto = true\n')
    build.keep_debug_info(tmp_path)
    text = (tmp_path / "Cargo.toml").read_text()
    assert '[profile.dev]\nopt-level = 2\ndebug = "line-tables-only"\n' in text and "debug = false" not in text
    assert "[profile.dev.package.amaru-consensus]\ndebug = true\n" in text
    assert "[profile.release]\nlto = true" in text, "other profiles are left alone"


def test_the_source_tab_attaches_steps_and_detaches(agent, tmp_path):
    asyncio.run(_the_source_tab(agent, tmp_path))


async def _the_source_tab(agent, tmp_path):
    client = FreezeClient("127.0.0.1", agent.port, timeout=5)
    client.connect()
    app = FreezeApp(client)
    async with app.run_test(size=(200, 60)) as pilot:
        await pilot.press("9")
        await pilot.press("s")
        await settle(pilot, lambda: "press A" in app.message)
        await pilot.press("A")
        await settle(pilot, lambda: "needs a node" in app.message)
        app.source_factory = lambda: fake_debugger(tmp_path)
        await pilot.press("A")
        await settle(pilot, lambda: app.debugger is not None and app.debugger.stopped is not None)
        code = app.query_one("#src-code-text")
        await settle(pilot, lambda: "→     3  line 3" in str(code.render()))
        assert "STOPPED IN THE DEBUGGER" in str(app.query_one("#status").render())
        assert client.timeout == 2.0, "other tabs must not wait long for a stopped agent"
        assert app.query_one("#src-vars").row_count == 1 and app.query_one("#src-stack").row_count == 2
        await pilot.press("n")
        await settle(pilot, lambda: "→     4  line 4" in str(code.render()))
        await pilot.press("c")
        await settle(pilot, lambda: "→     7  line 7" in str(code.render()) and "breakpoint 1.1" in str(code.render()))
        await pilot.press("A")
        await settle(pilot, lambda: not app.debugger.attached)
        await settle(pilot, lambda: client.timeout == 5)
        assert "STOPPED IN THE DEBUGGER" not in str(app.query_one("#status").render())
    client.close()


# Applier -----------------------------------------------------------------------------------------


def three_trees(tmp_path):
    """The tree the hooks were written for, that tree with hooks, and a later revision."""
    base, hooked, later = tmp_path / "base", tmp_path / "hooked", tmp_path / "later"
    for tree in (base, hooked, later):
        (tree / "src").mkdir(parents=True)
    body = "".join(f"fn step_{number}() {{}}\n" for number in range(1, 13))
    (base / "src" / "gate.rs").write_text(body)
    (hooked / "src" / "gate.rs").write_text(body.replace("fn step_6() {}\n", "fn step_6() { freeze::gate(); }\n"))
    (later / "src" / "gate.rs").write_text("// reworked upstream\n" + body.replace("fn step_12() {}\n", "fn step_12() { more(); }\n"))
    (base / "src" / "same.rs").write_text("fn same() {}\n")
    (hooked / "src" / "same.rs").write_text("fn same() { freeze::probe(); }\n")
    (later / "src" / "same.rs").write_text("fn same() {}\n")
    (hooked / "src" / "freeze.rs").write_text("pub fn gate() {}\n")
    (base / "src" / "clash.rs").write_text("fn clash() { old(); }\n")
    (hooked / "src" / "clash.rs").write_text("fn clash() { freeze::probe(); old(); }\n")
    (later / "src" / "clash.rs").write_text("fn clash() { new(); }\n")
    return base, hooked, later


def test_hooks_are_merged_into_a_later_revision_group_by_group(tmp_path):
    import shutil

    from freeze import applier

    base, hooked, later = three_trees(tmp_path)
    destination = tmp_path / "out"
    shutil.copytree(later, destination)
    report = applier.Report()
    gate = applier.Group("gate", ["src/gate.rs", "src/same.rs", "src/freeze.rs"], mandatory=True)
    assert applier.apply_group(gate, later, base, hooked, destination, report) is None
    merged = (destination / "src" / "gate.rs").read_text()
    assert "// reworked upstream" in merged and "freeze::gate()" in merged and "more()" in merged, "both sides' changes are kept"
    assert (destination / "src" / "same.rs").read_text() == "fn same() { freeze::probe(); }\n"
    assert (destination / "src" / "freeze.rs").exists()
    assert (report.merged, report.copied) == (1, 2)

    clash = applier.Group("probes", ["src/clash.rs", "src/same.rs"])
    before = (destination / "src" / "same.rs").read_text()
    why = applier.apply_group(clash, later, base, hooked, destination, report)
    assert "changed both upstream and by the hooks" in why
    assert (destination / "src" / "clash.rs").read_text() == "fn clash() { new(); }\n", "a group is applied whole or not at all"
    assert (destination / "src" / "same.rs").read_text() == before

    applier.revert_group(gate, later, destination)
    assert (destination / "src" / "gate.rs").read_text() == (later / "src" / "gate.rs").read_text()
    assert not (destination / "src" / "freeze.rs").exists(), "a file the hooks added goes away with them"


def test_compiler_errors_are_found_by_file_and_the_toolchain_is_read_from_the_tree(tmp_path):
    from freeze import applier

    output = ("    Checking amaru-consensus v0.1.0\n"
              "crates/amaru-consensus/src/stages/select_chain/mod.rs:120:9: error[E0425]: cannot find value `tip` in this scope\n"
              "crates/amaru-consensus/src/stages/select_chain/mod.rs:140:1: warning: unused\n"
              "error: crates/amaru-node/src/freeze_stores.rs:12:5: error: unresolved import\n"
              "error: could not compile `amaru-consensus` (lib) due to 1 previous error\n")
    assert applier.errors_by_file(output) == {
        "crates/amaru-consensus/src/stages/select_chain/mod.rs": ["line 120: cannot find value `tip` in this scope"],
        "crates/amaru-node/src/freeze_stores.rs": ["line 12: unresolved import"]}
    assert applier.toolchain_of(tmp_path, "nightly-2026-09-04") == "nightly-2026-09-04"
    (tmp_path / "rust-toolchain.toml").write_text('[toolchain]\nchannel = "nightly-2026-10-20"\n')
    assert applier.toolchain_of(tmp_path, "nightly-2026-09-04") == "nightly-2026-10-20"


# Function tracing and store writes ---------------------------------------------------------------


def test_functions_are_traced_by_whole_path_segments(agent, capsys):
    from freeze import cli

    agent.functions = {"amaru_consensus::stages::select_chain::SelectChain::decide": 12,
                       "amaru_consensus::stages::select_chain_extra::other": 3, "amaru_ledger::rules::block::execute": 0}
    with FreezeClient("127.0.0.1", agent.port, timeout=5) as client:
        assert client.trace_enable("amaru_consensus::stages::select_chain") == 1, "a prefix ends at a `::`"
        assert client.trace_enable("amaru-ledger") == 1, "a crate may be named as it is written in Cargo.toml"
        listed = client.traces("amaru_consensus")
        assert [(site["name"].rsplit("::", 1)[1], site["enabled"]) for site in listed["sites"]] == [("decide", True), ("other", False)]
        assert client.trace_disable() == 2 and client.traces()["enabled"] == []
    parser = cli.build_parser()
    args = parser.parse_args(["trace", "--port", str(agent.port), "--enable", "amaru_ledger", "--hit"])
    assert args.run(args) == 0
    out = capsys.readouterr().out
    assert "1 function(s) now traced by `amaru_ledger`" in out and "traced: amaru_ledger" in out
    assert "decide" in out and "block::execute" not in out, "--hit lists only what was called"


def test_a_store_that_writes_later_says_so(agent):
    agent.state["ledger/pots"] = {"pots": {"treasury": 1}}
    agent.receipts["ledger/pots"] = {"queued": True, "applied": "by the ledger before its next block", "scope": "the ledger as stored"}
    with FreezeClient("127.0.0.1", agent.port, timeout=5) as client:
        assert client.state_put("ledger/pots/pots", {"treasury": 2})["queued"] is True
        assert client.state_put("stages/x", {"a": 1}) == {} if "stages" in agent.state else True


def test_the_spec_describes_the_stored_hooks_and_porting_to_the_same_revision_reproduces_them(tmp_path):
    from freeze import applier, build
    from test_freeze_build import AMARU

    recipe = build.choose_recipe(AMARU, "v10.11.20260925")
    groups, _spec = applier.load_spec()
    assert applier.check_spec(recipe, groups) == []
    assert [group.name for group in groups if group.mandatory] == ["gate", "start"]

    checked = []

    def check(tree, toolchain):
        checked.append(toolchain)
        return True, ""

    report = applier.port(AMARU, AMARU, tmp_path / "recipes", recipe, "same", check=check, log=lambda _line: None)
    assert report.applied == [group.name for group in groups] and not report.dropped and not report.missing
    assert report.merged == 0, "nothing differs from the base, so nothing needs merging"
    assert checked == ["nightly-2026-09-04"]
    stored = recipe.patches[0].read_text()
    made = (tmp_path / "recipes" / "same" / "0001-freeze-hooks.patch").read_text()

    def without_lock(patch: str) -> list[str]:
        parts = ("\n" + patch).split("\ndiff --git ")[1:]
        return sorted(part for part in parts if not part.startswith("a/Cargo.lock "))

    assert without_lock(made) == without_lock(stored)
    manifest = json.loads((tmp_path / "recipes" / "same" / "manifest.json").read_text())
    assert manifest["ported_from"] == "v10.11.20260925" and manifest["missing"] == [] and manifest["source"] == "same"

    # A hook that does not compile in the other revision is left out if a build can do without it…
    def ledger_fails(tree, toolchain):
        if (tree / "crates/amaru-ledger/src/lib.rs").read_text() != (AMARU / "crates/amaru-ledger/src/lib.rs").read_text():
            return False, "crates/amaru-ledger/src/state.rs:10:1: error[E0432]: unresolved import\n"
        return True, ""

    partial = applier.port(AMARU, AMARU, tmp_path / "recipes", recipe, "partial", check=ledger_fails, log=lambda _line: None)
    assert "ledger-probes" in partial.dropped and partial.missing == ["probes:ledger"] and partial.rounds == 2
    assert "amaru-ledger/src" not in (tmp_path / "recipes" / "partial" / "0001-freeze-hooks.patch").read_text()

    # …and stops the port if it cannot.
    with pytest.raises(applier.PortError, match="`gate` is needed"):
        applier.port(AMARU, AMARU, tmp_path / "recipes", recipe, "broken", log=lambda _line: None,
                     check=lambda tree, toolchain: (False, "crates/amaru-pure-stage/src/tokio.rs:1:1: error: nope\n"))


def test_a_release_must_explicitly_name_spec_files_already_supplied_upstream(tmp_path):
    from freeze import applier, build

    patch = tmp_path / "hooks.patch"
    patch.write_text("diff --git a/changed.rs b/changed.rs\n", encoding="utf-8")
    group = applier.Group("hook", ["changed.rs", "already.rs"])

    def recipe(inherited=()):
        return build.Recipe("later", tmp_path, {"implementation": "amaru", "patches": [patch.name],
                                                "inherited": list(inherited)})

    assert applier.check_spec(recipe(), [group]) == ["already.rs is in the spec but the patch does not touch it"]
    assert applier.check_spec(recipe(["already.rs"]), [group]) == []
    assert applier.check_spec(recipe(["changed.rs"]), [group]) == [
        "changed.rs is declared inherited but the patch touches it",
        "already.rs is in the spec but the patch does not touch it",
    ]


def test_conflicts_can_be_left_for_a_person_and_the_port_finished_afterwards(tmp_path):
    import shutil

    from freeze import applier

    base, hooked, later = three_trees(tmp_path)
    destination = tmp_path / "out"
    shutil.copytree(later, destination)
    report = applier.Report()
    clash = applier.Group("probes", ["src/clash.rs", "src/same.rs"])
    assert applier.apply_group(clash, later, base, hooked, destination, report, by_hand=True) is None
    assert report.conflicts == {"probes": ["src/clash.rs"]}
    assert applier.unresolved(destination, [clash]) == ["src/clash.rs"]
    (destination / "src" / "clash.rs").write_text("fn clash() { freeze::probe(); new(); }\n")
    assert applier.unresolved(destination, [clash]) == []


# Third round: any node, frozen together, capabilities, crates per recipe -------------------------


def two_node_devnet(tmp_path):
    home = devnet.directory(tmp_path)
    home.mkdir(parents=True)
    (home / "token").write_text("secret")
    profile = devnet.find_profile("relay-pair")
    from dataclasses import asdict

    (home / "profile.json").write_text(json.dumps(asdict(profile)))
    (home / "run.json").write_text(json.dumps({
        "host": "127.0.0.1", "port": 7373, "token_file": str(home / "token"), "target": profile.target, "image": "img",
        "nodes": devnet.node_ports(profile, 7373), "started_at": "2026-11-14T10:00:00Z", "frozen_start": True}))
    return profile


def test_each_freeze_node_has_its_own_start_snapshot_and_takes_only_its_own_chain_back(tmp_path, monkeypatch):
    profile = two_node_devnet(tmp_path)
    other = next(name for name in profile.nodes if name != profile.target)
    assert devnet.start_name(tmp_path) == "start" and devnet.start_name(tmp_path, other) == f"start-{other}"
    assert devnet.start_snapshot_due(tmp_path, other) is True
    folder = devnet.snapshots_dir(tmp_path)
    (folder / f"start-{other}").mkdir(parents=True)
    (folder / f"start-{other}.json").write_text(json.dumps({"name": f"start-{other}", "target": other, "saved_at": "2026-11-14T10:05:00Z"}))
    assert devnet.start_snapshot_due(tmp_path, other) is False and devnet.start_snapshot_due(tmp_path) is True
    with pytest.raises(devnet.DevnetError, match="a node only takes its own chain back"):
        devnet.snapshot_restore(tmp_path, f"start-{other}", node=profile.target)
    with pytest.raises(devnet.DevnetError, match="not a Freeze node of this devnet"):
        devnet.start_name(tmp_path, "p1")

    restarted = []
    monkeypatch.setattr(devnet, "state_volume", lambda _root, **more: (f"volume-of-{more.get('node')}", "img"))
    monkeypatch.setattr(devnet, "_copy", lambda *_args, **_kwargs: "")
    monkeypatch.setattr(devnet, "_compose", lambda *_args, **_kwargs: type("Done", (), {"returncode": 0, "stdout": "", "stderr": ""})())
    monkeypatch.setattr(devnet, "restart_target", lambda _root, frozen=True, node=None: restarted.append(node) or {})
    devnet.snapshot_restore(tmp_path, f"start-{other}")
    assert restarted == [other], "without a node named, a snapshot goes back to the node it is of"


def test_the_main_brokers_arguments_name_the_other_freeze_nodes(tmp_path, monkeypatch):
    from freeze import broker

    profile = two_node_devnet(tmp_path)
    started = {}
    monkeypatch.setattr(broker, "start", lambda home, name, arguments: started.__setitem__(name, arguments) or 1)
    info = devnet.current(tmp_path, direct=True)
    ports = devnet.start_brokers(tmp_path, info, auto_pause=True, record="held", freeze_together=True)
    other = next(name for name in profile.nodes if name != profile.target)
    assert ports == {profile.target: 7423, other: 7424}
    main, second = started[profile.target], started[other]
    assert "--pause-peers" in main and "--freeze-together" in main and main[main.index("--also") + 1] == f"{other}=127.0.0.1:7374"
    assert "--pause-peers" not in second and "--also" not in second, "only the main node's broker moves the others"
    assert "--start-snapshot" in main and "--start-snapshot" in second and second[second.index("--record") + 1] == "held"


def test_freeze_nodes_are_frozen_and_thawed_together_but_not_one_frozen_by_hand():
    from freeze.broker import Peers

    first, second = FakeAgent(info=INFO), FakeAgent(info=INFO)
    for fake in (first, second):
        fake.start()
    second.frozen = True  # someone froze this one before
    peers = Peers(lambda: None, lambda: None, auto=True, together=True,
                  others={"one": ("127.0.0.1", first.port), "two": ("127.0.0.1", second.port), "gone": ("127.0.0.1", 1)})

    async def run():
        await peers.set(True)
        assert first.frozen and peers.froze == {"one"}
        await peers.set(False)
        assert not first.frozen and second.frozen, "the one frozen by hand stays frozen"
        assert peers.status()["froze"] == [] and peers.status()["freeze_nodes"] is True
        # A devnet started frozen: the nodes found frozen with the main one run with it.
        await peers.set(True)
        await peers.set(False)
        peers.paused = False
        await peers.set(True, adopt=True)
        assert peers.froze == {"one", "two"}
        await peers.set(False)
        assert not first.frozen and not second.frozen

    asyncio.run(run())
    for fake in (first, second):
        fake.stop()


def test_a_node_claims_only_what_its_image_was_built_with(tmp_path):
    profile = devnet.find_profile("relay")
    made = devnet.override("img", "token", profile=profile, capabilities="internal_input,probes:consensus")
    assert made["services"][profile.target]["environment"]["DWARF_FREEZE_CAPABILITIES"] == "internal_input,probes:consensus"
    assert "DWARF_FREEZE_CAPABILITIES" not in devnet.override("img", "token", profile=profile)["services"][profile.target]["environment"]
    client = FreezeClient.__new__(FreezeClient)
    client.hello = {"node": {"features": ["probes:consensus", "trace"]}}
    assert client.supports("probes") and client.supports("trace") and not client.supports("events:filter")


def test_a_recipe_may_bring_its_own_agent_crates(tmp_path):
    from freeze import applier, build

    shared = build.Recipe("x", tmp_path, {"implementation": "amaru", "crate": "amaru-freeze", "crates": ["amaru-freeze", "amaru-freeze-macros"]})
    assert [crate.parent for crate in shared.crates] == [build.TARGETS / "amaru" / "crate"] * 2
    own = build.Recipe("y", tmp_path, {**shared.manifest, "crate_dir": "crate"})
    assert [crate.parent for crate in own.crates] == [tmp_path / "crate"] * 2
    (tmp_path / "a" / "src").mkdir(parents=True)
    (tmp_path / "b" / "src").mkdir(parents=True)
    (tmp_path / "a" / "src" / "lib.rs").write_text("one")
    (tmp_path / "b" / "src" / "lib.rs").write_text("one")
    (tmp_path / "b" / "target").mkdir()
    (tmp_path / "b" / "target" / "junk").write_text("built")
    assert applier._tree_differs(tmp_path / "a", tmp_path / "b") is False, "build output does not count"
    (tmp_path / "b" / "src" / "lib.rs").write_text("two")
    assert applier._tree_differs(tmp_path / "a", tmp_path / "b") is True


def test_on_windows_only_the_client_side_is_offered():
    from freeze import cli

    parser = cli.build_parser()
    assert cli.needs_posix(parser.parse_args(["devnet", "up"]), "linux") is None
    for argv in (["devnet", "up"], ["build", "--source", "x"], ["run", "--network", "preview"], ["rewind", "--to", "1"], ["source"]):
        assert "WSL 2" in cli.needs_posix(parser.parse_args(argv), "win32"), argv
    assert "--host" in cli.needs_posix(parser.parse_args(["attach", "--network", "devnet"]), "win32")
    for argv in (["attach", "--host", "10.0.0.5", "--port", "7373", "--token-file", "t"], ["watch", "--port", "7373"], ["demo"],
                 ["probes", "--port", "7373"], ["search", "kind=proto_in"]):
        assert cli.needs_posix(parser.parse_args(argv), "win32") is None, argv


def test_a_later_release_is_started_under_its_own_words(tmp_path):
    import subprocess

    assert "amaru.real" not in devnet.extract_script() and "amaru.real" not in devnet.extract_script("run")
    script = devnet.extract_script("node run")
    target = tmp_path / "target"
    target.mkdir()
    node = tmp_path / "amaru"
    node.write_text('#!/bin/sh\necho "node got: $*"\n')
    done = subprocess.run(["sh", "-ec", script.replace("/usr/local/bin/amaru", str(node)).replace("/target", str(target))],
                          capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    said = subprocess.run([str(target / "amaru"), "run", "--network", "testnet_42", "--peer-address", "p1:3001"], capture_output=True, text=True)
    assert said.stdout.strip() == "node got: node run --network testnet_42 --peer-address p1:3001"
    other = subprocess.run([str(target / "amaru"), "--version"], capture_output=True, text=True)
    assert other.stdout.strip() == "node got: --version", "anything else is passed on as it is"


def test_hooks_are_placed_by_what_surrounds_them_when_a_merge_gives_up(tmp_path):
    import shutil

    from freeze import applier

    base = """fn first() {
    let a = one();
    let b = two(a);
    done(b)
}

fn second() {
    let a = one();
    done(a)
}
"""
    hooked = base.replace("    let b = two(a);\n", "    let b = two(a);\n    freeze::probe(\"first.two\", b);\n")
    # Upstream rewrote the line right after the hook's place, which a merge treats as the same place.
    theirs = base.replace("    done(b)\n", "    finish(b, later())\n").replace("fn second() {\n", "/// Now documented.\nfn second() {\n")
    placed, why = applier.place_by_anchor(base, hooked, theirs)
    assert why == "" and placed.count("freeze::probe") == 1
    assert "    let b = two(a);\n    freeze::probe(\"first.two\", b);\n    finish(b, later())\n" in placed

    # The same line stands in two functions: the hook goes into the one it was written for.
    in_both = base.replace("    done(a)\n", "    let b = two(a);\n    done(b)\n")
    hooked_both = in_both.replace("fn first() {\n    let a = one();\n    let b = two(a);\n",
                                  "fn first() {\n    let a = one();\n    let b = two(a);\n    freeze::probe(\"first.two\", b);\n")
    placed, why = applier.place_by_anchor(in_both, hooked_both, in_both.replace("fn second", "// moved\nfn second"))
    assert why == "" and placed.index("freeze::probe") < placed.index("fn second")

    # The lines on both sides of the hook are gone: nothing is placed, and the reason names the function.
    gone, why = applier.place_by_anchor(base, hooked, base.replace("    let b = two(a);\n    done(b)\n", "    both(a)\n"))
    assert gone is None and "`first`" in why
    # A line the hooks changed that upstream changed too cannot be placed either.
    changed = base.replace("    done(a)\n", "    freeze::gate(); done(a)\n")
    assert applier.place_by_anchor(base, changed, base.replace("    done(a)\n", "    done_differently(a)\n"))[0] is None

    trees = {name: tmp_path / name for name in ("base", "hooked", "later", "out")}
    for name, text in (("base", base), ("hooked", hooked), ("later", theirs)):
        (trees[name] / "src").mkdir(parents=True)
        (trees[name] / "src" / "probe.rs").write_text(text)
    shutil.copytree(trees["later"], trees["out"])
    report = applier.Report()
    group = applier.Group("probes", ["src/probe.rs"])
    assert applier._merge(trees["later"] / "src/probe.rs", trees["base"] / "src/probe.rs", trees["hooked"] / "src/probe.rs")[1] > 0, \
        "this is a case a three-way merge cannot do"
    assert applier.apply_group(group, trees["later"], trees["base"], trees["hooked"], trees["out"], report) is None
    assert (report.merged, report.anchored) == (1, 1) and "freeze::probe" in (trees["out"] / "src" / "probe.rs").read_text()


def test_a_schedule_is_moved_by_milliseconds_from_now():
    from freeze import views

    item = {"label": "schedule", "payload": {"at": "203.874512s", "message": {"Local": "SendKeepAlive"}}}
    assert views.as_sent(item, {"at": "50", "message": {"Local": "SendKeepAlive"}}) == {"at_ms": 50}
    assert views.as_sent(item, {"at": "203.874512s", "message": {"Local": "Other"}}) == {"message": {"Local": "Other"}}
    assert views.as_sent(item, {"at": " 1200 ", "message": {"Local": "Other"}}) == {"at_ms": 1200, "message": {"Local": "Other"}}
    assert views.as_sent(item, {"at": "soon", "message": {}})["at"] == "soon", "what is not a number is left for the node to refuse"
    other = {"label": "wait", "payload": {"ms": 5}}
    assert views.as_sent(other, {"ms": 1}) == {"ms": 1}


def test_an_unsafe_ledger_write_has_to_be_confirmed_by_typing_so(agent):
    asyncio.run(_an_unsafe_write(agent))


async def _an_unsafe_write(agent):
    from freeze.tui import EditScreen

    agent.state["ledger/accounts"] = {"key-ab": {"deposit": 2, "rewards": 0, "pool": None, "drep": None}}
    refuse = agent._state

    def guarded(method, params):
        if method == "state.put" and params["path"].startswith("ledger/accounts") and not params["value"].get("unsafe"):
            raise ValueError('ledger/accounts is only written with `"unsafe": true` in the value: the node\'s later behaviour is evidence of nothing but this edit')
        return refuse(method, params)

    agent._state = guarded
    client = FreezeClient("127.0.0.1", agent.port, timeout=5)
    client.connect()
    app = FreezeApp(client)
    async with app.run_test(size=(200, 60)) as pilot:
        await pilot.press("3")
        app.state_path, app.state_edit = "ledger/accounts/key-ab", {"deposit": 2, "rewards": 77, "pool": None, "drep": None}
        await pilot.click("#state-apply")
        await settle(pilot, lambda: isinstance(app.screen, EditScreen))
        app.screen.query_one("#dialog-input").value = "no"
        await pilot.press("enter")
        await settle(pilot, lambda: app.message == "not written")
        assert agent.state["ledger/accounts"]["key-ab"]["rewards"] == 0
        await pilot.click("#state-apply")
        await settle(pilot, lambda: isinstance(app.screen, EditScreen))
        app.screen.query_one("#dialog-input").value = "unsafe"
        await pilot.press("enter")
        await settle(pilot, lambda: agent.state["ledger/accounts"]["key-ab"].get("rewards") == 77)
        assert agent.state["ledger/accounts"]["key-ab"]["unsafe"] is True and app.session.tampers == 1
    client.close()


def test_rewind_finds_an_item_recorded_after_a_higher_numbered_one():
    from freeze import runs
    from test_freeze_runs import pay

    def item(number, fee):
        return {"event": "item", "data": {**pay(fee), "seq": number, "held": False}}

    # A node numbers items on several threads, so a recording can hold 4 before 3.
    events = [item(1, 10), item(2, 20), item(4, 40), item(3, 30), item(5, 50)]
    goal = runs.target(events, 3)
    assert (goal["seq"], goal["before"]) == (3, 2), "the two items numbered below it came before it"
    assert runs.target(events, 5)["before"] == 4
    # A change to item 3 is still found when item 4 was recorded first.
    changed = events + [{"event": "released", "data": {"seq": 3, "verdict": "drop"}}]
    assert [step["seq"] for step in runs.interventions(changed, 3)] == [3]


def test_rewind_says_so_when_the_item_went_by_while_earlier_changes_were_repeated():
    from freeze import runs
    from test_freeze_runs import node, pay

    def item(number, fee):
        return {"event": "item", "data": {**pay(fee), "seq": number, "held": False}}

    # Recorded: one payment let through, the next dropped, then the payment to go back to.
    recorded = [item(1, 10), item(2, 20), {"event": "released", "data": {"seq": 2, "verdict": "drop"}}, item(3, 30)]
    agent = FakeAgent(start_frozen=True)
    agent.start()
    client = FreezeClient("127.0.0.1", agent.port, timeout=5)
    client.connect()
    # This time the node delivers that payment first, so it goes by before the drop is repeated.
    node(agent, [pay(30), pay(5), pay(6)])
    result = runs.Rewinder(client, recorded, 3, log=lambda _line: None).run(timeout=6)
    assert result["reached"] is None, "an item that has gone on is not reported as held"
    assert result["passed"] is not None and result["passed"] not in client.status()["held"]
    assert agent.frozen
    client.close()
    agent.stop()
