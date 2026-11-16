"""Send a message from the past again, changed or not.

Two ways, chosen by what the item is:

- An input to a part of the node that still exists is delivered again directly (``inject``).
- A message from a peer cannot be put on the wire from here, so it is sent *in place of*
  the next message of the same kind from that protocol: a one-shot rule swaps it in.
"""

from __future__ import annotations

from typing import Any

from . import cardano, cbor_tree, payloads, render, rules
from .client import FreezeClient
from .views import item_view, resolve_field


class RepeatError(ValueError):
    pass


def edited(item: dict[str, Any], changes: list[tuple[str, Any]]) -> tuple[bytes | None, Any]:
    """The item's bytes (network items) or payload (others) with ``field = value`` changes applied.

    A value may be a literal or a transform such as ``add:1`` or ``xorlast:01``.
    """
    payload, names, raw = item_view(item)
    data = bytes.fromhex(item["raw"]) if raw else None
    for field, spec in changes:
        if field == rules.WHOLE:
            if data is None:
                raise RepeatError("this item is not bytes, so it has no whole message to change")
            value = payloads.apply(spec, {"$bytes": data.hex()})
            data = bytes.fromhex(value["$bytes"])
            payload = item_view(item, data)[0]
            continue
        path = resolve_field(payload, names, field, loose_bytes=not raw)
        value = payloads.apply(spec, render.get_at(payload, path))
        if data is not None:
            data = cbor_tree.edit(data, path, value)
            payload = item_view(item, data)[0]
        else:
            payload = render.set_at(payload, path, value)
    return data, payload


def plan(
    client: FreezeClient,
    item: dict[str, Any],
    changes: list[tuple[str, Any]] | None = None,
    times: int = 1,
    *,
    stale_negative_control: bool = False,
) -> dict[str, Any]:
    """How ``item`` can be sent again: ``{"how": "inject", ...}`` or ``{"how": "next", "rule": Rule}``."""
    data, payload = edited(item, changes or [])
    if item["kind"] == "internal_input":
        if item.get("to") not in client.destinations():
            raise RepeatError(f"{item.get('to') or 'its destination'} no longer exists in the node, so nothing can be delivered to it")
        return {"how": "inject", "to": item["to"], "payload": payload, "times": times}
    if item["kind"] in ("proto_in", "proto_out") and data is not None:
        impact = cardano.edit_impact(
            item.get("type", ""),
            bytes.fromhex(item["raw"]),
            data,
            resigning=client.resigning(),
            item_kind=item.get("kind"),
            stale_negative_control=stale_negative_control,
        )
        if impact["state"] == "refused":
            raise RepeatError(f"edit refused: {impact['reason']}")
        like = rules.rule_for_item(item, (), ["0x" + data.hex()])
        rule = rules.Rule(name=f"repeat item {item['seq']}", when=like.when, field=rules.WHOLE,
                          values=["0x" + data.hex()], mode="cycle", count=times,
                          stale_negative_control=stale_negative_control)
        return {"how": "next", "rule": rule, "bytes": len(data), "impact": impact}
    raise RepeatError(f"a {item['kind']} item cannot be sent again; choose an input or a network message")


def send(client: FreezeClient, step: dict[str, Any]) -> int:
    """Carry out an ``inject`` plan. Returns how many were delivered."""
    for _ in range(step["times"]):
        client.inject(step["to"], step["payload"])
    return step["times"]
