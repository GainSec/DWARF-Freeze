from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "dwarf"))

from freeze import conditions  # noqa: E402

# What a node reports for a header received from a peer: the label is the protocol, and the
# message name the UI shows (RollForward) comes from the first element of the payload.
HEADER_IN = {"kind": "proto_in", "type": "ouroboros/2", "label": "chain sync", "payload": [2, [6, "header"], "tip"]}
HEADER_OUT = {"kind": "proto_out", "type": "ouroboros/32770", "label": "chain sync", "payload": [2, [6, "header"], "tip"]}
ROLLBACK = {"kind": "proto_in", "type": "ouroboros/2", "label": "chain sync", "payload": [3, "point", "tip"]}
EFFECT = {"kind": "internal_action", "type": "amaru::Effect", "label": "RollForward", "payload": {}}
KEEP_ALIVE = {"kind": "proto_in", "type": "ouroboros/8", "label": "keep alive", "payload": [2]}


def test_label_matches_a_network_message_by_the_name_the_ui_shows():
    predicate = conditions.compile_condition("label=RollForward")

    assert conditions.matches(predicate, HEADER_IN)
    assert conditions.matches(predicate, HEADER_OUT), "the serving side of the protocol too"
    assert not conditions.matches(predicate, ROLLBACK)
    assert not conditions.matches(predicate, KEEP_ALIVE)
    assert conditions.matches(predicate, EFFECT), "an item really labelled so still matches"


def test_a_name_used_by_several_protocols_matches_each_of_them():
    predicate = conditions.compile_condition("kind=proto_in and label=Done")

    assert conditions.matches(predicate, KEEP_ALIVE)
    assert conditions.matches(predicate, {"kind": "proto_in", "type": "ouroboros/2", "label": "chain sync", "payload": [7]})
    assert not conditions.matches(predicate, HEADER_IN)


def test_the_expanded_condition_reads_back_as_it_was_typed():
    assert conditions.describe(conditions.compile_condition("label=RollForward")) == "label=RollForward"
    assert conditions.describe(conditions.compile_condition("kind=proto_in and label=RollBackward")) == "kind=proto_in and label=RollBackward"


def test_other_labels_and_other_tests_are_unchanged():
    assert conditions.compile_condition("label=send") == {"op": "label", "eq": "send"}
    assert conditions.compile_condition("label~RollForward") == {"op": "label", "contains": "RollForward"}
    assert conditions.compile_condition("label=chain sync") == {"op": "label", "eq": "chain sync"}
