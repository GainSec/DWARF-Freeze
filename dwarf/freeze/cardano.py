"""Cardano meaning for raw network messages: what each message is and what its fields are.

Node-independent. The wire format is the same whichever node implementation is being
debugged, so the names here apply to Amaru and cardano-node alike.
"""

from __future__ import annotations

import hashlib
from typing import Any, Callable

Path = tuple[Any, ...]
Names = dict[Path, str]

MINI_PROTOCOLS: dict[int, tuple[str, dict[int, str]]] = {
    0: ("handshake", {0: "ProposeVersions", 1: "AcceptVersion", 2: "Refuse", 3: "QueryReply"}),
    2: ("chain sync", {0: "RequestNext", 1: "AwaitReply", 2: "RollForward", 3: "RollBackward", 4: "FindIntersect",
                       5: "IntersectFound", 6: "IntersectNotFound", 7: "Done"}),
    3: ("block fetch", {0: "RequestRange", 1: "ClientDone", 2: "StartBatch", 3: "NoBlocks", 4: "Block", 5: "BatchDone"}),
    4: ("tx submission", {0: "RequestTxIds", 1: "ReplyTxIds", 2: "RequestTxs", 3: "ReplyTxs", 4: "Done", 6: "Init"}),
    8: ("keep alive", {0: "KeepAlive", 1: "KeepAliveResponse", 2: "Done"}),
    10: ("peer sharing", {0: "ShareRequest", 1: "SharePeers", 2: "Done"}),
}

HEADER_BODY = ["block_number", "slot", "prev_hash", "issuer_vkey", "vrf_vkey", "vrf_result", "block_body_size",
               "block_body_hash", "operational_cert", "protocol_version"]
TX_BODY = {0: "inputs", 1: "outputs", 2: "fee", 3: "ttl", 4: "certificates", 5: "withdrawals", 7: "auxiliary_data_hash",
           8: "validity_start", 9: "mint", 11: "script_data_hash", 13: "collateral", 14: "required_signers",
           15: "network_id", 16: "collateral_return", 17: "total_collateral", 18: "reference_inputs",
           19: "voting_procedures", 20: "proposal_procedures", 21: "treasury_value", 22: "donation"}
TX_OUTPUT = {0: "address", 1: "value", 2: "datum", 3: "script_ref"}
WITNESS_SET = {0: "vkey_witnesses", 1: "native_scripts", 2: "bootstrap_witnesses", 3: "plutus_v1_scripts",
               4: "plutus_data", 5: "redeemers", 6: "plutus_v2_scripts", 7: "plutus_v3_scripts"}
ERAS = {0: "byron", 1: "byron", 2: "shelley", 3: "allegra", 4: "mary", 5: "alonzo", 6: "babbage", 7: "conway"}


def protocol_number(type_name: str) -> int | None:
    if type_name.startswith("ouroboros/"):
        try:
            # The top bit marks the responder's side of a mini-protocol; the protocol is the rest.
            return int(type_name.split("/", 1)[1]) & 0x7FFF
        except ValueError:
            return None
    return None


def block_body_commitment(type_name: str, raw: bytes) -> tuple[str, int] | None:
    """Return the exact Cardano body hash and size for a BlockFetch ``MsgBlock``.

    The four body components are hashed and measured in their received CBOR encodings. No decoded
    value is re-encoded, so non-canonical but accepted encodings keep their protocol identity.
    """
    if protocol_number(type_name) != 3:
        return None
    from . import cbor_tree

    try:
        message = cbor_tree.parse(raw)
        cbor_tree.open_tagged(message)
        if message.major != cbor_tree.ARRAY or len(message.children) != 2:
            return None
        label, tagged = message.children
        if label.major != cbor_tree.UINT or label.arg != 4:
            return None
        if tagged.major != cbor_tree.TAG or tagged.arg != 24 or not tagged.children:
            return None
        wrapped = tagged.children[0]
        network = wrapped.embedded
        if network is None or network.major != cbor_tree.ARRAY or len(network.children) != 2:
            return None
        block = network.children[1]
        if block.major != cbor_tree.ARRAY or len(block.children) != 5:
            return None
        components = [cbor_tree.encode(item) for item in block.children[1:]]
    except (cbor_tree.CborError, IndexError, TypeError, ValueError):
        return None
    component_hashes = b"".join(hashlib.blake2b(item, digest_size=32).digest() for item in components)
    return hashlib.blake2b(component_hashes, digest_size=32).hexdigest(), sum(map(len, components))


def _raw_payload(raw: bytes) -> Any:
    """Decode one exact message into the tagged form used by the inspector."""
    from . import cbor_tree

    root = cbor_tree.parse(raw)
    cbor_tree.open_tagged(root)
    return cbor_tree.to_json(root)


def _different(before: Any, after: Any, path: Path = ()) -> set[Path]:
    if before == after:
        return set()
    if isinstance(before, dict) and isinstance(after, dict) and before.keys() == after.keys():
        changed: set[Path] = set()
        for key in before:
            changed |= _different(before[key], after[key], path + (key,))
        return changed
    if isinstance(before, list) and isinstance(after, list) and len(before) == len(after):
        changed = set()
        for index, (left, right) in enumerate(zip(before, after)):
            changed |= _different(left, right, path + (index,))
        return changed
    return {path}


def _changed_names(type_name: str, before: Any, after: Any) -> tuple[set[Path], set[str]]:
    paths = _different(before, after)
    named = names_for(type_name, before) | names_for(type_name, after)
    names = {
        name
        for path in paths
        for length in range(1, len(path) + 1)
        if (name := named.get(path[:length])) is not None
    }
    return paths, names


def _impact(
    surface: str,
    state: str,
    *,
    damage: list[str] | None = None,
    repairs: list[str] | None = None,
    remaining: list[str] | None = None,
    reason: str | None = None,
    code: str | None = None,
) -> dict[str, Any]:
    return {
        "surface": surface,
        "state": state,
        "code": code,
        "damage": damage or [],
        "repairs": repairs or [],
        "remaining": remaining or [],
        "reason": reason,
    }


def edit_impact(
    type_name: str,
    original: bytes,
    edited: bytes,
    *,
    resigning: dict[str, Any] | None = None,
    item_kind: str | None = None,
    stale_negative_control: bool = False,
) -> dict[str, Any]:
    """Describe what an exact network-byte edit damages and what Freeze can honestly repair.

    This is a pre-emission policy report. ``auto-repair`` means the typed Amaru protocol gate is
    required to perform and verify the listed cryptographic repair; this function never claims to
    have signed anything itself. Unknown shapes fail closed.
    """
    if original == edited:
        return _impact("unchanged", "auto-repair", repairs=["byte-exact forward"])
    resigning = resigning or {}
    protocol = protocol_number(type_name)
    if protocol is None:
        if stale_negative_control:
            return _impact(
                "raw-wire", "stale-negative-control", repairs=["byte lengths supplied by operator"],
                remaining=["all protocol validation material"],
            )
        return _impact(
            "raw-wire", "refused",
            code="typed_message_identity_unavailable",
            reason="raw wire bytes have no complete typed message or producer/request identity",
        )
    if protocol not in MINI_PROTOCOLS:
        if stale_negative_control:
            return _impact(
                f"protocol-{protocol}", "stale-negative-control", repairs=["canonical CBOR lengths"],
                remaining=["unknown protocol dependencies"],
            )
        return _impact(
            f"protocol-{protocol}", "refused", code="protocol_dependency_graph_unavailable",
            reason=f"unsupported protocol {protocol} has no proven dependency graph",
        )
    try:
        before, after = _raw_payload(original), _raw_payload(edited)
        paths, changed = _changed_names(type_name, before, after)
    except (ValueError, TypeError, IndexError, RecursionError) as error:
        if stale_negative_control:
            return _impact(
                f"protocol-{protocol}", "stale-negative-control",
                repairs=["operator-supplied byte length"],
                remaining=["unparseable protocol validation material"],
            )
        return _impact(
            f"protocol-{protocol}", "refused", code="edited_message_not_decodable",
            reason=f"edited message is not exact decodable CBOR: {error}",
        )
    if not paths:
        return _impact(f"protocol-{protocol}", "auto-repair", repairs=["byte-exact forward"])

    message_before, message_after = message_name(type_name, before), message_name(type_name, after)
    if message_before != message_after:
        return _impact(
            f"protocol-{protocol}", "stale-negative-control" if stale_negative_control else "refused",
            repairs=["canonical CBOR lengths"],
            remaining=["mini-protocol state transition"],
            code=None if stale_negative_control else "mini_protocol_transition_changed",
            reason=None if stale_negative_control else "an edit may not change its mini-protocol message type",
        )

    if protocol == 2 and message_before == "RollForward":
        damage = ["KES signature", "header hash", "tip/descendant points", "BlockFetch point mapping"]
        repairs = ["canonical CBOR lengths", "controlled-devnet KES signature", "header/tip/ancestry mappings"]
        if item_kind not in {"proto_in", "proto_out"}:
            reason = "network direction is absent, so producer identity and repair hook coverage cannot be proven"
            if stale_negative_control:
                return _impact(
                    "chainsync-header", "stale-negative-control", damage=damage,
                    repairs=["canonical CBOR lengths"], remaining=damage,
                )
            return _impact(
                "chainsync-header", "refused", damage=damage,
                repairs=["canonical CBOR lengths"], remaining=damage,
                code="network_direction_unavailable", reason=reason,
            )
        try:
            era_before, era_after = before[1][0], after[1][0]
            if not isinstance(era_before, int) or not isinstance(era_after, int):
                raise TypeError("era tag is not an integer")
        except (TypeError, IndexError) as error:
            if stale_negative_control:
                return _impact(
                    "chainsync-header", "stale-negative-control", damage=damage,
                    repairs=["operator-supplied byte length"], remaining=damage,
                )
            return _impact(
                "chainsync-header", "refused", damage=damage,
                repairs=["canonical CBOR lengths"], remaining=damage,
                code="edited_message_not_decodable",
                reason=f"RollForward does not contain an exact era-wrapped header: {error}",
            )
        if era_before != era_after:
            if stale_negative_control:
                return _impact(
                    "chainsync-header", "stale-negative-control", damage=damage,
                    repairs=["canonical CBOR lengths"], remaining=damage,
                )
            return _impact(
                "chainsync-header", "refused", damage=damage,
                repairs=["canonical CBOR lengths"], remaining=damage,
                code="era_identity_change_forbidden",
                reason="the ChainSync header era tag is an identity invariant of the supported Conway repair chain",
            )
        if era_before != 6:
            if stale_negative_control:
                return _impact(
                    "chainsync-header", "stale-negative-control", damage=damage,
                    repairs=["canonical CBOR lengths"], remaining=damage,
                )
            return _impact(
                "chainsync-header", "refused", damage=damage,
                repairs=["canonical CBOR lengths"], remaining=damage,
                code="unsupported_header_era",
                reason="the controlled repair chain supports Conway headers only",
            )
        protected = (
            ("slot", "slot_vrf_eligibility_unproven", "slot is protected because changing it requires a new Praos VRF proof and proven leader eligibility",
             ["Praos VRF proof/output", "leader eligibility", "KES signature", "header hash"]),
            ("issuer_vkey", "issuer_identity_change_forbidden", "issuer verification key is protected because no matching cold identity is authorized",
             ["pool identity", "operational certificate cold signature", "VRF/KES identity", "KES signature"]),
            ("vrf_vkey", "vrf_identity_change_forbidden", "VRF verification key is protected because leader identity and eligibility cannot be transferred",
             ["Praos VRF proof/output", "leader eligibility", "KES signature"]),
            ("vrf_output", "vrf_leader_proof_regeneration_unavailable", "VRF output is protected because Freeze cannot honestly regenerate leader eligibility",
             ["Praos VRF proof/output", "leader eligibility", "KES signature"]),
            ("vrf_proof", "vrf_leader_proof_regeneration_unavailable", "VRF proof is protected because Freeze cannot honestly regenerate leader eligibility",
             ["Praos VRF proof/output", "leader eligibility", "KES signature"]),
        )
        for field, code, reason, remaining in protected:
            if field in changed:
                if stale_negative_control:
                    return _impact("chainsync-header", "stale-negative-control", damage=damage, repairs=["canonical CBOR lengths"], remaining=remaining)
                return _impact(
                    "chainsync-header", "refused", damage=damage,
                    repairs=["canonical CBOR lengths"], remaining=remaining, code=code, reason=reason,
                )
        opcert = {"hot_vkey", "sequence_number", "kes_period", "cold_signature"}
        if changed & opcert:
            remaining = ["operational certificate cold signature", "KES identity/period binding", "KES signature", "header hash"]
            if stale_negative_control:
                return _impact("chainsync-header", "stale-negative-control", damage=damage, repairs=["canonical CBOR lengths"], remaining=remaining)
            return _impact(
                "chainsync-header", "refused", damage=damage, repairs=["canonical CBOR lengths"], remaining=remaining,
                code="operational_certificate_cold_key_unavailable",
                reason="operational certificate fields are protected because the producer cold signing key is not mounted",
            )
        if stale_negative_control:
            return _impact("chainsync-header", "stale-negative-control", damage=damage, repairs=["canonical CBOR lengths"], remaining=damage)
        if not (resigning.get("available") and resigning.get("enabled")):
            return _impact(
                "chainsync-header", "refused", damage=damage, repairs=["canonical CBOR lengths"], remaining=damage,
                code="controlled_devnet_signing_unavailable"
                if not resigning.get("available")
                else "controlled_devnet_signing_disabled",
                reason="controlled-devnet header re-signing is unavailable"
                if not resigning.get("available")
                else "controlled-devnet header re-signing is disabled; select stale negative-control mode to emit deliberately invalid validation material",
            )
        return _impact("chainsync-header", "auto-repair", damage=damage, repairs=repairs)

    if protocol == 3 and message_before == "Block":
        repairs = ["canonical CBOR lengths", "block body hash and size", "pre-announced signed header"]
        if item_kind not in {"proto_in", "proto_out"}:
            reason = "network direction is absent, so BlockFetch request identity and repair hook coverage cannot be proven"
            if stale_negative_control:
                return _impact(
                    "blockfetch-block", "stale-negative-control", repairs=["canonical CBOR lengths"],
                    remaining=["block body hash and size", "request-bound pre-announced signed header"],
                )
            return _impact(
                "blockfetch-block", "refused", repairs=["canonical CBOR lengths"],
                remaining=["block body hash and size", "request-bound pre-announced signed header"],
                code="network_direction_unavailable", reason=reason,
            )
        if changed & {"header", "header_body", "kes_signature"}:
            return _impact(
                "blockfetch-block", "stale-negative-control" if stale_negative_control else "refused",
                repairs=["canonical CBOR lengths"], remaining=["producer header identity and signature"],
                code=None if stale_negative_control else "producer_header_mutation_forbidden",
                reason=None if stale_negative_control else "an inbound block-body edit may not change the embedded producer header",
            )
        remaining: list[str] = []
        if "transaction_bodies" in changed:
            remaining.append("transaction vkey/bootstrap witnesses")
        if "transaction_witness_sets" in changed:
            remaining.append("transaction witness signer-set validity")
        if remaining and not stale_negative_control:
            return _impact(
                "blockfetch-block", "refused", damage=["block body hash and size", *remaining], repairs=repairs,
                remaining=remaining,
                code="tx_witness_material_unavailable",
                reason="transaction-body edits require witness regeneration, but no uniquely owned payment signing keys are mounted"
                if "transaction_bodies" in changed
                else "transaction witness edits cannot be validated against a uniquely owned signer set",
            )
        if not (resigning.get("available") and resigning.get("enabled")) and not stale_negative_control:
            return _impact(
                "blockfetch-block", "refused", damage=["block body hash and size"], repairs=["canonical CBOR lengths"],
                remaining=["block body hash and size", "pre-announced signed header"],
                code="controlled_devnet_signing_unavailable"
                if not resigning.get("available")
                else "controlled_devnet_signing_disabled",
                reason="controlled-devnet block resealing is unavailable",
            )
        return _impact(
            "blockfetch-block", "stale-negative-control" if stale_negative_control else "auto-repair",
            damage=["block body hash and size", *remaining], repairs=repairs, remaining=remaining,
        )

    if protocol == 4:
        remaining = ["transaction body IDs", "transaction vkey/bootstrap witnesses", "request/reply IDs and counts"]
        if stale_negative_control:
            return _impact("tx-submission", "stale-negative-control", repairs=["canonical CBOR lengths"], remaining=remaining)
        return _impact(
            "tx-submission", "refused", repairs=["canonical CBOR lengths"], remaining=remaining,
            code="tx_witness_material_unavailable",
            reason="TxSubmission edits need transaction ownership and witness material that is not mounted in the Freeze node",
        )

    surface = f"{MINI_PROTOCOLS[protocol][0].replace(' ', '-')}-message"
    remaining = ["mini-protocol state/correlation invariants"]
    if stale_negative_control:
        return _impact(
            surface, "stale-negative-control", repairs=["canonical CBOR lengths"], remaining=remaining,
        )
    return _impact(
        surface, "refused", repairs=["canonical CBOR lengths"], remaining=remaining,
        code="semantic_dependency_validator_unavailable",
        reason=f"{surface.removesuffix('-message')} edits have no proven semantic dependency validator at this boundary",
    )


def message_name(type_name: str, payload: Any) -> str | None:
    """The standard name of a mini-protocol message, e.g. ``RollForward``."""
    number = protocol_number(type_name)
    if number is None or not isinstance(payload, list) or not payload or not isinstance(payload[0], int):
        return None
    return MINI_PROTOCOLS.get(number, ("", {}))[1].get(payload[0])


def item_message_name(item: dict[str, Any]) -> str | None:
    """The message name of a network item, from its decoded payload or, failing that, its bytes."""
    name = message_name(item.get("type", ""), item.get("payload"))
    if name is not None or not item.get("raw") or protocol_number(item.get("type", "")) is None:
        return name
    from . import cbor_tree

    try:
        root, _used = cbor_tree.parse_prefix(bytes.fromhex(item["raw"]))
    except (cbor_tree.CborError, ValueError):
        return None
    if root.major == cbor_tree.ARRAY and root.children and root.children[0].major == cbor_tree.UINT:
        return message_name(item["type"], [root.children[0].arg])
    return None


# --- naming the parts of a decoded message ---------------------------------------------------


def _unwrap(path: Path, value: Any) -> tuple[Path, Any]:
    """Step through a tag-24 wrapper and opened embedded CBOR to the value inside."""
    while isinstance(value, dict):
        if "$tag" in value and "value" in value and len(value) == 2:
            path, value = path + ("value",), value["value"]
        elif "$embed" in value and len(value) == 1:
            path, value = path + ("$embed",), value["$embed"]
        else:
            break
    return path, value


def _sequence(names: list[str], path: Path, value: Any, out: Names, inner: dict[str, Callable[..., None]] | None = None) -> None:
    if not isinstance(value, list):
        return
    for index, name in enumerate(names[: len(value)]):
        out[path + (index,)] = name
        if inner and name in inner:
            child_path, child = _unwrap(path + (index,), value[index])
            inner[name](child_path, child, out)


def _keyed(names: dict[int, str], path: Path, value: Any, out: Names, inner: dict[str, Callable[..., None]] | None = None) -> None:
    """Name the values of a map with integer keys."""
    if not (isinstance(value, dict) and "$map" in value):
        return
    for index, pair in enumerate(value["$map"]):
        key, child = pair
        name = names.get(key) if isinstance(key, int) else None
        if name is None:
            continue
        child_path = path + ("$map", index, 1)
        out[child_path] = name
        if inner and name in inner:
            inner[name](*_unwrap(child_path, child), out)


def _each(function: Callable[..., None]) -> Callable[..., None]:
    def apply(path: Path, value: Any, out: Names) -> None:
        if isinstance(value, list):
            for index, child in enumerate(value):
                function(*_unwrap(path + (index,), child), out)
        elif isinstance(value, dict) and "$tag" in value:  # sets are tagged 258
            apply(*_unwrap(path, value), out)

    return apply


def point(path: Path, value: Any, out: Names) -> None:
    _sequence(["slot", "hash"], path, value, out)


def tip(path: Path, value: Any, out: Names) -> None:
    _sequence(["point", "block_number"], path, value, out, {"point": point})


def header_body(path: Path, value: Any, out: Names) -> None:
    _sequence(HEADER_BODY, path, value, out, {
        "vrf_result": lambda p, v, o: _sequence(["vrf_output", "vrf_proof"], p, v, o),
        "operational_cert": lambda p, v, o: _sequence(["hot_vkey", "sequence_number", "kes_period", "cold_signature"], p, v, o),
        "protocol_version": lambda p, v, o: _sequence(["major", "minor"], p, v, o),
    })


def header(path: Path, value: Any, out: Names) -> None:
    _sequence(["header_body", "kes_signature"], path, value, out, {"header_body": header_body})


def tx_input(path: Path, value: Any, out: Names) -> None:
    _sequence(["transaction_id", "output_index"], path, value, out)


def tx_output(path: Path, value: Any, out: Names) -> None:
    if isinstance(value, list):
        _sequence(["address", "value", "datum_hash"], path, value, out)
    else:
        _keyed(TX_OUTPUT, path, value, out)


def tx_body(path: Path, value: Any, out: Names) -> None:
    inputs = _each(tx_input)
    _keyed(TX_BODY, path, value, out, {
        "inputs": inputs, "collateral": inputs, "reference_inputs": inputs,
        "outputs": _each(tx_output), "collateral_return": tx_output,
    })


def witness_set(path: Path, value: Any, out: Names) -> None:
    _keyed(WITNESS_SET, path, value, out, {
        "vkey_witnesses": _each(lambda p, v, o: _sequence(["vkey", "signature"], p, v, o)),
    })


def block(path: Path, value: Any, out: Names) -> None:
    _sequence(["header", "transaction_bodies", "transaction_witness_sets", "auxiliary_data", "invalid_transactions"],
              path, value, out, {"header": header, "transaction_bodies": _each(tx_body),
                                 "transaction_witness_sets": _each(witness_set)})


def _era_wrapped(inner: Callable[..., None], what: str) -> Callable[..., None]:
    """``[era, <thing>]`` as blocks and headers are sent between nodes."""

    def apply(path: Path, value: Any, out: Names) -> None:
        if isinstance(value, list) and len(value) == 2 and isinstance(value[0], int):
            out[path + (0,)] = "era"
            out[path + (1,)] = what
            inner(*_unwrap(path + (1,), value[1]), out)

    return apply


def _message(number: int, payload: list[Any], out: Names) -> None:
    tag = payload[0]
    out[(0,)] = "message"
    if number == 2:
        if tag == 2:
            _sequence(["message", "header", "tip"], (), payload, out, {"header": _era_wrapped(header, "header"), "tip": tip})
        elif tag in (3, 5):
            _sequence(["message", "point", "tip"], (), payload, out, {"point": point, "tip": tip})
        elif tag == 4:
            _sequence(["message", "points"], (), payload, out, {"points": _each(point)})
        elif tag == 6:
            _sequence(["message", "tip"], (), payload, out, {"tip": tip})
    elif number == 3:
        if tag == 0:
            _sequence(["message", "from_point", "to_point"], (), payload, out, {"from_point": point, "to_point": point})
        elif tag == 4:
            _sequence(["message", "block"], (), payload, out, {"block": _era_wrapped(block, "block")})
    elif number == 4:
        if tag == 0:
            _sequence(["message", "blocking", "acknowledge", "request"], (), payload, out)
        elif tag == 1:
            _sequence(["message", "transaction_ids"], (), payload, out,
                      {"transaction_ids": _each(lambda p, v, o: _sequence(["transaction_id", "size"], p, v, o))})
        elif tag == 2:
            _sequence(["message", "transaction_ids"], (), payload, out)
        elif tag == 3:
            _sequence(["message", "transactions"], (), payload, out)
    elif number == 8 and tag in (0, 1):
        _sequence(["message", "cookie"], (), payload, out)
    elif number == 0:
        if tag == 0:
            _sequence(["message", "versions"], (), payload, out)
        elif tag == 1:
            _sequence(["message", "version", "parameters"], (), payload, out, {
                "parameters": lambda p, v, o: _sequence(["network_magic", "initiator_only", "peer_sharing", "query"], p, v, o)})
        elif tag == 2:
            _sequence(["message", "reason"], (), payload, out)
    elif number == 10:
        if tag == 0:
            _sequence(["message", "amount"], (), payload, out)
        elif tag == 1:
            _sequence(["message", "peers"], (), payload, out)


def names_for(type_name: str, payload: Any) -> Names:
    """Field names for every part of ``payload`` this module recognises, keyed by path."""
    out: Names = {}
    number = protocol_number(type_name)
    if number is not None and isinstance(payload, list) and payload and isinstance(payload[0], int):
        _message(number, payload, out)
    return out


def about(type_name: str, payload: Any) -> str | None:
    """A one-line summary of a mini-protocol message for tables."""
    name = message_name(type_name, payload)
    if name is None:
        return None
    names = names_for(type_name, payload)
    by_name: dict[str, Any] = {}
    for path, field in names.items():
        value: Any = payload
        try:
            for step in path:
                value = value[step] if not (step == "$map") else value
        except (KeyError, IndexError, TypeError):
            continue
        by_name.setdefault(field, value)
    parts = []
    for field in ("block_number", "slot", "cookie", "amount", "version"):
        value = by_name.get(field)
        if isinstance(value, int):
            parts.append(f"{field.replace('_', ' ')} {value:,}" if field != "cookie" else f"cookie {value}")
    if isinstance(by_name.get("transaction_bodies"), list):
        parts.append(f"{len(by_name['transaction_bodies'])} transactions")
    return " · ".join(parts[:3])
