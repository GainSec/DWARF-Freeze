#!/usr/bin/env python3
"""Exercise edited-header signing against the running controlled Freeze devnet.

This is deliberately not a general network/key utility. It only connects through the
recorded local devnet run, accepts a public VRF verification key for identity selection,
and never reads or accepts signing material.
"""

from __future__ import annotations

import argparse
import time
from pathlib import Path

from dwarf.freeze import cbor_tree, conditions, devnet, render, rules
from dwarf.freeze.client import FreezeClient
from dwarf.freeze.session import Session
from dwarf.freeze.views import item_view, resolve_field


HEADER = "kind=proto_in and type=ouroboros/2 and [0]=2"


def field(item: dict, name: str):
    payload, names, raw = item_view(item)
    path = resolve_field(payload, names, name, loose_bytes=not raw)
    return render.get_at(payload, path), path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("on", "off"))
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--vrf-vkey", help="optional public hex identity to select")
    parser.add_argument("--timeout", type=float, default=180)
    parser.add_argument("--untouched-first", action="store_true")
    args = parser.parse_args()

    info = devnet.current(args.data_root / "freeze")
    if info is None or info.get("network") != "testnet_42":
        raise SystemExit("a recorded controlled testnet_42 Freeze devnet run is required")
    if args.vrf_vkey is not None and len(args.vrf_vkey) != 64:
        raise SystemExit("--vrf-vkey must be a 32-byte public key in hex")

    client = FreezeClient(info["host"], info["port"], token=info["token"])
    client.connect()
    session = Session()
    predicate = conditions.compile_condition(HEADER)
    condition_id = client.add_condition(predicate, "freeze", False)
    client.set_resigning(args.mode == "on")
    client.thaw(keep=predicate)
    deadline = time.time() + args.timeout
    untouched = args.untouched_first
    edited_seq: int | None = None
    skipped = 0
    try:
        while time.time() < deadline and edited_seq is None:
            event = client.next_event(timeout=1)
            if event is None:
                continue
            session.record(event["event"], event["data"])
            item = event["data"] if event["event"] == "item" else None
            if not item or not item.get("held") or not conditions.matches(predicate, item):
                continue
            vrf, _ = field(item, "header_body.vrf_vkey")
            if args.vrf_vkey is not None and (
                not isinstance(vrf, dict) or vrf.get("$bytes") != args.vrf_vkey.lower()
            ):
                skipped += 1
                client.forward(item["seq"])
                client.thaw(keep=predicate)
                continue
            if untouched:
                untouched = False
                print(
                    f"untouched seq={item['seq']} identity=match from={item.get('from')} "
                    f"raw_bytes={len(item['raw']) // 2}"
                )
                client.forward(item["seq"])
                client.thaw(keep=predicate)
                continue

            size, size_path = field(item, "header_body.block_body_size")
            block_number, _ = field(item, "header_body.block_number")
            slot, _ = field(item, "header_body.slot")
            if not isinstance(size, int):
                raise RuntimeError("block_body_size was not an integer")
            # Small deltas can accidentally equal another size representation on empty devnet
            # blocks. Use an impossible-for-this-devnet value so the later ledger rule is
            # deterministic while still changing only this unprotected header-body field.
            replacement_size = size + 1_000_000
            replacement = cbor_tree.edit(bytes.fromhex(item["raw"]), size_path, replacement_size)
            edited_seq = item["seq"]
            client.remove_condition(condition_id)
            condition_id = None
            client.replace(edited_seq, raw=replacement)
            client.thaw()
            print(
                f"edited seq={edited_seq} identity=match from={item.get('from')} "
                f"block={block_number} slot={slot} block_body_size={size}->{replacement_size} "
                f"resigning={args.mode} skipped_other_identities={skipped}"
            )

        if edited_seq is None:
            print("no matching producer header arrived before timeout")
            return 2

        expected = "BlockSizeMismatch" if args.mode == "on" else "InvalidKesSignature"
        reaction_deadline = time.time() + args.timeout
        while time.time() < reaction_deadline:
            event = client.next_event(timeout=1)
            if event is not None:
                session.record(event["event"], event["data"])
                item = event["data"] if event["event"] == "item" else None
                if item and item.get("type") == "ouroboros/3":
                    payload = item.get("payload")
                    message = payload[0] if isinstance(payload, list) and payload else "?"
                    print(
                        f"blockfetch seq={item.get('seq')} kind={item.get('kind')} "
                        f"message={message} from={item.get('from')} to={item.get('to')} "
                        f"cause={item.get('cause')}"
                    )
            phrases = rules.reaction(session, edited_seq)
            rendered = "; ".join(phrases)
            if expected.lower() in rendered.lower():
                print(f"reaction={rendered}")
                print(f"acceptance={expected}")
                return 0
            if any(marker in rendered for marker in ("re-signing failed", "InvalidVrf", "InvalidProof")):
                print(f"unexpected_reaction={rendered}")
                return 1
        print("reaction=" + "; ".join(rules.reaction(session, edited_seq)))
        print(f"expected reaction containing {expected}")
        return 1
    finally:
        if condition_id is not None:
            try:
                client.remove_condition(condition_id)
            except Exception:
                pass
        try:
            client.thaw()
        except Exception:
            pass
        client.close()


if __name__ == "__main__":
    raise SystemExit(main())
