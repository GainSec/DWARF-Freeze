"""Whole Freeze runs: export, import, and doing again what a run did.

A run is the recorded event stream of a session plus what the debugger changed in it. An
exported run is one ``.tar.gz``:

    manifest.json        what node, when, how many items, up to which step
    events.jsonl         every event, in order
    interventions.json   every item that was dropped or edited, as a replayable instruction

Replaying ("ingest") restarts the node and repeats the interventions in order. A live node
does not produce the same items in the same order twice, so an intervention is not tied to
an item number: it is tied to what the item was (its kind, where it was going, which message)
and to how many like it were let through first.
"""

from __future__ import annotations

import io
import json
import tarfile
import time
from pathlib import Path
from typing import Any, Callable, Iterable

from . import cardano, cbor_tree, conditions, render
from .client import FreezeClient, FreezeError
from .session import part_of
from .views import diff_paths, item_view, path_text

FORMAT = "dwarf-freeze-run-v1"


class RunError(Exception):
    pass


def signature(item: dict[str, Any]) -> str:
    """A condition matching items of the same nature as ``item``, whatever their number."""
    terms = [f"kind={item['kind']}"]
    if cardano.protocol_number(item.get("type", "")) is not None:
        terms.append(f"type={item['type']}")
        payload = item.get("payload")
        if not (isinstance(payload, list) and payload and isinstance(payload[0], int)) and item.get("raw"):
            payload = item_view(item)[0]
        if isinstance(payload, list) and payload and isinstance(payload[0], int):
            terms.append(f"[0]={payload[0]}")
    else:
        if item.get("label"):
            terms.append(f"label={item['label']}")
        side = "from" if item["kind"] in ("internal_action", "proto_out", "wire_out") else "to"
        part, _peer = part_of(item.get(side, ""))
        if part and part != "connection":
            terms.append(f"{side}~{part}")
    return " and ".join(terms)


def interventions(events: Iterable[dict[str, Any]], until: int | None = None) -> list[dict[str, Any]]:
    """Every drop and edit in an event stream, as instructions that can be applied again.

    ``after`` is how many items of the same nature went by, untouched, since the previous
    intervention on that nature (or since the recording began).
    """
    items: dict[int, dict[str, Any]] = {}
    natures: dict[int, str] = {}
    seen: dict[str, int] = {}
    out: list[dict[str, Any]] = []
    for event in events:
        name, data = event["event"], event["data"]
        if name == "item":
            # Items are not always recorded in the order of their numbers, so one past the limit is not the end.
            if until is not None and data["seq"] > until:
                continue
            if data["kind"] == "internal_state":
                continue
            items[data["seq"]] = data
            natures[data["seq"]] = when = signature(data)
            seen[when] = seen.get(when, 0) + 1
        elif name == "released" and data["seq"] in items and data.get("verdict") in ("drop", "replace"):
            item, when = items[data["seq"]], natures[data["seq"]]
            entry: dict[str, Any] = {
                "when": when,
                "after": max(0, seen.get(when, 1) - 1),
                "action": data["verdict"],
                "seq": item["seq"],
                "what": cardano.item_message_name(item) or item.get("label", ""),
            }
            if data["verdict"] == "replace":
                original, names, raw = item_view(item)
                edited = item_view(item, bytes.fromhex(data["raw"]))[0] if "raw" in data else data.get("payload")
                paths = sorted(diff_paths(original, edited), key=str)
                try:
                    entry["edits"] = [{"path": list(path), "field": path_text(path, names), "value": render.get_at(edited, path)}
                                      for path in paths]
                except (KeyError, IndexError, TypeError):
                    entry["edits"] = []
                if not entry["edits"] or (raw and paths == [()]):
                    # the shape changed, or the item is plain bytes: keep the whole replacement
                    entry["edits"] = []
                    entry["whole"] = {"raw": data["raw"]} if "raw" in data else {"payload": edited}
            out.append(entry)
            seen[when] = 0
    return out


def read_events(path: Path) -> list[dict[str, Any]]:
    with path.open("r", encoding="utf-8") as lines:
        return [json.loads(line) for line in lines if line.strip()]


def export_run(session_dir: Path, archive: Path, until: int | None = None, note: str = "") -> dict[str, Any]:
    """Write a session, or its start up to item ``until``, as one archive. Returns the manifest."""
    events_file = session_dir / "events.jsonl"
    if not events_file.is_file():
        raise RunError(f"{session_dir} has no recorded events")
    kept: list[dict[str, Any]] = []
    for event in read_events(events_file):
        seq = event["data"].get("seq") if isinstance(event.get("data"), dict) else None
        if until is not None and isinstance(seq, int) and seq > until:
            continue
        kept.append(event)
    steps = interventions(kept, until)
    info_file = session_dir / "session.json"
    info = json.loads(info_file.read_text(encoding="utf-8")) if info_file.is_file() else {}
    item_numbers = [e["data"]["seq"] for e in kept if e["event"] == "item"]
    manifest = {
        "format": FORMAT,
        "name": session_dir.name,
        "exported_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "node": info.get("node", {}),
        "started_at": info.get("started_at"),
        "items": len(item_numbers),
        "first_item": min(item_numbers) if item_numbers else None,
        "last_item": max(item_numbers) if item_numbers else None,
        "until": until,
        "interventions": len(steps),
        "note": note,
    }
    archive.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive, "w:gz") as tar:
        def add(name: str, text: str) -> None:
            data = text.encode("utf-8")
            member = tarfile.TarInfo(name)
            member.size, member.mtime, member.mode = len(data), int(time.time()), 0o644
            tar.addfile(member, io.BytesIO(data))

        add("manifest.json", json.dumps(manifest, indent=2) + "\n")
        add("events.jsonl", "".join(json.dumps(event, separators=(",", ":")) + "\n" for event in kept))
        add("interventions.json", json.dumps(steps, indent=2) + "\n")
    return manifest


def import_run(archive: Path, runs_dir: Path) -> Path:
    """Unpack an exported run under ``runs_dir`` and return its directory."""
    if not archive.is_file():
        raise RunError(f"no such file: {archive}")
    name = archive.name.removesuffix(".tar.gz").removesuffix(".tgz")
    target = runs_dir / name
    target.mkdir(parents=True, exist_ok=True)
    wanted = {"manifest.json", "events.jsonl", "interventions.json"}
    with tarfile.open(archive, "r:gz") as tar:
        members = {member.name: member for member in tar.getmembers() if member.isfile()}
        if not wanted <= set(members):
            raise RunError(f"{archive.name} is not a Freeze run (missing {', '.join(sorted(wanted - set(members)))})")
        for member_name in wanted:
            source = tar.extractfile(members[member_name])
            assert source is not None
            (target / member_name).write_bytes(source.read())
    manifest = json.loads((target / "manifest.json").read_text(encoding="utf-8"))
    if manifest.get("format") != FORMAT:
        raise RunError(f"{archive.name}: unknown format {manifest.get('format')!r}")
    return target


def load_run(path: Path, runs_dir: Path | None = None) -> tuple[dict[str, Any], list[dict[str, Any]], Path]:
    """The manifest, interventions and directory of a run given as an archive or a directory."""
    if path.is_file():
        if runs_dir is None:
            raise RunError("an archive must be imported first")
        path = import_run(path, runs_dir)
    if not (path / "interventions.json").is_file():
        raise RunError(f"{path} is not a Freeze run")
    return (json.loads((path / "manifest.json").read_text(encoding="utf-8")),
            json.loads((path / "interventions.json").read_text(encoding="utf-8")), path)


def describe(step: dict[str, Any]) -> str:
    """One intervention in a sentence."""
    what = step.get("what") or "item"
    lead = f"after letting {step['after']} through, " if step.get("after") else ""
    if step["action"] == "drop":
        return f"{lead}drop the next {what}  [{step['when']}]"
    if step.get("edits"):
        changes = ", ".join(f"{edit.get('field') or '.'.join(str(p) for p in edit['path'] if p not in ('$embed', 'value')) or 'value'} = "
                            f"{render.build(edit['value'], loose_bytes=False).summary}" for edit in step["edits"])
        return f"{lead}in the next {what} set {changes}  [{step['when']}]"
    return f"{lead}replace the next {what} entirely  [{step['when']}]"


class Replayer:
    """Repeats a run's interventions, in order, on a live node."""

    def __init__(
        self,
        client: FreezeClient,
        steps: list[dict[str, Any]],
        log: Callable[[str], None] = print,
        *,
        stale_negative_control: bool = False,
    ) -> None:
        self.client, self.steps, self.log = client, steps, log
        self.stale_negative_control = stale_negative_control
        self.done: list[dict[str, Any]] = []

    def _apply(self, step: dict[str, Any], item: dict[str, Any]) -> str:
        if step["action"] == "drop":
            self.client.drop(item["seq"])
            return "dropped"
        payload, _names, raw = item_view(item)
        whole = step.get("whole")
        if whole and "raw" in whole:
            data = bytes.fromhex(whole["raw"])
        elif whole:
            self.client.replace(item["seq"], payload=whole["payload"])
            return "edited"
        elif raw:
            data = bytes.fromhex(item["raw"])
            for edit in step["edits"]:
                path = tuple(tuple(part) if isinstance(part, list) else part for part in edit["path"])
                if path == () and render.is_bytes(edit["value"]):
                    data = bytes.fromhex(edit["value"]["$bytes"])
                else:
                    data = cbor_tree.edit(data, path, edit["value"])
        else:
            for edit in step["edits"]:
                payload = render.set_at(payload, tuple(edit["path"]), edit["value"])
            self.client.replace(item["seq"], payload=payload)
            return "edited"
        if not raw:
            raise RunError("raw replacement has no complete original network message")
        impact = cardano.edit_impact(
            item.get("type", ""),
            bytes.fromhex(item["raw"]),
            data,
            resigning=self.client.resigning(),
            item_kind=item.get("kind"),
            stale_negative_control=self.stale_negative_control,
        )
        if impact["state"] == "refused":
            raise RunError(f"edit refused: {impact['reason']}")
        self.client.replace(
            item["seq"], raw=data, stale_negative_control=self.stale_negative_control,
        )
        return "edited"

    def run(self, timeout: float = 900.0) -> list[dict[str, Any]]:
        """Apply every intervention. The node is left frozen right after the last one.

        Interventions on items of one nature are applied in their recorded order, each after
        the recorded number of such items has gone by. Items of different natures arrive in
        whatever order the node produces them, so no order is imposed between natures.
        """
        deadline = time.time() + timeout
        pending: dict[str, list[tuple[int, dict[str, Any]]]] = {}
        for number, step in enumerate(self.steps, start=1):
            pending.setdefault(step["when"], []).append((number, step))
        predicates = {when: conditions.compile_condition(when) for when in pending}
        installed = {when: self.client.add_condition(predicate, "freeze", False) for when, predicate in predicates.items()}
        passed = {when: 0 for when in pending}
        handled: set[int] = set()

        def nature_of(item: dict[str, Any]) -> str | None:
            for when in pending:
                if pending[when] and conditions.matches(predicates[when], item):
                    return when
            return None

        def waiting() -> list[dict[str, Any]]:
            held = [self.client.item(seq) for seq in self.client.status().get("held", []) if seq not in handled]
            return [item for item in held if nature_of(item) is not None]

        stopped = ""
        try:
            queue = waiting()
            while any(pending.values()):
                if time.time() > deadline:
                    stopped = "timed out waiting for a matching item"
                    break
                if not self.client.connected:
                    stopped = "the node stopped answering"
                    break
                if queue:
                    item = queue.pop(0)
                else:
                    queue = waiting()
                    if queue:
                        continue
                    # run on, but keep held anything a remaining step is waiting for
                    wanted = [predicates[when] for when in pending if pending[when]]
                    self.client.thaw(keep={"op": "or", "args": wanted})
                    event = self.client.next_event(timeout=1.0)
                    if event is None or event["event"] != "item" or not event["data"].get("held"):
                        continue
                    item = event["data"]
                if item["seq"] in handled:
                    continue
                when = nature_of(item)
                if when is None:
                    continue
                handled.add(item["seq"])
                number, step = pending[when][0]
                try:
                    if passed[when] < int(step.get("after", 0)):
                        self.client.forward(item["seq"])
                        passed[when] += 1
                        continue
                    outcome = f"{self._apply(step, item)} item {item['seq']}"
                except FreezeError as error:
                    if "not held" in str(error):
                        continue  # it was released before we got to it
                    outcome = f"could not apply to item {item['seq']}: {error}"
                except (RunError, cbor_tree.CborError, KeyError, IndexError, TypeError, ValueError) as error:
                    outcome = f"refused edit for item {item['seq']}; forwarded original: {error}"
                    try:
                        self.client.forward(item["seq"])
                    except FreezeError:
                        pass
                pending[when].pop(0)
                passed[when] = 0
                self.done.append({"number": number, "step": step, "outcome": outcome})
                self.log(f"{number}/{len(self.steps)}: {describe(step)}\n    {outcome}")
                if not pending[when]:
                    try:
                        self.client.remove_condition(installed.pop(when))
                    except FreezeError:
                        pass
        finally:
            for condition in installed.values():
                try:
                    self.client.remove_condition(condition)
                except FreezeError:
                    pass
        for when, steps in pending.items():
            for number, step in steps:
                self.done.append({"number": number, "step": step, "outcome": stopped or "not reached"})
                self.log(f"{number}/{len(self.steps)}: {describe(step)}\n    {stopped or 'not reached'}")
        self.done.sort(key=lambda entry: entry["number"])
        try:
            self.client.freeze()  # stop where the recorded run was when it was exported
        except FreezeError:
            pass
        return self.done


def _content(item: dict[str, Any]) -> str:
    """What an item carries, as text that is the same whenever the node handles the same thing again."""
    if cardano.protocol_number(item.get("type", "")) in (2, 32770):
        # A chain-sync roll forward also carries the sender's tip, which moves on between runs;
        # the header is what makes it the same message.
        try:
            payload = item_view(item)[0]
        except Exception:  # noqa: BLE001 - bytes that do not decode are compared as they are
            payload = None
        if isinstance(payload, list) and len(payload) >= 2 and payload[0] == 2:
            return json.dumps(payload[1], sort_keys=True, separators=(",", ":"), default=str)
    if item.get("raw"):
        return str(item["raw"])
    return json.dumps(item.get("payload"), sort_keys=True, separators=(",", ":"))


def target(events: Iterable[dict[str, Any]], seq: int) -> dict[str, Any]:
    """How to recognise item ``seq`` in another run of the same node: its nature, what it
    carries, and how many items of that nature came before it."""
    before = 0
    wanted: dict[str, Any] | None = None
    natures: list[str] = []
    for event in events:
        if event["event"] != "item":
            continue
        item = event["data"]
        # Items are not always recorded in the order of their numbers: look at all of them.
        if item["seq"] == seq:
            wanted = item
        elif item["seq"] < seq and item["kind"] != "internal_state":
            natures.append(signature(item))
    if wanted is None:
        raise RunError(f"item {seq} is not in this recording")
    if wanted["kind"] == "internal_state":
        raise RunError(f"item {seq} is a state the node reported, not a step it can be stopped at; choose the step before it")
    when = signature(wanted)
    before = natures.count(when)
    return {"seq": seq, "when": when, "before": before, "content": _content(wanted),
            "what": cardano.item_message_name(wanted) or wanted.get("label", "")}


class Rewinder:
    """Brings a freshly restarted node to the step that corresponds to one item of a recording."""

    def __init__(
        self,
        client: FreezeClient,
        events: list[dict[str, Any]],
        seq: int,
        log: Callable[[str], None] = print,
        *,
        stale_negative_control: bool = False,
    ) -> None:
        self.client, self.log = client, log
        self.stale_negative_control = stale_negative_control
        self.goal = target(events, seq)
        self.steps = interventions(events, seq - 1)

    def run(self, timeout: float = 900.0, by_count: bool = False) -> dict[str, Any]:
        """Repeat the changes made before the item, then run until the item comes again.

        The node is left frozen with that item held. ``by_count`` stops at the item in the same
        position among its like, whatever it carries; otherwise the item must carry the same.
        Returns ``{"reached": seq | None, "seen": n, "changes": [...], "passed": seq | None}``.
        ``passed`` is set when the item went by while the earlier changes were being repeated,
        which happens when the second run delivers it earlier than the first did.
        """
        started = time.time()
        changes = Replayer(
            self.client,
            self.steps,
            self.log,
            stale_negative_control=self.stale_negative_control,
        ).run(timeout) if self.steps else []
        goal = self.goal
        predicate = conditions.compile_condition(goal["when"])
        limit = goal["before"] * 3 + 100
        seen, handled, reached, passed = 0, set(), None, None
        # Items of this nature the replayed changes already let through count as seen.
        for entry in changes:
            if entry["step"]["when"] == goal["when"] and entry["outcome"].startswith(("edited", "dropped")):
                seen += int(entry["step"].get("after", 0)) + 1
        condition = self.client.add_condition(predicate, "freeze", False)
        try:
            while reached is None and time.time() - started < timeout and self.client.connected:
                status = self.client.status()
                held = [self.client.item(number) for number in status.get("held", []) if number not in handled]
                held = [item for item in held if conditions.matches(predicate, item)]
                if not held:
                    if status.get("frozen"):
                        self.client.thaw(keep=predicate)
                    event = self.client.next_event(timeout=1.0)
                    if event is None or event["event"] != "item" or not event["data"].get("held"):
                        continue
                    held = [event["data"]] if conditions.matches(predicate, event["data"]) else []
                    if held:
                        # An event can be an old one, from while the earlier changes were repeated: that item
                        # has gone on already. It was counted then, and it is not held now.
                        still = set(self.client.status().get("held", []))
                        for item in [item for item in held if item["seq"] not in still]:
                            handled.add(item["seq"])
                            if not by_count and _content(item) == goal["content"]:
                                passed = item["seq"]
                        held = [item for item in held if item["seq"] in still]
                        if passed is not None:
                            break
                for item in held:
                    if item["seq"] in handled:
                        continue
                    handled.add(item["seq"])
                    same = seen == goal["before"] if by_count else _content(item) == goal["content"]
                    if same:
                        reached = item["seq"]
                        break
                    seen += 1
                    try:
                        self.client.forward(item["seq"])
                    except FreezeError:
                        pass
                if reached is None and seen > (goal["before"] if by_count else limit):
                    break
        finally:
            try:
                self.client.remove_condition(condition)
            except FreezeError:
                pass
            try:
                self.client.freeze()
            except FreezeError:
                pass
        return {"reached": reached, "seen": seen, "changes": changes, "goal": goal, "passed": passed}


def sessions(sessions_dir: Path) -> list[dict[str, Any]]:
    """Recorded sessions, newest first, with how much is in each."""
    found = []
    if not sessions_dir.is_dir():
        return found
    for directory in sorted(sessions_dir.iterdir(), reverse=True):
        events_file = directory / "events.jsonl"
        if not events_file.is_file():
            continue
        items = tampers = 0
        last = None
        with events_file.open("r", encoding="utf-8") as lines:
            for line in lines:
                if '"event":"item"' in line:
                    items += 1
                elif '"event":"released"' in line and ('"verdict":"drop"' in line or '"verdict":"replace"' in line):
                    tampers += 1
        info_file = directory / "session.json"
        info = json.loads(info_file.read_text(encoding="utf-8")) if info_file.is_file() else {}
        found.append({"name": directory.name, "path": directory, "items": items, "tampers": tampers,
                      "node": info.get("node", {}), "last": last})
    return found
