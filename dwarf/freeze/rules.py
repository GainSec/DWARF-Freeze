"""Rules: automatic tampering.

A rule says: when an item matches a condition, replace one of its fields with the next value
from a list (or drop it), forward it, and note how the node reacts. A template is a named
set of rules, with any payload lists it brings along, loaded from one file:

    name: header boundaries
    description: Slot and block number at their limits.
    lists:
      tiny: [0, 1]
    rules:
      - name: header slot
        when: kind=proto_in and type=ouroboros/2 and [0]=2
        field: header_body.slot
        values: integer boundaries        # a list name, or an inline list
        mode: each                        # each (one value per matching item, then stop) or cycle
      - name: starve chain selection
        when: kind=input and to~select_chain
        action: drop
        count: 3
      - name: old header again
        when: kind=proto_in and type=ouroboros/2 and [0]=2
        action: replay                    # let `after` go by (remembering them), then send the first one again
        after: 3
        expect: [rejected, hung up, hostile]   # the run is "as expected" if the node's reaction mentions any

``field: "@message"`` means the whole message as bytes, for replacing or damaging it wholesale.

The engine holds matching items by adding a freeze condition for each armed rule, acts on
them as they arrive, and lets the node run on.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from dataclasses import field as dc_field
from pathlib import Path
from typing import Any, Callable

import yaml

from . import cardano, cbor_tree, conditions, payloads, render
from .client import FreezeClient, FreezeError
from .session import Session, part_of
from .views import FieldError, item_view, path_text, resolve_field

ACTIONS = ("replace", "drop", "forward", "replay", "capture", "delay", "overtake")
WHOLE = "@message"
MODES = ("each", "cycle")


class RuleError(ValueError):
    pass


@dataclass
class Attempt:
    """One time a rule acted on an item."""

    rule: str
    number: int
    seq: int
    action: str
    value: str = ""
    before: str = ""
    after: str = ""
    error: str = ""
    at: float = 0.0
    what: str = ""


@dataclass
class Rule:
    name: str
    when: str
    action: str = "replace"
    field: Any = None
    values: list[Any] = dc_field(default_factory=list)
    mode: str = "each"
    count: int | None = None  # for drop / forward / replay: how many items to act on; None is unlimited
    after: int = 0  # let this many matching items through untouched first
    expect: list[str] = dc_field(default_factory=list)  # any of these in the node's reaction means "as expected"
    list_name: str = ""
    var: str = ""  # capture: the name the field's value is kept under, for `$name` in later values
    delay_ms: int = 0  # delay: how long the item is set aside while the node runs on
    until: str = ""  # overtake: the items that must pass first (default: the rule's own `when`)
    stale_negative_control: bool = False
    passed: int = 0
    remembered: list[str] = dc_field(default_factory=list)  # raw hex of items let through, for replay
    predicate: dict[str, Any] = dc_field(default_factory=dict)
    cursor: int = 0
    condition_id: int | None = None
    armed: bool = False

    def __post_init__(self) -> None:
        if self.action not in ACTIONS:
            raise RuleError(f"rule `{self.name}`: action must be one of {', '.join(ACTIONS)}")
        if self.mode not in MODES:
            raise RuleError(f"rule `{self.name}`: mode must be one of {', '.join(MODES)}")
        if self.action == "replace" and not self.values:
            raise RuleError(f"rule `{self.name}`: a replace rule needs values")
        if self.action == "capture" and (not self.var or self.field is None):
            raise RuleError(f"rule `{self.name}`: a capture rule needs `field` and `as` (a name)")
        if self.action == "delay" and self.delay_ms <= 0:
            raise RuleError(f"rule `{self.name}`: a delay rule needs `delay_ms`")
        try:
            self.until_predicate = conditions.compile_condition(self.until or self.when)
        except conditions.ConditionError as error:
            raise RuleError(f"rule `{self.name}`: until: {error}") from error
        try:
            self.predicate = conditions.compile_condition(self.when)
        except conditions.ConditionError as error:
            raise RuleError(f"rule `{self.name}`: {error}") from error

    @property
    def total(self) -> int | None:
        """How many items this rule will act on in all, or None if it never stops by itself."""
        if self.action == "replace":
            return len(self.values) if self.mode == "each" else self.count
        if self.action in ("replay", "capture", "delay", "overtake"):
            return self.count if self.count is not None else 1
        return self.count

    @property
    def finished(self) -> bool:
        return self.total is not None and self.cursor >= self.total

    def to_document(self) -> dict[str, Any]:
        document: dict[str, Any] = {"name": self.name, "when": self.when, "action": self.action}
        if self.action == "replace":
            document["field"] = list(self.field) if isinstance(self.field, tuple) else self.field
            document["values"] = self.list_name or self.values
            document["mode"] = self.mode
        if self.action == "capture":
            document["field"] = list(self.field) if isinstance(self.field, tuple) else self.field
            document["as"] = self.var
        if self.delay_ms:
            document["delay_ms"] = self.delay_ms
        if self.until:
            document["until"] = self.until
        if self.count is not None:
            document["count"] = self.count
        if self.after:
            document["after"] = self.after
        if self.expect:
            document["expect"] = self.expect
        if self.stale_negative_control:
            document["stale_negative_control"] = True
        return document


@dataclass
class Template:
    name: str
    description: str = ""
    rules: list[Rule] = dc_field(default_factory=list)
    lists: dict[str, list[Any]] = dc_field(default_factory=dict)

    def to_document(self) -> dict[str, Any]:
        document: dict[str, Any] = {"name": self.name, "description": self.description}
        if self.lists:
            document["lists"] = self.lists
        document["rules"] = [rule.to_document() for rule in self.rules]
        return document


def parse_template(document: Any, store: payloads.Store | None = None, fallback_name: str = "template") -> Template:
    if not isinstance(document, dict) or not isinstance(document.get("rules"), list):
        raise RuleError("a template needs a `rules` list")
    lists = {str(name): list(values) for name, values in (document.get("lists") or {}).items()}
    rules = []
    for index, raw in enumerate(document["rules"], start=1):
        if not isinstance(raw, dict) or "when" not in raw:
            raise RuleError(f"rule {index} needs a `when`")
        name = str(raw.get("name") or f"rule {index}")
        action = str(raw.get("action") or ("replace" if "values" in raw else "forward"))
        values, list_name = raw.get("values", []), ""
        if isinstance(values, str):
            list_name = values
            if values in lists:
                values = lists[values]
            elif store is not None:
                try:
                    values = store.get(values).values
                except payloads.PayloadError as error:
                    raise RuleError(f"rule `{name}`: {error}") from error
            else:
                raise RuleError(f"rule `{name}`: no payload list named {values!r}")
        rules.append(Rule(name=name, when=str(raw["when"]), action=action, field=raw.get("field"), values=list(values),
                          mode=str(raw.get("mode") or "each"), count=raw.get("count"), list_name=list_name,
                          var=str(raw.get("as") or ""), delay_ms=int(raw.get("delay_ms") or 0), until=str(raw.get("until") or ""),
                          after=int(raw.get("after") or 0), expect=[str(text) for text in raw.get("expect") or []],
                          stale_negative_control=bool(raw.get("stale_negative_control", False))))
    return Template(str(document.get("name") or fallback_name), str(document.get("description") or ""), rules, lists)


def load_template(path: Path, store: payloads.Store | None = None) -> Template:
    text = path.read_text(encoding="utf-8")
    document = json.loads(text) if path.suffix.lower() == ".json" else yaml.safe_load(text)
    return parse_template(document, store, path.stem)


def save_template(template: Template, path: Path) -> None:
    document = template.to_document()
    text = json.dumps(document, indent=2) + "\n" if path.suffix.lower() == ".json" else yaml.safe_dump(document, sort_keys=False)
    path.write_text(text, encoding="utf-8")


def rule_for_item(item: dict[str, Any], path: render.Path, values: list[Any], list_name: str = "") -> Rule:
    """A replace rule aimed at this field of every later item like ``item``."""
    from .session import part_of

    terms = [f"kind={item['kind']}"]
    payload, names, _raw = item_view(item)
    if cardano.protocol_number(item.get("type", "")) is not None:
        terms.append(f"type={item['type']}")
        if isinstance(payload, list) and payload and isinstance(payload[0], int):
            terms.append(f"[0]={payload[0]}")
    else:
        if item.get("label"):
            terms.append(f"label={item['label']}")
        side = "from" if item["kind"] in ("internal_action", "proto_out", "wire_out") else "to"
        part, _peer = part_of(item.get(side, ""))
        if part and part != "connection":
            terms.append(f"{side}~{part}")
    label = path_text(path, names)
    return Rule(name=f"{label} ← {list_name or 'values'}", when=" and ".join(terms), action="replace", field=list(path),
                values=list(values), list_name=list_name)


# --- how the node reacted --------------------------------------------------------------------


def _error_path(value: Any, depth: int = 0) -> str:
    """The chain of names inside an error value: ``Assert › VrfProof › InvalidProof``."""
    if depth > 5:
        return ""
    if isinstance(value, str):
        return value if len(value) <= 40 else ""
    if isinstance(value, dict) and len(value) == 1:
        key, inner = next(iter(value.items()))
        if key.startswith("$"):
            return ""
        rest = _error_path(inner, depth + 1)
        return f"{key} › {rest}" if rest else key
    if isinstance(value, list) and value:
        return _error_path(value[0], depth + 1)
    return ""


def reaction(session: Session, seq: int, until: int | None = None, window: int = 600) -> list[str]:
    """What the node did about item ``seq``, as short phrases, most telling first.

    A node that reports causes says exactly which steps followed from the item, however they
    interleave with other traffic. Otherwise the steps that came next are taken as a guess.
    """
    seen: list[str] = []

    def note(phrase: str) -> None:
        if phrase and phrase not in seen:
            seen.append(phrase)

    if session.traced:
        items = session.consequences(seq)
    else:
        last = until if until is not None else seq + window
        with session._lock:
            items = [item for number, item in session.items.items() if seq < number <= last]
    for item in items:
        label, payload = item.get("label", ""), item.get("payload")
        if item["kind"] == "resign_error":
            message = payload.get("error") if isinstance(payload, dict) else None
            note("re-signing failed" + (f": {message}" if isinstance(message, str) else ""))
        elif item["kind"] == "internal_result" and isinstance(payload, dict) and "Err" in payload:
            note("error: " + (_error_path(payload["Err"]) or "returned an error"))
        elif "Rejected" in label:
            outcome = payload.get("outcome") if isinstance(payload, dict) else None
            if outcome == "DuplicateHeader":
                # Another peer delivered the same header first; the node keeps the one it has
                # and holds nothing against this peer.
                note("already had it (duplicate, no penalty)")
            else:
                note(f"rejected ({outcome})" if isinstance(outcome, str) else "rejected")
        elif label == "schedule" and isinstance(payload, dict) and payload.get("message") == "RecheckLedgerHeight":
            # Amaru parks a header that is too far ahead of its ledger or its clock, and looks
            # again on a timer. Nothing has been checked or decided about it yet.
            note("set aside by the node, undecided (waiting for its ledger or clock to catch up)")
        elif label == "PeerAdversarialEffect":
            note("marked the peer hostile")
        elif label == "CloseEffect":
            note("hung up")
    if session.traced and not seen and items:
        reached = []
        for item in items:
            part = part_of(item.get("to", ""))[0] if item["kind"] == "internal_input" else None
            if part and part != "connection" and part not in reached:
                reached.append(part)
        note(f"no error in {len(items)} steps" + (f", reached {', '.join(reached[:4])}" if reached else ""))
    return seen


class Engine:
    """Applies armed rules to held items and keeps a record of every attempt."""

    def __init__(self, client: FreezeClient, session: Session, on_change: Callable[[], None] | None = None) -> None:
        self.client, self.session = client, session
        self.rules: list[Rule] = []
        self.attempts: list[Attempt] = []
        self.on_change = on_change or (lambda: None)
        self.paused = False
        self.variables: dict[str, Any] = {}  # values kept by capture rules, used as `$name`

    # arming --------------------------------------------------------------------------------

    def arm(self, rule: Rule) -> None:
        """Start applying ``rule``: matching items are held from now on."""
        if rule not in self.rules:
            self.rules.append(rule)
        if not rule.armed and not rule.finished:
            rule.condition_id = self.client.add_condition(rule.predicate, "freeze", False)
            rule.armed = True
        self.on_change()

    def disarm(self, rule: Rule) -> None:
        if rule.armed and rule.condition_id is not None:
            try:
                self.client.remove_condition(rule.condition_id)
            except FreezeError:
                pass
        rule.armed, rule.condition_id = False, None
        self.on_change()

    def remove(self, rule: Rule) -> None:
        self.disarm(rule)
        if rule in self.rules:
            self.rules.remove(rule)
        self.on_change()

    def arm_template(self, template: Template) -> None:
        for rule in template.rules:
            self.arm(rule)

    def stop(self) -> None:
        for rule in list(self.rules):
            self.disarm(rule)

    @property
    def active(self) -> bool:
        return any(rule.armed for rule in self.rules)

    # acting --------------------------------------------------------------------------------

    def _matching(self, item: dict[str, Any]) -> Rule | None:
        for rule in self.rules:
            if rule.armed and not rule.finished and conditions.matches(rule.predicate, item):
                return rule
        return None

    def offer(self, item: dict[str, Any]) -> bool:
        """Act on a held item if an armed rule matches it. Returns whether it was handled."""
        if self.paused or not item.get("held"):
            return False
        rule = self._matching(item)
        if rule is None:
            return False
        if rule.passed < rule.after:
            # not yet: let it through, remembering it in case this rule replays an earlier message
            rule.passed += 1
            if item.get("raw") is not None:
                rule.remembered.append(item["raw"])
            try:
                self.client.forward(item["seq"])
            except FreezeError:
                pass
            self._release()
            return True
        attempt = Attempt(rule=rule.name, number=rule.cursor + 1, seq=item["seq"], action=rule.action, at=time.time(),
                          what=cardano.item_message_name(item) or item.get("label", ""))
        try:
            if rule.action == "drop":
                self.client.drop(item["seq"])
            elif rule.action == "forward":
                self.client.forward(item["seq"])
            elif rule.action == "capture":
                payload, names, raw = item_view(item)
                value = render.get_at(payload, resolve_field(payload, names, rule.field, loose_bytes=not raw))
                self.variables[rule.var] = value
                attempt.value = f"kept as ${rule.var}"
                attempt.before = render.build(value, loose_bytes=not raw).summary
                self.client.forward(item["seq"])
            elif rule.action == "delay":
                attempt.value = f"set aside for {rule.delay_ms} ms"
                self.client.defer(item["seq"], ms=rule.delay_ms)
            elif rule.action == "overtake":
                passing = rule.values[0] if rule.values and isinstance(rule.values[0], int) else 1
                attempt.value = f"set aside until {passing} other item(s) pass"
                # A long stop for safety: the item goes on by itself if nothing ever overtakes it.
                self.client.defer(item["seq"], ms=rule.delay_ms or 30_000, until=rule.until_predicate, count=passing)
            elif rule.action == "replay":
                if not rule.remembered:
                    raise RuleError("nothing earlier to send again; give the rule an `after` of 1 or more")
                earlier = bytes.fromhex(rule.remembered[0])
                attempt.value = "an earlier message"
                attempt.before, attempt.after = f"{len(item.get('raw', '')) // 2} bytes", f"{len(earlier)} bytes, sent before"
                self._replace_raw(rule, item, earlier)
            else:
                self._replace(rule, item, attempt)
        except (FreezeError, FieldError, payloads.PayloadError, cbor_tree.CborError, RuleError, ValueError, KeyError, IndexError) as error:
            attempt.error = str(error)
            try:
                self.client.forward(item["seq"])  # never leave the node stuck on an item we could not change
            except FreezeError:
                pass
        rule.cursor += 1
        self.attempts.append(attempt)
        if rule.finished:
            self.disarm(rule)
        self._release()
        self.on_change()
        return True

    def _replace(self, rule: Rule, item: dict[str, Any], attempt: Attempt) -> None:
        spec = rule.values[rule.cursor % len(rule.values)]
        if isinstance(spec, str) and spec.startswith("$") and not spec.startswith("$$"):
            if spec[1:] not in self.variables:
                raise RuleError(f"nothing has been kept as {spec} yet; a capture rule must act first")
            spec = self.variables[spec[1:]]
        if rule.field == WHOLE:
            if item.get("raw") is None:
                raise RuleError("this item is not bytes, so it has no whole message to replace")
            original = {"$bytes": item["raw"]}
            value = payloads.apply(spec, original)
            if not render.is_bytes(value):
                raise RuleError("a whole message can only be replaced by bytes")
            attempt.value = payloads.describe(spec)
            attempt.before = render.build(original).summary
            attempt.after = render.build(value).summary
            self._replace_raw(rule, item, bytes.fromhex(value["$bytes"]))
            return
        payload, names, raw = item_view(item)
        path = resolve_field(payload, names, rule.field, loose_bytes=not raw)
        original = render.get_at(payload, path)
        value = payloads.apply(spec, original)
        attempt.value = payloads.describe(spec)
        attempt.before = render.build(original, loose_bytes=not raw).summary
        attempt.after = render.build(value, loose_bytes=not raw).summary
        if raw and path == () and render.is_bytes(value):
            self._replace_raw(rule, item, bytes.fromhex(value["$bytes"]))
        elif raw:
            self._replace_raw(rule, item, cbor_tree.edit(bytes.fromhex(item["raw"]), path, value))
        else:
            self.client.replace(item["seq"], payload=render.set_at(payload, path, value))

    def _replace_raw(self, rule: Rule, item: dict[str, Any], data: bytes) -> None:
        impact = cardano.edit_impact(
            item.get("type", ""),
            bytes.fromhex(item["raw"]),
            data,
            resigning=self.client.resigning(),
            item_kind=item.get("kind"),
            stale_negative_control=rule.stale_negative_control,
        )
        if impact["state"] == "refused":
            raise RuleError(f"edit refused: {impact['reason']}")
        self.client.replace(
            item["seq"], raw=data, stale_negative_control=rule.stale_negative_control,
        )

    def _release(self) -> None:
        """Let the node run on. Anything an armed rule still wants stays held, to be acted on in its turn."""
        wanted = [rule.predicate for rule in self.rules if rule.armed and not rule.finished]
        try:
            self.client.thaw(keep={"op": "or", "args": wanted} if wanted else None)
        except FreezeError:
            pass

    def sweep(self) -> None:
        """Act on anything already held that a rule matches (items held before the rule was armed)."""
        with self.session._lock:
            held = sorted(self.session.held.values(), key=lambda item: item["seq"])
        for item in held:
            self.offer(item)

    # results -------------------------------------------------------------------------------

    def reaction_of(self, attempt: Attempt) -> str:
        later = [other.seq for other in self.attempts if other.seq > attempt.seq]
        if attempt.error:
            return f"not applied: {attempt.error}"
        phrases = reaction(self.session, attempt.seq, min(later) if later else None)
        if attempt.action in ("delay", "overtake"):
            # How the wait ended comes first: it is what the rule was about.
            with self.session._lock:
                release = self.session.releases.get(attempt.seq)
            if release is None:
                phrases.insert(0, "still set aside")
            elif release is not None and release.get("overtaken_by"):
                phrases.insert(0, f"went on after item {release['overtaken_by']} passed it")
            elif release is not None:
                phrases.insert(0, "went on when its time was up")
        if not self.client.connected:
            phrases.insert(0, "THE NODE STOPPED ANSWERING")
        if attempt.action == "drop" and not phrases:
            return "dropped; no reaction seen"
        if phrases:
            return "; ".join(phrases)
        return "nothing followed from it yet" if self.session.traced else "no reaction seen (accepted?)"

    def as_expected(self, attempt: Attempt) -> bool | None:
        """Whether the node reacted as the rule expects; None if the rule states no expectation."""
        expect = next((rule.expect for rule in self.rules if rule.name == attempt.rule), [])
        if not expect:
            return None
        said = self.reaction_of(attempt).lower()
        return any(phrase.lower() in said for phrase in expect)

    def report(self) -> list[dict[str, Any]]:
        return [{"rule": a.rule, "number": a.number, "seq": a.seq, "action": a.action, "what": a.what, "value": a.value,
                 "before": a.before, "after": a.after, "node_said": self.reaction_of(a), "as_expected": self.as_expected(a)}
                for a in self.attempts]

    def run(self, timeout: float = 600.0, settle: float = 3.0, log: Callable[[str], None] | None = None) -> list[dict[str, Any]]:
        """Without a UI: act on items until every armed rule has finished or ``timeout`` passes."""
        deadline = time.time() + timeout
        self.sweep()
        while self.active and time.time() < deadline and self.client.connected:
            event = self.client.next_event(timeout=1.0)
            if event is None:
                continue
            self.session.record(event["event"], event["data"])
            before = len(self.attempts)
            if event["event"] == "item" and event["data"].get("held") and self.offer(event["data"]) and log and len(self.attempts) > before:
                attempt = self.attempts[-1]
                log(f"{attempt.rule} #{attempt.number}: item {attempt.seq} {attempt.action} {attempt.before} → {attempt.after}".rstrip(" →"))
        # Items that were set aside are still inside the node: wait for them to go on, so the
        # run can say how each wait ended and what the node then did.
        def waiting() -> bool:
            # By the attempts, not by `session.deferred`: the node's word that it set an item
            # aside may not have been read yet.
            return any(a.action in ("delay", "overtake") and not a.error and a.seq not in self.session.releases
                       for a in self.attempts)

        while waiting() and time.time() < deadline and self.client.connected:
            event = self.client.next_event(timeout=0.5)
            if event is not None:
                self.session.record(event["event"], event["data"])
        quiet = time.time() + settle  # let the reaction to the last attempt arrive
        while time.time() < quiet and self.client.connected:
            event = self.client.next_event(timeout=0.5)
            if event is not None:
                self.session.record(event["event"], event["data"])
        return self.report()


def write_report(rows: list[dict[str, Any]], path: Path) -> None:
    path.write_text(json.dumps(rows, indent=2) + "\n", encoding="utf-8")
