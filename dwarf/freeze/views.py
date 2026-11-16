"""How an item is looked at and addressed: its payload, the names of its fields, and field lookup."""

from __future__ import annotations

import re
from typing import Any

from . import cardano, cbor_tree, render


class FieldError(ValueError):
    pass


def item_view(item: dict[str, Any], raw: bytes | None = None) -> tuple[Any, dict[render.Path, str], bool]:
    """What to show for an item: (payload, field names, whether it is edited as raw bytes).

    Network items are parsed from their exact bytes so that edits change nothing else.
    """
    if raw is None and item.get("raw") is not None:
        raw = bytes.fromhex(item["raw"])
    if raw is None:
        return item.get("payload"), {}, False
    if item["kind"] in ("proto_in", "proto_out"):
        try:
            root = cbor_tree.parse(raw)
            cbor_tree.open_tagged(root)
            payload = cbor_tree.to_json(root)
            return payload, cardano.names_for(item.get("type", ""), payload), True
        except cbor_tree.CborError:
            pass
    return {"$bytes": raw.hex()}, {(): "bytes"}, True


def path_text(path: render.Path, names: dict[render.Path, str] | None = None) -> str:
    """A readable form of a path, using field names where they exist: ``header.header_body.slot``."""
    parts: list[str] = []
    for index in range(1, len(path) + 1):
        prefix, step = path[:index], path[index - 1]
        if names and prefix in names:
            parts.append(names[prefix])
        elif step in ("$embed", "value", "$map") or (index >= 2 and path[index - 2] == "$map") or (index >= 3 and path[index - 3] == "$map"):
            continue
        elif isinstance(step, int):
            parts.append(f"[{step}]")
        else:
            parts.append(str(step))
    text = ".".join(parts).replace(".[", "[")
    return text or "(whole value)"


def _walk_paths(node: render.Node) -> list[render.Node]:
    return list(render.walk(node))


def resolve_field(payload: Any, names: dict[render.Path, str], spec: Any, loose_bytes: bool) -> render.Path:
    """The path a field specification points at.

    ``spec`` may be a path as a list, a field name (``slot``), a dotted chain of names
    (``header_body.slot``, ``tip.point.slot``), or a plain path (``txs[0].fee``).
    """
    if spec is None or spec == "" or spec == []:
        return ()
    if isinstance(spec, (list, tuple)):
        path = tuple(spec)
        render.get_at(payload, path)
        return path
    text = str(spec).strip()
    nodes = _walk_paths(render.build(payload, names=names, loose_bytes=loose_bytes))
    wanted = [part for part in text.split(".") if part]
    matches = []
    for node in nodes:
        if not node.name or node.name != wanted[-1]:
            continue
        chain = [names[node.path[:i]] for i in range(1, len(node.path) + 1) if node.path[:i] in names] if names else []
        if not names:
            chain = [str(step) for step in node.path if isinstance(step, str)]
        position = len(chain)
        ok = True
        for name in reversed(wanted):
            while position > 0 and chain[position - 1] != name:
                position -= 1
            if position == 0:
                ok = False
                break
            position -= 1
        if ok:
            matches.append(node.path)
    if matches:
        return matches[0]
    # a plain path: keys and [n]
    path: list[Any] = []
    for key, index in re.findall(r"([^.\[\]]+)|\[(\d+)\]", text):
        path.append(int(index) if index else key)
    try:
        render.get_at(payload, tuple(path))
    except (KeyError, IndexError, TypeError) as error:
        raise FieldError(f"no field `{text}` in this item") from error
    return tuple(path)


def diff_paths(before: Any, after: Any, path: render.Path = ()) -> set[render.Path]:
    """Paths of the leaves that differ between two payloads of the same shape."""
    if before == after:
        return set()
    if isinstance(before, dict) and isinstance(after, dict) and before.keys() == after.keys() and not render.is_scalar(before):
        out: set[render.Path] = set()
        for key in before:
            out |= diff_paths(before[key], after[key], path + (key,))
        return out
    if isinstance(before, list) and isinstance(after, list) and len(before) == len(after) and not render.is_byte_array(before):
        out = set()
        for index, (left, right) in enumerate(zip(before, after)):
            out |= diff_paths(left, right, path + (index,))
        return out
    return {path}


def as_sent(item: dict[str, Any] | None, edited: Any) -> Any:
    """An edited payload in the form the node takes it back.

    Most items take what they showed, changed. A schedule shows when it fires as text and is
    moved by `at_ms`, milliseconds from now: a changed `at` holding a number is sent as that.
    """
    if not item or item.get("label") != "schedule" or not isinstance(edited, dict):
        return edited
    original = item.get("payload") if isinstance(item.get("payload"), dict) else {}
    sent: dict[str, Any] = {}
    if edited.get("at") != original.get("at"):
        try:
            sent["at_ms"] = int(str(edited.get("at")).strip())
        except ValueError:
            return edited  # the node says what is wrong with it
    if edited.get("message") != original.get("message"):
        sent["message"] = edited.get("message")
    return sent
