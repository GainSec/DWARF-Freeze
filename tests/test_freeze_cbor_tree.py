import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "dwarf"))

from freeze import cbor_tree as ct  # noqa: E402

# A block-fetch style message with deliberately awkward encodings:
#   [4, 24(h'<inner>')] where inner = [_ {0: [h'aa'], 2: 301204 (as 8 bytes), "x": "y"}, 1(over-long), -5, true]
INNER = bytes.fromhex(
    "9f"  # indefinite array
    "a3" "00" "81" "41aa" "02" "1b0000000000049894" "6178" "6179"  # map; fee written as uint64
    "1801"  # the integer 1 written in two bytes
    "24"  # -5
    "f5"  # true
    "ff"
)
MESSAGE = bytes.fromhex("8204d818") + bytes([0x58, len(INNER)]) + INNER


def test_untouched_bytes_come_back_exactly():
    for data in (MESSAGE, INNER, bytes.fromhex("5f41aa42bbccff"), bytes.fromhex("fb3ff8000000000000"), bytes.fromhex("c249010000000000000000")):
        assert ct.encode(ct.parse(data)) == data


def test_embedded_cbor_is_shown_and_edited_in_place():
    root = ct.parse(MESSAGE)
    ct.open_tagged(root)
    shown = ct.to_json(root)
    inner = shown[1]["value"]["$embed"]
    assert inner[0] == {"$map": [[0, [{"$bytes": "aa"}]], [2, 301204], ["x", "y"]]}
    assert inner[1:] == [1, -5, True]

    fee_path = (1, "value", "$embed", 0, "$map", 1, 1)
    edited = ct.edit(MESSAGE, fee_path, 1)
    assert len(edited) == len(MESSAGE), "the fee keeps its 8-byte encoding, so no length changes"
    differing = [i for i, (a, b) in enumerate(zip(MESSAGE, edited)) if a != b]
    assert differing == list(range(MESSAGE.index(bytes.fromhex("049894")), MESSAGE.index(bytes.fromhex("049894")) + 3))
    again = ct.parse(edited)
    ct.open_tagged(again)
    assert ct.to_json(again)[1]["value"]["$embed"][0]["$map"][1] == [2, 1]


def test_a_longer_value_grows_only_the_enclosing_lengths():
    path = (1, "value", "$embed", 0, "$map", 0, 1, 0)
    edited = ct.edit(MESSAGE, path, {"$bytes": "aabbccdd"})
    root = ct.parse(edited)
    ct.open_tagged(root)
    assert ct.to_json(root)[1]["value"]["$embed"][0]["$map"][0] == [0, [{"$bytes": "aabbccdd"}]]
    assert len(edited) == len(MESSAGE) + 3
    assert edited[4:6] == bytes([0x58, len(INNER) + 3]), "the wrapping byte string's length was updated"
    assert edited.endswith(INNER[-6:]), "everything after the edit is unchanged"


def test_integers_change_sign_and_width_only_when_needed():
    assert ct.edit(bytes.fromhex("1801"), (), 2) == bytes.fromhex("1802")
    assert ct.edit(bytes.fromhex("1801"), (), 70000) == bytes.fromhex("1a00011170")
    assert ct.edit(bytes.fromhex("05"), (), -1) == bytes.fromhex("20")
    assert ct.edit(bytes.fromhex("05"), (), {"$int": "18446744073709551615"}) == bytes.fromhex("1bffffffffffffffff")
    with pytest.raises(ct.CborError):
        ct.edit(bytes.fromhex("05"), (), "text")


def test_malformed_input_is_rejected_not_guessed():
    for bad in ("", "18", "5803aa", "9f01", "a101", "ff", "8201", "0102"):
        with pytest.raises(ct.CborError):
            ct.parse(bytes.fromhex(bad))
    assert ct.parse_prefix(bytes.fromhex("0102"))[1] == 1
    assert not ct.open_embedded(ct.parse(bytes.fromhex("42ffff")))
