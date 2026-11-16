"""Payload lists: named lists of test values, and how a value is applied to a field.

A value is either a literal that replaces the field, or a transform of what is already
there. Text forms:

    123   -1   true   null   "text"        literals (JSON)
    0xdeadbeef                              bytes
    add:N  sub:N  mul:N                     arithmetic on the original number
    empty                                   an empty byte string or text
    trunc:N                                 keep the first N bytes / characters
    append:HEX  prepend:HEX                 add bytes at the end / start
    zero  ones                              same length, every byte 00 / ff
    flip:K                                  flip bit K (0 is the lowest bit of the first byte)
    xorlast:HH                              XOR the last byte with HH (xorlast:01 flips its lowest bit)
    repeat:N                                the original repeated N times
    len:N                                   N zero bytes

Lists shipped with Freeze live beside this file in ``payloads/``; a user's own are kept in
the data root and win over a shipped list of the same name.
"""

from __future__ import annotations

import csv
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from . import render

BUILTIN = Path(__file__).with_name("payloads")
_SAFE_INT = 9_007_199_254_740_991
_TRANSFORM = re.compile(r"^(add|sub|mul|trunc|append|prepend|flip|xorlast|repeat|len):(.+)$")


class PayloadError(ValueError):
    pass


@dataclass
class PayloadList:
    name: str
    values: list[Any] = field(default_factory=list)
    description: str = ""
    builtin: bool = False

    def to_document(self) -> dict[str, Any]:
        return {"name": self.name, "description": self.description, "values": self.values}


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "list"


def _from_document(document: Any, fallback_name: str, builtin: bool = False) -> list[PayloadList]:
    """One or several lists from a parsed file: a bare list, ``{name, values}`` or ``{lists: {...}}``."""
    if isinstance(document, list):
        return [PayloadList(fallback_name, list(document), builtin=builtin)]
    if isinstance(document, dict) and isinstance(document.get("lists"), dict):
        return [PayloadList(str(name), list(values), builtin=builtin) for name, values in document["lists"].items()]
    if isinstance(document, dict) and isinstance(document.get("values"), list):
        return [PayloadList(str(document.get("name") or fallback_name), list(document["values"]),
                            str(document.get("description") or ""), builtin)]
    raise PayloadError("expected a list of values, {name, values}, or {lists: {name: [values]}}")


def read_file(path: Path) -> list[PayloadList]:
    """Payload lists from a ``.txt`` (one value per line), ``.csv`` (first column), ``.json`` or ``.yaml`` file."""
    suffix = path.suffix.lower()
    text = path.read_text(encoding="utf-8")
    if suffix in (".yaml", ".yml"):
        return _from_document(yaml.safe_load(text), path.stem)
    if suffix == ".json":
        return _from_document(json.loads(text), path.stem)
    if suffix == ".csv":
        return [PayloadList(path.stem, [row[0] for row in csv.reader(text.splitlines()) if row and row[0].strip()])]
    lines = [line.strip() for line in text.splitlines()]
    return [PayloadList(path.stem, [line for line in lines if line and not line.startswith("#")])]


class Store:
    """The payload lists available: the ones shipped with Freeze plus the user's own."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory

    def all(self) -> list[PayloadList]:
        found: dict[str, PayloadList] = {}
        for path in sorted(BUILTIN.glob("*.yaml")):
            for item in _from_document(yaml.safe_load(path.read_text(encoding="utf-8")), path.stem, builtin=True):
                found[item.name] = item
        if self.directory.is_dir():
            for path in sorted(self.directory.glob("*.json")):
                for item in _from_document(json.loads(path.read_text(encoding="utf-8")), path.stem):
                    found[item.name] = item
        return sorted(found.values(), key=lambda item: item.name.lower())

    def get(self, name: str) -> PayloadList:
        for item in self.all():
            if item.name == name:
                return item
        raise PayloadError(f"no payload list named {name!r}")

    def save(self, item: PayloadList) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        item.builtin = False
        (self.directory / f"{_slug(item.name)}.json").write_text(json.dumps(item.to_document(), indent=2) + "\n", encoding="utf-8")

    def delete(self, name: str) -> bool:
        """Remove a user's list. A shipped list of the same name becomes visible again."""
        path = self.directory / f"{_slug(name)}.json"
        if path.exists():
            path.unlink()
            return True
        return False

    def import_file(self, path: Path) -> list[PayloadList]:
        items = read_file(path)
        for item in items:
            self.save(item)
        return items

    def export(self, name: str, path: Path) -> None:
        document = self.get(name).to_document()
        text = yaml.safe_dump(document, sort_keys=False) if path.suffix.lower() in (".yaml", ".yml") else json.dumps(document, indent=2) + "\n"
        path.write_text(text, encoding="utf-8")


def _as_bytes(value: Any) -> bytes | None:
    if render.is_bytes(value):
        return bytes.fromhex(value["$bytes"])
    if render.is_byte_array(value):
        return bytes(value)
    return None


def _from_bytes(data: bytes, like: Any) -> Any:
    return list(data) if render.is_byte_array(like) else {"$bytes": data.hex()}


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, dict) and set(value) == {"$int"}:
        return int(value["$int"])
    return None


def _from_int(number: int) -> Any:
    return number if abs(number) <= _SAFE_INT else {"$int": str(number)}


def describe(spec: Any) -> str:
    """How a value is shown in a table."""
    if isinstance(spec, str):
        return spec
    return json.dumps(spec)


def apply(spec: Any, original: Any) -> Any:
    """The value ``spec`` produces when applied to a field currently holding ``original``."""
    if not isinstance(spec, str):
        if isinstance(spec, int) and not isinstance(spec, bool):
            return _from_int(spec)
        return spec
    text = spec.strip()
    data, number = _as_bytes(original), _as_int(original)

    if text in ("empty", "zero", "ones"):
        if text == "empty":
            if data is not None:
                return _from_bytes(b"", original)
            if isinstance(original, str):
                return ""
            raise PayloadError("`empty` needs bytes or text")
        if data is None:
            raise PayloadError(f"`{text}` needs bytes")
        return _from_bytes((b"\x00" if text == "zero" else b"\xff") * len(data), original)

    transform = _TRANSFORM.match(text)
    if transform:
        name, argument = transform.group(1), transform.group(2).strip()
        if name in ("add", "sub", "mul"):
            if number is None:
                raise PayloadError(f"`{name}` needs a whole number")
            operand = int(argument.replace("_", ""))
            return _from_int(number + operand if name == "add" else number - operand if name == "sub" else number * operand)
        if name == "len":
            return _from_bytes(b"\x00" * int(argument), original if data is not None else {"$bytes": ""})
        if name == "trunc":
            if data is not None:
                return _from_bytes(data[: int(argument)], original)
            if isinstance(original, str):
                return original[: int(argument)]
            raise PayloadError("`trunc` needs bytes or text")
        if name == "repeat":
            if data is not None:
                return _from_bytes(data * int(argument), original)
            if isinstance(original, str):
                return original * int(argument)
            raise PayloadError("`repeat` needs bytes or text")
        if data is None:
            raise PayloadError(f"`{name}` needs bytes")
        if name == "xorlast":
            if not data:
                raise PayloadError("`xorlast` needs at least one byte")
            return _from_bytes(data[:-1] + bytes([data[-1] ^ int(argument, 16)]), original)
        if name == "flip":
            bit = int(argument)
            if not data or bit >= len(data) * 8:
                raise PayloadError(f"bit {bit} is outside {len(data)} bytes")
            changed = bytearray(data)
            changed[bit // 8] ^= 1 << (bit % 8)
            return _from_bytes(bytes(changed), original)
        extra = bytes.fromhex(argument.removeprefix("0x"))
        return _from_bytes(data + extra if name == "append" else extra + data, original)

    if text.startswith("0x"):
        return _from_bytes(bytes.fromhex(text[2:]), original if data is not None else {"$bytes": ""})
    try:
        literal = json.loads(text)
    except ValueError:
        try:
            return render.parse_edit(text, original)
        except ValueError as error:
            raise PayloadError(f"cannot use {text!r} here: {error}") from error
    if isinstance(literal, int) and not isinstance(literal, bool):
        return _from_int(literal)
    return literal
