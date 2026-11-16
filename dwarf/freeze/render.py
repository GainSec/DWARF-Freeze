"""Turn item payloads (tagged JSON, see the protocol spec section 4) into displayable trees."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterator

Path = tuple[Any, ...]


@dataclass
class Node:
    """One line of a payload tree."""

    path: Path
    name: str  # raw field name, or "" for array elements and the root
    summary: str  # value on the line; for containers a size hint
    leaf: bool
    value: Any = None
    children: list["Node"] = field(default_factory=list)


def abbreviate_hex(text: str, keep: int = 4) -> str:
    return text if len(text) <= 2 * keep * 2 + 1 else f"{text[: 2 * keep]}…{text[-2 * keep :]}"


def is_bytes(value: Any) -> bool:
    return isinstance(value, dict) and set(value) == {"$bytes"}


def is_byte_array(value: Any) -> bool:
    """Some node types serialise fixed-size byte arrays as arrays of small integers."""
    return (
        isinstance(value, list)
        and len(value) >= 8
        and all(isinstance(v, int) and not isinstance(v, bool) and 0 <= v <= 255 for v in value)
    )


def scalar_text(value: Any) -> str:
    if value is None:
        return "none"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, (int, float)):
        return f"{value:,}" if isinstance(value, int) and abs(value) >= 10_000 else str(value)
    if isinstance(value, str):
        return value if value else '""'
    if is_bytes(value):
        raw = value["$bytes"]
        return f"{abbreviate_hex(raw)}  ({len(raw) // 2} bytes)" if raw else "(empty)"
    if isinstance(value, dict) and set(value) == {"$int"}:
        return value["$int"]
    if isinstance(value, dict) and set(value) == {"$float"}:
        return value["$float"]
    if isinstance(value, dict) and set(value) == {"$simple"}:
        return f"simple({value['$simple']})"
    if isinstance(value, dict) and "$unserializable" in value:
        return f"(cannot be shown: {value['$unserializable']})"
    return str(value)


def is_scalar(value: Any) -> bool:
    if isinstance(value, dict):
        return bool(value) and next(iter(value)) in ("$bytes", "$int", "$float", "$simple", "$unserializable") and len(value) == 1
    return not isinstance(value, list)


def build(
    value: Any, name: str = "", path: Path = (), names: dict[Path, str] | None = None, loose_bytes: bool = True
) -> Node:
    """Build the display tree for a payload.

    ``names`` gives field names by path for parts that have none of their own (array positions,
    integer map keys). ``loose_bytes`` treats arrays of small integers as byte strings, which is
    how some node types serialise fixed-size byte arrays; turn it off for real CBOR.
    """
    if names and path in names:
        name = names[path]
    if isinstance(value, dict) and len(value) == 1 and "$embed" in value:
        return build(value["$embed"], name, path + ("$embed",), names, loose_bytes)
    if isinstance(value, dict) and value.get("$tag") == 24 and isinstance(value.get("value"), dict) and "$embed" in value["value"]:
        return build(value["value"], name, path + ("value",), names, loose_bytes)
    if loose_bytes and is_byte_array(value):
        raw = bytes(value).hex()
        return Node(path, name, f"{abbreviate_hex(raw)}  ({len(value)} bytes)", True, value)
    if is_scalar(value):
        return Node(path, name, scalar_text(value), True, value)
    node = Node(path, name, "", False, value)
    if isinstance(value, list):
        node.summary = "(empty)" if not value else f"[{len(value)}]"
        node.children = [build(item, "", path + (index,), names, loose_bytes) for index, item in enumerate(value)]
    elif "$map" in value and len(value) == 1:
        pairs = value["$map"]
        node.summary = "(empty)" if not pairs else f"{{{len(pairs)}}}"
        node.children = [
            build(v, scalar_text(k), path + ("$map", index, 1), names, loose_bytes) for index, (k, v) in enumerate(pairs)
        ]
    elif "$tag" in value and "value" in value and len(value) == 2:
        inner = build(value["value"], f"tag {value['$tag']}", path + ("value",), names, loose_bytes)
        node.summary, node.children = "", [inner]
    else:
        node.summary = "(empty)" if not value else ""
        node.children = [build(v, k, path + (k,), names, loose_bytes) for k, v in value.items()]
    return node


def walk(node: Node) -> Iterator[Node]:
    yield node
    for child in node.children:
        yield from walk(child)


def get_at(payload: Any, path: Path) -> Any:
    for step in path:
        payload = payload[step]
    return payload


def set_at(payload: Any, path: Path, value: Any) -> Any:
    """A copy of ``payload`` with the value at ``path`` replaced."""
    if not path:
        return value
    head, rest = path[0], path[1:]
    if isinstance(payload, list):
        copy = list(payload)
        copy[head] = set_at(payload[head], rest, value)
        return copy
    copy = dict(payload)
    copy[head] = set_at(payload[head], rest, value)
    return copy


def parse_edit(text: str, original: Any) -> Any:
    """Interpret what a person typed as a replacement for ``original``, keeping its kind."""
    import json

    text = text.strip()
    if is_bytes(original) or is_byte_array(original):
        raw = text.replace(" ", "").removeprefix("0x")
        data = bytes.fromhex(raw)
        return list(data) if is_byte_array(original) else {"$bytes": data.hex()}
    if isinstance(original, bool):
        if text.lower() in ("yes", "true", "1"):
            return True
        if text.lower() in ("no", "false", "0"):
            return False
        raise ValueError("type yes or no")
    if isinstance(original, int) or (isinstance(original, dict) and "$int" in original):
        number = int(text.replace(",", "").replace("_", ""))
        return number if abs(number) <= 9_007_199_254_740_991 else {"$int": str(number)}
    if isinstance(original, float):
        return float(text)
    if isinstance(original, str):
        return text
    if original is None and text.lower() in ("none", "null", ""):
        return None
    return json.loads(text)


def one_line(payload: Any, limit: int = 60) -> str:
    """A short description of a payload for table rows."""
    if is_scalar(payload) or is_byte_array(payload):
        text = build(payload).summary
    elif isinstance(payload, list):
        text = ", ".join(one_line(item, 24) for item in payload[:4]) + (" …" if len(payload) > 4 else "")
    elif isinstance(payload, dict) and len(payload) == 1 and not next(iter(payload)).startswith("$"):
        key, inner = next(iter(payload.items()))
        inner_text = "" if inner in (None, [], {}) else " " + one_line(inner, limit - len(key) - 1)
        text = f"{key}{inner_text}"
    elif isinstance(payload, dict):
        parts = []
        for key, value in payload.items():
            if is_scalar(value) or is_byte_array(value):
                parts.append(f"{key.replace('_', ' ')} {build(value).summary.split('  (')[0]}")
            if len(parts) == 3:
                break
        text = " · ".join(parts) if parts else f"{len(payload)} fields"
    else:
        text = str(payload)
    return text if len(text) <= limit else text[: limit - 1] + "…"
