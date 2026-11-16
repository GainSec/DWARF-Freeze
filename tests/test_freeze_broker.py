"""The broker: forwarding, recording, pausing the other nodes, and claiming a session."""

import asyncio
import json
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "dwarf"))

from freeze import broker as broker_module  # noqa: E402
from freeze.broker import Broker, Peers  # noqa: E402
from freeze.client import FreezeClient, FreezeError  # noqa: E402
from freeze.testing.fake_agent import FakeAgent  # noqa: E402


def free_port() -> int:
    import socket

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def until(condition, timeout: float = 10.0) -> None:
    deadline = time.time() + timeout
    while not condition() and time.time() < deadline:
        time.sleep(0.02)
    assert condition()


class Running:
    """A fake agent with a broker in front of it, on a thread of its own."""

    prepare = None
    record = "all"

    def __init__(self, tmp_path: Path, auto: bool = True, frozen: bool = False, wait: bool = True, features: list | None = None) -> None:
        self.agent = FakeAgent(token="secret", start_frozen=frozen, info={
            "implementation": "amaru", "version": "10.11.0", "network": "test", "kinds": {}, "features": features or []})
        self.agent.start()
        self.calls: list[str] = []
        self.peers = None
        self.port = free_port()
        self.tmp_path, self.auto = tmp_path, auto
        self.broker: Broker | None = None
        self.loop: asyncio.AbstractEventLoop | None = None
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()
        until(lambda: self.broker is not None and (not wait or bool(self.broker.hello)))

    def _run(self) -> None:
        async def serve() -> None:
            self.loop = asyncio.get_running_loop()
            self.peers = Peers(lambda: self.calls.append("pause"), lambda: self.calls.append("resume"), auto=self.auto)
            self.broker = Broker("127.0.0.1", self.agent.port, "secret", self.port, self.tmp_path / "sessions", self.peers, "relay",
                                 prepare=self.prepare, record=self.record)
            await self.broker.run()

        asyncio.run(serve())

    def client(self, name: str = "") -> FreezeClient:
        client = FreezeClient("127.0.0.1", self.port, token="secret", timeout=5.0, name=name)
        client.connect()
        return client

    def close(self) -> None:
        assert self.loop is not None and self.broker is not None
        self.loop.call_soon_threadsafe(self.broker.stop)
        self.thread.join(5)
        self.agent.stop()


@pytest.fixture
def running(tmp_path):
    setup = Running(tmp_path)
    yield setup
    setup.close()


def test_a_client_cannot_tell_the_broker_from_an_agent_except_by_hello(running):
    with running.client("one") as client:
        assert client.hello["protocol"] == "fcp/1"
        assert client.hello["node"]["implementation"] == "amaru"
        assert client.broker["node"] == "relay" and client.broker["owner"] is None
        assert client.status()["frozen"] is False
        client.freeze()
        assert client.status()["frozen"] is True
        with pytest.raises(FreezeError):
            client.call("no.such.method")


def test_the_token_is_checked_by_the_broker(running):
    stranger = FreezeClient("127.0.0.1", running.port, token="nope", timeout=5.0)
    with pytest.raises(FreezeError) as refused:
        stranger.connect()
    assert refused.value.code == "bad_token"
    stranger.close()


def test_every_client_sees_the_events_and_they_are_recorded(running, tmp_path):
    with running.client("one") as one, running.client("two") as two:
        one.freeze()
        assert one.wait_for("frozen", timeout=5) is not None
        assert two.wait_for("frozen", timeout=5) is not None
        directory = Path(one.broker["session"])
        assert directory.parent == tmp_path / "sessions" and directory.name.endswith("-relay")
        until(lambda: '"event":"frozen"' in (directory / "events.jsonl").read_text())
        first = json.loads((directory / "events.jsonl").read_text().splitlines()[0])
        assert set(first) >= {"at", "event", "data"}
        assert json.loads((directory / "session.json").read_text())["broker"] is True
    with running.client("late") as late:
        assert "frozen" in [entry["event"] for entry in late.session_events()], "a client that attaches later gets what it missed"


def test_the_other_nodes_are_paused_while_the_node_is_frozen(running):
    with running.client() as client:
        client.freeze()
        until(lambda: running.calls == ["pause"])
        assert client.peers_status()["paused"] is True
        client.thaw()
        until(lambda: running.calls == ["pause", "resume"])
        assert client.peers_status() == {"available": True, "auto": True, "paused": False, "freeze_nodes": False, "froze": [],
                                         "seconds": 0.0, "error": None}


def test_a_single_step_does_not_pause_and_resume_them(running):
    with running.client() as client:
        client.freeze()
        until(lambda: running.calls == ["pause"])
        # Running for less than the grace time and freezing again leaves them paused throughout.
        client.thaw()
        client.freeze()
        time.sleep(broker_module.GRACE * 3)
        assert running.calls == ["pause"]


def test_pausing_by_hand_and_switching_it_off(running):
    with running.client() as client:
        assert client.peers_auto(False)["auto"] is False
        client.freeze()
        time.sleep(broker_module.GRACE * 3)
        assert running.calls == []
        assert client.peers_pause()["paused"] is True
        assert client.peers_resume()["paused"] is False
        assert running.calls == ["pause", "resume"]


def test_a_claimed_session_can_be_watched_but_not_changed_by_others(running):
    with running.client("mine") as mine, running.client("theirs") as theirs:
        theirs.freeze()  # nobody has claimed it: everyone may act
        assert mine.claim()["owner"] == mine.broker["client"]
        assert theirs.status()["frozen"] is True, "watching still works"
        with pytest.raises(FreezeError) as refused:
            theirs.thaw()
        assert refused.value.code == "not_owner"
        with pytest.raises(FreezeError) as refused:
            theirs.claim()
        assert refused.value.code == "not_owner"
        assert [entry["name"] for entry in theirs.who()["clients"] if entry["owner"]] == ["mine"]
        assert theirs.claim(force=True)["owner"] == theirs.broker["client"]
        with pytest.raises(FreezeError):
            mine.thaw()
        theirs.release()
        mine.thaw()


def test_a_claim_ends_when_its_client_leaves(running):
    mine = running.client("mine")
    mine.claim()
    with running.client("theirs") as theirs:
        mine.close()
        until(lambda: theirs.who()["owner"] is None)
        theirs.freeze()


def test_the_other_nodes_are_not_left_paused_when_the_broker_stops(tmp_path):
    setup = Running(tmp_path)
    with setup.client() as client:
        client.freeze()
        until(lambda: setup.calls == ["pause"])
    setup.close()
    assert setup.calls == ["pause", "resume"]


def test_the_broker_survives_a_restart_of_the_node(running):
    client = running.client()
    port = running.agent.port
    running.agent.stop()
    until(lambda: not client.connected)
    assert not running.broker.hello
    running.agent = FakeAgent(token="secret", info={"implementation": "amaru", "version": "10.11.0", "network": "test", "kinds": {}})
    running.agent.start(port=port)
    until(lambda: bool(running.broker.hello), timeout=15)
    with running.client() as again:
        assert again.status()["frozen"] is False


def test_being_told_to_run_again_does_not_put_off_resuming_the_others(running):
    with running.client() as client:
        client.freeze()
        until(lambda: running.calls == ["pause"])
        deadline = time.time() + broker_module.GRACE * 6
        while time.time() < deadline:
            client.thaw()  # a tool that keeps saying "run" faster than the grace time
            time.sleep(broker_module.GRACE / 5)
        assert running.calls == ["pause", "resume"]


# What a connection is sent -------------------------------------------------------------------------


def step(agent, kind="internal_input", **more):
    return agent.offer({"kind": kind, "to": "stage:fake", "label": "Tick", "payload": {"n": 1}, **more}, holdable=False)


def test_a_client_is_sent_only_what_it_subscribed_to_and_always_what_is_held():
    agent = FakeAgent(info={"implementation": "amaru", "features": ["events:filter"]})
    agent.start()
    client = FreezeClient("127.0.0.1", agent.port, timeout=5)
    client.connect()
    assert client.supports("events:filter") and not client.supports("trace")
    seen = []
    client.on_event(lambda name, data: seen.append((name, data.get("seq") if isinstance(data, dict) else None)))
    client.subscribe(kinds=["proto_in"])
    first, _, _ = step(agent)
    second, _, _ = step(agent, kind="proto_in")
    client.set_hold({"op": "kind", "in": ["internal_input"]})
    threading.Thread(target=lambda: step_held(agent), daemon=True).start()
    held = client.wait_held(timeout=5)
    client.forward(held["seq"], then_run=True)
    for number in (first, second, held["seq"]):
        agent.done(number, {})
    until(lambda: ("item.done", held["seq"]) in seen)
    items = [seq for name, seq in seen if name == "item"]
    assert items == [second, held["seq"]], "the input that was not held was left out; the held one was not"
    assert ("item.done", first) not in seen and ("item.done", second) in seen
    client.set_hold(None)
    client.subscribe(sample=3)
    numbers = [step(agent, kind="proto_in")[0] for _ in range(6)]
    until(lambda: len([seq for name, seq in seen if name == "item" and seq in numbers]) == 2)
    client.subscribe()
    last, _, _ = step(agent)
    until(lambda: ("item", last) in seen)
    client.close()
    agent.stop()


def step_held(agent):
    agent.offer({"kind": "internal_input", "to": "stage:fake", "label": "Tick", "payload": {"n": 2}})


def filtering(tmp_path, record):
    setup = Running.__new__(Running)
    setup.record = record
    Running.__init__(setup, tmp_path, auto=False, features=["events:filter"])
    return setup


def test_the_broker_asks_the_node_for_what_its_clients_want(tmp_path):
    setup = filtering(tmp_path, "held")
    try:
        until(lambda: setup.agent.subscriptions[-1:] == [{"kinds": []}])  # nobody is listening yet
        one = setup.client("one")
        until(lambda: setup.agent.subscriptions[-1] == {}), "a client that has not said what it wants gets everything"
        one.subscribe(kinds=["proto_in"], sample=4)
        until(lambda: setup.agent.subscriptions[-1] == {"when": {"op": "or", "args": [{"op": "and", "args": [{"op": "kind", "in": ["proto_in"]}]}]}, "sample": 4})
        two = setup.client("two")
        until(lambda: setup.agent.subscriptions[-1] == {})
        two.subscribe(when={"op": "label", "eq": "Tick"})
        until(lambda: setup.agent.subscriptions[-1]["when"]["op"] == "or" and len(setup.agent.subscriptions[-1]["when"]["args"]) == 2)
        assert "sample" not in setup.agent.subscriptions[-1], "the one who wants every match decides"
        two.close()
        until(lambda: setup.agent.subscriptions[-1].get("sample") == 4)
        # One that wants nothing but what is always sent does not widen what the other gets.
        quiet = setup.client("quiet")
        until(lambda: setup.agent.subscriptions[-1] == {})
        quiet.subscribe(kinds=[])
        until(lambda: setup.agent.subscriptions[-1].get("sample") == 4)
        one.subscribe(sample=50)
        until(lambda: setup.agent.subscriptions[-1] == {"sample": 50})
        quiet.close()
        one.close()
        until(lambda: setup.agent.subscriptions[-1] == {"kinds": []})
    finally:
        setup.close()


def test_a_broker_that_records_everything_asks_for_everything(tmp_path):
    setup = filtering(tmp_path, "all")
    try:
        with setup.client() as client:
            client.subscribe(kinds=["proto_in"])
            time.sleep(0.3)
            assert setup.agent.subscriptions in ([], [{}]), "its recording needs every item, whatever a client wants"
            assert client.broker["record"] == "all"
    finally:
        setup.close()


def test_events_are_understood_whichever_way_round_the_agent_writes_them(running):
    """An agent may put an event's data before its name."""
    original = running.agent._emit

    def reordered(event, data):
        line = (json.dumps({"data": data, "event": event}, separators=(",", ":")) + "\n").encode()
        with running.agent._lock:
            for connection in list(running.agent._connections):
                connection.send(line)

    running.agent._emit = reordered
    with running.client() as client:
        client.freeze()
        until(lambda: running.calls == ["pause"])
        client.thaw()
        until(lambda: running.calls == ["pause", "resume"])
        seq, _, _ = running.agent.offer({"kind": "proto_in", "label": "x"}, holdable=False)
        assert client.wait_for("item", lambda item: item["seq"] == seq, timeout=5)
    running.agent._emit = original
