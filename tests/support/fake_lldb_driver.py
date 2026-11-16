"""Stands in for `lldb_driver.py` in tests: answers the same requests about a made-up node."""

import json
import sys

FILE = sys.argv[1]
line = 3
attached = False


def say(message):
    sys.stdout.write(json.dumps(message) + "\n")
    sys.stdout.flush()


def stop(reason):
    frames = [{"index": 0, "function": "select_chain::decide", "file": FILE, "line": line},
              {"index": 1, "function": "tokio::run", "file": "", "line": 0}]
    say({"event": "stopped", "thread": 7, "name": "worker", "reason": reason, "at": frames[0], "frames": frames})


say({"event": "ready", "lldb": "fake"})
for text in sys.stdin:
    request = json.loads(text)
    do = request["do"]
    if do == "attach":
        attached = True
        say({"id": request["id"], "ok": {"pid": 42, "executable": "/target/amaru", "threads": 9, "source_files": 1200}})
        stop("signal SIGSTOP")
    elif do == "break":
        if request.get("line") == 999:
            say({"id": request["id"], "error": "no code there"})
        else:
            say({"id": request["id"], "ok": {"id": 1, "places": 1}})
    elif do in ("step", "next", "out", "continue", "interrupt"):
        say({"id": request["id"], "ok": {}})
        say({"event": "running"})
        line += {"step": 1, "next": 1, "out": 2, "continue": 3, "interrupt": 0}[do]
        stop("breakpoint 1.1" if do == "continue" else "step")
    elif do == "source":
        lines = open(request["file"]).read().splitlines()
        first = max(1, request["line"] - request.get("around", 12))
        say({"id": request["id"], "ok": {"file": request["file"], "first": first, "lines": lines[first - 1:request["line"] + request.get("around", 12)]}})
    elif do == "vars":
        say({"id": request["id"], "ok": [{"name": "tip", "type": "Point", "value": f"slot {line}"}]})
    elif do == "where":
        say({"id": request["id"], "ok": {"thread": 7, "name": "worker", "reason": "step", "at": {"index": 0, "function": "select_chain::decide", "file": FILE, "line": line}, "frames": []}})
    elif do == "detach":
        say({"id": request["id"], "ok": {}})
        break
    else:
        say({"id": request["id"], "error": f"unknown request `{do}`"})
