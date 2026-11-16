"""What happened in a Freeze session: every item, how it ended, and what the debugger changed.

Fed with protocol events. Keeps a bounded window in memory for the UI and, when given a
directory, appends every event to ``events.jsonl`` so the full history survives.
"""

from __future__ import annotations

import json
import re
import threading
import time
from collections import OrderedDict, defaultdict
from pathlib import Path
from typing import Any, Callable, Iterable


_CONNECTION_STAGE = re.compile(r"^\d+-(.+:\d+)-\d+$")
_NUMBERED = re.compile(r"(-\d+)+$")


def part_of(party: str) -> tuple[str | None, str | None]:
    """For an item's ``from``/``to``: (part of the node, peer address), either of which may be None.

    ``stage:track_peers-3`` is the part ``track_peers``; ``stage:0-10.0.0.3:3001-15`` is the
    connection to that peer; ``peer:10.0.0.3:3001`` is the peer itself.
    """
    if party.startswith("peer:"):
        address = party[len("peer:") :]
        return None, address if ":" in address else None
    if not party.startswith("stage:"):
        return None, None
    name = party[len("stage:") :]
    connection = _CONNECTION_STAGE.match(name)
    if connection:
        return "connection", connection.group(1)
    return _NUMBERED.sub("", name) or name, None


class Session:
    def __init__(self, directory: Path | None = None, window: int = 200_000) -> None:
        self.window = window
        self.items: OrderedDict[int, dict[str, Any]] = OrderedDict()
        self.outcomes: dict[int, Any] = {}
        self.releases: dict[int, dict[str, Any]] = {}
        self.children: dict[int, list[int]] = defaultdict(list)
        # Items by the root of their chain, when the node reports causes (`cause` on items).
        self.caused: dict[int, list[int]] = defaultdict(list)
        self.traced = False
        self.held: dict[int, dict[str, Any]] = {}
        self.deferred: dict[int, dict[str, Any]] = {}
        self.logs: list[dict[str, Any]] = []
        self.frozen = False
        self.tampers = 0
        # Running totals for the overview: what each part of the node has handled, and each peer.
        self.parts: dict[str, dict[str, Any]] = {}
        self.peers: dict[str, dict[str, Any]] = {}
        self._stages: dict[str, set[str]] = defaultdict(set)
        self._lock = threading.RLock()
        self.directory = directory
        self._file = None
        if directory is not None:
            directory.mkdir(parents=True, exist_ok=True)
            self._file = (directory / "events.jsonl").open("a", encoding="utf-8")

    def close(self) -> None:
        with self._lock:
            if self._file is not None:
                self._file.close()
                self._file = None

    def record(self, event: str, data: Any) -> None:
        """Apply one protocol event."""
        with self._lock:
            if self._file is not None:
                self._file.write(json.dumps({"at": time.time(), "event": event, "data": data}, separators=(",", ":")) + "\n")
                self._file.flush()
            if event == "item":
                seq = data["seq"]
                self.items[seq] = data
                if data.get("parent"):
                    self.children[data["parent"]].append(seq)
                if data.get("cause"):
                    self.caused[data["cause"]].append(seq)
                    self.traced = True
                if data.get("held"):
                    self.held[seq] = data
                self._tally(data)
                while len(self.items) > self.window:
                    old, _ = self.items.popitem(last=False)
                    self.outcomes.pop(old, None)
                    self.releases.pop(old, None)
                    self.children.pop(old, None)
                    self.caused.pop(old, None)
            elif event == "item.done":
                self.outcomes[data["seq"]] = data.get("outcome")
            elif event == "released":
                self.held.pop(data["seq"], None)
                self.deferred.pop(data["seq"], None)
                self.releases[data["seq"]] = data
                if data.get("verdict") in ("drop", "replace"):
                    self.tampers += 1
            elif event == "deferred":
                # Set aside: still waiting inside the node, but no longer something to decide.
                item = self.held.pop(data["seq"], None)
                if item is not None:
                    self.deferred[data["seq"]] = item
            elif event == "frozen":
                self.frozen = True
            elif event == "thawed":
                self.frozen = False
            elif event == "log":
                self.logs.append(data)
                del self.logs[:-500]

    def consequences(self, seq: int) -> list[dict[str, Any]]:
        """Every step the node took because of item ``seq``, in order. Needs a node that reports causes."""
        with self._lock:
            return [self.items[number] for number in self.caused.get(seq, ()) if number in self.items]

    def _part(self, name: str) -> dict[str, Any]:
        return self.parts.setdefault(name, {"handled": 0, "actions": 0, "last": "", "stages": 0})

    def _peer(self, address: str) -> dict[str, Any]:
        return self.peers.setdefault(
            address, {"received": 0, "sent": 0, "bytes_in": 0, "bytes_out": 0, "last": "", "last_at": 0.0,
                      "protocols": set(), "hostile": False, "closed": False}
        )

    def _tally(self, item: dict[str, Any]) -> None:
        kind, label = item["kind"], item.get("label", "")
        source_part, source_peer = part_of(item.get("from", ""))
        target_part, target_peer = part_of(item.get("to", ""))
        now = item.get("t_ms", 0) / 1000
        if kind == "internal_input" and target_part:
            part = self._part(target_part)
            part["handled"] += 1
            self._stages[target_part].add(item["to"])
            part["stages"] = len(self._stages[target_part])
            if target_peer:
                peer = self._peer(target_peer)
                peer["closed"] = False
        elif kind == "internal_action" and source_part:
            part = self._part(source_part)
            part["actions"] += 1
            part["last"] = label
            if label == "terminate":
                self._stages[source_part].discard(item["from"])
                part["stages"] = len(self._stages[source_part])
                if source_peer:
                    self._peer(source_peer)["closed"] = True
            payload = item.get("payload")
            if label == "PeerAdversarialEffect" and isinstance(payload, dict) and isinstance(payload.get("peer"), dict):
                address = payload["peer"]
                if "address" in address and "port" in address:
                    self._peer(f"{address['address']}:{address['port']}")["hostile"] = True
        elif kind in ("proto_in", "wire_in") and source_peer:
            peer = self._peer(source_peer)
            peer["closed"] = False
            peer["last_at"] = now
            if kind == "proto_in":
                peer["received"] += 1
                peer["last"] = item
                if label:
                    peer["protocols"].add(label)
            else:
                peer["bytes_in"] += len(item.get("raw", "")) // 2
        elif kind in ("proto_out", "wire_out") and target_peer:
            peer = self._peer(target_peer)
            if kind == "proto_out":
                peer["sent"] += 1
                if label:
                    peer["protocols"].add(label)
            else:
                peer["bytes_out"] += len(item.get("raw", "")) // 2

    def held_at(self) -> dict[str, int]:
        """How many items are held at each part of the node."""
        counts: dict[str, int] = defaultdict(int)
        with self._lock:
            for item in self.held.values():
                side = "from" if item["kind"] in ("internal_action", "proto_out", "wire_out") else "to"
                part, _peer = part_of(item.get(side, ""))
                if part is None and item["kind"] in ("wire_in", "wire_out"):
                    part = "connection"
                if part:
                    counts[part] += 1
        return dict(counts)

    def sync(self, status: dict[str, Any], fetch: Callable[[int], dict[str, Any]]) -> None:
        """Adopt the agent's view after connecting: what is frozen and what is held."""
        with self._lock:
            self.frozen = bool(status.get("frozen"))
            current = set(status.get("held", []))
            for seq in list(self.held):
                if seq not in current:
                    del self.held[seq]
            for seq in sorted(current - set(self.held)):
                try:
                    item = fetch(seq)
                except Exception:  # the item may have been released in the meantime
                    continue
                self.items.setdefault(seq, item)
                self.held[seq] = item

    def verdict(self, seq: int) -> str:
        """What happened to an item, in a word."""
        with self._lock:
            if seq in self.held:
                return "held"
            release = self.releases.get(seq)
            if release and release.get("verdict") == "drop":
                return "dropped"
            if release and release.get("verdict") == "replace":
                return "edited"
            return ""

    def reaction(self, seq: int, depth: int = 0, limit: int = 200) -> list[tuple[int, dict[str, Any]]]:
        """Everything that followed from an item, as (indent, item) in order."""
        out: list[tuple[int, dict[str, Any]]] = []
        with self._lock:
            if self.caused.get(seq):
                # The node named this item as the root of a chain: show the whole chain, each
                # step indented under the step that led to it.
                levels: dict[int, int] = {}
                for number in self.caused[seq][:limit]:
                    item = self.items.get(number)
                    if item is None:
                        continue
                    levels[number] = levels.get(item.get("parent", 0), depth - 1) + 1
                    out.append((min(levels[number], depth + 8), item))
                return out
            stack = [(depth, child) for child in reversed(self.children.get(seq, []))]
            while stack and len(out) < limit:
                level, current = stack.pop()
                item = self.items.get(current)
                if item is None:
                    continue
                out.append((level, item))
                stack.extend((level + 1, child) for child in reversed(self.children.get(current, [])))
        return out

    def recent(self, count: int, keep: Callable[[dict[str, Any]], bool] = lambda _item: True) -> list[dict[str, Any]]:
        with self._lock:
            out: list[dict[str, Any]] = []
            for item in reversed(self.items.values()):
                if keep(item):
                    out.append(item)
                    if len(out) >= count:
                        break
            return out[::-1]


def replay(lines: Iterable[str]) -> Session:
    """Rebuild a session from an ``events.jsonl`` file."""
    session = Session()
    for line in lines:
        if line.strip():
            entry = json.loads(line)
            session.record(entry["event"], entry["data"])
    return session
