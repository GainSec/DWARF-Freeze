import asyncio
import sys
import threading
from pathlib import Path

import pytest

pytest.importorskip("textual")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "dwarf"))

from freeze.client import FreezeClient  # noqa: E402
from freeze.testing.fake_agent import FakeAgent  # noqa: E402
from freeze.tui import FreezeApp  # noqa: E402


@pytest.fixture
def agent():
    fake = FakeAgent(info={"implementation": "amaru", "version": "10.11.0", "network": "test", "kinds": {}})
    fake.resigning = {"enabled": True, "available": True}
    fake.state["stages"] = {"validate_block-6": {"type": "ValidateBlock", "payload": {"seen": 3}}}
    fake.start()
    yield fake
    fake.stop()


def block(fee: int = 301204) -> dict:
    return {"kind": "proto_in", "from": "peer:10.0.0.12:3001", "to": "stage:blockfetch-43", "label": "Block",
            "type": "amaru_protocols::blockfetch::messages::Message", "editable": True, "droppable": True,
            "payload": {"header": {"slot": 91440112, "vrf_proof": {"$bytes": "80b1" * 20}}, "transactions": [{"fee": fee}]}}


def offer(agent: FakeAgent, item: dict) -> list:
    result: list = []
    threading.Thread(target=lambda: result.append(agent.offer(item)), daemon=True).start()
    return result


async def settle(pilot, condition, timeout: float = 20.0) -> None:
    waited = 0.0
    while not condition() and waited < timeout:
        await pilot.pause(0.05)
        waited += 0.05
    assert condition()


def test_hold_edit_a_field_and_forward(agent):
    asyncio.run(_hold_edit_a_field_and_forward(agent))


async def _hold_edit_a_field_and_forward(agent):
    client = FreezeClient("127.0.0.1", agent.port, timeout=5)
    client.connect()
    app = FreezeApp(client)
    async with app.run_test(size=(180, 50)) as pilot:
        assert "Header/body repair is on" in str(app.query_one("#resign-toggle").label)
        await pilot.press("s")
        await settle(pilot, lambda: agent.resigning["enabled"] is False)
        assert "Header/body repair is off" in str(app.query_one("#resign-toggle").label)
        await pilot.press("i")
        await settle(pilot, lambda: agent.hold is not None)
        assert set(agent.hold["in"]) == {"proto_in", "proto_out"}, "by default only decoded peer traffic is held"

        result = offer(agent, block())
        await settle(pilot, lambda: app.selected is not None)
        tree = app.query_one("#item")
        labels = [node.label.plain for node in tree.root.children[0].children]
        assert labels[0] == "slot  91,440,112"
        assert labels[1].startswith("vrf proof  80b180b1…"), labels

        tree.focus()
        fee = next(node for node in _walk(tree.root) if node.data is not None and node.data.name == "fee")
        tree.move_cursor(fee)
        await settle(pilot, lambda: tree.cursor_node is fee)
        await pilot.press("e")
        await pilot.pause(0.1)
        for _ in range(6):
            await pilot.press("backspace")
        await pilot.press("1", "enter")
        await settle(pilot, lambda: app.selected in app.edits)
        assert "CHANGED" in next(n for n in _walk(tree.root) if n.data is not None and n.data.name == "fee").label.plain
        assert result == [], "nothing reaches the node until Forward"

        await pilot.press("f")
        await settle(pilot, lambda: bool(result))
        seq, verdict, replacement = result[0]
        assert verdict == "replace" and replacement["transactions"] == [{"fee": 1}]
        await settle(pilot, lambda: app.session.tampers == 1 and not app.session.frozen)

        await pilot.press("2")
        await settle(pilot, lambda: app.query_one("#history-table").row_count == 1)
        await pilot.pause(0.5)
        assert app.query_one("TabbedContent").active == "history", "the tab key switches tabs and it stays switched"
        assert app.session.verdict(seq) == "edited"
    client.close()


def test_drop_and_state_edit(agent):
    asyncio.run(_drop_and_state_edit(agent))


async def _drop_and_state_edit(agent):
    client = FreezeClient("127.0.0.1", agent.port, timeout=5)
    client.connect()
    app = FreezeApp(client)
    async with app.run_test(size=(180, 50)) as pilot:
        await pilot.press("z")
        await settle(pilot, lambda: agent.frozen)
        result = offer(agent, block())
        await settle(pilot, lambda: app.selected is not None)
        await pilot.press("d")
        await settle(pilot, lambda: bool(result))
        assert result[0][1] == "drop"

        await pilot.press("3")
        keys = app.query_one("#state-keys")
        await settle(pilot, lambda: len(keys.root.children) == 1 and len(keys.root.children[0].children) == 1)
        keys.focus()
        keys.select_node(keys.root.children[0].children[0])
        await settle(pilot, lambda: app.state_value is not None)
        value = app.query_one("#state-tree")
        value.focus()
        value.move_cursor(value.root.children[0])
        await pilot.press("e")
        await pilot.pause(0.1)
        await pilot.press("backspace", "9", "9", "enter")
        await settle(pilot, lambda: bool(app.state_changed))
        app._apply_state()
        await settle(pilot, lambda: agent.state["stages"]["validate_block-6"]["payload"] == {"seen": 99})
    client.close()


def _walk(node):
    yield node
    for child in node.children:
        yield from _walk(child)


def roll_forward() -> bytes:
    """A chain-sync RollForward as it crosses the network: [2, [6, 24(header)], [[slot, hash], block number]]."""
    header_body = (
        bytes.fromhex("8a1a00526dc01a0845f1e3")
        + b"".join(bytes([0x58, 32]) + bytes([fill]) * 32 for fill in (0x11, 0x22, 0x33))
        + bytes.fromhex("82") + bytes([0x58, 64]) + b"\x44" * 64 + bytes([0x58, 80]) + b"\x55" * 80
        + bytes.fromhex("192c5c") + bytes([0x58, 32]) + b"\x66" * 32
        + bytes.fromhex("84") + bytes([0x58, 32]) + b"\x77" * 32 + bytes.fromhex("0c1902c1") + bytes([0x58, 64]) + b"\x88" * 64
        + bytes.fromhex("820a00")
    )
    header = bytes.fromhex("82") + header_body + bytes.fromhex("5901c0") + b"\x99" * 448
    tip = bytes.fromhex("82821a0845f1e3") + bytes([0x58, 32]) + b"\xaa" * 32 + bytes.fromhex("1a00526dc0")
    return bytes.fromhex("83028206d818") + bytes([0x59]) + len(header).to_bytes(2, "big") + header + tip


def test_network_messages_are_named_and_edited_byte_exactly(agent):
    asyncio.run(_network_messages_are_named_and_edited_byte_exactly(agent))


async def _network_messages_are_named_and_edited_byte_exactly(agent):
    original = roll_forward()
    client = FreezeClient("127.0.0.1", agent.port, timeout=5)
    client.connect()
    app = FreezeApp(client)
    async with app.run_test(size=(180, 50)) as pilot:
        await pilot.press("i")
        await settle(pilot, lambda: agent.hold is not None)
        result = offer(agent, {"kind": "proto_in", "from": "peer", "to": "stage:chainsync-39", "label": "chain sync",
                               "type": "ouroboros/2", "raw": original.hex(), "payload": None, "editable": True,
                               "droppable": True})
        await settle(pilot, lambda: app.selected is not None)
        held = app.query_one("#held")
        assert "RollForward" in str(held.get_row_at(0)[2])

        tree = app.query_one("#item")
        labels = {node.data.name: node.label.plain for node in _walk(tree.root) if node.data is not None and node.data.name}
        assert labels["vrf_proof"].startswith("vrf proof  55555555…")
        assert labels["kes_period"] == "kes period  705"
        assert labels["block_body_hash"].startswith("body hash  66666666…")

        tree.focus()
        kes = next(node for node in _walk(tree.root) if node.data is not None and node.data.name == "kes_period")
        kes.parent.expand()
        await pilot.pause(0.1)
        tree.move_cursor(kes)
        await settle(pilot, lambda: tree.cursor_node is kes)
        await pilot.press("e")
        await pilot.pause(0.1)
        await pilot.press("backspace", "backspace", "backspace", "1", "enter")
        await settle(pilot, lambda: app.selected in app.raw_edits)
        await pilot.press("f")
        await settle(pilot, lambda: "operational certificate fields are protected" in app.message)
        assert result == [], "a protected edit stays held until stale negative-control mode is explicit"
        shown = str(app.query_one("#inspector-text").render())
        assert "REFUSED" in shown and "operational certificate cold signature" in shown
        await pilot.press("N")
        await settle(pilot, lambda: "STALE NEGATIVE CONTROL" in str(app.query_one("#inspector-text").render()))
        await pilot.press("f")
        await settle(pilot, lambda: bool(result))
        _seq, verdict, replacement = result[0]
        edited = bytes.fromhex(replacement["$raw"])
        assert verdict == "replace" and len(edited) == len(original)
        assert [i for i, (a, b) in enumerate(zip(original, edited)) if a != b] == [original.index(bytes.fromhex("1902c1")) + 1, original.index(bytes.fromhex("1902c1")) + 2]
    client.close()


def test_public_header_edit_is_refused_and_inspector_names_the_missing_repair(agent):
    asyncio.run(_public_header_edit_is_refused_and_inspector_names_the_missing_repair(agent))


async def _public_header_edit_is_refused_and_inspector_names_the_missing_repair(agent):
    agent.resigning = {"enabled": False, "available": False, "error": "not a controlled Freeze devnet"}
    client = FreezeClient("127.0.0.1", agent.port, timeout=5)
    client.connect()
    app = FreezeApp(client)
    async with app.run_test(size=(180, 50)) as pilot:
        await pilot.press("i")
        result = offer(agent, {"kind": "proto_in", "from": "peer", "to": "stage:chainsync-39", "label": "chain sync",
                               "type": "ouroboros/2", "raw": roll_forward().hex(), "payload": None, "editable": True,
                               "droppable": True})
        await settle(pilot, lambda: app.selected is not None)
        seq = app.selected
        assert seq is not None
        original = bytes.fromhex(app.session.held[seq]["raw"])
        position = original.index(bytes.fromhex("192c5c"))
        edited = original[: position + 2] + bytes([original[position + 2] + 1]) + original[position + 3 :]
        app.raw_edits[seq] = edited
        from freeze.views import item_view

        _payload, names, _raw = item_view(app.session.held[seq], edited)
        app.changed[seq] = {next(path for path, name in names.items() if name == "block_body_size")}
        app._refresh_held()
        await pilot.press("f")
        await settle(pilot, lambda: "controlled-devnet header re-signing is unavailable" in app.message)
        assert result == []
        shown = str(app.query_one("#inspector-text").render())
        assert "REFUSED" in shown and "KES signature" in shown and "remaining stale" in shown
        assert "controlled_devnet_signing_unavailable" in shown
    client.close()


def test_overview_shows_peers_parts_and_where_the_node_is_stopped(agent):
    asyncio.run(_overview_shows_peers_parts_and_where_the_node_is_stopped(agent))


async def _overview_shows_peers_parts_and_where_the_node_is_stopped(agent):
    client = FreezeClient("127.0.0.1", agent.port, timeout=5)
    client.connect()
    app = FreezeApp(client)
    async with app.run_test(size=(180, 50)) as pilot:
        agent.offer({"kind": "proto_in", "from": "peer:10.0.0.12:3001", "to": "stage:chainsync-39", "label": "chain sync",
                     "type": "ouroboros/2", "raw": roll_forward().hex(), "payload": None})
        agent.offer({"kind": "proto_out", "from": "stage:chainsync-39", "to": "peer:10.0.0.12:3001", "label": "chain sync",
                     "type": "ouroboros/2", "raw": "8100", "payload": [0]})
        agent.offer({"kind": "internal_input", "to": "stage:track_peers-3", "label": "TrackPeersMsg", "payload": "x"})
        agent.offer({"kind": "internal_action", "from": "stage:track_peers-3", "to": "node", "label": "ValidateHeaderEffect"})
        agent.offer({"kind": "internal_action", "from": "stage:peer_selection-2", "to": "node", "label": "PeerAdversarialEffect",
                     "payload": {"peer": {"peer": "ipv4", "address": "10.0.0.14", "port": 3001}}})
        agent.offer({"kind": "internal_action", "from": "stage:1-10.0.0.13:3001-21", "label": "terminate"})
        await pilot.press("z")
        await settle(pilot, lambda: agent.frozen)
        offer(agent, {"kind": "internal_input", "to": "stage:validate_block-6", "label": "ValidateBlockMsg", "payload": "y"})
        await settle(pilot, lambda: len(app.session.held) == 1)

        await pilot.press("5")
        await settle(pilot, lambda: app.query_one("TabbedContent").active == "overview")
        await pilot.pause(0.3)
        shown = str(app.query_one("#overview-text").render())
        assert "INSIDE THE NODE" in shown and "10.0.0.12:3001" in shown, "the tab shows the overview"
        text = app._overview().plain
        lines = {line.split("  ")[0].strip(" ❄●○✗↓"): line for line in text.splitlines() if line.strip()}
        assert "FROZEN" in text and "1 held" in text
        peer = next(line for line in text.splitlines() if "10.0.0.12:3001" in line)
        assert peer.strip().startswith("●") and "RollForward" in peer and "block number 5,402,048" in peer
        assert "marked hostile" in next(line for line in text.splitlines() if "10.0.0.14:3001" in line)
        assert "hung up" in next(line for line in text.splitlines() if "10.0.0.13:3001" in line)
        track = next(line for line in text.splitlines() if "track peers" in line)
        assert "check header" in track and " 1 " in track
        stopped = next(line for line in text.splitlines() if "validate block" in line)
        assert stopped.strip().startswith("❄") and "1 held" in stopped
        assert text.index("track peers") < text.index("select chain") < text.index("validate block") < text.index("adopt chain")
        assert lines
    client.close()


def test_payload_lists_are_edited_and_run_against_a_field(agent, tmp_path):
    asyncio.run(_payload_lists_are_edited_and_run_against_a_field(agent, tmp_path))


async def _payload_lists_are_edited_and_run_against_a_field(agent, tmp_path):
    from freeze import payloads

    store = payloads.Store(tmp_path / "payloads")
    client = FreezeClient("127.0.0.1", agent.port, timeout=5)
    client.connect()
    app = FreezeApp(client, store=store)

    async def type_and_enter(pilot, text: str) -> None:
        await pilot.pause(0.15)
        for character in text:
            await pilot.press(character if character != " " else "space")
        await pilot.press("enter")
        await pilot.pause(0.15)

    async with app.run_test(size=(200, 50)) as pilot:
        # make a list of three values in the Payloads tab
        await pilot.press("6")
        await settle(pilot, lambda: app.query_one("TabbedContent").active == "payloads" and app.query_one("#pl-lists").row_count >= 4)
        await pilot.press("n")
        await type_and_enter(pilot, "kes")
        await settle(pilot, lambda: app.current_list == "kes")
        for value in ("1", "add:1", "0"):
            await pilot.press("a")
            await type_and_enter(pilot, value)
        await settle(pilot, lambda: store.get("kes").values == ["1", "add:1", "0"])
        app.query_one("#pl-values").focus()
        app.query_one("#pl-values").move_cursor(row=2)
        await settle(pilot, lambda: app.query_one("#pl-values").cursor_row == 2)
        await pilot.press("e")
        await pilot.pause(0.15)
        await pilot.press("backspace")
        await type_and_enter(pilot, "9")
        await settle(pilot, lambda: store.get("kes").values[2] == "9")
        await pilot.press("x")
        await settle(pilot, lambda: store.get("kes").values == ["1", "add:1"])

        # hold a header, pick the kes period, and run the list against it
        await pilot.press("1")
        await pilot.press("i")
        await settle(pilot, lambda: agent.hold is not None)
        message = {"kind": "proto_in", "from": "peer:10.0.0.12:3001", "to": "stage:chainsync-39", "label": "chain sync",
                   "type": "ouroboros/2", "raw": roll_forward().hex(), "payload": [2], "editable": True, "droppable": True}
        results: list = []

        def node() -> None:
            for _ in range(3):
                results.append(agent.offer(dict(message)))

        threading.Thread(target=node, daemon=True).start()
        await settle(pilot, lambda: app.selected is not None)
        tree = app.query_one("#item")
        tree.focus()
        kes = next(node for node in _walk(tree.root) if node.data is not None and node.data.name == "kes_period")
        kes.parent.expand()
        await pilot.pause(0.1)
        tree.move_cursor(kes)
        await settle(pilot, lambda: tree.cursor_node is kes)
        await pilot.press("N")  # this payload experiment intentionally mutates protected opcert material
        await settle(pilot, lambda: app.selected in app.stale_negative_controls)
        await pilot.press("p")
        await pilot.pause(0.15)
        await pilot.press("enter")  # the dialog offers the list selected in Payloads: kes
        await settle(pilot, lambda: len(app.engine.attempts) == 2)
        await pilot.press("i")  # intercept off, so the third header is not held for us
        await settle(pilot, lambda: len(results) == 3)

        original = roll_forward()
        position = original.index(bytes.fromhex("1902c1"))
        sent = [bytes.fromhex(new["$raw"]) for _seq, verdict, new in results if verdict == "replace"]
        assert [raw[position : position + 3].hex() for raw in sent] == ["190001", "1902c2"], "kes period 705 became 1, then 706"
        assert results[2][1] == "forward", "after the last value the rule stops and traffic flows untouched"

        await pilot.press("6")
        await settle(pilot, lambda: app.query_one("#pl-results").row_count == 2)
        row = [str(cell) for cell in app.query_one("#pl-results").get_row_at(1)]
        assert row[3] == "RollForward" and row[4] == "add:1" and row[5] == "705 → 706"
        assert [str(cell) for cell in app.query_one("#pl-rules").get_row_at(0)][3:] == ["2/2", "done"]
    client.close()


def test_scenarios_tab_lists_dwarf_scenarios_and_starts_one(agent, tmp_path):
    asyncio.run(_scenarios_tab_lists_dwarf_scenarios_and_starts_one(agent, tmp_path))


async def _scenarios_tab_lists_dwarf_scenarios_and_starts_one(agent, tmp_path):
    from freeze import payloads
    from freeze.session import Session

    client = FreezeClient("127.0.0.1", agent.port, timeout=5)
    client.connect()
    app = FreezeApp(client, session=Session(tmp_path / "session"), store=payloads.Store(tmp_path / "payloads"))
    async with app.run_test(size=(200, 50)) as pilot:
        await pilot.press("7")
        table = app.query_one("#sc-table")
        await settle(pilot, lambda: app.query_one("TabbedContent").active == "scenarios" and table.row_count > 40)
        ready_only = table.row_count
        await pilot.press("A")
        await settle(pilot, lambda: table.row_count > 300)
        await pilot.press("A")
        await settle(pilot, lambda: table.row_count == ready_only)

        app.query_one("#sc-filter").focus()
        app.query_one("#sc-filter").value = "praos-header-assertion"
        await pilot.press("enter")
        await settle(pilot, lambda: table.row_count == 1)
        detail = str(app.query_one("#sc-detail-text").render())
        assert "damage the header's signature" in detail and "praos_header_assertion_rejected" in detail and "RR-013" in detail

        await pilot.press("L")
        await settle(pilot, lambda: len(app.engine.rules) == 1 and app.engine.rules[0].armed)
        assert agent.conditions, "the rule is holding matching items on the node"

        # two headers go by untouched, the third has its signature damaged
        results: list = []
        message = {"kind": "proto_in", "from": "peer:10.0.0.12:3001", "to": "stage:chainsync-39", "label": "chain sync",
                   "type": "ouroboros/2", "raw": roll_forward().hex(), "payload": [2], "editable": True, "droppable": True}

        def node() -> None:
            for _ in range(3):
                results.append(agent.offer(dict(message)))

        threading.Thread(target=node, daemon=True).start()
        await settle(pilot, lambda: len(results) == 3)
        assert [verdict for _seq, verdict, _new in results] == ["forward", "forward", "replace"]
        original = roll_forward()
        damaged = bytes.fromhex(results[2][2]["$raw"])
        differing = [i for i, (a, b) in enumerate(zip(original, damaged)) if a != b]
        signature_end = original.index(b"\x99" * 448) + 447
        assert differing == [signature_end], "only the last byte of the kes signature changed"

        # export the run from History
        await pilot.press("2")
        await settle(pilot, lambda: app.query_one("TabbedContent").active == "history")
        await pilot.press("X")
        await pilot.pause(0.2)
        target = tmp_path / "run.tar.gz"
        dialog = app.screen.query_one("#dialog-input")
        dialog.value = str(target)
        await pilot.press("enter")
        await settle(pilot, lambda: target.is_file())
    client.close()
    from freeze import runs

    _manifest, steps, _where = runs.load_run(target, tmp_path / "runs")
    assert [(step["action"], step["after"], step["what"]) for step in steps] == [("replace", 2, "RollForward")]


def test_history_marks_compare_and_send_again(agent, tmp_path):
    asyncio.run(_history_marks_compare_and_send_again(agent, tmp_path))


async def _history_marks_compare_and_send_again(agent, tmp_path):
    from freeze.session import Session

    client = FreezeClient("127.0.0.1", agent.port, timeout=5)
    client.connect()
    app = FreezeApp(client, Session(tmp_path / "s"))
    async with app.run_test(size=(180, 50)) as pilot:
        agent.offer({"kind": "internal_input", "to": "stage:fake", "label": "Pay", "payload": {"fee": 5}, "editable": True})
        agent.offer({"kind": "internal_input", "to": "stage:fake", "label": "Pay", "payload": {"fee": 9}, "editable": True})
        await settle(pilot, lambda: len(app.session.items) == 2)
        await pilot.press("2")
        table = app.query_one("#history-table")
        await settle(pilot, lambda: table.row_count == 2)
        table.focus()
        first = min(app.session.items)
        table.move_cursor(row=[str(key.value) for key in table.rows].index(str(first)))
        await settle(pilot, lambda: app.history_seq == first)

        await pilot.press("b")
        await settle(pilot, lambda: app.marks.badge(first) == "*", timeout=5)
        await pilot.press("F")
        await pilot.pause(0.1)
        await pilot.press(*"fee looks odd", "enter")
        await settle(pilot, lambda: "finding" in app.marks.marks[first])
        assert app.marks.marks[first]["finding"] == {**app.marks.marks[first]["finding"], "severity": "medium", "title": "fee looks odd"}
        assert (tmp_path / "s" / "notes.json").is_file(), "marks are saved with the session"

        await pilot.press("c")
        other = next(seq for seq in app.session.items if seq != first)
        table.move_cursor(row=[str(key.value) for key in table.rows].index(str(other)))
        await settle(pilot, lambda: app.history_seq == other)
        await pilot.press("c")
        shown = str(app.query_one("#history-did-text").render())
        assert "1 field(s) differ" in shown and "fee" in shown

        await pilot.press("R")
        await pilot.pause(0.1)
        for _ in range(len("as it is")):
            await pilot.press("backspace")
        await pilot.press(*"fee=add:1", "enter")
        await settle(pilot, lambda: bool(agent.injected))
        assert agent.injected == [("stage:fake", {"fee": app.session.items[other]["payload"]["fee"] + 1})]
    client.close()


def test_a_narrow_terminal_keeps_every_button_box_and_pane_on_screen(agent):
    asyncio.run(_a_narrow_terminal_keeps_every_button_box_and_pane_on_screen(agent))


async def _a_narrow_terminal_keeps_every_button_box_and_pane_on_screen(agent):
    for width, height in ((80, 24), (100, 30), (120, 36)):
        client = FreezeClient("127.0.0.1", agent.port, timeout=5)
        client.connect()
        app = FreezeApp(client)
        async with app.run_test(size=(width, height)) as pilot:
            await pilot.pause(0.2)
            widgets = [*app.query("#actions Button"), *app.query("#holding Checkbox"),
                       app.query_one("#held"), app.query_one("#item"), app.query_one("#inspector")]
            assert len(widgets) == 7 + 7 + 3
            for widget in widgets:
                region = widget.region
                assert region.width > 0 and region.height > 0, f"{widget.id} has no room at {width} columns"
                assert region.right <= width, f"{widget.id} runs off the right edge at {width} columns"
            # The held list sits above the item and the inspector, which share the row below it.
            assert app.query_one("#held").region.bottom <= app.query_one("#item").region.y
            assert app.query_one("#item").region.y == app.query_one("#inspector").region.y
        client.close()


def test_a_wide_terminal_keeps_the_buttons_in_one_row_and_the_panes_side_by_side(agent):
    asyncio.run(_a_wide_terminal_keeps_the_buttons_in_one_row_and_the_panes_side_by_side(agent))


async def _a_wide_terminal_keeps_the_buttons_in_one_row_and_the_panes_side_by_side(agent):
    client = FreezeClient("127.0.0.1", agent.port, timeout=5)
    client.connect()
    app = FreezeApp(client)
    async with app.run_test(size=(180, 50)) as pilot:
        await pilot.pause(0.2)
        assert len({button.region.y for button in app.query("#actions Button")}) == 1
        assert len({app.query_one(name).region.y for name in ("#held", "#item", "#inspector")}) == 1
        # Narrowing the window rearranges it, and widening it puts it back.
        await pilot.resize_terminal(90, 30)
        await pilot.pause(0.2)
        assert len({button.region.y for button in app.query("#actions Button")}) > 1
        await pilot.resize_terminal(180, 50)
        await pilot.pause(0.2)
        assert len({button.region.y for button in app.query("#actions Button")}) == 1
    client.close()


def test_source_tab_says_what_to_do_before_a_debugger_is_attached(agent):
    asyncio.run(_source_tab_says_what_to_do_before_a_debugger_is_attached(agent))


async def _source_tab_says_what_to_do_before_a_debugger_is_attached(agent):
    client = FreezeClient("127.0.0.1", agent.port, timeout=5)
    client.connect()
    app = FreezeApp(client)
    async with app.run_test(size=(180, 50)) as pilot:
        await pilot.press("9")
        await pilot.pause(0.2)
        shown = str(app.query_one("#src-code-text").render())
        assert "Press A to attach" in shown and "--debug-info" in shown
    client.close()
