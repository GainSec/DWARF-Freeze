from __future__ import annotations

import hashlib
import sys
from pathlib import Path

import cbor2

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "dwarf"))

from freeze import cardano  # noqa: E402


def _header(*, slot: int = 10, body_size: int = 20, kes_period: int = 3) -> list:
    body = [
        7,
        slot,
        b"p" * 32,
        b"i" * 32,
        b"v" * 32,
        [b"o" * 64, b"r" * 80],
        body_size,
        b"b" * 32,
        [b"k" * 32, 1, kes_period, b"c" * 64],
        [10, 0],
    ]
    return [body, b"s" * 448]


def _roll_forward(*, era: int = 6, **header_changes) -> bytes:
    header = _header(**header_changes)
    return cbor2.dumps([2, [era, cbor2.CBORTag(24, cbor2.dumps(header))], [[header[0][1], b"h" * 32], 7]])


def _block_message(*, fee: int = 7, witness: bytes = b"w" * 64) -> bytes:
    block = [
        _header(),
        [{0: [], 1: [], 2: fee}],
        [{0: [[b"q" * 32, witness]]}],
        {},
        [],
    ]
    return cbor2.dumps([4, cbor2.CBORTag(24, cbor2.dumps([7, block]))])


def test_block_body_commitment_uses_the_four_exact_cbor_components():
    header = cbor2.dumps([[1, 2], b"signature"])
    components = (
        cbor2.dumps([{0: [], 1: [], 2: 7}]),
        cbor2.dumps([{}]),
        cbor2.dumps({}),
        cbor2.dumps([]),
    )
    block = b"\x85" + header + b"".join(components)
    network_block = b"\x82" + cbor2.dumps(7) + block
    message = cbor2.dumps([4, cbor2.CBORTag(24, network_block)])
    expected_hash = hashlib.blake2b(
        b"".join(hashlib.blake2b(component, digest_size=32).digest() for component in components),
        digest_size=32,
    ).hexdigest()

    assert cardano.block_body_commitment("ouroboros/3", message) == (expected_hash, sum(map(len, components)))


def test_block_body_commitment_ignores_other_or_malformed_messages():
    assert cardano.block_body_commitment("ouroboros/2", cbor2.dumps([4, b"not a block"])) is None
    assert cardano.block_body_commitment("ouroboros/3", b"not cbor") is None
    assert cardano.block_body_commitment("ouroboros/3", cbor2.dumps([5])) is None


def test_edit_impact_names_header_repair_and_protected_consensus_refusals():
    original = _roll_forward()
    body_edit = cardano.edit_impact(
        "ouroboros/2", original, _roll_forward(body_size=21),
        resigning={"available": True, "enabled": True}, item_kind="proto_in",
    )
    assert body_edit == {
        "surface": "chainsync-header",
        "state": "auto-repair",
        "code": None,
        "damage": ["KES signature", "header hash", "tip/descendant points", "BlockFetch point mapping"],
        "repairs": ["canonical CBOR lengths", "controlled-devnet KES signature", "header/tip/ancestry mappings"],
        "remaining": [],
        "reason": None,
    }

    protected = cardano.edit_impact(
        "ouroboros/2", original, _roll_forward(slot=11),
        resigning={"available": True, "enabled": True}, item_kind="proto_in",
    )
    assert protected["state"] == "refused"
    assert protected["code"] == "slot_vrf_eligibility_unproven"
    assert protected["reason"] == "slot is protected because changing it requires a new Praos VRF proof and proven leader eligibility"

    stale = cardano.edit_impact(
        "ouroboros/2", original, _roll_forward(kes_period=4),
        resigning={"available": True, "enabled": True}, item_kind="proto_in", stale_negative_control=True,
    )
    assert stale["state"] == "stale-negative-control"
    assert "operational certificate cold signature" in stale["remaining"]


def test_edit_impact_refuses_a_roll_forward_era_tag_change():
    original = _roll_forward()
    edited = cbor2.loads(original)
    edited[1][0] = 5

    report = cardano.edit_impact(
        "ouroboros/2", original, cbor2.dumps(edited),
        resigning={"available": True, "enabled": True}, item_kind="proto_out",
    )

    assert report["surface"] == "chainsync-header"
    assert report["state"] == "refused"
    assert report["code"] == "era_identity_change_forbidden"
    assert "era" in report["reason"]


def test_edit_impact_refuses_an_edited_roll_forward_in_an_unsupported_unchanged_era():
    report = cardano.edit_impact(
        "ouroboros/2", _roll_forward(era=5), _roll_forward(era=5, body_size=21),
        resigning={"available": True, "enabled": True}, item_kind="proto_out",
    )

    assert report["surface"] == "chainsync-header"
    assert report["state"] == "refused"
    assert report["code"] == "unsupported_header_era"
    assert "Conway" in report["reason"]


def test_malformed_roll_forward_shape_requires_explicit_stale_negative_control():
    original = cbor2.dumps([2, 3])
    edited = cbor2.dumps([2, 4])

    refused = cardano.edit_impact(
        "ouroboros/2", original, edited,
        resigning={"available": True, "enabled": True}, item_kind="proto_in",
    )
    stale = cardano.edit_impact(
        "ouroboros/2", original, edited,
        resigning={"available": True, "enabled": True}, item_kind="proto_in",
        stale_negative_control=True,
    )

    assert refused["state"] == "refused"
    assert refused["code"] == "edited_message_not_decodable"
    assert stale["state"] == "stale-negative-control"


def test_edit_impact_refuses_partial_block_repair_until_stale_witnesses_are_explicit():
    original = _block_message()
    edited = _block_message(fee=8)
    refused = cardano.edit_impact(
        "ouroboros/3", original, edited,
        resigning={"available": True, "enabled": True}, item_kind="proto_in",
    )
    assert refused["surface"] == "blockfetch-block"
    assert refused["state"] == "refused"
    assert refused["code"] == "tx_witness_material_unavailable"
    assert refused["repairs"] == ["canonical CBOR lengths", "block body hash and size", "pre-announced signed header"]
    assert refused["remaining"] == ["transaction vkey/bootstrap witnesses"]
    assert refused["reason"] == "transaction-body edits require witness regeneration, but no uniquely owned payment signing keys are mounted"

    allowed = cardano.edit_impact(
        "ouroboros/3", original, edited,
        resigning={"available": True, "enabled": True}, item_kind="proto_in", stale_negative_control=True,
    )
    assert allowed["state"] == "stale-negative-control"
    assert allowed["remaining"] == ["transaction vkey/bootstrap witnesses"]


def test_edit_impact_fails_closed_for_raw_wire_unknown_and_public_header_edits():
    wire = cardano.edit_impact("network/tcp", b"a", b"b", resigning={})
    assert wire["state"] == "refused"
    assert wire["reason"] == "raw wire bytes have no complete typed message or producer/request identity"

    unknown = cardano.edit_impact("ouroboros/99", cbor2.dumps([0]), cbor2.dumps([1]), resigning={})
    assert unknown["state"] == "refused"
    assert "unsupported protocol 99" in unknown["reason"]

    public = cardano.edit_impact(
        "ouroboros/2", _roll_forward(), _roll_forward(body_size=21),
        resigning={}, item_kind="proto_in",
    )
    assert public["state"] == "refused"
    assert public["code"] == "controlled_devnet_signing_unavailable"
    assert public["reason"] == "controlled-devnet header re-signing is unavailable"


def test_edit_impact_has_stable_codes_for_every_protected_consensus_input():
    original = _roll_forward()
    mutations = {
        "issuer_identity_change_forbidden": (3, b"I" * 32),
        "vrf_identity_change_forbidden": (4, b"V" * 32),
        "vrf_leader_proof_regeneration_unavailable": (5, [b"O" * 64, b"R" * 80]),
        "operational_certificate_cold_key_unavailable": (8, [b"K" * 32, 1, 3, b"C" * 64]),
    }
    for code, (index, value) in mutations.items():
        edited = cbor2.loads(original)
        header = cbor2.loads(edited[1][1].value)
        header[0][index] = value
        edited[1][1] = cbor2.CBORTag(24, cbor2.dumps(header))
        report = cardano.edit_impact(
            "ouroboros/2", original, cbor2.dumps(edited),
            resigning={"available": True, "enabled": True}, item_kind="proto_out",
        )
        assert report["state"] == "refused"
        assert report["code"] == code


def test_edit_impact_refuses_known_protocol_edits_without_a_semantic_validator():
    original = cbor2.dumps([0, 11])
    edited = cbor2.dumps([0, 12])

    refused = cardano.edit_impact("ouroboros/8", original, edited, item_kind="proto_in")
    assert refused["state"] == "refused"
    assert refused["remaining"] == ["mini-protocol state/correlation invariants"]
    assert refused["reason"] == "keep-alive edits have no proven semantic dependency validator at this boundary"

    stale = cardano.edit_impact(
        "ouroboros/8", original, edited, item_kind="proto_in", stale_negative_control=True,
    )
    assert stale["state"] == "stale-negative-control"
    assert stale["remaining"] == ["mini-protocol state/correlation invariants"]


def test_edit_impact_requires_direction_but_reports_controlled_outbound_repair():
    absent = cardano.edit_impact(
        "ouroboros/2", _roll_forward(), _roll_forward(body_size=21),
        resigning={"available": True, "enabled": True}, item_kind=None,
    )
    assert absent["state"] == "refused"
    assert absent["code"] == "network_direction_unavailable"

    outbound = cardano.edit_impact(
        "ouroboros/2", _roll_forward(), _roll_forward(body_size=21),
        resigning={"available": True, "enabled": True}, item_kind="proto_out",
    )
    assert outbound["state"] == "auto-repair"
    assert outbound["code"] is None
    assert "controlled-devnet KES signature" in outbound["repairs"]

    block = cardano.edit_impact(
        "ouroboros/3", _block_message(), _block_message(witness=b"x" * 64),
        resigning={"available": True, "enabled": True}, item_kind="proto_out",
    )
    assert block["state"] == "refused"
    assert block["code"] == "tx_witness_material_unavailable"
