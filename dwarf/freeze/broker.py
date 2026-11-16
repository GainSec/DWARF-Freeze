"""The broker: one process between a Freeze node's agent and everything that talks to it.

It speaks the Freeze Control Protocol on both sides, so a client cannot tell it from an agent
except by ``hello.broker``. On top of forwarding it does three things an agent cannot:

- it records the session whether or not a UI is attached, and hands the recent part of it to
  a UI that attaches later (``session.events``);
- it pauses the devnet's other nodes while the Freeze node is frozen, and lets them run again
  when it runs (``peers.auto``, ``peers.pause``, ``peers.resume``, ``peers.status``);
- it lets one client claim the session, after which the others can watch but not change the
  node (``session.claim``, ``session.release``, ``session.who``).

Claiming is a courtesy between clients of the broker, not a security boundary: the token
still decides who may connect at all, and the agent's own port is not guarded by it.
"""

from __future__ import annotations

import argparse
import asyncio
import collections
import hmac
import json
import os
import signal
import sys
import time
from pathlib import Path
from typing import Any, Callable

PROTOCOL = "fcp/1"
OFFSET = 50
"""A node's broker listens this many ports above the node's agent port."""

GRACE = 0.25
"""Seconds a freeze or a thaw has to last before the other nodes are paused or resumed, so
single steps do not pause and resume them every time."""

READS = frozenset({
    "hello", "status", "item.get", "destinations", "cond.list", "state.namespaces", "state.list", "state.get",
    "probe.list", "trace.list", "resign.get", "session.who", "session.events", "session.claim", "session.release",
    "peers.status",
})
"""Requests an observer may make while another client has claimed the session."""

KEPT = 50_000
"""How many recent events are kept in memory for a client that attaches later."""


def _line(message: dict[str, Any]) -> bytes:
    return json.dumps(message, separators=(",", ":")).encode() + b"\n"


class Peers:
    """Pausing and resuming the other nodes. ``pause`` and ``resume`` are plain blocking calls."""

    def __init__(self, pause: Callable[[], None] | None, resume: Callable[[], None] | None, auto: bool,
                 others: dict[str, tuple[str, int]] | None = None, token: str | None = None, together: bool = False) -> None:
        self._pause, self._resume = pause, resume
        self.available = pause is not None and resume is not None
        self.auto = auto and self.available
        # The devnet's other Freeze nodes, frozen and thawed with this one when `together` is set.
        self.others, self.token, self.together = others or {}, token, together and bool(others)
        self.froze: set[str] = set()
        self.paused = False
        self.since: float | None = None
        self.error: str | None = None
        self._lock = asyncio.Lock()

    def status(self) -> dict[str, Any]:
        return {"available": self.available, "auto": self.auto, "paused": self.paused,
                "freeze_nodes": self.together, "froze": sorted(self.froze),
                "seconds": round(time.time() - self.since, 1) if self.paused and self.since else 0.0,
                "error": self.error}

    async def set(self, paused: bool, adopt: bool = False) -> None:
        if not self.available:
            raise RuntimeError("this node has no other nodes the broker can pause")
        async with self._lock:
            if paused == self.paused:
                return
            try:
                await asyncio.to_thread(self._pause if paused else self._resume)  # type: ignore[arg-type]
                if self.together or self.froze:
                    await asyncio.to_thread(self._others, paused, adopt)
            except Exception as error:  # the reason is reported to whoever asks for the status
                self.error = str(error)
                raise
            self.error = None
            self.paused = paused
            self.since = time.time() if paused else None


    def _others(self, freeze: bool, adopt: bool = False) -> None:
        """Freeze the other Freeze nodes, or let run again the ones this broker froze. A node
        someone froze by hand before is left frozen when the rest run; with ``adopt``, a node
        found frozen counts as frozen by this broker."""
        from .client import FreezeClient, FreezeError

        for name, (host, port) in self.others.items():
            if not freeze and name not in self.froze:
                continue
            client = FreezeClient(host, port, token=self.token, timeout=5.0, name="broker")
            try:
                client.connect()
                if freeze:
                    if not client.status().get("frozen"):
                        client.freeze()
                        self.froze.add(name)
                    elif adopt:
                        self.froze.add(name)
                else:
                    client.thaw()
                    self.froze.discard(name)
            except (OSError, FreezeError):
                continue  # a node that is not answering is neither frozen nor thawed by us
            finally:
                client.close()


class Client:
    def __init__(self, number: int, writer: asyncio.StreamWriter) -> None:
        self.number, self.writer = number, writer
        self.name = ""
        self.since = time.time()
        self.ready = False
        # What this client asked to be sent (`events.subscribe`); None is everything.
        self.wants: dict[str, Any] | None = None

    def send(self, data: bytes) -> None:
        if not self.writer.is_closing():
            self.writer.write(data)


class Broker:
    def __init__(self, host: str, port: int, token: str, listen_port: int, sessions: Path | None = None,
                 peers: Peers | None = None, node: str = "", listen_host: str = "127.0.0.1",
                 prepare: Callable[[], bool] | None = None, record: str = "all") -> None:
        self.upstream_address = (host, port)
        self.token, self.listen_host, self.listen_port = token, listen_host, listen_port
        self.sessions, self.node = sessions, node
        self.peers = peers or Peers(None, None, False)
        self.hello: dict[str, Any] = {}
        self.frozen = False
        self.clients: dict[int, Client] = {}
        self.owner: int | None = None
        self._numbers = 0
        self._requests = 0
        self._waiting: dict[int, tuple[Client | None, Any, asyncio.Future[dict[str, Any]] | None]] = {}
        self._upstream: asyncio.StreamWriter | None = None
        self._recent: collections.deque[bytes] = collections.deque(maxlen=KEPT)
        self._file: Any = None
        self._flushed = 0.0
        self.directory: Path | None = None
        self._settle: asyncio.Task[None] | None = None
        self._server: asyncio.AbstractServer | None = None
        self.stopping = asyncio.Event()
        # Called (on a thread) each time the agent is first reached; True means it restarted the
        # node, so clients are kept out until the agent is back.
        self._prepare = prepare
        self.preparing = False
        # "all" keeps everything on disk and so asks the agent for everything; "held" records
        # whatever the clients' subscriptions bring in (held items and verdicts always); "none"
        # writes nothing.
        self.record = record
        self._asked: dict[str, Any] | None = None

    # the agent's side ----------------------------------------------------------------------

    async def _ask(self, method: str, **params: Any) -> Any:
        """A request of the broker's own to the agent."""
        if self._upstream is None:
            raise RuntimeError("the node's agent is not connected")
        self._requests += 1
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._waiting[self._requests] = (None, None, future)
        self._upstream.write(_line({"id": self._requests, "method": method, "params": params}))
        response = await asyncio.wait_for(future, 10.0)
        if "error" in response:
            raise RuntimeError(response["error"].get("message", "failed"))
        return response.get("result")

    async def _connect(self) -> asyncio.StreamReader:
        reader, writer = await asyncio.open_connection(*self.upstream_address, limit=64 * 1024 * 1024)
        self._upstream = writer
        return reader

    async def _read_agent(self, reader: asyncio.StreamReader) -> None:
        while True:
            line = await reader.readline()
            if not line:
                return
            # Events are passed on as the bytes they came in; only what the broker itself acts on
            # is looked at. An agent may write an event's name before or after its data.
            if line.startswith(b'{"event":"item"') or line.endswith(b'"event":"item"}\n'):
                self._event(line)
                continue
            try:
                message = json.loads(line)
            except ValueError:
                continue
            if "event" in message:
                self._event(line, message["event"])
                continue
            client, original, future = self._waiting.pop(message.get("id"), (None, None, None))
            if future is not None:
                if not future.done():
                    future.set_result(message)
            elif client is not None:
                message["id"] = original
                client.send(_line(message))

    def _event(self, line: bytes, name: str = "item") -> None:
        self._recent.append(line)
        if self._file is not None:
            now = time.time()
            self._file.write(b'{"at":%.3f,' % now + line[1:])
            if now - self._flushed > 0.5:
                self._file.flush()
                self._flushed = now
        for client in self.clients.values():
            if client.ready:
                client.send(line)
        if name == "frozen":
            self._node_is(True)
        elif name == "thawed":
            self._node_is(False)

    def _node_is(self, frozen: bool, again: bool = False) -> None:
        changed = frozen != self.frozen or again
        self.frozen = frozen
        if self._file is not None:
            self._file.flush()
        if not self.peers.auto:
            return
        # A node told to run while it is running says so again; that must not put off the
        # moment the other nodes follow it.
        if not changed and (self.peers.paused == frozen or (self._settle is not None and not self._settle.done())):
            return
        if self._settle is not None:
            self._settle.cancel()
        self._settle = asyncio.get_running_loop().create_task(self._follow(frozen, adopt=again))

    async def _follow(self, frozen: bool, adopt: bool = False) -> None:
        """Bring the other nodes into the node's state, once that state has lasted a moment.
        ``adopt``: this is the state the node was found in, not a change; Freeze nodes found
        frozen with it (a devnet started frozen) are then let run with it too."""
        try:
            await asyncio.sleep(GRACE)
            if self.frozen == frozen and self.peers.auto:
                await self.peers.set(frozen, adopt)
                self._announce("peers", self.peers.status())
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self._announce("log", {"level": "error", "message": f"broker: the other nodes: {error}"})

    def _announce(self, event: str, data: Any) -> None:
        """An event of the broker's own, to every client and into the recording."""
        self._event(_line({"event": event, "data": data}), event)

    def _upstream_filter(self) -> dict[str, Any]:
        """What to ask the agent for: everything any client wants, and everything if it is all recorded."""
        clients = [client for client in self.clients.values() if client.ready]
        if self.record == "all" or any(client.wants is None for client in clients):
            return {}
        # A client that asked for no kinds at all wants only what is always sent; it has no say
        # in what the others get.
        asking = [client.wants or {} for client in clients if (client.wants or {}).get("kinds") != []]
        if not asking:
            return {"kinds": []}
        wanted = []
        for wants in asking:
            terms = []
            if "kinds" in wants:
                terms.append({"op": "kind", "in": wants["kinds"]})
            if "when" in wants:
                terms.append(wants["when"])
            wanted.append({"op": "and", "args": terms} if terms else {"op": "true"})
        asked: dict[str, Any] = {} if wanted == [{"op": "true"}] else {"when": {"op": "or", "args": wanted}}
        sample = min(int(wants.get("sample", 1)) for wants in asking)
        if sample > 1:
            asked["sample"] = sample
        return asked or {}

    async def _resubscribe(self) -> None:
        """Bring the agent's subscription in line with what the clients now want."""
        if self._upstream is None or "events:filter" not in self.hello.get("node", {}).get("features", []):
            return
        asked = self._upstream_filter()
        if asked == self._asked:
            return
        try:
            await self._ask("events.subscribe", **asked)
            self._asked = asked
        except (RuntimeError, asyncio.TimeoutError):
            pass

    def _open_recording(self) -> None:
        if self.sessions is None or self.record == "none":
            return
        label = time.strftime("%Y%m%d-%H%M%S") + (f"-{self.node}" if self.node else "")
        self.directory = self.sessions / label
        self.directory.mkdir(parents=True, exist_ok=True)
        (self.directory / "session.json").write_text(json.dumps({
            "node": self.hello.get("node", {}), "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "first_step": self.hello.get("status", {}).get("seq"), "broker": True, "name": self.node,
        }, indent=2) + "\n", encoding="utf-8")
        self._file = (self.directory / "events.jsonl").open("ab")

    def _close_recording(self) -> None:
        if self._file is not None:
            self._file.close()
            self._file = None

    async def serve_agent(self) -> None:
        """Stay connected to the agent for as long as the broker runs, through node restarts."""
        while not self.stopping.is_set():
            try:
                reader = await self._connect()
            except OSError:
                await self._pause_or_stop(2.0)
                continue
            pump = asyncio.get_running_loop().create_task(self._read_agent(reader))
            try:
                self.hello = await self._ask("hello", token=self.token)
                if self.hello.get("protocol") != PROTOCOL:
                    raise RuntimeError(f"the agent speaks {self.hello.get('protocol')!r}")
                self._recent.clear()
                if self._prepare is not None and self.hello.get("status", {}).get("frozen"):
                    self.preparing = True
                    try:
                        restarted = await asyncio.to_thread(self._prepare)
                    except Exception as error:
                        print(f"broker: preparing the node failed: {error}", flush=True)
                        restarted = False
                    finally:
                        self.preparing = False
                    if restarted:
                        continue
                self._open_recording()
                self._asked = None
                await self._resubscribe()
                self._node_is(bool(self.hello.get("status", {}).get("frozen")), again=True)
                await pump
            except (OSError, RuntimeError, asyncio.TimeoutError):
                pass
            finally:
                pump.cancel()
                self._lost_agent()
            await self._pause_or_stop(1.0)

    async def _pause_or_stop(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self.stopping.wait(), seconds)
        except asyncio.TimeoutError:
            pass

    def _lost_agent(self) -> None:
        if self._upstream is not None:
            self._upstream.close()
            self._upstream = None
        self.hello = {}
        self._close_recording()
        for _client, _original, future in self._waiting.values():
            if future is not None and not future.done():
                future.set_exception(RuntimeError("the node's agent went away"))
        self._waiting.clear()
        # Clients learn of a restart the way they do without a broker: their connection ends.
        for client in list(self.clients.values()):
            client.writer.close()

    # the clients' side ---------------------------------------------------------------------

    def _who(self) -> dict[str, Any]:
        return {"owner": self.owner, "clients": [
            {"client": client.number, "name": client.name, "owner": client.number == self.owner,
             "seconds": round(time.time() - client.since, 1)} for client in self.clients.values() if client.ready]}

    def _about(self, client: Client) -> dict[str, Any]:
        return {"client": client.number, "owner": self.owner, "you_own": self.owner == client.number,
                "session": str(self.directory) if self.directory else None, "peers": self.peers.status(),
                "record": self.record,
                "node": self.node}

    async def _own(self, client: Client, method: str, params: dict[str, Any]) -> Any:
        """Requests the broker answers itself."""
        if method == "session.who":
            return self._who()
        if method == "session.claim":
            if self.owner not in (None, client.number) and not params.get("force"):
                holder = self.clients.get(self.owner)
                raise PermissionError(f"client {self.owner}{f' ({holder.name})' if holder and holder.name else ''} has claimed this session")
            self.owner = client.number
            self._announce("session", self._who())
            return self._who()
        if method == "session.release":
            if self.owner == client.number:
                self.owner = None
                self._announce("session", self._who())
            return self._who()
        if method == "session.events":
            limit = max(0, min(int(params.get("limit", 5000)), KEPT))
            return _RawEvents(list(self._recent)[-limit:] if limit else [])
        if method == "peers.status":
            return self.peers.status()
        if method in ("peers.pause", "peers.resume"):
            await self.peers.set(method == "peers.pause")
            self._announce("peers", self.peers.status())
            return self.peers.status()
        if method == "peers.auto":
            if params.get("on") and not self.peers.available:
                raise RuntimeError("this node has no other nodes the broker can pause")
            self.peers.auto = bool(params.get("on"))
            if "freeze_nodes" in params:
                if params["freeze_nodes"] and not self.peers.others:
                    raise RuntimeError("this devnet has no other Freeze nodes")
                self.peers.together = bool(params["freeze_nodes"])
            if self.peers.auto:
                self._node_is(self.frozen, again=True)
            self._announce("peers", self.peers.status())
            return self.peers.status()
        raise LookupError(method)

    async def _client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._numbers += 1
        client = Client(self._numbers, writer)
        try:
            while True:
                line = await reader.readline()
                if not line:
                    return
                try:
                    request = json.loads(line)
                    method, request_id = request["method"], request.get("id")
                    params = request.get("params") or {}
                except (ValueError, KeyError, TypeError):
                    client.send(_line({"id": None, "error": {"code": "bad_json", "message": "not a request"}}))
                    continue
                if not client.ready:
                    if method != "hello":
                        client.send(_line({"id": request_id, "error": {"code": "hello_first", "message": "send `hello` first"}}))
                        continue
                    if not hmac.compare_digest(str(params.get("token") or ""), self.token):
                        client.send(_line({"id": request_id, "error": {"code": "bad_token", "message": "wrong or missing token"}}))
                        continue
                    if not self.hello or self.preparing:
                        client.send(_line({"id": request_id, "error": {"code": "not_ready", "message": "the node's agent is not answering yet"}}))
                        return
                    try:
                        status = await self._ask("status")
                    except (RuntimeError, asyncio.TimeoutError):
                        return
                    client.name = str(params.get("client") or "")[:80]
                    client.ready = True
                    self.clients[client.number] = client
                    client.send(_line({"id": request_id, "result": {**self.hello, "status": status, "broker": self._about(client)}}))
                    await self._resubscribe()
                    continue
                if method == "hello":
                    client.send(_line({"id": request_id, "result": {**self.hello, "broker": self._about(client)}}))
                    continue
                if method == "events.subscribe":
                    client.wants = {key: params[key] for key in ("kinds", "when", "sample") if key in params} or None
                    await self._resubscribe()
                    client.send(_line({"id": request_id, "result": {}}))
                    continue
                if self.owner not in (None, client.number) and method not in READS:
                    client.send(_line({"id": request_id, "error": {
                        "code": "not_owner", "message": f"client {self.owner} has claimed this session; you can watch, or take it over"}}))
                    continue
                if method.startswith(("session.", "peers.")):
                    try:
                        result = await self._own(client, method, params)
                    except PermissionError as error:
                        client.send(_line({"id": request_id, "error": {"code": "not_owner", "message": str(error)}}))
                    except LookupError:
                        client.send(_line({"id": request_id, "error": {"code": "failed", "message": f"unknown method `{method}`"}}))
                    except Exception as error:
                        client.send(_line({"id": request_id, "error": {"code": "failed", "message": str(error)}}))
                    else:
                        if isinstance(result, _RawEvents):
                            client.send(b'{"id":%s,"result":{"events":[' % json.dumps(request_id).encode()
                                        + b",".join(event.rstrip(b"\n") for event in result.lines) + b"]}}\n")
                        else:
                            client.send(_line({"id": request_id, "result": result}))
                    continue
                if self._upstream is None:
                    client.send(_line({"id": request_id, "error": {"code": "closed", "message": "the node's agent went away"}}))
                    continue
                self._requests += 1
                self._waiting[self._requests] = (client, request_id, None)
                self._upstream.write(_line({"id": self._requests, "method": method, "params": params}))
        except (OSError, asyncio.IncompleteReadError, asyncio.LimitOverrunError):
            pass
        finally:
            self.clients.pop(client.number, None)
            for number, (waiting, _original, _future) in list(self._waiting.items()):
                if waiting is client:
                    del self._waiting[number]
            if self.owner == client.number:
                self.owner = None
                if self.hello:
                    self._announce("session", self._who())
            writer.close()
            await self._resubscribe()

    async def run(self) -> None:
        self._server = await asyncio.start_server(self._client, self.listen_host, self.listen_port, limit=64 * 1024 * 1024)
        agent = asyncio.get_running_loop().create_task(self.serve_agent())
        try:
            await self.stopping.wait()
        finally:
            self._server.close()
            agent.cancel()
            if self._settle is not None:
                self._settle.cancel()
            # Never leave the other nodes paused behind a broker that is gone.
            if self.peers.paused:
                try:
                    await self.peers.set(False)
                except Exception:
                    pass
            self._lost_agent()

    def stop(self) -> None:
        self.stopping.set()


class _RawEvents:
    def __init__(self, lines: list[bytes]) -> None:
        self.lines = lines


# running one as a process ----------------------------------------------------------------


def pid_file(home: Path, node: str) -> Path:
    return home / f"broker-{node}.pid"


def running(home: Path, node: str) -> int | None:
    """The process id of this node's broker, if it is alive."""
    try:
        pid = int(pid_file(home, node).read_text(encoding="utf-8").strip())
        os.kill(pid, 0)
        return pid
    except (OSError, ValueError):
        return None


def start(home: Path, node: str, arguments: list[str]) -> int:
    """Start a broker in the background. ``home`` gets its pid file and its log."""
    import subprocess

    existing = running(home, node)
    if existing is not None:
        return existing
    home.mkdir(parents=True, exist_ok=True)
    with (home / f"broker-{node}.log").open("a", encoding="utf-8") as log:
        process = subprocess.Popen(
            [sys.executable, "-m", "freeze.broker", "--name", node, "--pid-file", str(pid_file(home, node)), *arguments],
            stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, start_new_session=True,
            cwd=str(Path(__file__).resolve().parents[1]),
        )
    pid_file(home, node).write_text(str(process.pid), encoding="utf-8")
    return process.pid


def stop(home: Path, node: str, wait: float = 10.0) -> bool:
    pid = running(home, node)
    if pid is None:
        pid_file(home, node).unlink(missing_ok=True)
        return False
    os.kill(pid, signal.SIGTERM)
    deadline = time.time() + wait
    while time.time() < deadline and running(home, node) is not None:
        time.sleep(0.1)
    if running(home, node) is not None:
        os.kill(pid, signal.SIGKILL)
    pid_file(home, node).unlink(missing_ok=True)
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m freeze.broker", description="The broker between a Freeze node's agent and its clients.")
    parser.add_argument("--host", default="127.0.0.1", help="where the node's agent is")
    parser.add_argument("--port", type=int, required=True, help="the agent's port")
    parser.add_argument("--listen", type=int, required=True, help="the port clients connect to")
    parser.add_argument("--token-file", required=True)
    parser.add_argument("--sessions", help="folder to record sessions in")
    parser.add_argument("--name", default="", help="the node's name, for records and logs")
    parser.add_argument("--devnet-root", help="Freeze data folder of the devnet this node is in")
    parser.add_argument("--pause-peers", action="store_true", help="this broker pauses the devnet's other nodes with its node")
    parser.add_argument("--no-auto-pause", action="store_true", help="do not pause the other nodes when the node freezes")
    parser.add_argument("--also", action="append", default=[], metavar="NAME=HOST:PORT",
                        help="another Freeze node of the devnet (its agent); repeatable")
    parser.add_argument("--freeze-together", action="store_true", help="freeze and thaw the other Freeze nodes with this one")
    parser.add_argument("--start-snapshot", action="store_true",
                        help="save the node's stored chain as `start` before its first step, if this run has none yet")
    parser.add_argument("--record", choices=["all", "held", "none"], default="all",
                        help="what to keep on disk: everything, what the clients are sent (held items and verdicts always), or nothing")
    parser.add_argument("--pid-file")
    args = parser.parse_args(argv)

    token = Path(args.token_file).read_text(encoding="utf-8").strip()
    peers = prepare = None
    if args.devnet_root:
        from . import devnet

        root = Path(args.devnet_root)
        if args.pause_peers:
            others = {}
            for entry in args.also:
                name, _, address = entry.partition("=")
                host, _, port = address.rpartition(":")
                others[name] = (host, int(port))
            peers = Peers(lambda: devnet.pause_peers(root), lambda: devnet.resume_peers(root), auto=not args.no_auto_pause,
                          others=others, token=token, together=args.freeze_together)
        if args.start_snapshot:
            def prepare() -> bool:
                if not devnet.start_snapshot_due(root, args.name):
                    return False
                name = devnet.start_name(root, args.name)
                print(f"broker: saving the node's chain as `{name}` before its first step", flush=True)
                devnet.snapshot_save(root, name, "taken by itself before the node's first step", node=args.name)
                return True

    broker = Broker(args.host, args.port, token, args.listen, Path(args.sessions) if args.sessions else None, peers, args.name,
                    prepare=prepare, record=args.record)

    async def serve() -> None:
        loop = asyncio.get_running_loop()
        for number in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(number, broker.stop)
        await broker.run()

    try:
        asyncio.run(serve())
    finally:
        if args.pid_file:
            Path(args.pid_file).unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
