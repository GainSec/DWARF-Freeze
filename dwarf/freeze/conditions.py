"""A small text language for item conditions, compiled to protocol predicates.

Terms are joined with ``and`` / ``or`` (``and`` binds tighter) and may be prefixed with ``not``:

    kind=internal_input,proto_in      one of these kinds
    to~validate   from=peer:1.2.3.4   text tests: = exact, ~ contains, ^ starts with, $ ends with
    type~Block    label=send
    label=RollForward                 a network message by the name the UI shows (any mini-protocol message)
    spot=block-check                  a cold spot tag
    seq>=5000
    slot>100  txs[0].fee=1  body~ff10  txs#>2  hash?     payload fields: = != < > ~, #> length, ? exists
    everything                         always true

Friendly kinds: input, action, result, state, wire, proto, store, timer, probe, call.
"""

from __future__ import annotations

import json
import re
from typing import Any

KIND_ALIASES = {
    "input": ["internal_input"],
    "action": ["internal_action"],
    "result": ["internal_result"],
    "state": ["internal_state"],
    "wire": ["wire_in", "wire_out"],
    "proto": ["proto_in", "proto_out"],
    "store": ["store_write"],
    "timer": ["timer"],
    "probe": ["probe"],
    "call": ["call"],
}
_TEXT_OPS = {"=": "eq", "~": "contains", "^": "prefix", "$": "suffix"}
_TEXT_ATTRIBUTES = ("to", "from", "type", "label")


class ConditionError(ValueError):
    pass


def _named_message(name: str) -> dict[str, Any] | None:
    """``label=<name>`` for a mini-protocol message name such as ``RollForward``.

    A network item's label is its protocol ("chain sync"); the message name the UI shows comes
    from the first element of its payload. So the name is spelled out as protocol and message
    number, for either side of the protocol, which the node's agent can test as well.
    """
    from .cardano import MINI_PROTOCOLS

    found = [
        {"op": "and", "args": [
            {"op": "or", "args": [{"op": "type", "eq": f"ouroboros/{protocol}"},
                                  {"op": "type", "eq": f"ouroboros/{protocol | 0x8000}"}]},
            {"op": "field", "path": "[0]", "eq": number},
        ]}
        for protocol, (_protocol_name, messages) in sorted(MINI_PROTOCOLS.items())
        for number, message in sorted(messages.items())
        if message == name
    ]
    if not found:
        return None
    return {"op": "or", "args": [{"op": "label", "eq": name}] + found}


def _value(text: str) -> Any:
    text = text.strip()
    try:
        return json.loads(text)
    except ValueError:
        pass
    if re.fullmatch(r"-?[\d_,]+", text):
        return int(text.replace("_", "").replace(",", ""))
    return text.strip("'\"")


def _term(text: str) -> dict[str, Any]:
    text = text.strip()
    if not text:
        raise ConditionError("empty condition")
    if text.startswith("not "):
        return {"op": "not", "arg": _term(text[4:])}
    if text in ("everything", "true", "*"):
        return {"op": "true"}
    match = re.fullmatch(r"kind\s*=\s*(.+)", text)
    if match:
        kinds: list[str] = []
        for name in match.group(1).split(","):
            kinds.extend(KIND_ALIASES.get(name.strip(), [name.strip()]))
        return {"op": "kind", "in": kinds}
    match = re.fullmatch(r"(?:spot|cold_spot)\s*=\s*(.+)", text)
    if match:
        return {"op": "cold_spot", "eq": match.group(1).strip()}
    match = re.fullmatch(r"seq\s*>=\s*(\d+)", text)
    if match:
        return {"op": "seq", "ge": int(match.group(1))}
    match = re.fullmatch(r"(to|from|type|label)\s*([=~^$])\s*(.+)", text)
    if match:
        wanted = match.group(3).strip().strip("'\"")
        if match.group(1) == "label" and match.group(2) == "=" and (named := _named_message(wanted)):
            return named
        return {"op": match.group(1), _TEXT_OPS[match.group(2)]: wanted}
    match = re.fullmatch(r"([\w./\[\]-]+)\s*\?", text)
    if match:
        return {"op": "field", "path": match.group(1), "exists": True}
    match = re.fullmatch(r"([\w./\[\]-]+)\s*(#>|!=|>=|<=|=|<|>|~)\s*(.+)", text)
    if match:
        path, op, raw = match.group(1), match.group(2), match.group(3)
        value = _value(raw)
        if op == "#>":
            return {"op": "field", "path": path, "len_gt": int(value)}
        if op == "~":
            return {"op": "field", "path": path, "contains": str(value)}
        if op == "=":
            return {"op": "field", "path": path, "eq": value}
        if op == "!=":
            return {"op": "field", "path": path, "ne": value}
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            raise ConditionError(f"`{op}` needs a number, got {raw!r}")
        if op == ">":
            return {"op": "field", "path": path, "gt": value}
        if op == "<":
            return {"op": "field", "path": path, "lt": value}
        if op == ">=":
            return {"op": "not", "arg": {"op": "field", "path": path, "lt": value}}
        return {"op": "not", "arg": {"op": "field", "path": path, "gt": value}}
    raise ConditionError(f"cannot understand `{text}`")


def compile_condition(text: str) -> dict[str, Any]:
    """Compile condition text to a protocol predicate."""
    alternatives = []
    for alternative in re.split(r"\s+or\s+", text.strip()):
        terms = [_term(part) for part in re.split(r"\s+and\s+", alternative)]
        alternatives.append(terms[0] if len(terms) == 1 else {"op": "and", "args": terms})
    return alternatives[0] if len(alternatives) == 1 else {"op": "or", "args": alternatives}


def describe(predicate: dict[str, Any] | None) -> str:
    """Condition text for a predicate, for display."""
    if not predicate:
        return "nothing"
    op = predicate.get("op")
    if op == "true":
        return "everything"
    if op == "or":
        first = (predicate.get("args") or [{}])[0]
        if first.get("op") == "label" and "eq" in first and predicate == _named_message(first["eq"]):
            return f"label={first['eq']}"
    if op in ("and", "or"):
        return f" {op} ".join(describe(arg) for arg in predicate.get("args", []))
    if op == "not":
        return f"not {describe(predicate.get('arg'))}"
    if op == "kind":
        return "kind=" + ",".join(predicate.get("in", []))
    if op == "cold_spot":
        return f"spot={predicate.get('eq')}"
    if op == "seq":
        return f"seq>={predicate.get('ge')}"
    if op in _TEXT_ATTRIBUTES:
        for symbol, name in _TEXT_OPS.items():
            if name in predicate:
                return f"{op}{symbol}{predicate[name]}"
    if op == "field":
        path = predicate.get("path", "")
        for symbol, name in (("=", "eq"), ("!=", "ne"), ("<", "lt"), (">", "gt"), ("~", "contains"), ("#>", "len_gt")):
            if name in predicate:
                return f"{path}{symbol}{json.dumps(predicate[name]) if isinstance(predicate[name], (dict, list)) else predicate[name]}"
        return f"{path}?"
    return json.dumps(predicate)


def matches(predicate: dict[str, Any], item: dict[str, Any]) -> bool:
    """Evaluate a protocol predicate locally (used for filtering what is shown)."""
    op = predicate.get("op")
    if op == "true":
        return True
    if op == "and":
        return all(matches(arg, item) for arg in predicate.get("args", []))
    if op == "or":
        return any(matches(arg, item) for arg in predicate.get("args", []))
    if op == "not":
        return not matches(predicate["arg"], item)
    if op == "kind":
        return item.get("kind") in predicate.get("in", [])
    if op == "cold_spot":
        return predicate.get("eq") in item.get("cold_spots", [])
    if op == "seq":
        return item.get("seq", 0) >= predicate.get("ge", 0)
    if op in ("to", "from", "type", "label"):
        text = item.get(op) or ""
        if "eq" in predicate:
            return text == predicate["eq"]
        if "prefix" in predicate:
            return text.startswith(predicate["prefix"])
        if "suffix" in predicate:
            return text.endswith(predicate["suffix"])
        return predicate.get("contains", "") in text
    if op == "field":
        raw = predicate.get("path", "")
        value: Any = item if raw.startswith("/") else item.get("payload")
        try:
            for key, index in re.findall(r"([^.\[\]/]+)|\[(\d+)\]", raw):
                value = value[int(index)] if index else value[key]
        except (KeyError, IndexError, TypeError):
            return "ne" in predicate
        number = None
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            number = float(value)
        elif isinstance(value, dict) and "$int" in value:
            number = float(value["$int"])
        if "exists" in predicate:
            return True
        if "eq" in predicate:
            return value == predicate["eq"]
        if "ne" in predicate:
            return value != predicate["ne"]
        if "lt" in predicate:
            return number is not None and number < predicate["lt"]
        if "gt" in predicate:
            return number is not None and number > predicate["gt"]
        if "contains" in predicate:
            needle = predicate["contains"]
            if isinstance(value, str):
                return needle in value
            if isinstance(value, dict):
                return needle in value.get("$bytes", "") if "$bytes" in value else needle in value
            return isinstance(value, list) and needle in value
        if "len_gt" in predicate:
            if isinstance(value, dict) and "$bytes" in value:
                return len(value["$bytes"]) // 2 > predicate["len_gt"]
            if isinstance(value, dict) and "$map" in value:
                return len(value["$map"]) > predicate["len_gt"]
            return isinstance(value, (str, list, dict)) and len(value) > predicate["len_gt"]
    return False
