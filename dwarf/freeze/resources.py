"""What the node costs while it is being tested: memory and CPU of its container, and queues.

A test that makes a node slow or makes it grow is a finding too, and it never shows up as a
rejection.
"""

from __future__ import annotations

import json
import re
import subprocess
import time
from collections import Counter
from typing import Any, Callable

from .session import Session

_UNITS = {"b": 1, "kb": 1000, "mb": 1000**2, "gb": 1000**3, "kib": 1024, "mib": 1024**2, "gib": 1024**3}


def _bytes(text: str) -> int:
    match = re.match(r"\s*([\d.]+)\s*([a-zA-Z]+)", text)
    return int(float(match.group(1)) * _UNITS.get(match.group(2).lower(), 1)) if match else 0


def parse_stats(line: str) -> dict[str, Any]:
    """One line of ``docker stats --format json`` as numbers."""
    row = json.loads(line)
    return {"at": time.time(), "cpu_percent": float(str(row.get("CPUPerc", "0")).rstrip("%") or 0),
            "memory_bytes": _bytes(str(row.get("MemUsage", "0B")).split("/")[0]), "pids": int(row.get("PIDs") or 0)}


def sample(container: str) -> dict[str, Any] | None:
    result = subprocess.run(["docker", "stats", "--no-stream", "--format", "json", container], capture_output=True, text=True)
    line = result.stdout.strip().splitlines()[0] if result.returncode == 0 and result.stdout.strip() else ""
    return parse_stats(line) if line else None


def watch(container: str, every: float, duration: float, on_sample: Callable[[dict[str, Any]], None]) -> list[dict[str, Any]]:
    samples, deadline = [], time.time() + duration
    while time.time() < deadline:
        current = sample(container)
        if current is not None:
            samples.append(current)
            on_sample(current)
        time.sleep(every)
    return samples


def verdict(samples: list[dict[str, Any]], growth: float = 0.25) -> str:
    """A sentence on whether memory grew noticeably over the samples."""
    if len(samples) < 2:
        return "not enough samples to judge"
    first, last = samples[0]["memory_bytes"], samples[-1]["memory_bytes"]
    peak_cpu = max(s["cpu_percent"] for s in samples)
    change = (last - first) / first if first else 0.0
    grew = "MEMORY GREW" if change > growth else "memory steady"
    return f"{grew}: {first / 1e6:.0f} MB → {last / 1e6:.0f} MB ({change:+.0%}); peak CPU {peak_cpu:.0f}%"


def backlog(session: Session) -> list[tuple[str, int]]:
    """Messages sent to each part of the node and not yet handled by it, largest first.

    Counted from the session: a send to a stage adds one, that stage taking an input removes one.
    """
    waiting: Counter[str] = Counter()
    for item in list(session.items.values()):
        if item["kind"] == "internal_action" and item.get("label") == "send" and item.get("to", "").startswith("stage:"):
            waiting[item["to"]] += 1
        elif item["kind"] == "internal_input" and item.get("parent") and item.get("to") in waiting:
            waiting[item["to"]] -= 1
    return sorted(((stage, count) for stage, count in waiting.items() if count > 0), key=lambda pair: -pair[1])
