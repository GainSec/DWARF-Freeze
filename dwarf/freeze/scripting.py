"""Run explicit local Python callbacks against held Freeze items."""

from __future__ import annotations

import copy
import math
import re
import runpy
from dataclasses import dataclass
from dataclasses import field
from pathlib import Path
from typing import Any, Callable

from .client import FreezeClient, FreezeError
from .conditions import matches
from .session import Session


HOLD_ALL = {"op": "true"}
JSON_SAFE_INTEGER = 9_007_199_254_740_991


class ScriptError(ValueError):
    """A script cannot be loaded or made an unsafe decision."""


@dataclass(frozen=True)
class Decision:
    action: str
    value: Any = None


@dataclass
class ScriptSummary:
    actions: dict[str, int] = field(default_factory=lambda: {
        "forward": 0, "drop": 0, "replace": 0, "replace_raw": 0, "hold": 0
    })
    filtered: int = 0
    non_holdable: int = 0
    held_seq: int | None = None
    hold_restored: bool = False
    running_restored: bool = False
    kept_frozen_for_safety: bool = False
    record_errors: list[str] = field(default_factory=list)
    restore_errors: list[str] = field(default_factory=list)


def load_callback(path: Path) -> Callable[[dict[str, Any]], Any]:
    """Load ``on_item`` from one explicitly named local Python file."""
    path = Path(path)
    if path.suffix != ".py":
        raise ScriptError(f"script must name a local .py file: {path}")
    if not path.is_file():
        raise ScriptError(f"script does not exist: {path}")
    try:
        namespace = runpy.run_path(str(path))
    except Exception as error:
        raise ScriptError(f"failed while loading {path}: {type(error).__name__}: {error}") from error
    callback = namespace.get("on_item")
    if not callable(callback):
        raise ScriptError(f"script {path} must define callable `on_item`")
    return callback


def _payload_error(path: str, problem: str) -> None:
    raise ScriptError(f"replace payload violates protocol section 4 at {path}: {problem}")


def _validate_payload(value: Any, path: str = "$", active: set[int] | None = None) -> None:
    """Validate one replacement payload against FCP v1 section 4."""
    if value is None or isinstance(value, (bool, str)):
        return
    if isinstance(value, int):
        if abs(value) > JSON_SAFE_INTEGER:
            _payload_error(path, f"unsafe bare integer {value} must use `$int`")
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            _payload_error(path, "non-finite float must use `$float`")
        return
    if not isinstance(value, (list, dict)):
        _payload_error(path, f"unsupported value type {type(value).__name__}")

    active = active if active is not None else set()
    identity = id(value)
    if identity in active:
        _payload_error(path, "cyclic containers are not JSON payloads")
    active.add(identity)
    try:
        if isinstance(value, list):
            for index, child in enumerate(value):
                _validate_payload(child, f"{path}[{index}]", active)
            return

        for key in value:
            if not isinstance(key, str):
                _payload_error(path, f"object key {key!r} is not text")
        reserved = [key for key in value if key.startswith("$")]
        if not reserved:
            for key, child in value.items():
                _validate_payload(child, f"{path}.{key}", active)
            return

        if "$int" in value:
            if set(value) != {"$int"}:
                _payload_error(path, "malformed `$int` form")
            text = value["$int"]
            if (
                not isinstance(text, str)
                or re.fullmatch(r"-?(0|[1-9][0-9]*)", text) is None
                or text == "-0"
            ):
                _payload_error(path, "`$int` must be a canonical decimal string")
            if abs(int(text)) <= JSON_SAFE_INTEGER:
                _payload_error(path, "`$int` must encode an unsafe integer")
            return

        if "$float" in value:
            if set(value) != {"$float"}:
                _payload_error(path, "malformed `$float` form")
            if value["$float"] not in ("nan", "inf", "-inf"):
                _payload_error(path, "`$float` must be 'nan', 'inf', or '-inf'")
            return

        if "$bytes" in value:
            if set(value) != {"$bytes"}:
                _payload_error(path, "malformed `$bytes` form")
            raw = value["$bytes"]
            if (
                not isinstance(raw, str)
                or len(raw) % 2
                or re.fullmatch(r"[0-9a-f]*", raw) is None
            ):
                _payload_error(path, "`$bytes` must be lowercase even-length hexadecimal text")
            return

        if "$map" in value:
            if set(value) != {"$map"}:
                _payload_error(path, "malformed `$map` form")
            pairs = value["$map"]
            if not isinstance(pairs, list):
                _payload_error(path, "`$map` must contain an array of pairs")
            keys = []
            for index, pair in enumerate(pairs):
                if not isinstance(pair, list) or len(pair) != 2:
                    _payload_error(path, f"`$map` entry {index} must be a two-element array")
                keys.append(pair[0])
                _validate_payload(pair[0], f"{path}.$map[{index}][0]", active)
                _validate_payload(pair[1], f"{path}.$map[{index}][1]", active)
            if (
                all(isinstance(key, str) and not key.startswith("$") for key in keys)
                and len(set(keys)) == len(keys)
            ):
                _payload_error(path, "`$map` with unique non-reserved text keys must use an ordinary object")
            return

        if "$tag" in value:
            if set(value) != {"$tag", "value"}:
                _payload_error(path, "malformed `$tag` form")
            tag = value["$tag"]
            if type(tag) is not int or not 0 <= tag <= JSON_SAFE_INTEGER:
                _payload_error(path, "`$tag` must be a non-negative JSON-safe integer")
            _validate_payload(value["value"], f"{path}.value", active)
            return

        _payload_error(path, f"reserved key {reserved[0]!r} is not a legal tagged payload form")
    finally:
        active.remove(identity)


def validate_decision(result: Any, item: dict[str, Any]) -> Decision:
    """Turn an ``on_item`` result into a fully validated action."""
    if result is None:
        return Decision("forward")
    if isinstance(result, str):
        if result == "forward":
            return Decision("forward")
        if result == "hold":
            return Decision("hold")
        if result == "drop":
            if not item.get("droppable"):
                raise ScriptError(f"item {item.get('seq')} is not droppable")
            return Decision("drop")
        raise ScriptError(f"unsupported decision {result!r}")
    if not isinstance(result, dict):
        raise ScriptError(f"unsupported decision result of type {type(result).__name__}")
    if "action" not in result:
        raise ScriptError("decision mapping requires an `action`")

    action = result["action"]
    if not isinstance(action, str):
        raise ScriptError("decision mapping `action` must be a string")
    if action == "replace":
        if set(result) != {"action", "payload"}:
            raise ScriptError("replace decision must contain exactly `action` and `payload`")
        _validate_payload(result["payload"])
        if not item.get("editable"):
            raise ScriptError(f"item {item.get('seq')} is not editable")
        return Decision("replace", result["payload"])
    if action == "replace_raw":
        if set(result) != {"action", "raw"}:
            raise ScriptError("replace_raw decision must contain exactly `action` and `raw`")
        raw = result["raw"]
        if not isinstance(raw, str) or len(raw) % 2 or re.fullmatch(r"[0-9a-fA-F]*", raw) is None:
            raise ScriptError("replace_raw `raw` must be an even-length hexadecimal string")
        if not item.get("editable"):
            raise ScriptError(f"item {item.get('seq')} is not editable")
        return Decision("replace_raw", bytes.fromhex(raw))
    raise ScriptError(f"unsupported mapping action {action!r}")


def _restore_state(
    client: FreezeClient,
    old_hold: dict[str, Any] | None,
    was_frozen: bool,
    summary: ScriptSummary,
) -> None:
    try:
        client.set_hold(old_hold)
        summary.hold_restored = True
    except (FreezeError, OSError) as error:
        summary.restore_errors.append(f"could not restore hold predicate: {error}")
        if not was_frozen:
            summary.kept_frozen_for_safety = True
        return

    if was_frozen:
        # Each decision lets the node run to the next item, so put it back as it was found.
        try:
            client.freeze()
        except (FreezeError, OSError) as error:
            summary.restore_errors.append(f"could not freeze the node again: {error}")
        return
    try:
        status = client.status()
        unsafe = []
        for seq in status.get("held", []):
            item = client.item(seq)
            if old_hold is None or not matches(old_hold, item):
                unsafe.append(seq)
        if unsafe:
            summary.kept_frozen_for_safety = True
            return
        client.thaw()
        summary.running_restored = True
    except (FreezeError, OSError) as error:
        summary.restore_errors.append(f"could not restore running state: {error}")
        summary.kept_frozen_for_safety = True


def _record_event(
    event: str,
    data: Any,
    session: Session,
    summary: ScriptSummary,
    *,
    suppress_errors: bool = False,
) -> None:
    held_seq = data.get("seq") if event == "item" and data.get("held") else None
    if held_seq is not None:
        summary.held_seq = held_seq if summary.held_seq is None else min(summary.held_seq, held_seq)
    try:
        if event != "item" or data["seq"] not in session.items:
            session.record(event, data)
    except Exception as error:
        retained = f" for held item {held_seq}" if held_seq is not None else ""
        problem = f"could not record `{event}` event{retained}: {error}"
        summary.record_errors.append(problem)
        if not suppress_errors:
            raise ScriptError(problem) from error
        return
    if event == "item" and not data.get("held"):
        summary.non_holdable += 1


def _synchronize_held(
    client: FreezeClient,
    session: Session,
    summary: ScriptSummary,
) -> list[int]:
    """Refresh the summary and recording from held agent state without releasing anything."""
    held = sorted(client.status().get("held", []))
    summary.held_seq = held[0] if held else None
    for seq in held:
        if seq not in session.items:
            previous_errors = set(summary.record_errors)
            _record_event(
                "item",
                client.item(seq),
                session,
                summary,
                suppress_errors=True,
            )
            if summary.record_errors and summary.record_errors[-1] in previous_errors:
                summary.record_errors.pop()
    return held


def _record_message(
    client: FreezeClient,
    message: dict[str, Any],
    session: Session,
    summary: ScriptSummary,
    *,
    suppress_record_errors: bool = False,
) -> None:
    event, data = message["event"], message.get("data")
    _record_event(event, data, session, summary, suppress_errors=suppress_record_errors)
    if event == "error" and data.get("code") == "events_lost":
        missed = data.get("missed")
        count = f"missed {missed} {'event' if missed == 1 else 'events'}"
        try:
            status = client.status()
            held = sorted(status.get("held", []))
            for seq in held:
                if seq not in session.items:
                    _record_event("item", client.item(seq), session, summary)
        except (FreezeError, OSError) as error:
            raise ScriptError(
                f"agent reported `events_lost` ({count}); "
                f"could not resynchronize held items: {error}; event stream is incomplete"
            ) from error
        if held:
            summary.held_seq = held[0]
            retained = f"resynchronized held item {held[0]}"
        else:
            retained = "no held item was reported during resynchronization"
        raise ScriptError(
            f"agent reported `events_lost` ({count}); {retained}; event stream is incomplete"
        )


def _next_held(
    client: FreezeClient,
    processed: set[int],
    session: Session,
    summary: ScriptSummary,
) -> dict[str, Any]:
    while True:
        message = client.next_event(timeout=0.25)
        if message is None:
            continue
        _record_message(client, message, session, summary)
        if message["event"] == "closed":
            raise FreezeError("connection closed", "closed")
        item = message.get("data")
        if message["event"] == "item" and item.get("held") and item["seq"] not in processed:
            return item


def _drain_events(
    client: FreezeClient,
    session: Session,
    summary: ScriptSummary,
    *,
    suppress_errors: bool = False,
) -> list[dict[str, Any]]:
    held: list[dict[str, Any]] = []
    while True:
        message = client.next_event(timeout=0)
        if message is None:
            return held
        _record_message(
            client,
            message,
            session,
            summary,
            suppress_record_errors=suppress_errors,
        )
        data = message.get("data")
        if message["event"] == "item" and data.get("held"):
            held.append(data)


def run(
    client: FreezeClient,
    on_item: Callable[[dict[str, Any]], Any],
    session: Session,
    *,
    only: dict[str, Any] | None = None,
    summary: ScriptSummary | None = None,
) -> ScriptSummary:
    """Intercept held items until ``on_item`` returns ``"hold"`` or an error occurs."""
    summary = summary or ScriptSummary()
    original = client.status()
    old_hold = original.get("hold")
    was_frozen = bool(original.get("frozen"))
    pending: list[dict[str, Any]] = []
    processed: set[int] = set()
    failure: BaseException | None = None
    try:
        client.set_hold(HOLD_ALL)
        queued = _drain_events(client, session, summary)
        current = set(client.status().get("held", []))
        startup = {item["seq"]: item for item in queued if item["seq"] in current}
        for seq in sorted(current):
            if seq in startup:
                continue
            item = client.item(seq)
            if seq not in session.items:
                _record_event("item", item, session, summary)
            startup[seq] = item
        pending.extend(startup[seq] for seq in sorted(startup))

        while True:
            item = pending.pop(0) if pending else _next_held(client, processed, session, summary)
            if item["seq"] in processed:
                continue
            summary.held_seq = item["seq"]
            if only is not None and not matches(only, item):
                summary.filtered += 1
                result = None
            else:
                try:
                    result = on_item(copy.deepcopy(item))
                except KeyboardInterrupt:
                    raise
                except Exception as error:
                    raise ScriptError(
                        f"on_item failed for item {item['seq']}: {type(error).__name__}: {error}"
                    ) from error
            decision = validate_decision(result, item)
            if decision.action == "hold":
                summary.actions[decision.action] += 1
                return summary
            if decision.action == "forward":
                client.forward(item["seq"])
            elif decision.action == "drop":
                client.drop(item["seq"])
            elif decision.action == "replace":
                client.replace(item["seq"], payload=decision.value)
            else:
                client.replace(item["seq"], raw=decision.value)
            summary.actions[decision.action] += 1
            processed.add(item["seq"])
            summary.held_seq = None
            client.thaw()
    except BaseException as error:
        failure = error
        raise
    finally:
        new_record_errors = len(summary.record_errors)
        cleanup_failure: BaseException | None = None
        try:
            _drain_events(client, session, summary, suppress_errors=True)
        except BaseException as error:
            cleanup_failure = error
        try:
            _restore_state(client, old_hold, was_frozen, summary)
        except BaseException as error:
            if cleanup_failure is None:
                cleanup_failure = error
        try:
            _drain_events(client, session, summary, suppress_errors=True)
        except BaseException as error:
            if cleanup_failure is None:
                cleanup_failure = error
        try:
            _synchronize_held(client, session, summary)
        except BaseException as error:
            summary.kept_frozen_for_safety = True
            if (
                cleanup_failure is None
                and len(summary.record_errors) == new_record_errors
                and not summary.restore_errors
            ):
                if isinstance(error, (FreezeError, OSError)):
                    cleanup_failure = ScriptError(
                        f"could not synchronize held items: {error}"
                    )
                else:
                    cleanup_failure = error
        if failure is None:
            if cleanup_failure is not None:
                raise cleanup_failure
            if len(summary.record_errors) > new_record_errors:
                raise ScriptError(summary.record_errors[-1])
            if summary.restore_errors:
                raise ScriptError(summary.restore_errors[-1])
