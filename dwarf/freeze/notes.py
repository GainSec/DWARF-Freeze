"""Your marks on a recorded session: bookmarks, comments and findings, and the report made from them.

Kept in ``notes.json`` beside the session's ``events.jsonl``, so they travel with an export.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from . import cardano, render, rules, runs
from .session import Session
from .views import diff_paths, item_view, path_text

SEVERITIES = ("info", "low", "medium", "high", "critical")


class NotesError(ValueError):
    pass


def load_session(directory: Path, until: int | None = None) -> Session:
    """A session rebuilt from its recorded events (nothing is written)."""
    events = directory / "events.jsonl"
    if not events.is_file():
        raise NotesError(f"no recorded events in {directory}")
    session = Session()
    for event in runs.read_events(events):
        data = event.get("data")
        if until is not None and event.get("event") == "item" and data["seq"] > until:
            break
        session.record(event["event"], data)
    return session


class Notes:
    def __init__(self, directory: Path | None) -> None:
        self.path = directory / "notes.json" if directory else None
        self.marks: dict[int, dict[str, Any]] = {}
        if self.path and self.path.is_file():
            self.marks = {int(seq): mark for seq, mark in json.loads(self.path.read_text(encoding="utf-8")).items()}

    def _save(self) -> None:
        self.marks = {seq: mark for seq, mark in self.marks.items() if mark}
        if self.path:
            self.path.write_text(json.dumps({str(k): v for k, v in sorted(self.marks.items())}, indent=2) + "\n", encoding="utf-8")

    def toggle_bookmark(self, seq: int) -> bool:
        mark = self.marks.setdefault(seq, {})
        if mark.pop("bookmark", False):
            self._save()
            return False
        mark["bookmark"] = True
        self._save()
        return True

    def comment(self, seq: int, text: str) -> None:
        mark = self.marks.setdefault(seq, {})
        if text.strip():
            mark["comment"] = text.strip()
        else:
            mark.pop("comment", None)
        self._save()

    def finding(self, seq: int, title: str, severity: str = "medium", detail: str = "") -> None:
        """Mark an item as a finding; an empty title removes the finding."""
        mark = self.marks.setdefault(seq, {})
        if not title.strip():
            mark.pop("finding", None)
        else:
            if severity not in SEVERITIES:
                raise NotesError(f"severity is one of: {', '.join(SEVERITIES)}")
            mark["finding"] = {"title": title.strip(), "severity": severity, "detail": detail.strip(),
                               "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
        self._save()

    def badge(self, seq: int) -> str:
        mark = self.marks.get(seq, {})
        return ("!" if "finding" in mark else "") + ("*" if mark.get("bookmark") else "") + ("#" if "comment" in mark else "")

    def findings(self) -> list[tuple[int, dict[str, Any]]]:
        order = {name: index for index, name in enumerate(reversed(SEVERITIES))}
        found = [(seq, mark["finding"]) for seq, mark in self.marks.items() if "finding" in mark]
        return sorted(found, key=lambda pair: (order[pair[1]["severity"]], pair[0]))


def _item_line(item: dict[str, Any]) -> str:
    what = cardano.item_message_name(item) or item.get("label", "")
    return f"item {item['seq']} · {item['kind']} · {what} · {item.get('from', '') or '-'} → {item.get('to', '') or '-'}"


def evidence(session: Session, seq: int, limit: int = 40) -> dict[str, Any]:
    """Everything needed to stand behind a finding: the item, what you changed, what the node did."""
    item = session.items.get(seq)
    if item is None:
        raise NotesError(f"item {seq} is not in this session")
    release = session.releases.get(seq, {})
    original, names, raw = item_view(item)
    changes = []
    if release.get("verdict") == "replace" and ("raw" in release or "payload" in release):
        edited = item_view(item, bytes.fromhex(release["raw"]))[0] if "raw" in release else release["payload"]
        for path in sorted(diff_paths(original, edited), key=str):
            try:
                before = render.build(render.get_at(original, path), loose_bytes=not raw).summary
            except (KeyError, IndexError, TypeError):
                before = "(absent)"
            try:
                after = render.build(render.get_at(edited, path), loose_bytes=not raw).summary
            except (KeyError, IndexError, TypeError):
                after = "(absent)"
            changes.append({"field": path_text(path, names), "before": before, "after": after})
    chain = [{"seq": step["seq"], "depth": depth, "kind": step["kind"], "label": step.get("label", ""),
              "to": step.get("to", ""), "payload": render.one_line(step.get("payload"), 100)}
             for depth, step in session.reaction(seq, limit=limit) if step["kind"] != "internal_state"]
    return {"item": item, "line": _item_line(item), "verdict": release.get("verdict", "forward"), "changes": changes,
            "node_said": rules.reaction(session, seq), "chain": chain, "traced": session.traced}


def report(directory: Path, output: Path, title: str = "") -> int:
    """Write the session's findings as one Markdown report, with the evidence beside it as JSON."""
    notes, session = Notes(directory), load_session(directory)
    found = notes.findings()
    lines = [f"# {title or 'Freeze findings'}", "", f"Session `{directory.name}` · {len(session.items):,} items · "
             f"{session.tampers} intervention(s) · {len(found)} finding(s)", ""]
    bundle = []
    for number, (seq, finding) in enumerate(found, 1):
        proof = evidence(session, seq)
        bundle.append({"finding": finding, "seq": seq, **{k: proof[k] for k in ("item", "verdict", "changes", "node_said", "chain")}})
        lines += [f"## {number}. {finding['title']}  ({finding['severity']})", ""]
        if finding.get("detail"):
            lines += [finding["detail"], ""]
        lines += [f"- **Where:** {proof['line']}", f"- **What was done:** {proof['verdict']}"]
        for change in proof["changes"]:
            lines.append(f"  - `{change['field']}`: {change['before']} → {change['after']}")
        said = "; ".join(proof["node_said"]) or "nothing recorded"
        lines += [f"- **What the node did:** {said}" + ("" if proof["traced"] else " (by position; this node did not report causes)"), ""]
        if proof["chain"]:
            lines += ["```"] + [f"{'  ' * step['depth']}{step['seq']} {step['kind']} {step['label']}"
                                + (f" → {step['to']}" if step["to"] else "") + (f"  {step['payload']}" if step["payload"] else "")
                                for step in proof["chain"]] + ["```", ""]
        comment = notes.marks.get(seq, {}).get("comment")
        if comment:
            lines += [f"Note: {comment}", ""]
    if not found:
        lines += ["No findings were marked in this session.", ""]
    output.write_text("\n".join(lines), encoding="utf-8")
    output.with_suffix(".evidence.json").write_text(json.dumps(bundle, indent=2) + "\n", encoding="utf-8")
    return len(found)
