"""Runs inside the Freeze tools image, next to a node, and drives lldb for `freeze-debugger source`.

Reads one JSON request per line on stdin (``{"id": 1, "do": "break", "file": "x.rs", "line": 10}``)
and writes one JSON line per reply (``{"id": 1, "ok": {...}}`` or ``{"id": 1, "error": "..."}``)
and per event (``{"event": "stopped", ...}``). It needs nothing but lldb's Python module.
"""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
import threading

import glob

# Debian keeps lldb's module outside Python's own path and leaves an empty `lldb` folder
# inside it, so the real one has to come first.
def _version(path: str) -> int:
    digits = "".join(character for character in path.rsplit("-", 1)[-1].split("/")[0] if character.isdigit())
    return int(digits or 0)


def _module_path() -> str | None:
    """Where the newest installed lldb keeps its Python module. Distributions put it outside
    Python's own path, and some leave an empty `lldb` folder inside it, so it has to come first."""
    for tool in sorted(glob.glob("/usr/bin/lldb-[0-9]*"), key=_version, reverse=True) + ["lldb"]:
        try:
            found = subprocess.run([tool, "-P"], capture_output=True, text=True, check=True).stdout.strip()
        except (OSError, subprocess.CalledProcessError):
            continue
        if os.path.exists(os.path.join(found, "lldb", "__init__.py")):
            return found
    for found in sorted(glob.glob("/usr/lib/llvm-*/lib/python3*/*-packages/lldb/__init__.py"), key=_version, reverse=True):
        return os.path.dirname(os.path.dirname(found))
    return None


_found = _module_path()
if _found:
    sys.path.insert(0, _found)

# lldb looks for its helper under a name carrying its full version, which Debian does not install.
if "LLDB_DEBUGSERVER_PATH" not in os.environ:
    for helper in sorted(glob.glob("/usr/lib/llvm-*/bin/lldb-server*"), key=_version, reverse=True) + sorted(glob.glob("/usr/bin/lldb-server*")):
        os.environ["LLDB_DEBUGSERVER_PATH"] = helper
        break

import lldb  # noqa: E402

OUT = threading.Lock()
TEXT = 300


def say(message: dict) -> None:
    with OUT:
        sys.stdout.write(json.dumps(message, separators=(",", ":")) + "\n")
        sys.stdout.flush()


def node_pid(name: str) -> int:
    """The process called ``name`` with the most threads: the node, not a helper it started."""
    best, most = 0, -1
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            with open(f"/proc/{entry}/comm", encoding="utf-8") as comm:
                if comm.read().strip() != name:
                    continue
            threads = len(os.listdir(f"/proc/{entry}/task"))
        except OSError:
            continue
        if threads > most:
            best, most = int(entry), threads
    if not best:
        raise RuntimeError(f"no process called `{name}` is running in this container")
    return best


def short(text: object) -> str:
    text = "" if text is None else str(text)
    return text if len(text) <= TEXT else text[:TEXT] + "…"


class Driver:
    def __init__(self) -> None:
        self.debugger = lldb.SBDebugger.Create()
        self.debugger.SetAsync(True)
        self.rust = self.load_rust_formatters()
        self.target = None
        self.process = None
        self.listener = self.debugger.GetListener()
        self.running = False
        self.done = False
        self.pid = 0

    def load_rust_formatters(self) -> bool:
        """Teach lldb to show Rust strings, vectors, options and enums as what they hold, with
        the scripts of the toolchain that built the node. Without them values still show, raw."""
        home = "/opt/rust-lldb"
        if not os.path.exists(os.path.join(home, "lldb_lookup.py")):
            return False
        interpreter, result = self.debugger.GetCommandInterpreter(), lldb.SBCommandReturnObject()
        interpreter.HandleCommand(f"command script import {home}/lldb_lookup.py", result)
        loaded = result.Succeeded()
        if loaded and os.path.exists(os.path.join(home, "lldb_commands")):
            interpreter.HandleCommand(f"command source -s 1 {home}/lldb_commands", result)
        return loaded

    def show(self, value, depth: int = 1) -> str:
        """A value as text: its summary or plain value, or its fields one level down."""
        text = value.GetSummary() or value.GetValue()
        if text:
            return short(text)
        count = value.GetNumChildren()
        if not count:
            return ""
        if depth <= 0:
            return "{…}"
        parts = []
        for index in range(min(count, 8)):
            child = value.GetChildAtIndex(index)
            parts.append(f"{child.GetName() or index}: {self.show(child, depth - 1) or '…'}")
        return short("{ " + ", ".join(parts) + (", …" if count > 8 else "") + " }")

    # what the node is doing ------------------------------------------------------------------

    def frame_of(self, frame) -> dict:
        entry = frame.GetLineEntry()
        spec = entry.GetFileSpec()
        path = os.path.join(spec.GetDirectory() or "", spec.GetFilename() or "") if spec.IsValid() else ""
        return {"index": frame.GetFrameID(), "function": short(frame.GetDisplayFunctionName() or frame.GetFunctionName() or "?"),
                "file": path, "line": entry.GetLine() if entry.IsValid() else 0}

    def stopped_thread(self):
        interesting = (lldb.eStopReasonBreakpoint, lldb.eStopReasonPlanComplete, lldb.eStopReasonSignal,
                       lldb.eStopReasonException, lldb.eStopReasonWatchpoint, lldb.eStopReasonTrace)
        for thread in self.process:
            if thread.GetStopReason() in interesting:
                self.process.SetSelectedThread(thread)
                return thread
        return self.process.GetSelectedThread()

    def where(self) -> dict:
        thread = self.stopped_thread()
        # The first frame with a source line: a stop inside a library call is shown where the node called it.
        frames = [self.frame_of(thread.GetFrameAtIndex(index)) for index in range(min(thread.GetNumFrames(), 60))]
        here = next((frame for frame in frames if frame["line"]), frames[0] if frames else {})
        return {"thread": thread.GetIndexID(), "name": thread.GetName() or "", "reason": short(thread.GetStopDescription(200)),
                "at": here, "frames": frames}

    def watch(self) -> None:
        event = lldb.SBEvent()
        while not self.done:
            if not self.listener.WaitForEvent(1, event) or not lldb.SBProcess.EventIsProcessEvent(event):
                continue
            state = lldb.SBProcess.GetStateFromEvent(event)
            if state == lldb.eStateStopped:
                if lldb.SBProcess.GetRestartedFromEvent(event):
                    continue
                self.running = False
                say({"event": "stopped", **self.where()})
            elif state in (lldb.eStateRunning, lldb.eStateStepping):
                # A signal the node handles itself stops and restarts it without anyone
                # noticing; only a change is worth saying.
                if not self.running:
                    self.running = True
                    say({"event": "running"})
            elif state in (lldb.eStateExited, lldb.eStateDetached, lldb.eStateCrashed):
                self.running = False
                say({"event": "gone", "state": self.debugger.StateAsCString(state)})

    def release(self) -> None:
        """Let the node go. A process that was stopped when its debugger left stays stopped
        unless it is told to continue, so it is told."""
        if self.process is not None:
            self.process.Detach()
            self.process = None
            if self.pid:
                try:
                    os.kill(self.pid, signal.SIGCONT)
                except OSError:
                    pass

    # requests --------------------------------------------------------------------------------

    def need_stopped(self):
        if self.process is None:
            raise RuntimeError("not attached to the node yet")
        if self.running:
            raise RuntimeError("the node is running; `interrupt` stops it")
        return self.stopped_thread()

    def do(self, request: dict) -> object:
        what = request["do"]
        if what == "attach":
            pid = int(request.get("pid") or node_pid(request.get("name", "amaru")))
            executable = os.readlink(f"/proc/{pid}/exe")
            if not os.path.exists(executable):
                executable = request.get("executable") or f"/proc/{pid}/exe"
            self.target = self.debugger.CreateTarget(executable)
            if not self.target or not self.target.IsValid():
                raise RuntimeError(f"lldb cannot read {executable}")
            error = lldb.SBError()
            self.process = self.target.AttachToProcessWithID(self.listener, pid, error)
            if not error.Success():
                raise RuntimeError(f"cannot attach to process {pid}: {error.GetCString()}")
            self.pid = pid
            threading.Thread(target=self.watch, daemon=True).start()
            units = self.target.GetModuleAtIndex(0).GetNumCompileUnits() if self.target.GetNumModules() else 0
            return {"pid": pid, "executable": executable, "threads": self.process.GetNumThreads(), "source_files": units}
        if self.target is None:
            raise RuntimeError("not attached to the node yet")
        if what == "break":
            if request.get("function"):
                # Rust names carry their whole path; a part of it is enough to find the function.
                point = self.target.BreakpointCreateByRegex(re.escape(request["function"]))
            else:
                point = self.target.BreakpointCreateByLocation(request["file"], int(request["line"]))
            if point.GetNumLocations() == 0:
                self.target.BreakpointDelete(point.GetID())
                raise RuntimeError("no code there: check the file and line, and that the node was built with `--debug-info`")
            return {"id": point.GetID(), "places": point.GetNumLocations()}
        if what == "breaks":
            found = []
            for index in range(self.target.GetNumBreakpoints()):
                point = self.target.GetBreakpointAtIndex(index)
                place = point.GetLocationAtIndex(0).GetAddress().GetLineEntry() if point.GetNumLocations() else None
                found.append({"id": point.GetID(), "hits": point.GetHitCount(), "places": point.GetNumLocations(),
                              "file": place.GetFileSpec().GetFilename() if place and place.IsValid() else "",
                              "line": place.GetLine() if place and place.IsValid() else 0})
            return found
        if what == "unbreak":
            if request.get("id") is None:
                self.target.DeleteAllBreakpoints()
                return {"removed": "all"}
            if not self.target.BreakpointDelete(int(request["id"])):
                raise RuntimeError(f"no breakpoint {request['id']}")
            return {"removed": int(request["id"])}
        if what == "interrupt":
            error = self.process.Stop()
            if not error.Success():
                raise RuntimeError(error.GetCString())
            return {}
        if what == "continue":
            self.need_stopped()
            error = self.process.Continue()
            if not error.Success():
                raise RuntimeError(error.GetCString())
            return {}
        if what in ("step", "next", "out"):
            thread = self.need_stopped()
            if request.get("frame") is not None:
                thread.SetSelectedFrame(int(request["frame"]))
            {"step": thread.StepInto, "next": thread.StepOver, "out": thread.StepOut}[what]()
            return {}
        if what == "where":
            self.need_stopped()
            return self.where()
        if what == "threads":
            self.need_stopped()
            return [{"thread": thread.GetIndexID(), "name": thread.GetName() or "",
                     "at": self.frame_of(thread.GetFrameAtIndex(0)) if thread.GetNumFrames() else {}} for thread in self.process]
        if what == "vars":
            thread = self.need_stopped()
            frame = thread.GetFrameAtIndex(int(request.get("frame", self.where()["at"].get("index", 0))))
            found = []
            wanted, depth = request.get("name"), 3 if request.get("name") else 1
            for value in frame.GetVariables(True, True, False, True):
                if wanted and value.GetName() != wanted:
                    continue
                found.append({"name": value.GetName() or "", "type": short(value.GetTypeName()), "value": self.show(value, depth)})
                if len(found) >= 80:
                    break
            if wanted and not found:
                raise RuntimeError(f"no variable called `{wanted}` here")
            return found
        if what == "source":
            path, line, around = request["file"], int(request["line"]), int(request.get("around", 12))
            with open(path, encoding="utf-8", errors="replace") as source:
                lines = source.read().splitlines()
            first = max(1, line - around)
            return {"file": path, "first": first, "lines": lines[first - 1:line + around]}
        if what == "detach":
            self.done = True
            self.release()
            return {}
        raise RuntimeError(f"unknown request `{what}`")


def main() -> int:
    driver = Driver()
    say({"event": "ready", "lldb": lldb.SBDebugger.GetVersionString().splitlines()[0], "rust": driver.rust})
    for line in sys.stdin:
        if not line.strip():
            continue
        request = {}
        try:
            request = json.loads(line)
            say({"id": request.get("id"), "ok": driver.do(request)})
            if request.get("do") == "attach" and driver.process.GetState() == lldb.eStateStopped:
                # Attaching stops the node without an event of its own.
                say({"event": "stopped", **driver.where()})
        except Exception as error:  # every failure is an answer; the driver itself keeps going
            say({"id": request.get("id"), "error": str(error) or type(error).__name__})
        if driver.done:
            break
    # Leaving without detaching would kill the node along with the debugger.
    driver.release()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
