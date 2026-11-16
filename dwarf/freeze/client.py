"""Client for the Freeze Control Protocol (newline-delimited JSON over TCP).

One background thread reads the socket. Responses are matched to the calls waiting for them;
everything else is an event, delivered to listeners and kept in a queue for ``next_event``.
"""

from __future__ import annotations

import json
import queue
import socket
import threading
from typing import Any, Callable, Iterator

PROTOCOL = "fcp/1"


class FreezeError(Exception):
    """The agent refused a request, or the connection failed."""

    def __init__(self, message: str, code: str = "failed") -> None:
        super().__init__(message)
        self.code = code


class FreezeClient:
    def __init__(self, host: str, port: int, token: str | None = None, timeout: float = 10.0, name: str = "") -> None:
        self.host, self.port, self.token, self.timeout = host, port, token, timeout
        self.name = name
        self.hello: dict[str, Any] = {}
        self._socket: socket.socket | None = None
        self._send_lock = threading.Lock()
        self._next_id = 0
        self._waiting: dict[int, queue.Queue[dict[str, Any]]] = {}
        self._events: queue.Queue[dict[str, Any]] = queue.Queue()
        self._listeners: list[Callable[[str, Any], None]] = []
        self._closed = threading.Event()
        self._reader: threading.Thread | None = None

    # connection ----------------------------------------------------------------------------

    def connect(self) -> dict[str, Any]:
        self._socket = socket.create_connection((self.host, self.port), timeout=self.timeout)
        self._socket.settimeout(None)
        self._socket.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self._reader = threading.Thread(target=self._read_loop, name="freeze-client", daemon=True)
        self._reader.start()
        said = ({"token": self.token} if self.token else {}) | ({"client": self.name} if self.name else {})
        self.hello = self.call("hello", **said)
        if self.hello.get("protocol") != PROTOCOL:
            raise FreezeError(f"agent speaks {self.hello.get('protocol')!r}, this client speaks {PROTOCOL!r}", "protocol")
        return self.hello

    def close(self) -> None:
        self._closed.set()
        if self._socket is not None:
            try:
                self._socket.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            self._socket.close()

    def __enter__(self) -> "FreezeClient":
        if self._socket is None:
            self.connect()
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    @property
    def connected(self) -> bool:
        return self._socket is not None and not self._closed.is_set()

    def _read_loop(self) -> None:
        assert self._socket is not None
        try:
            with self._socket.makefile("r", encoding="utf-8", newline="\n") as lines:
                for line in lines:
                    if not line.strip():
                        continue
                    message = json.loads(line)
                    if "event" in message:
                        self._events.put(message)
                        for listener in list(self._listeners):
                            listener(message["event"], message.get("data"))
                    else:
                        waiter = self._waiting.pop(message.get("id"), None)
                        if waiter is not None:
                            waiter.put(message)
        except (OSError, ValueError):
            pass
        finally:
            self._closed.set()
            for waiter in list(self._waiting.values()):
                waiter.put({"error": {"code": "closed", "message": "connection closed"}})
            self._events.put({"event": "closed", "data": {}})
            for listener in list(self._listeners):
                listener("closed", {})

    # requests ------------------------------------------------------------------------------

    def call(self, method: str, **params: Any) -> Any:
        if self._socket is None or self._closed.is_set():
            raise FreezeError("not connected", "closed")
        waiter: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=1)
        with self._send_lock:
            self._next_id += 1
            request_id = self._next_id
            self._waiting[request_id] = waiter
            line = json.dumps({"id": request_id, "method": method, "params": params}, separators=(",", ":"))
            try:
                self._socket.sendall(line.encode() + b"\n")
            except OSError as error:
                self._waiting.pop(request_id, None)
                raise FreezeError(str(error), "closed") from error
        try:
            response = waiter.get(timeout=self.timeout)
        except queue.Empty:
            self._waiting.pop(request_id, None)
            raise FreezeError(f"no answer to `{method}` within {self.timeout}s", "timeout") from None
        if "error" in response:
            error = response["error"]
            raise FreezeError(error.get("message", "failed"), error.get("code", "failed"))
        return response.get("result")

    def status(self) -> dict[str, Any]:
        return self.call("status")

    def resigning(self) -> dict[str, Any]:
        return self.call("resign.get")

    def set_resigning(self, enabled: bool) -> dict[str, Any]:
        return self.call("resign.set", enabled=enabled)

    def freeze(self) -> dict[str, Any]:
        return self.call("freeze")

    def thaw(self, keep: dict[str, Any] | None = None) -> dict[str, Any]:
        """Let the node run. Items matching ``keep`` (a predicate) stay held, as do those matching the hold filter."""
        return self.call("thaw", keep=keep) if keep is not None else self.call("thaw")

    def step(self, seq: int | None = None) -> int:
        return self.call("step", **({} if seq is None else {"seq": seq}))["seq"]

    def run_until(self, when: dict[str, Any]) -> int:
        return self.call("run_until", when=when)["condition"]

    def set_hold(self, when: dict[str, Any] | None) -> dict[str, Any]:
        return self.call("set_hold", when=when)

    def item(self, seq: int) -> dict[str, Any]:
        return self.call("item.get", seq=seq)

    def forward(self, seq: int, then_run: bool = False) -> dict[str, Any]:
        return self.call("item.forward", seq=seq, **({"then": "run"} if then_run else {}))

    def defer(self, seq: int, ms: int | None = None, until: dict[str, Any] | None = None, count: int = 1,
              then_run: bool = False) -> dict[str, Any]:
        """Set a held item aside while the node runs on: for ``ms`` milliseconds, or until ``count``
        items matching the predicate ``until`` have passed it."""
        params: dict[str, Any] = {"seq": seq}
        if ms is not None:
            params["ms"] = int(ms)
        if until is not None:
            params["until"] = {"when": until, "count": count}
        if then_run:
            params["then"] = "run"
        return self.call("item.defer", **params)

    def resume(self, seq: int) -> bool:
        """Let an item that was set aside go on now."""
        return self.call("item.resume", seq=seq)["resumed"]

    def drop(self, seq: int) -> dict[str, Any]:
        return self.call("item.drop", seq=seq)

    def replace(
        self,
        seq: int,
        payload: Any = None,
        raw: bytes | None = None,
        *,
        stale_negative_control: bool = False,
    ) -> dict[str, Any]:
        if raw is not None:
            return self.call(
                "item.replace",
                seq=seq,
                raw=raw.hex(),
                **({"stale_negative_control": True} if stale_negative_control else {}),
            )
        if stale_negative_control:
            raise ValueError("stale negative-control mode is only valid for exact raw bytes")
        return self.call("item.replace", seq=seq, payload=payload)

    def inject(self, to: str, payload: Any) -> None:
        self.call("item.inject", to=to, payload=payload)

    def destinations(self) -> list[str]:
        return self.call("destinations")

    def add_condition(self, when: dict[str, Any], action: str = "freeze", once: bool = False) -> int:
        return self.call("cond.add", when=when, action=action, once=once)["id"]

    def remove_condition(self, condition_id: int) -> bool:
        return self.call("cond.remove", id=condition_id)["removed"]

    def conditions(self) -> list[dict[str, Any]]:
        return self.call("cond.list")

    def namespaces(self) -> list[str]:
        return self.call("state.namespaces")

    def state_list(self, path: str, after: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"path": path, "limit": limit}
        if after is not None:
            params["after"] = after
        return self.call("state.list", **params)

    def state_get(self, path: str) -> Any:
        return self.call("state.get", path=path)

    def state_put(self, path: str, value: Any) -> dict[str, Any]:
        """Write a value. The answer is empty when it was written at once; a store that writes
        later says so (``queued``) and where it will take effect (``scope``)."""
        return self.call("state.put", path=path, value=value) or {}

    def state_delete(self, path: str) -> dict[str, Any]:
        return self.call("state.delete", path=path) or {}

    # probes: named places inside the node's functions ------------------------------------------

    def probes(self) -> list[dict[str, Any]]:
        """Every probe the node has: ``name``, ``where``, ``enabled``, ``hits``. Hits count even while disabled."""
        return self.call("probe.list")

    def probe_enable(self, name: str = "all") -> int:
        """Report an item each time these probes are reached. ``name`` is one probe, a prefix ending in ``*``, or ``all``."""
        return self.call("probe.enable", name=name)["changed"]

    def probe_disable(self, name: str = "all") -> int:
        return self.call("probe.disable", name=name)["changed"]

    # what this connection is sent -------------------------------------------------------------

    def supports(self, feature: str) -> bool:
        """Whether the node lists ``feature``, or a refinement of it (``probes`` for ``probes:ledger``)."""
        features = self.hello.get("node", {}).get("features", [])
        return any(name == feature or name.startswith(feature + ":") for name in features)

    def subscribe(self, kinds: list[str] | None = None, when: dict[str, Any] | None = None, sample: int | None = None) -> None:
        """Choose which items this connection is sent: of these ``kinds``, matching ``when``, one in
        ``sample``. Held items, verdicts and freezes are always sent. With no arguments: everything.

        Through a broker a connection may be sent more than it asked for, when another asks for more.
        """
        params: dict[str, Any] = {}
        if kinds is not None:
            params["kinds"] = kinds
        if when is not None:
            params["when"] = when
        if sample is not None and sample > 1:
            params["sample"] = int(sample)
        self.call("events.subscribe", **params)

    # function tracing --------------------------------------------------------------------------

    def traces(self, prefix: str = "") -> dict[str, Any]:
        """What is traced: ``enabled`` prefixes and the functions seen so far (``sites``)."""
        return self.call("trace.list", **({"prefix": prefix} if prefix else {}))

    def trace_enable(self, prefix: str) -> int:
        """Report every call of the functions whose full name starts with ``prefix``."""
        return self.call("trace.enable", prefix=prefix)["changed"]

    def trace_disable(self, prefix: str = "all") -> int:
        return self.call("trace.disable", prefix=prefix)["changed"]

    def trace_hold(self, prefix: str) -> int:
        """Hold the node each time one of these functions is entered, where that is possible."""
        return self.call("trace.hold", prefix=prefix)["changed"]

    def trace_unhold(self, prefix: str = "all") -> int:
        return self.call("trace.unhold", prefix=prefix)["changed"]

    # the broker, when the connection goes through one ---------------------------------------------

    @property
    def broker(self) -> dict[str, Any] | None:
        """What the broker said about itself in ``hello``; ``None`` when talking to an agent directly."""
        return self.hello.get("broker")

    def who(self) -> dict[str, Any]:
        return self.call("session.who")

    def claim(self, force: bool = False) -> dict[str, Any]:
        """Make this client the only one that may change the node."""
        return self.call("session.claim", force=force)

    def release(self) -> dict[str, Any]:
        return self.call("session.release")

    def session_events(self, limit: int = 5000) -> list[dict[str, Any]]:
        """The most recent events the broker recorded, oldest first."""
        return self.call("session.events", limit=limit)["events"]

    def peers_status(self) -> dict[str, Any]:
        return self.call("peers.status")

    def peers_pause(self) -> dict[str, Any]:
        return self.call("peers.pause")

    def peers_resume(self) -> dict[str, Any]:
        return self.call("peers.resume")

    def peers_auto(self, on: bool) -> dict[str, Any]:
        return self.call("peers.auto", on=on)

    # events --------------------------------------------------------------------------------

    def on_event(self, listener: Callable[[str, Any], None]) -> Callable[[], None]:
        """Call ``listener(name, data)`` for every event, on the reader thread. Returns an unsubscribe."""
        self._listeners.append(listener)
        return lambda: self._listeners.remove(listener) if listener in self._listeners else None

    def next_event(self, timeout: float | None = None) -> dict[str, Any] | None:
        try:
            return self._events.get(timeout=timeout)
        except queue.Empty:
            return None

    def events(self, timeout: float | None = None) -> Iterator[dict[str, Any]]:
        """Events in arrival order until the connection closes or ``timeout`` passes with none."""
        while True:
            event = self.next_event(timeout)
            if event is None or event["event"] == "closed":
                return
            yield event

    def wait_for(
        self, event: str, where: Callable[[Any], bool] = lambda _data: True, timeout: float = 30.0
    ) -> Any:
        """The data of the next ``event`` for which ``where(data)`` holds."""
        import time

        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise FreezeError(f"no `{event}` event within {timeout}s", "timeout")
            message = self.next_event(remaining)
            if message is None:
                continue
            if message["event"] == "closed":
                raise FreezeError("connection closed", "closed")
            if message["event"] == event and where(message.get("data")):
                return message.get("data")

    def wait_held(self, where: Callable[[dict[str, Any]], bool] = lambda _item: True, timeout: float = 30.0) -> dict[str, Any]:
        """The next held item matching ``where``."""
        return self.wait_for("item", lambda item: bool(item.get("held")) and where(item), timeout)
