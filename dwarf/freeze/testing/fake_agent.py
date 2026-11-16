"""A stand-in for a node's freeze agent, for tests and for trying the UI without a node.

It implements the agent side of the Freeze Control Protocol with the same holding rules
(spec section 5). A "node" is any code that calls :meth:`FakeAgent.offer`; :func:`replay`
drives one from a recorded event stream.
"""

from __future__ import annotations

import gzip
import json
import socket
import socketserver
import threading
import time
from pathlib import Path
from typing import Any, Iterable

from ..client import PROTOCOL
from ..conditions import matches


class FakeAgent:
    def __init__(self, token: str | None = None, start_frozen: bool = False, info: dict[str, Any] | None = None) -> None:
        self.token = token
        self.info = info or {"implementation": "fake", "version": "0", "network": "test", "kinds": {}}
        self.frozen = start_frozen
        self.hold: dict[str, Any] | None = None
        self.conditions: dict[int, dict[str, Any]] = {}
        self.held: dict[int, tuple[dict[str, Any], threading.Event, list[Any]]] = {}
        self.items: dict[int, dict[str, Any]] = {}
        self.state: dict[str, dict[str, Any]] = {}
        self.read_only: set[str] = set()
        # name -> where, enabled, hits
        self.probes: dict[str, dict[str, Any]] = {}
        # Function tracing: every function seen with its call count, and the enabled prefixes.
        self.functions: dict[str, int] = {}
        self.traced: set[str] = set()
        # What a namespace answers to a write, for stores that write later (`{"queued": True, ...}`).
        self.receipts: dict[str, dict[str, Any]] = {}
        # Every `events.subscribe` received, in order, for tests to look at.
        self.subscriptions: list[dict[str, Any]] = []
        self.held_functions: set[str] = set()
        self.injected: list[tuple[str, Any]] = []
        self.deferred: dict[int, tuple[threading.Event, list[Any], list[Any] | None]] = {}
        self.resigning: dict[str, Any] = {
            "enabled": False, "available": False, "error": "not a controlled Freeze devnet"
        }
        self.seq = 0
        self._next_condition = 1
        self._lock = threading.RLock()
        self._connections: list[Any] = []
        self._server: socketserver.ThreadingTCPServer | None = None
        self.port = 0

    # node side -----------------------------------------------------------------------------

    def offer(self, item: dict[str, Any], holdable: bool = True) -> tuple[int, str, Any]:
        """Offer a step. Blocks while it is held. Returns (seq, verdict, replacement)."""
        with self._lock:
            self.seq += 1
            seq = self.seq
            item = {"cold_spots": [], "payload": None, "editable": False, "droppable": False, "from": "", "to": "",
                    "type": "", "label": "", **item, "seq": seq, "t_ms": int(time.time() * 1000)}
            item.pop("held", None)
            for waiting in list(self.deferred):
                until = self.deferred[waiting][2]
                if until is not None and matches(until[0], item):
                    until[1] -= 1
                    if until[1] <= 0:
                        self._resume(waiting, overtaken_by=seq)
            reasons: list[Any] = []
            if holdable:
                if self.frozen:
                    reasons.append("frozen")
                if self.hold is not None and matches(self.hold, item):
                    reasons.append("hold")
                for identifier, condition in list(self.conditions.items()):
                    if matches(condition["predicate"], item):
                        condition["hits"] += 1
                        if condition["action"] == "freeze":
                            reasons.append({"condition": identifier})
                        else:
                            self._emit("condition", {"id": identifier, "seq": seq})
                        if condition["once"]:
                            del self.conditions[identifier]
            item["held"] = bool(reasons)
            self.items[seq] = item
            if not reasons:
                self._emit("item", item)
                return seq, "forward", None
            was_frozen, self.frozen = self.frozen, True
            waiter, verdict = threading.Event(), []
            self.held[seq] = (item, waiter, verdict)
            self._emit("item", item)
            if not was_frozen:
                self._emit("frozen", {"seq": seq, "reasons": reasons})
        waiter.wait()
        return seq, verdict[0], verdict[1]

    def done(self, seq: int, outcome: Any) -> None:
        self._emit("item.done", {"seq": seq, "outcome": outcome})

    # protocol ------------------------------------------------------------------------------

    def _emit(self, event: str, data: Any) -> None:
        line = (json.dumps({"event": event, "data": data}, separators=(",", ":")) + "\n").encode()
        with self._lock:
            for connection in list(self._connections):
                if self._wanted(connection, event, data):
                    connection.send(line)

    def _wanted(self, connection: Any, event: str, data: Any) -> bool:
        """Whether a connection's subscription lets this event through (`events.subscribe`)."""
        wants = getattr(connection, "wants", None)
        if not wants:
            return True
        sent = connection.sent
        if event == "item.done":
            return data["seq"] in sent
        if event != "item":
            return True
        passes = bool(data.get("held"))
        if not passes:
            passes = ("kinds" not in wants or data["kind"] in wants["kinds"]) and \
                ("when" not in wants or matches(wants["when"], data))
            if passes and wants.get("sample", 1) > 1:
                connection.passed += 1
                passes = connection.passed % wants["sample"] == 1
        if passes:
            sent.add(data["seq"])
        else:
            connection.dropped += 1
        return passes

    @staticmethod
    def _selects(prefix: str, name: str) -> bool:
        """Whether a trace prefix selects a function: everything, a raw prefix (``text*``), or whole path segments."""
        prefix = prefix.replace("-", "_")
        if prefix in ("", "all", "*"):
            return True
        if prefix.endswith("*"):
            return name.startswith(prefix[:-1])
        return name == prefix or name.startswith(prefix + "::")

    def _traced(self, name: str) -> bool:
        return any(self._selects(prefix, name) for prefix in self.traced)

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {"frozen": self.frozen, "seq": self.seq, "held": sorted(self.held), "deferred": sorted(self.deferred),
                    "hold": self.hold,
                    "conditions": len(self.conditions), "frozen_ms": 0, "resigning": dict(self.resigning)}

    def _release(self, seq: int, verdict: str, replacement: Any = None) -> None:
        with self._lock:
            if seq not in self.held:
                raise ValueError(f"item {seq} is not held")
            item, waiter, slot = self.held[seq]
            if verdict == "drop" and not item["droppable"] or verdict == "replace" and not item["editable"]:
                raise ValueError(f"item {seq} does not allow that")
            del self.held[seq]
            slot.extend([verdict, replacement])
            released: dict[str, Any] = {"seq": seq, "verdict": verdict}
            if verdict == "replace" and isinstance(replacement, dict) and set(replacement) == {"$raw"}:
                released["raw"] = replacement["$raw"]
            elif verdict == "replace":
                released["payload"] = replacement
            self._emit("released", released)
            waiter.set()

    def _defer(self, seq: int, ms: int | None, until: dict[str, Any] | None) -> None:
        if seq not in self.held:
            raise ValueError(f"item {seq} is not held")
        if ms is None and until is None:
            raise ValueError("needs `ms` or `until`")
        _item, waiter, slot = self.held.pop(seq)
        self.deferred[seq] = (waiter, slot, [until["when"], int(until.get("count", 1))] if until else None)
        self._emit("deferred", {"seq": seq})
        if ms is not None:
            timer = threading.Timer(ms / 1000, self._resume, args=(seq,))
            timer.daemon = True
            timer.start()

    def _resume(self, seq: int, overtaken_by: int | None = None) -> bool:
        with self._lock:
            if seq not in self.deferred:
                return False
            waiter, slot, _until = self.deferred.pop(seq)
            slot.extend(["forward", None])
            released = {"seq": seq, "verdict": "forward", "deferred": True}
            if overtaken_by is not None:
                released["overtaken_by"] = overtaken_by
            self._emit("released", released)
            waiter.set()
            return True

    def thaw(self, keep: dict[str, Any] | None = None) -> None:
        with self._lock:
            for seq in sorted(self.held):
                item = self.held[seq][0]
                if (self.hold is not None and matches(self.hold, item)) or (keep is not None and matches(keep, item)):
                    continue
                self._release(seq, "forward")
            if not self.held:
                self.frozen = False
                self._emit("thawed", {})

    def handle(self, method: str, params: dict[str, Any]) -> Any:
        with self._lock:
            if method == "hello":
                return {"protocol": PROTOCOL, "node": self.info, "status": self.status(),
                        "namespaces": self._namespaces(), "destinations": ["stage:fake"]}
            if method == "status":
                return self.status()
            if method == "resign.get":
                return dict(self.resigning)
            if method == "resign.set":
                if params.get("enabled") and not self.resigning.get("available"):
                    raise ValueError(self.resigning.get("error", "re-signing unavailable"))
                self.resigning["enabled"] = bool(params.get("enabled"))
                return dict(self.resigning)
            if method == "freeze":
                if not self.frozen:
                    self.frozen = True
                    self._emit("frozen", {"reasons": ["requested"]})
                return self.status()
            if method == "thaw":
                self.thaw(params.get("keep"))
                return self.status()
            if method == "step":
                if not self.held:
                    raise ValueError("nothing is held")
                seq = params.get("seq") or min(self.held)
                self._release(seq, "forward")
                return {"seq": seq}
            if method == "run_until":
                identifier = self._add_condition(params["when"], "freeze", True)
                self.thaw()
                return {"condition": identifier}
            if method == "set_hold":
                self.hold = params.get("when")
                return self.status()
            if method == "item.get":
                if params["seq"] not in self.items:
                    raise ValueError("no such item (it may have aged out)")
                return self.items[params["seq"]]
            if method == "item.forward":
                self._release(params["seq"], "forward")
                if params.get("then") == "run":
                    self.thaw()
                return self.status()
            if method == "item.defer":
                self._defer(params["seq"], params.get("ms"), params.get("until"))
                if params.get("then") == "run":
                    self.thaw()
                return self.status()
            if method == "item.resume":
                return {"resumed": self._resume(params["seq"])}
            if method == "item.drop":
                self._release(params["seq"], "drop")
                return self.status()
            if method == "item.replace":
                replacement = {"$raw": params["raw"]} if "raw" in params else params.get("payload")
                if params.get("stale_negative_control") and isinstance(replacement, dict):
                    replacement["$stale_negative_control"] = True
                self._release(params["seq"], "replace", replacement)
                return self.status()
            if method == "item.inject":
                self.injected.append((params["to"], params["payload"]))
                return {}
            if method == "destinations":
                return ["stage:fake"]
            if method == "cond.add":
                return {"id": self._add_condition(params["when"], params.get("action", "freeze"), params.get("once", False))}
            if method == "cond.remove":
                return {"removed": self.conditions.pop(params["id"], None) is not None}
            if method == "cond.list":
                return [{"id": i, "predicate": c["predicate"], "action": c["action"], "once": c["once"], "hits": c["hits"]}
                        for i, c in self.conditions.items()]
            if method == "trace.list":
                prefix = params.get("prefix", "")
                sites = [{"name": name, "hits": hits, "enabled": self._traced(name)}
                         for name, hits in sorted(self.functions.items()) if self._selects(prefix, name)]
                for site in sites:
                    site["held"] = any(self._selects(on, site["name"]) for on in self.held_functions)
                return {"enabled": sorted(self.traced), "holding": sorted(self.held_functions), "sites": sites, "total": len(sites)}
            if method in ("trace.hold", "trace.unhold"):
                prefix = params.get("prefix", "all") if method == "trace.unhold" else params["prefix"]
                before = set(self.held_functions)
                if method == "trace.hold":
                    self.held_functions.add(prefix)
                else:
                    self.held_functions = {on for on in self.held_functions if not self._selects(prefix, on)}
                return {"changed": len(before ^ self.held_functions)}
            if method in ("trace.enable", "trace.disable"):
                prefix = params.get("prefix", "all") if method == "trace.disable" else params["prefix"]
                before = {name for name in self.functions if self._traced(name)}
                if method == "trace.enable":
                    self.traced.add(prefix)
                else:
                    self.traced = {on for on in self.traced if not self._selects(prefix, on)}
                return {"changed": len(before ^ {name for name in self.functions if self._traced(name)})}
            if method == "probe.list":
                return [{"name": name, **self.probes[name]} for name in sorted(self.probes)]
            if method in ("probe.enable", "probe.disable"):
                wanted, changed = params.get("name", "all"), 0
                for name, probe in self.probes.items():
                    matches = wanted == "all" or name == wanted or (wanted.endswith("*") and name.startswith(wanted[:-1]))
                    if matches and probe["enabled"] != (method == "probe.enable"):
                        probe["enabled"], changed = method == "probe.enable", changed + 1
                return {"changed": changed}
            if method == "state.namespaces":
                return self._namespaces()
            if method in ("state.list", "state.get", "state.put", "state.delete"):
                return self._state(method, params)
            raise ValueError(f"unknown method `{method}`")

    def _add_condition(self, when: dict[str, Any], action: str, once: bool) -> int:
        identifier = self._next_condition
        self._next_condition += 1
        self.conditions[identifier] = {"predicate": when, "action": action, "once": once, "hits": 0}
        return identifier

    def _namespaces(self) -> list[str]:
        return sorted(self.state)

    def _state(self, method: str, params: dict[str, Any]) -> Any:
        # Like the real agent: the longest namespace that the path starts with.
        path = params["path"]
        fitting = [name for name in self.state if path == name or path.startswith(name + "/")]
        if not fitting:
            raise ValueError(f"no state namespace for `{path}`")
        namespace = max(fitting, key=len)
        key = path[len(namespace):].lstrip("/")
        if namespace in self.read_only and method in ("state.put", "state.delete"):
            raise ValueError(f"{namespace} is read-only")
        table = self.state[namespace]
        if method == "state.list":
            after = params.get("after")
            keys = [k for k in sorted(table) if k.startswith(key) and (after is None or k > after)]
            return [{"key": k, "value": table[k]} for k in keys[: params.get("limit", 100)]]
        if method == "state.get":
            return table.get(key)
        if method == "state.put":
            table[key] = params["value"]
            return dict(self.receipts.get(namespace, {}))
        table.pop(key, None)
        return {}

    # server --------------------------------------------------------------------------------

    def start(self, host: str = "127.0.0.1", port: int = 0) -> int:
        agent = self

        class Handler(socketserver.StreamRequestHandler):
            def setup(self) -> None:
                super().setup()
                self._send_lock = threading.Lock()
                self.wants: dict[str, Any] = {}
                self.sent: set[int] = set()
                self.passed = self.dropped = 0

            def send(self, line: bytes) -> None:
                try:
                    with self._send_lock:
                        self.wfile.write(line)
                        self.wfile.flush()
                except OSError:
                    with agent._lock:
                        if self in agent._connections:
                            agent._connections.remove(self)

            def handle(self) -> None:
                authenticated = False
                try:
                    for raw in self.rfile:
                        if not raw.strip():
                            continue
                        try:
                            request = json.loads(raw)
                        except ValueError as error:
                            self.send(json.dumps({"id": None, "error": {"code": "bad_json", "message": str(error)}}).encode() + b"\n")
                            continue
                        identifier, method = request.get("id"), request.get("method", "")
                        params = request.get("params") or {}
                        if not authenticated:
                            if method != "hello":
                                self.send(json.dumps({"id": identifier, "error": {"code": "hello_first", "message": "send `hello` first"}}).encode() + b"\n")
                                continue
                            if agent.token is not None and params.get("token") != agent.token:
                                self.send(json.dumps({"id": identifier, "error": {"code": "bad_token", "message": "wrong or missing token"}}).encode() + b"\n")
                                return
                            authenticated = True
                            with agent._lock:
                                agent._connections.append(self)
                        if method == "events.subscribe":
                            self.wants = {key: params[key] for key in ("kinds", "when", "sample") if key in params}
                            agent.subscriptions.append(dict(self.wants))
                            self.send(json.dumps({"id": identifier, "result": {}}).encode() + b"\n")
                            continue
                        try:
                            response = {"id": identifier, "result": agent.handle(method, params)}
                        except (ValueError, KeyError) as error:
                            response = {"id": identifier, "error": {"code": "failed", "message": str(error)}}
                        self.send(json.dumps(response, separators=(",", ":")).encode() + b"\n")
                except OSError:
                    pass
                finally:
                    with agent._lock:
                        if self in agent._connections:
                            agent._connections.remove(self)

        class Server(socketserver.ThreadingTCPServer):
            allow_reuse_address = True
            daemon_threads = True

        self._server = Server((host, port), Handler)
        self.port = self._server.server_address[1]
        threading.Thread(target=self._server.serve_forever, name="fake-agent", daemon=True).start()
        return self.port

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        with self._lock:
            for seq in list(self.held):
                self._release(seq, "forward")
            # A node that stops takes its connections with it.
            for connection in list(self._connections):
                try:
                    connection.connection.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass


def load_capture(path: Path) -> list[dict[str, Any]]:
    """Events recorded from a real node, one JSON object per line, optionally gzipped."""
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8") as lines:
        return [json.loads(line) for line in lines if line.strip()]


def replay(
    agent: FakeAgent, events: Iterable[dict[str, Any]], delay: float = 0.0, loop: bool = False, holdable: bool = True
) -> threading.Thread:
    """Offer recorded items to ``agent`` in order, from a background thread, as a node would."""

    def run() -> None:
        while True:
            renumbered: dict[int, int] = {}
            for event in events:
                data = event["data"]
                if event["event"] == "item":
                    item = dict(data)
                    original = item.pop("seq")
                    if item.get("parent") in renumbered:
                        item["parent"] = renumbered[item["parent"]]
                    else:
                        item.pop("parent", None)
                    seq, _verdict, _replacement = agent.offer(item, holdable=holdable and item["kind"] != "internal_state")
                    renumbered[original] = seq
                elif event["event"] == "item.done" and data["seq"] in renumbered:
                    agent.done(renumbered[data["seq"]], data.get("outcome"))
                elif event["event"] == "released" and not holdable and data["seq"] in renumbered:
                    # showing a recorded run: keep what the debugger did to each item
                    agent._emit("released", {**data, "seq": renumbered[data["seq"]]})
                if delay:
                    time.sleep(delay)
            if not loop:
                return

    thread = threading.Thread(target=run, name="fake-node", daemon=True)
    thread.start()
    return thread
