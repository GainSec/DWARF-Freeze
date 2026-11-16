"""Source-line stepping: an lldb next to the node, driven from here.

The node must have been built with ``freeze-debugger build --debug-info``, which also builds a
tools image holding lldb, the source tree and ``lldb_driver.py``. That image is run beside the
node's container, sharing its processes, and spoken to over its standard input and output.

A process stopped by a debugger stops its Freeze agent too: while the node is stopped here,
nothing else in Freeze gets an answer from it.
"""

from __future__ import annotations

import json
import queue
import subprocess
import threading
import time
from typing import Any, Callable


class SourceError(Exception):
    pass


def tools_image(image: str) -> str:
    return image + "-tools"


def have_image(image: str) -> bool:
    return subprocess.run(["docker", "image", "inspect", image], capture_output=True).returncode == 0


def command_for(container: str, image: str) -> list[str]:
    """Run the tools image beside ``container``: same processes, same volumes (so the node's
    binary is found where the node runs it from), and allowed to trace."""
    return ["docker", "run", "--rm", "-i", "--pid", f"container:{container}", "--network", "none",
            "--cap-add", "SYS_PTRACE", "--security-opt", "apparmor=unconfined", "--security-opt", "seccomp=unconfined",
            "--volumes-from", f"{container}:ro", "--user", "0", tools_image(image)]


class Debugger:
    def __init__(self, command: list[str], timeout: float = 30.0) -> None:
        self.command, self.timeout = command, timeout
        self._process: subprocess.Popen[str] | None = None
        self._waiting: dict[int, queue.Queue[dict[str, Any]]] = {}
        self._next = 0
        self._lock = threading.Lock()
        self._listeners: list[Callable[[dict[str, Any]], None]] = []
        self.events: queue.Queue[dict[str, Any]] = queue.Queue()
        self.stopped: dict[str, Any] | None = None
        self.running = False
        self.attached = False
        self.gone = False

    def start(self) -> dict[str, Any]:
        self._process = subprocess.Popen(self.command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                         text=True, bufsize=1)
        threading.Thread(target=self._read, name="freeze-source", daemon=True).start()
        try:
            ready = self.events.get(timeout=self.timeout)
        except queue.Empty:
            self.close()
            raise SourceError("the debugger did not start") from None
        if ready.get("event") != "ready":
            raise SourceError(ready.get("error") or "the debugger did not start")
        return ready

    def _read(self) -> None:
        assert self._process is not None and self._process.stdout is not None
        for line in self._process.stdout:
            try:
                message = json.loads(line)
            except ValueError:
                continue
            if "event" in message:
                if message["event"] == "stopped":
                    self.stopped, self.running = message, False
                elif message["event"] == "running":
                    self.running = True
                elif message["event"] == "gone":
                    self.running, self.stopped, self.attached = False, None, False
                self.events.put(message)
                for listener in list(self._listeners):
                    listener(message)
            else:
                waiter = self._waiting.pop(message.get("id"), None)
                if waiter is not None:
                    waiter.put(message)
        self.gone = True
        error = (self._process.stderr.read() if self._process.stderr else "").strip()
        failure = {"event": "closed", "error": error[-400:]}
        self.events.put(failure)
        for waiter in list(self._waiting.values()):
            waiter.put({"error": error[-400:] or "the debugger ended"})
        for listener in list(self._listeners):
            listener(failure)

    def on_event(self, listener: Callable[[dict[str, Any]], None]) -> None:
        self._listeners.append(listener)

    def call(self, do: str, **params: Any) -> Any:
        if self._process is None or self.gone or self._process.stdin is None:
            raise SourceError("the debugger is not running")
        waiter: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=1)
        with self._lock:
            self._next += 1
            number = self._next
            self._waiting[number] = waiter
            try:
                self._process.stdin.write(json.dumps({"id": number, "do": do, **params}) + "\n")
                self._process.stdin.flush()
            except (OSError, ValueError) as error:  # ValueError: the pipe was closed under us
                self._waiting.pop(number, None)
                raise SourceError(str(error)) from error
        try:
            answer = waiter.get(timeout=self.timeout)
        except queue.Empty:
            self._waiting.pop(number, None)
            raise SourceError(f"the debugger did not answer `{do}`") from None
        if "error" in answer:
            raise SourceError(answer["error"])
        return answer.get("ok")

    def attach(self) -> dict[str, Any]:
        about = self.call("attach")
        self.attached = True
        return about

    def wait_stopped(self, timeout: float = 30.0) -> dict[str, Any]:
        """The next stop. Raises if the node ends or the time passes first."""
        deadline = time.monotonic() + timeout
        while True:
            try:
                event = self.events.get(timeout=max(0.0, deadline - time.monotonic()))
            except queue.Empty:
                raise SourceError("the node did not stop") from None
            if event["event"] == "stopped":
                return event
            if event["event"] in ("gone", "closed"):
                raise SourceError(event.get("error") or "the node ended")

    def close(self) -> None:
        """Let the node go and end the debugger."""
        if self._process is None:
            return
        if not self.gone:
            try:
                self.call("detach")
            except SourceError:
                pass
        try:
            if self._process.stdin:
                self._process.stdin.close()
            self._process.wait(timeout=10)
        except (OSError, subprocess.TimeoutExpired):
            self._process.kill()
        self.attached = False


def listing(source: dict[str, Any], line: int) -> list[tuple[int, str, bool]]:
    """The lines of a `source` answer as (number, text, is the current line)."""
    return [(source["first"] + index, text, source["first"] + index == line) for index, text in enumerate(source["lines"])]
