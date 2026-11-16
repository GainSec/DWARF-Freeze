"""Saved filters, several Freeze nodes in one devnet, stores in the State tab, and probes."""

import asyncio
import json
import sys
from pathlib import Path

import pytest

pytest.importorskip("textual")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "dwarf"))

from freeze import analysis, cli, devnet, filters  # noqa: E402
from freeze.client import FreezeClient, FreezeError  # noqa: E402
from freeze.session import Session  # noqa: E402
from freeze.testing.fake_agent import FakeAgent  # noqa: E402
from freeze.tui import FreezeApp  # noqa: E402


async def settle(pilot, condition, timeout: float = 20.0) -> None:
    waited = 0.0
    while not condition() and waited < timeout:
        await pilot.pause(0.05)
        waited += 0.05
    assert condition()


# Saved filters ---------------------------------------------------------------------------------


def test_a_filter_is_saved_once_and_used_by_name(tmp_path):
    store = filters.Filters(tmp_path)
    assert store.all() == {}
    store.save("from-peers", "kind=proto_in", about="what peers sent")
    assert store.resolve("@from-peers") == "kind=proto_in"
    assert store.resolve("kind=proto_out") == "kind=proto_out", "anything else is left as typed"
    assert json.loads((tmp_path / "filters.json").read_text())["from-peers"]["about"] == "what peers sent"

    with pytest.raises(filters.FilterError, match="no saved filter `nope`"):
        store.resolve("@nope")
    with pytest.raises(filters.FilterError, match="not a condition"):
        store.save("bad", "this is not a condition ((")
    with pytest.raises(filters.FilterError, match="not a usable name"):
        store.save("has space", "kind=proto_in")
    with pytest.raises(filters.FilterError, match="another saved filter"):
        store.save("loop", "@from-peers")

    assert store.delete("from-peers") is True
    assert store.delete("from-peers") is False


def test_what_is_typed_into_a_filter_box(tmp_path):
    store = filters.Filters(tmp_path)
    assert store.typed("kind=proto_in") == ("", "kind=proto_in")
    assert store.typed("@") == ("no saved filters yet", "")
    assert store.typed("@headers = kind=proto_in and label=RollForward") == ("saved @headers", "kind=proto_in and label=RollForward")
    assert store.typed("@headers") == ("", "kind=proto_in and label=RollForward")
    assert store.typed("@") == ("saved filters: @headers", "")
    assert store.typed("@-headers") == ("forgot @headers", "")
    assert store.typed("@-headers") == ("no saved filter `headers`", "")


def test_search_and_filters_commands_use_saved_filters(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("DWARF_DATA_ROOT", str(tmp_path))
    session_dir = tmp_path / "freeze" / "sessions" / "s1"
    session = Session(session_dir)
    session.record("item", {"seq": 1, "kind": "proto_in", "label": "RollForward", "from": "peer:10.0.0.3:3001", "to": "stage:chainsync-1", "payload": None})
    session.record("item", {"seq": 2, "kind": "proto_out", "label": "RequestNext", "from": "stage:chainsync-1", "to": "peer:10.0.0.3:3001", "payload": None})
    session.close()

    assert cli.main(["filters", "save", "incoming", "kind=proto_in", "--about", "from peers"]) == 0
    assert cli.main(["filters"]) == 0
    assert "@incoming" in capsys.readouterr().out
    assert cli.main(["search", "@incoming", "--session", "s1"]) == 0
    out = capsys.readouterr().out
    assert "RollForward" in out and "RequestNext" not in out and "1 item(s)" in out
    assert cli.main(["search", "@missing", "--session", "s1"]) == 2
    assert cli.main(["filters", "delete", "incoming"]) == 0
    assert cli.main(["filters", "delete", "incoming"]) == 1


# Several Freeze nodes --------------------------------------------------------------------------


def test_every_freeze_node_gets_its_own_port_and_settings():
    profile = devnet.Profile(name="pair", compose=["x.yaml"], target="amaru-relay-2", also=["amaru-relay-1"],
                             node_environment={"amaru-relay-2": {"AMARU_PEER": "amaru-relay-1.example:3000"}})
    assert profile.nodes == ["amaru-relay-2", "amaru-relay-1"]
    assert devnet.node_ports(profile, 7400) == {"amaru-relay-2": 7400, "amaru-relay-1": 7401}

    document = devnet.override("image:x", "secret", port=7400, frozen=["amaru-relay-1"], profile=profile)
    assert set(document["services"]) == {"freeze-target-extract", "amaru-relay-1", "amaru-relay-2"}
    under_test, peer = document["services"]["amaru-relay-2"], document["services"]["amaru-relay-1"]
    assert under_test["ports"] == ["127.0.0.1:7400:7373"] and peer["ports"] == ["127.0.0.1:7401:7373"]
    assert under_test["environment"]["AMARU_PEER"] == "amaru-relay-1.example:3000"
    assert "AMARU_PEER" not in peer["environment"], "one node's settings do not leak into another"
    assert peer["environment"]["DWARF_FREEZE_START"] == "frozen" and "DWARF_FREEZE_START" not in under_test["environment"]
    assert under_test["environment"]["DWARF_FREEZE_TOKEN"] == peer["environment"]["DWARF_FREEZE_TOKEN"] == "secret"

    everyone = devnet.override("image:x", "secret", frozen=True, profile=profile)
    assert all(everyone["services"][name]["environment"]["DWARF_FREEZE_START"] == "frozen" for name in profile.nodes)


def test_shipped_profiles_with_two_nodes_load_and_fit_dwarfs_layout():
    available = {profile.name: profile for profile in devnet.profiles()}
    pair, scripted = available["relay-pair"], available["scripted-peer"]
    assert pair.nodes == ["amaru-relay-1", "amaru-relay-2"]
    assert scripted.target == "amaru-relay-2" and scripted.also == ["amaru-relay-1"]
    assert scripted.node_environment["amaru-relay-2"]["AMARU_PEER"].startswith("amaru-relay-1.")
    for name in ("relay-pair", "scripted-peer"):
        found = devnet.find_profile(name)
        assert set(found.nodes) <= set(devnet.targets(found.files[0]))

    with pytest.raises(devnet.DevnetError, match="cannot be a Freeze node"):
        devnet.find_profile("relay", also=["p1"])
    widened = devnet.find_profile("relay", also=["amaru-relay-2", "amaru-relay-1"])
    assert widened.nodes == ["amaru-relay-1", "amaru-relay-2"], "the main node is not listed twice"


def test_a_node_of_the_devnet_is_reached_by_name(tmp_path):
    home = devnet.directory(tmp_path)
    home.mkdir(parents=True)
    (home / "token").write_text("secret")
    (home / "run.json").write_text(json.dumps({
        "host": "127.0.0.1", "port": 7373, "token_file": str(home / "token"), "target": "amaru-relay-2",
        "nodes": {"amaru-relay-2": 7373, "amaru-relay-1": 7374}}))
    assert devnet.current(tmp_path)["port"] == 7373
    other = devnet.current(tmp_path, "amaru-relay-1")
    assert (other["port"], other["target"], other["token"]) == (7374, "amaru-relay-1", "secret")
    with pytest.raises(devnet.DevnetError, match="not a Freeze node of this devnet"):
        devnet.current(tmp_path, "p1")

    # A devnet started before there could be several nodes has no list; it has its one node.
    (home / "run.json").write_text(json.dumps({"host": "127.0.0.1", "port": 7373, "token_file": str(home / "token"), "target": "amaru-relay-1"}))
    assert devnet.current(tmp_path)["nodes"] == {"amaru-relay-1": 7373}


# Probes and stores -----------------------------------------------------------------------------


@pytest.fixture
def agent():
    fake = FakeAgent(info={"implementation": "amaru", "version": "10.11.0", "network": "test", "kinds": {}})
    fake.state["stages"] = {"validate_block-6": {"type": "ValidateBlock", "payload": {"seen": 3}}}
    fake.state["chain/headers"] = {
        f"{slot:020d}-{slot:064x}": {"slot": slot, "height": slot // 2, "hash": f"{slot:064x}", "parent": f"{slot - 2:064x}"}
        for slot in range(2, 2 + 2 * 450, 2)
    }
    fake.state["ledger/utxo"] = {f"{'ab' * 32}#{index}": {"address": "addr_test1", "value": 1000 + index} for index in range(3)}
    fake.read_only = {"chain/headers", "ledger/utxo"}
    fake.probes = {
        "consensus.validate_header.kes": {"where": "crates/amaru-consensus/src/x.rs::validate_kes", "enabled": False, "hits": 12},
        "consensus.select_chain.switch_fork": {"where": "crates/amaru-consensus/src/y.rs::switch", "enabled": False, "hits": 0},
        "ledger.apply_block": {"where": "crates/amaru-ledger/src/z.rs::apply", "enabled": True, "hits": 4},
    }
    fake.start()
    yield fake
    fake.stop()


def test_probes_are_listed_and_turned_on_by_name_or_prefix(agent):
    client = FreezeClient("127.0.0.1", agent.port, timeout=5)
    client.connect()
    try:
        assert [probe["name"] for probe in client.probes()] == sorted(agent.probes)
        assert client.probe_enable("consensus.*") == 2
        assert client.probe_enable("consensus.*") == 0, "already on"
        assert client.probe_disable("ledger.apply_block") == 1
        assert {probe["name"]: probe["enabled"] for probe in client.probes()} == {
            "consensus.validate_header.kes": True, "consensus.select_chain.switch_fork": True, "ledger.apply_block": False}
        assert client.probe_disable("all") == 2
    finally:
        client.close()


def test_stores_are_read_only_and_found_by_their_longest_namespace(agent):
    client = FreezeClient("127.0.0.1", agent.port, timeout=5)
    client.connect()
    try:
        assert set(client.namespaces()) == {"stages", "chain/headers", "ledger/utxo"}
        first = client.state_list("chain/headers", limit=200)
        assert len(first) == 200 and first[0]["value"]["slot"] == 2
        second = client.state_list("chain/headers", after=first[-1]["key"], limit=200)
        assert second[0]["value"]["slot"] == 402
        assert client.state_get(f"ledger/utxo/{'ab' * 32}#1")["value"] == 1001
        with pytest.raises(FreezeError, match="read-only"):
            client.state_put(f"ledger/utxo/{'ab' * 32}#1", {"value": 0})
    finally:
        client.close()


def test_coverage_counts_probes_that_reported():
    session = Session()
    for seq, name in ((1, "ledger.apply_block"), (2, "ledger.apply_block"), (3, "consensus.validate_header.kes")):
        session.record("item", {"seq": seq, "kind": "probe", "label": name, "from": f"probe:{name}", "to": "", "payload": {}})
    assert analysis.coverage(session)["probes"] == {"ledger.apply_block": 2, "consensus.validate_header.kes": 1}


def test_state_tab_pages_through_a_store_and_probes_are_toggled(agent):
    asyncio.run(_state_tab_pages_through_a_store_and_probes_are_toggled(agent))


async def _state_tab_pages_through_a_store_and_probes_are_toggled(agent):
    client = FreezeClient("127.0.0.1", agent.port, timeout=5)
    client.connect()
    app = FreezeApp(client)
    async with app.run_test(size=(200, 60)) as pilot:
        await pilot.press("3")
        keys = app.query_one("#state-keys")
        await settle(pilot, lambda: len(keys.root.children) == 3)
        branches = {str(node.label): node for node in keys.root.children}
        assert len(branches["stages"].children) == 1, "the node's parts are loaded straight away"
        headers = branches["chain/headers"]
        assert len(headers.children) == 1, "a store is not read until it is opened"

        headers.expand()
        await settle(pilot, lambda: len(headers.children) == 201)
        assert "slot 2" in str(headers.children[0].label) and "block 1" in str(headers.children[0].label)
        more = headers.children[-1]
        assert "more" in str(more.label)
        keys.select_node(more)
        await settle(pilot, lambda: len(headers.children) == 401)
        keys.select_node(headers.children[-1])
        await settle(pilot, lambda: len(headers.children) == 450)
        assert "more" not in str(headers.children[-1].label), "the last page has no `more`"

        keys.select_node(headers.children[0])
        await settle(pilot, lambda: isinstance(app.state_value, dict) and app.state_value.get("slot") == 2)

        await pilot.press("4")
        probes = app.query_one("#probes-table")
        await settle(pilot, lambda: probes.row_count == 3)
        assert "2 of 3 reached" in str(probes.border_subtitle)
        probes.focus()
        probes.move_cursor(row=0)  # consensus.select_chain.switch_fork, off
        await pilot.press("enter")
        await settle(pilot, lambda: agent.probes["consensus.select_chain.switch_fork"]["enabled"] is True)
        await pilot.press("a")
        await settle(pilot, lambda: all(probe["enabled"] for probe in agent.probes.values()))
        await pilot.press("n")
        await settle(pilot, lambda: not any(probe["enabled"] for probe in agent.probes.values()))
    client.close()


def test_history_filter_box_saves_and_uses_a_filter(agent, tmp_path, monkeypatch):
    monkeypatch.setenv("DWARF_DATA_ROOT", str(tmp_path))
    asyncio.run(_history_filter_box_saves_and_uses_a_filter(agent, tmp_path))


async def _history_filter_box_saves_and_uses_a_filter(agent, tmp_path):
    client = FreezeClient("127.0.0.1", agent.port, timeout=5)
    client.connect()
    app = FreezeApp(client)
    async with app.run_test(size=(200, 60)) as pilot:
        await pilot.press("2")
        box = app.query_one("#history-filter")
        box.focus()
        box.value = "@incoming = kind=proto_in"
        await pilot.press("enter")
        await settle(pilot, lambda: app.history_filter is not None)
        assert filters.Filters(tmp_path / "freeze").all()["incoming"]["query"] == "kind=proto_in"

        box.focus()
        box.value = ""
        await pilot.press("enter")
        await settle(pilot, lambda: app.history_filter is None)
        box.focus()
        box.value = "@incoming"
        await pilot.press("enter")
        await settle(pilot, lambda: app.history_filter is not None)
    client.close()
