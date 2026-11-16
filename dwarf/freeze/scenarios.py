"""DWARF's scenarios, seen from Freeze.

DWARF describes each test as a scenario file: a topology, a workload made of primitives, and
assertions. Freeze reads those files (it never writes them or runs DWARF's own machinery) and
does three things with them:

* lists every scenario with what it is about;
* says whether Freeze can carry out the same test from inside a running node, and if not, why;
* for the ones it can, builds a Freeze template: the rules that do the tampering, with the
  test values DWARF uses, and what reaction to expect.

DWARF applies most of these to cardano-node through an external proxy or a fake peer. Freeze
applies the same tampering to the traffic of the node it sits in, so on Amaru many of these are
new coverage rather than a repeat.
"""

from __future__ import annotations

import ast
import json
from dataclasses import dataclass
from dataclasses import field as dc_field
from functools import lru_cache
from pathlib import Path
from typing import Any

from . import rules

REPO = Path(__file__).resolve().parents[2]
SCENARIOS = REPO / "dwarf" / "scenarios"
SOURCES = REPO / "dwarf"

READY, PARTLY, NO = "ready", "partly", "no"
REJECTION = ["rejected", "error", "hung up", "hostile"]
PROTOCOLS = {"0": "handshake", "2": "chain sync", "3": "block fetch", "4": "tx submission", "8": "keep alive", "10": "peer sharing"}
MESSAGE_NUMBERS = {
    ("2", "roll_forward"): 2, ("2", "roll_backward"): 3, ("2", "request_next"): 0, ("2", "find_intersect"): 4,
    ("3", "request_range"): 0, ("3", "block"): 4, ("3", "start_batch"): 2, ("3", "batch_done"): 5,
    ("0", "propose_versions"): 0,
}


@dataclass
class Entry:
    id: str
    title: str
    implementation: str
    runtime: str
    tags: list[str]
    threats: list[str]
    risks: list[str]
    primitives: list[dict[str, Any]]
    assertions: list[str]
    path: Path
    status: str = NO
    reason: str = ""
    subject: str = ""
    notes: list[str] = dc_field(default_factory=list)


# --- reading DWARF's own tables without importing DWARF ----------------------------------------


def _literal(node: ast.AST, names: dict[str, Any]) -> Any:
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Name) and node.id in names:
        return names[node.id]
    if isinstance(node, (ast.List, ast.Tuple)):
        return [_literal(element, names) for element in node.elts]
    if isinstance(node, ast.Dict):
        return {_literal(key, names): _literal(value, names) for key, value in zip(node.keys, node.values) if key is not None}
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
        return -_literal(node.operand, names)
    raise ValueError("not plain data")


@lru_cache(maxsize=None)
def _tables(relative: str) -> dict[str, Any]:
    """Every module-level name in a DWARF source file that is plain data."""
    path = SOURCES / relative
    if not path.is_file():
        return {}
    found: dict[str, Any] = {}
    tree = ast.parse(path.read_text(encoding="utf-8"))
    # module level first, then anything nested (some tables live inside classes or functions)
    for node in list(tree.body) + [node for node in ast.walk(tree) if node not in tree.body]:
        target = value = None
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            target, value = node.targets[0].id, node.value
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.value is not None:
            target, value = node.target.id, node.value
        if target is None or target in found:
            continue
        try:
            found[target] = _literal(value, found)
        except (ValueError, TypeError):
            continue
    return found


def _hex_cases(table: Any) -> dict[str, str]:
    """``{case name: hex}`` from one of DWARF's case tables, whatever its exact shape."""
    cases: dict[str, str] = {}
    if isinstance(table, dict):
        for name, value in table.items():
            if isinstance(value, str):
                cases[str(name)] = value
            elif isinstance(value, dict):
                for key in ("payload_hex", "hex", "message_hex", "cbor_hex"):
                    if isinstance(value.get(key), str):
                        cases[str(name)] = value[key]
                        break
    elif isinstance(table, list):
        for index, value in enumerate(table):
            if isinstance(value, dict):
                name = str(value.get("id") or value.get("name") or value.get("case_id") or index)
                for key in ("payload_hex", "hex", "message_hex", "cbor_hex"):
                    if isinstance(value.get(key), str):
                        cases[name] = value[key]
                        break
    return {name: text for name, text in cases.items() if text and all(c in "0123456789abcdefABCDEF" for c in text)}


def value_lists() -> dict[str, dict[str, str]]:
    """The test values DWARF carries, as ``{list name: {case name: hex}}``."""
    primitives = _tables("profile_manager/primitives.py")
    lists = {
        "dwarf handshake cases": _hex_cases(primitives.get("_HANDSHAKE_CASES")),
        "dwarf block fetch cases": _hex_cases(primitives.get("_BLOCKFETCH_CASES")),
        "dwarf tx submission cases": _hex_cases(primitives.get("_TXSUBMISSION_CASES")),
        "dwarf invalid cbor cases": _hex_cases(primitives.get("_INVALID_CBOR_CASES")),
        "dwarf tx submission probe cases": _hex_cases(_tables("scripts/runtime_txsubmission_probe.py").get("MODE_CASES")),
    }
    for name, cases in (_tables("scripts/runtime_amaru_measurement_calibration.py").get("_CASE_SETS") or {}).items():
        lists[f"dwarf handshake set {name}"] = _hex_cases(cases)
    for protocol in ("handshake", "chainsync", "blockfetch", "txsubmission"):
        seeds = SOURCES / "corpora" / f"amaru-cargo-fuzz-{protocol}" / "seeds"
        if seeds.is_dir():
            lists[f"dwarf {protocol} seeds"] = {path.stem: path.read_bytes().hex() for path in sorted(seeds.glob("*.cbor"))}
    return {name: cases for name, cases in lists.items() if cases}


# --- from a scenario's primitives to Freeze rules ----------------------------------------------


def _when(direction: str, protocol: str, message: int | None = None) -> str:
    kind = "proto_in" if direction in ("inbound", "in") else "proto_out"
    text = f"kind={kind} and type=ouroboros/{protocol}"
    return text + (f" and [0]={message}" if message is not None else "")


def _proxy_rule(name: str, mode: dict[str, Any]) -> tuple[list[dict[str, Any]], str, str]:
    """Rules for one of DWARF's man-in-the-middle proxy modes: (rules, status, note)."""
    protocol = str(mode.get("mutation_protocol", "2"))
    direction = str(mode.get("mutation_direction", "inbound"))
    after = int(mode.get("mutate_after_segments", 0) or 0)
    message = MESSAGE_NUMBERS.get((protocol, str(mode.get("mutation_message", ""))))
    when = _when(direction, protocol, message)
    what = PROTOCOLS.get(protocol, f"protocol {protocol}")
    kind = str(mode.get("mutation_mode", ""))
    if kind == "replay_earlier":
        return [{"name": f"{name}: send an earlier {what} message again", "when": when, "action": "replay", "after": after,
                 "expect": REJECTION, "stale_negative_control": True}], READY, ""
    if kind == "corrupt_header_signature":
        return [{"name": f"{name}: damage the header's signature", "when": _when("inbound", "2", 2), "field": "kes_signature",
                 "values": ["xorlast:01"], "after": max(after, 1), "expect": REJECTION,
                 "stale_negative_control": True}], READY, ""
    if kind in ("flip_payload_byte", "flip_once"):
        rule: dict[str, Any] = {"name": f"{name}: flip the last byte of a {what} message", "when": when, "field": rules.WHOLE,
                                "values": ["xorlast:01"], "after": after, "stale_negative_control": True}
        if kind == "flip_payload_byte":
            rule.update({"mode": "cycle", "count": 3})
        if direction == "inbound":
            rule["expect"] = REJECTION
        return [rule], READY, ""
    if kind == "pass_through":
        return [{"name": f"{name}: watch {what} without changing it", "when": when, "action": "forward", "count": 20}], PARTLY, \
            "DWARF only measures here; Freeze lets the messages through and shows them."
    return [], NO, f"the proxy mode `{kind}` inserts new messages into a connection, which Freeze cannot do yet"


def _whole_message(name: str, direction: str, protocol: str, values: dict[str, str], only: str | None = None) -> list[dict[str, Any]]:
    chosen = {only: values[only]} if only and only in values else values
    return [{"name": f"{name}: {case}", "when": _when(direction, protocol), "field": rules.WHOLE, "values": [f"0x{text}"],
             "expect": REJECTION, "stale_negative_control": True} for case, text in chosen.items()]


def _convert(primitive: dict[str, Any]) -> tuple[list[dict[str, Any]], str, str]:
    """Freeze rules for one DWARF primitive: (rules, status, why not or caveat)."""
    name = primitive.get("primitive", "")
    lists = value_lists()
    modes = _tables("scripts/runtime_chainsync_blockfetch_fault.py").get("MODE_DEFAULTS") or {}

    for prefix in ("runtime_", ""):
        key = name.removeprefix("runtime_") if prefix else name
        if key in modes:
            return _proxy_rule(key.replace("_", " "), modes[key])
    if name == "runtime_praos_header_assertion_probe":
        return _proxy_rule("praos header", modes.get("praos_header_invalid_signature", {"mutation_mode": "corrupt_header_signature"}))
    if name == "runtime_byzantine_cardano_node":
        mode = {"mutation_mode": primitive.get("mutation_mode", "flip_payload_byte"),
                "mutation_direction": primitive.get("mutation_direction", "outbound"),
                "mutation_protocol": str(primitive.get("mutation_protocol", "0")),
                "mutate_after_segments": primitive.get("mutate_after_segments", 0)}
        found, status, note = _proxy_rule(str(primitive.get("behavior", "byzantine")).replace("_", " "), mode)
        return found, status, note or "DWARF makes one node misbehave towards another; Freeze damages what this node sends or receives."

    shims = {
        "shim_peer_malformed_handshake": ("0", "dwarf handshake cases", "malformation_id"),
        "shim_peer_malformed_blockfetch": ("3", "dwarf block fetch cases", "malformation_id"),
        "shim_peer_malformed_txsubmission": ("4", "dwarf tx submission cases", "malformation_id"),
        "shim_peer_invalid_cbor": ("0", "dwarf invalid cbor cases", "invalid_cbor_id"),
    }
    if name in shims:
        protocol, list_name, key = shims[name]
        found = _whole_message(PROTOCOLS[protocol], "inbound", protocol, lists.get(list_name, {}), primitive.get(key))
        note = "DWARF opens a fresh connection as a fake peer; Freeze substitutes the same bytes into a message from a real peer."
        return found, (READY if found else NO), note if found else f"DWARF's `{list_name}` table could not be read"
    if name == "shim_peer_raw_bytes" and isinstance(primitive.get("payload_hex"), str):
        return [{"name": "raw bytes from a peer", "when": "kind=wire_in", "field": rules.WHOLE,
                 "values": [f"0x{primitive['payload_hex']}"], "expect": REJECTION,
                 "stale_negative_control": True}], READY, \
            "Replaces the next chunk of bytes read from a peer."
    if name.startswith("runtime_txsubmission_") or name == "runtime_mempool_failure_probe":
        key = name.removeprefix("runtime_")
        cases = lists.get("dwarf tx submission probe cases", {})
        if key in cases:
            return _whole_message("tx submission", "inbound", "4", cases, key), READY, \
                "DWARF sends this as a fake peer; Freeze substitutes it into a real peer's message."
        return [], NO, "this probe builds its message from live state (a window or batch count), not a fixed value"
    if name in ("runtime_protocol_decode_cases", "runtime_amaru_measurement_calibration", "runtime_cardano_measurement_calibration"):
        calibration = _tables("scripts/runtime_amaru_measurement_calibration.py")
        fallback = "CLIENT_INVALID_CASE_SET" if name == "runtime_protocol_decode_cases" else "DEFAULT_CASE_SET"
        case_set = primitive.get("case_set") or calibration.get(fallback) or calibration.get("DEFAULT_CASE_SET")
        cases = lists.get(f"dwarf handshake set {case_set}") if case_set else None
        if cases:
            return _whole_message("handshake", "inbound", "0", cases), READY, \
                "Each case replaces a handshake message from a real peer. DWARF's per-case expected outcomes are not carried over."
        return [], NO, "no fixed handshake case set is named"
    if name in ("fault_local_port_drop", "runtime_preview_upstream_drop", "runtime_network_partition", "runtime_blockfetch_drop_timeout"):
        return [{"name": "drop what peers send", "when": "kind=wire_in", "action": "drop", "count": 20},
                {"name": "drop what the node sends", "when": "kind=wire_out", "action": "drop", "count": 20}], PARTLY, \
            "DWARF drops traffic for a period of time; Freeze drops a number of chunks."
    return [], NO, ""


_WHY_NOT = [
    (("cbor_fuzz", "cbor_edge_cases", "aflpp", "conformance", "dataset_differential"), "tests a decoder on its own, with no running node"),
    (("measurement", "resource_profile", "pcap", "tracer_capture", "syscall_trace", "bundle"), "measures or collects evidence; nothing is tampered with"),
    (("delay", "loss", "reset", "impairment", "throttle", "slow_loris"), "needs traffic delayed or thinned over time, which Freeze cannot do while keeping the node running"),
    (("opcert", "forge_block", "serve_crafted_block", "block_apply"), "needs blocks or headers re-signed with pool keys"),
    (("snapshot", "checkpoint", "resume", "disk_full"), "works on the node's stored data from outside the node"),
    (("time_skew", "epoch_boundary", "hf_boundary", "force_rollback"), "needs the node's clock or chain position forced, which Freeze does not offer yet"),
    (("differential", "submit", "plutus", "redeemer", "governance", "multiasset", "datum", "uplc"), "compares two nodes' verdicts on crafted transactions"),
    (("kill_node", "restart_node", "recovery", "freeze"), "stops and restarts whole nodes"),
    (("burst", "multi_peer", "pressure", "overrun", "cascade", "stress", "bootstrap"), "needs a fake peer or client generating load"),
    (("simulate", "peer_weights", "big_ledger", "churn", "chain_switch", "eclipse"), "arranges the whole topology"),
]


def _subject(primitives: list[str], tags: list[str], identifier: str) -> str:
    text = " ".join(primitives + tags + [identifier])
    for word, subject in (("handshake", "handshake"), ("chainsync", "chain sync"), ("blockfetch", "block fetch"),
                          ("txsubmission", "tx submission"), ("mempool", "mempool"), ("keepalive", "keep alive"),
                          ("peersharing", "peer sharing"), ("opcert", "operational certificates"), ("praos", "headers"),
                          ("plutus", "smart contracts"), ("ledger", "ledger"), ("cbor", "CBOR decoding"),
                          ("consensus", "consensus"), ("measurement", "measurement"), ("snapshot", "stored state"),
                          ("bundle", "evidence tooling"), ("recovery", "recovery"), ("restart", "recovery")):
        if word in text:
            return subject
    return "other"


def _read(path: Path) -> Entry | None:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(document, dict) or "id" not in document:
        return None
    steps: list[dict[str, Any]] = []
    for section in ("setup", "load", "faults"):
        steps += [step for step in document.get(section) or [] if isinstance(step, dict)]
    for phase in document.get("phases") or []:
        for section in ("setup", "load", "faults"):
            steps += [step for step in (phase.get(section) or []) if isinstance(step, dict)]
    assertions = [step.get("primitive", "") for step in document.get("assertions") or [] if isinstance(step, dict)]
    target = document.get("target") or {}
    participants = {node.get("impl") for node in (document.get("substrate") or {}).get("nodes") or [] if isinstance(node, dict)}
    implementation = str(target.get("implementation", ""))
    if "amaru" in participants and implementation != "amaru":
        implementation += " + amaru"
    trace = document.get("m1_trace") or {}
    tags = [str(tag) for tag in document.get("tags") or []]
    entry = Entry(
        id=str(document["id"]), title=str(document.get("title", "")), implementation=implementation,
        runtime=str(document.get("runtime", "")), tags=tags,
        threats=sorted({str(t) for t in trace.get("threat_ids") or []} | {t.upper() for t in tags if t.startswith("tm-")}),
        risks=sorted({str(r) for r in trace.get("risk_candidate_ids") or []} | {t.upper() for t in tags if t.startswith("rr-")}),
        primitives=steps, assertions=assertions, path=path,
    )
    names = [step.get("primitive", "") for step in steps]
    entry.subject = _subject(names, tags, entry.id)
    entry.notes = [str(line) for line in document.get("promotion_blockers") or []]
    found: list[tuple[str, str]] = []
    for step in steps:
        _rules, status, note = _convert(step)
        if status != NO:
            found.append((status, note))
    if found:
        entry.status = READY if all(status == READY for status, _note in found) else PARTLY
        entry.reason = next((note for _status, note in found if note), "")
    else:
        entry.status = NO
        if document.get("runtime") == "library":
            entry.reason = "tests a decoder on its own, with no running node"
        else:
            joined = " ".join(names)
            entry.reason = next((why for words, why in _WHY_NOT if any(word in joined for word in words)),
                                "its workload has no counterpart inside a single node")
    return entry


def catalog(directory: Path | None = None) -> list[Entry]:
    """Every DWARF scenario, with whether Freeze can carry it out."""
    return [entry for entry in (_read(path) for path in sorted((directory or SCENARIOS).glob("*.yaml"))) if entry is not None]


def find(identifier: str, directory: Path | None = None) -> Entry:
    for entry in catalog(directory):
        if entry.id == identifier:
            return entry
    raise KeyError(f"no DWARF scenario with id {identifier!r}")


def template_for(entry: Entry) -> rules.Template:
    """The Freeze template that carries out what a scenario's workload does."""
    documents: list[dict[str, Any]] = []
    notes: list[str] = []
    for step in entry.primitives:
        found, status, note = _convert(step)
        if status != NO:
            documents += found
            if note and note not in notes:
                notes.append(note)
    if not documents:
        raise rules.RuleError(f"`{entry.id}` cannot be carried out by Freeze: {entry.reason}")
    description = entry.title + (" — " + " ".join(notes) if notes else "")
    if entry.assertions:
        description += f" DWARF asserts: {', '.join(entry.assertions)}."
    return rules.parse_template({"name": entry.id, "description": description, "rules": documents})


def summary(entries: list[Entry]) -> dict[str, int]:
    counts = {READY: 0, PARTLY: 0, NO: 0}
    for entry in entries:
        counts[entry.status] += 1
    return counts
