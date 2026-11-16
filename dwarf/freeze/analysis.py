"""Questions about a whole recorded session: find items, see what was reached, compare two runs."""

from __future__ import annotations

import json
from collections import Counter
from typing import Any

from . import cardano, conditions, render, rules
from .session import Session, part_of
from .views import diff_paths, item_view, path_text


def search(session: Session, query: str, limit: int = 200) -> list[dict[str, Any]]:
    """Items matching a condition (``kind=proto_in and slot>100``) or, failing that, containing the text."""
    try:
        predicate = conditions.compile_condition(query)
        keep = lambda item: conditions.matches(predicate, item)  # noqa: E731
    except conditions.ConditionError:
        needle = query.lower()
        keep = lambda item: needle in json.dumps(item, separators=(",", ":")).lower()  # noqa: E731
    found = []
    for item in list(session.items.values()):
        if keep(item):
            found.append(item)
            if len(found) >= limit:
                break
    return found


def coverage(session: Session) -> dict[str, Counter]:
    """What a run actually exercised: parts of the node, effects, protocol messages, errors and rejections."""
    reached: dict[str, Counter] = {name: Counter() for name in ("parts", "effects", "messages", "errors", "rejections", "probes", "calls")}
    for item in list(session.items.values()):
        kind, label, payload = item["kind"], item.get("label", ""), item.get("payload")
        if kind == "internal_input":
            part = part_of(item.get("to", ""))[0]
            if part:
                reached["parts"][part] += 1
        elif kind == "internal_action":
            if item.get("to") == "node" and label:
                reached["effects"][label] += 1
            if "Rejected" in label:
                outcome = payload.get("outcome") if isinstance(payload, dict) else None
                reached["rejections"][f"{label}: {outcome}" if isinstance(outcome, str) else label] += 1
        elif kind == "probe" and label:
            reached["probes"][label] += 1
        elif kind == "call":
            reached["calls"][item.get("type") or label] += 1
        elif kind in ("proto_in", "proto_out"):
            name = cardano.item_message_name(item) or label
            reached["messages"][("← " if kind == "proto_in" else "→ ") + name] += 1
        elif kind == "internal_result" and isinstance(payload, dict) and "Err" in payload:
            reached["errors"][rules._error_path(payload["Err"]) or "error"] += 1
    return reached


def compare_coverage(first: Session, second: Session) -> dict[str, dict[str, list[str]]]:
    """Per kind of thing: what only the first run reached, and what only the second did."""
    a, b = coverage(first), coverage(second)
    return {name: {"only_first": sorted(set(a[name]) - set(b[name])), "only_second": sorted(set(b[name]) - set(a[name]))}
            for name in a}


def compare_results(first: list[dict[str, Any]], second: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Two result files from `template`/`scenarios run`, attempt by attempt; `same` says whether the node agreed."""
    index = {(row["rule"], row["number"]): row for row in second}
    rows = []
    for row in first:
        other = index.pop((row["rule"], row["number"]), None)
        rows.append({"rule": row["rule"], "number": row["number"], "value": row.get("value", ""),
                     "first": row["node_said"], "second": other["node_said"] if other else "(not attempted)",
                     "same": other is not None and _gist(row["node_said"]) == _gist(other["node_said"])})
    for (rule, number), other in index.items():
        rows.append({"rule": rule, "number": number, "value": other.get("value", ""), "first": "(not attempted)",
                     "second": other["node_said"], "same": False})
    return rows


def _gist(said: str) -> str:
    """A reaction without the details that differ between any two runs (step counts)."""
    return "no error" if said.startswith("no error in") else said


def compare_items(session: Session, first: int, second: int, other: Session | None = None) -> dict[str, Any]:
    """Two items side by side: which fields differ, and what the node did with each."""
    there = other or session
    a, b = session.items.get(first), there.items.get(second)
    if a is None or b is None:
        raise KeyError(f"item {first if a is None else second} is not in the session")
    payload_a, names, raw = item_view(a)
    payload_b = item_view(b)[0]
    fields = []
    for path in sorted(diff_paths(payload_a, payload_b), key=str):
        def at(payload: Any) -> str:
            try:
                return render.build(render.get_at(payload, path), loose_bytes=not raw).summary
            except (KeyError, IndexError, TypeError):
                return "(absent)"
        fields.append({"field": path_text(path, names), "first": at(payload_a), "second": at(payload_b)})
    return {"fields": fields, "first": rules.reaction(session, first), "second": rules.reaction(there, second)}
