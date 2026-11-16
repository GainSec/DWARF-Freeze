"""A byte-exact CBOR editor.

Cardano hashes are taken over the exact bytes a node received, and real blocks mix
canonical and non-canonical encodings (indefinite-length arrays, over-long integers). A
decode / re-encode round trip through an ordinary CBOR library silently changes those
bytes. This module parses CBOR into a tree that remembers how every item was written, so
that after an edit every untouched item is written back exactly as it was.

Byte strings that themselves contain CBOR (tag 24, block bodies inside messages) can be
opened with :func:`open_embedded`; edits inside are folded back into the byte string.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from typing import Any

UINT, NINT, BYTES, TEXT, ARRAY, MAP, TAG, SIMPLE = range(8)
EMBED = "$embed"


class CborError(ValueError):
    pass


@dataclass
class Item:
    major: int
    info: int  # the low five bits of the head byte: how the argument was written
    arg: int | None  # the argument (integer value, length, tag number); None for indefinite
    value: Any = None  # bytes / str / float / simple value; unused for containers
    children: list["Item"] = field(default_factory=list)  # array items, map k/v interleaved, tag content, chunks
    embedded: "Item | None" = None  # the CBOR inside a byte string, once opened

    @property
    def indefinite(self) -> bool:
        return self.info == 31 and self.major in (BYTES, TEXT, ARRAY, MAP)


def _read_head(data: bytes, at: int) -> tuple[int, int, int | None, int]:
    if at >= len(data):
        raise CborError("unexpected end of data")
    head = data[at]
    major, info = head >> 5, head & 0x1F
    at += 1
    if info < 24:
        return major, info, info, at
    if info in (24, 25, 26, 27):
        width = 1 << (info - 24)
        if at + width > len(data):
            raise CborError("unexpected end of data")
        return major, info, int.from_bytes(data[at : at + width], "big"), at + width
    if info == 31:
        return major, info, None, at
    raise CborError(f"reserved additional information {info}")


def _parse(data: bytes, at: int, depth: int = 0) -> tuple[Item, int]:
    if depth > 256:
        raise CborError("nesting too deep")
    major, info, arg, at = _read_head(data, at)
    item = Item(major, info, arg)
    if major in (UINT, NINT):
        if arg is None:
            raise CborError("indefinite integer")
    elif major in (BYTES, TEXT):
        if arg is None:
            chunks = []
            while True:
                if at >= len(data):
                    raise CborError("unterminated indefinite string")
                if data[at] == 0xFF:
                    at += 1
                    break
                chunk, at = _parse(data, at, depth + 1)
                if chunk.major != major or chunk.indefinite:
                    raise CborError("bad chunk in indefinite string")
                chunks.append(chunk)
            item.children = chunks
            joined = b"".join(c.value if major == BYTES else c.value.encode() for c in chunks)
            item.value = joined if major == BYTES else joined.decode("utf-8", "replace")
        else:
            if at + arg > len(data):
                raise CborError("string runs past the end of the data")
            raw = data[at : at + arg]
            item.value = raw if major == BYTES else raw.decode("utf-8", "replace")
            if major == TEXT:
                item.children = []
                item._raw_text = raw  # type: ignore[attr-defined]
            at += arg
    elif major in (ARRAY, MAP):
        count = None if arg is None else arg * (2 if major == MAP else 1)
        while count is None or len(item.children) < count:
            if count is None:
                if at >= len(data):
                    raise CborError("unterminated indefinite container")
                if data[at] == 0xFF:
                    at += 1
                    break
            child, at = _parse(data, at, depth + 1)
            item.children.append(child)
        if major == MAP and len(item.children) % 2:
            raise CborError("map with a key but no value")
    elif major == TAG:
        if arg is None:
            raise CborError("indefinite tag")
        child, at = _parse(data, at, depth + 1)
        item.children = [child]
    else:
        if info == 31:
            raise CborError("unexpected break")
        if info == 25:
            item.value = struct.unpack(">e", arg.to_bytes(2, "big"))[0]
        elif info == 26:
            item.value = struct.unpack(">f", arg.to_bytes(4, "big"))[0]
        elif info == 27:
            item.value = struct.unpack(">d", arg.to_bytes(8, "big"))[0]
        else:
            item.value = {20: False, 21: True, 22: None}.get(arg, ("simple", arg))
    return item, at


def parse(data: bytes) -> Item:
    """Parse exactly one CBOR item occupying all of ``data``."""
    item, end = _parse(data, 0)
    if end != len(data):
        raise CborError(f"{len(data) - end} bytes left over after the item")
    return item


def parse_prefix(data: bytes) -> tuple[Item, int]:
    """Parse one CBOR item from the start of ``data``; returns it and how many bytes it used."""
    return _parse(data, 0)


def _head(major: int, info: int, arg: int | None) -> bytes:
    if info < 24 or info == 31:
        return bytes([major << 5 | info])
    assert arg is not None
    return bytes([major << 5 | info]) + arg.to_bytes(1 << (info - 24), "big")


def _shortest(arg: int) -> int:
    if arg < 24:
        return arg
    for candidate, limit in ((24, 1 << 8), (25, 1 << 16), (26, 1 << 32), (27, 1 << 64)):
        if arg < limit:
            return candidate
    raise CborError("integer too large for CBOR")


def encode(item: Item) -> bytes:
    """Write an item back. Untouched items come out byte-for-byte as they were parsed."""
    major = item.major
    if major in (UINT, NINT):
        return _head(major, item.info, item.arg)
    if major in (BYTES, TEXT):
        if item.embedded is not None:
            inner = encode(item.embedded)
            if item.indefinite:
                raise CborError("cannot edit CBOR embedded in an indefinite-length byte string")
            info = item.info if item.arg == len(inner) else _refit(item.info, len(inner))
            return _head(major, info, len(inner)) + inner
        if item.indefinite:
            return _head(major, 31, None) + b"".join(encode(c) for c in item.children) + b"\xff"
        raw = item.value if major == BYTES else getattr(item, "_raw_text", None) or item.value.encode()
        return _head(major, item.info, item.arg) + raw
    if major in (ARRAY, MAP):
        body = b"".join(encode(c) for c in item.children)
        if item.indefinite:
            return _head(major, 31, None) + body + b"\xff"
        count = len(item.children) // (2 if major == MAP else 1)
        info = item.info if item.arg == count else _refit(item.info, count)
        return _head(major, info, count) + body
    if major == TAG:
        return _head(TAG, item.info, item.arg) + encode(item.children[0])
    return _head(SIMPLE, item.info, item.arg)


def _refit(info: int, arg: int) -> int:
    """Keep the old argument width when the new value fits it; otherwise use the shortest form."""
    if info in (24, 25, 26, 27) and arg < 1 << (8 << (info - 24)):
        return info
    return _shortest(arg)


def open_embedded(item: Item) -> bool:
    """If a byte string holds exactly one CBOR item, attach it as ``item.embedded``."""
    if item.major != BYTES or item.indefinite or item.embedded is not None or not item.value:
        return item.embedded is not None
    try:
        item.embedded = parse(item.value)
    except (CborError, UnicodeDecodeError, RecursionError):
        return False
    return True


def open_tagged(item: Item) -> None:
    """Open every byte string wrapped in tag 24 ("encoded CBOR data item"), at any depth."""
    if item.major == TAG and item.arg == 24 and item.children[0].major == BYTES:
        open_embedded(item.children[0])
    if item.embedded is not None:
        open_tagged(item.embedded)
    for child in item.children:
        open_tagged(child)


def to_json(item: Item) -> Any:
    """The item in the protocol's tagged JSON form (spec section 4). Embedded CBOR is shown opened."""
    major = item.major
    if major == UINT:
        return _json_int(item.arg)
    if major == NINT:
        return _json_int(-1 - item.arg)
    if major == BYTES:
        if item.embedded is not None:
            return {EMBED: to_json(item.embedded)}
        return {"$bytes": item.value.hex()}
    if major == TEXT:
        return item.value
    if major == ARRAY:
        return [to_json(c) for c in item.children]
    if major == MAP:
        pairs = [(to_json(k), to_json(v)) for k, v in zip(item.children[::2], item.children[1::2])]
        keys = [k for k, _ in pairs]
        if all(isinstance(k, str) and not k.startswith("$") for k in keys) and len(set(keys)) == len(keys):
            return dict(pairs)
        return {"$map": [[k, v] for k, v in pairs]}
    if major == TAG:
        return {"$tag": item.arg, "value": to_json(item.children[0])}
    if isinstance(item.value, float):
        if item.value != item.value:
            return {"$float": "nan"}
        if item.value in (float("inf"), float("-inf")):
            return {"$float": "inf" if item.value > 0 else "-inf"}
        return item.value
    if isinstance(item.value, tuple):
        return {"$simple": item.value[1]}
    return item.value


def _json_int(value: int) -> Any:
    return value if abs(value) <= 9_007_199_254_740_991 else {"$int": str(value)}


def child_at(item: Item, step: Any) -> Item:
    """Follow one step of a path as produced for the tagged JSON form."""
    if step == EMBED:
        if item.embedded is None and not open_embedded(item):
            raise CborError("these bytes are not CBOR")
        return item.embedded  # type: ignore[return-value]
    if item.major == ARRAY:
        return item.children[step]
    if item.major == TAG and step == "value":
        return item.children[0]
    if item.major == MAP:
        if isinstance(step, tuple) and step[0] == "$map":
            return item.children[2 * step[1] + step[2]]
        for key, value in zip(item.children[::2], item.children[1::2]):
            if key.major == TEXT and key.value == step:
                return value
    raise CborError(f"no such step {step!r}")


def locate(item: Item, path: tuple[Any, ...]) -> Item:
    """The item at a tagged-JSON path. ``("$map", i, 1)`` is three path steps for one map value."""
    steps = list(path)
    while steps:
        step = steps.pop(0)
        if step == "$map" and item.major == MAP:
            index, side = steps.pop(0), steps.pop(0)
            item = item.children[2 * index + side]
        else:
            item = child_at(item, step)
    return item


def set_scalar(item: Item, value: Any) -> None:
    """Replace a scalar in place, keeping its kind and, where possible, how it was written."""
    if isinstance(value, dict) and set(value) == {"$int"}:
        value = int(value["$int"])
    if isinstance(value, dict) and set(value) == {"$bytes"}:
        value = bytes.fromhex(value["$bytes"])
    if isinstance(value, bool) or value is None:
        if item.major != SIMPLE:
            raise CborError("this value is not a yes/no value")
        item.arg = {False: 20, True: 21, None: 22}[value]
        item.info, item.value = item.arg, value
    elif isinstance(value, int):
        if item.major not in (UINT, NINT):
            raise CborError("this value is not a whole number")
        item.major, item.arg = (UINT, value) if value >= 0 else (NINT, -1 - value)
        item.info = _refit(item.info, item.arg) if item.info >= 24 else _shortest(item.arg)
    elif isinstance(value, bytes):
        if item.major != BYTES:
            raise CborError("this value is not a byte string")
        item.children, item.embedded, item.value, item.arg = [], None, value, len(value)
        item.info = _refit(item.info, len(value)) if 24 <= item.info < 31 else _shortest(len(value))
    elif isinstance(value, str):
        if item.major != TEXT:
            raise CborError("this value is not text")
        raw = value.encode()
        item.children, item.value, item.arg = [], value, len(raw)
        item._raw_text = raw  # type: ignore[attr-defined]
        item.info = _refit(item.info, len(raw)) if 24 <= item.info < 31 else _shortest(len(raw))
    else:
        raise CborError("only single values can be replaced")


def edit(data: bytes, path: tuple[Any, ...], value: Any) -> bytes:
    """``data`` with the scalar at ``path`` replaced and nothing else disturbed."""
    root = parse(data)
    set_scalar(locate(root, path), value)
    return encode(root)
