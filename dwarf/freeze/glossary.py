"""Plain names, colour classes and explanations for what the Freeze UI shows."""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import yaml

CLASSES = ("field", "kind", "part", "action")
_SUFFIXES = ("effect", "msg", "message")


@dataclass(frozen=True)
class Entry:
    label: str
    cls: str
    what: str = ""
    why: str = ""
    known: bool = True

    @property
    def explanation(self) -> str:
        return " ".join(part for part in (self.what, self.why) if part)


def normalise(name: str) -> str:
    """Reduce a field, type, stage or effect name to its glossary key."""
    base = name.split("<", 1)[0].rsplit("::", 1)[-1].strip()
    base = re.sub(r"(-\d+)+$", "", base)
    base = re.sub(r"^\d+-", "", base)
    key = base.lower()
    for suffix in _SUFFIXES:
        if key.endswith(suffix) and len(key) > len(suffix):
            key = key[: -len(suffix)]
            break
    return key.rstrip("_")


def humanise(name: str) -> str:
    """A readable form of a name that has no glossary entry."""
    base = name.split("<", 1)[0].rsplit("::", 1)[-1]
    base = re.sub(r"(-\d+)+$", "", base)
    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", base).replace("_", " ")
    return spaced.strip() or name


class Glossary:
    def __init__(self, entries: dict[str, Entry]) -> None:
        self._entries = entries
        self._squashed = {re.sub(r"[_\- ]", "", key): entry for key, entry in entries.items()}

    @classmethod
    def load(cls, *paths: Path) -> "Glossary":
        entries: dict[str, Entry] = {}
        here = Path(__file__).parent
        for path in paths or (here / "glossary.yaml", here / "glossary-cardano.yaml"):
            for key, raw in (yaml.safe_load(path.read_text(encoding="utf-8")) or {}).items():
                if raw.get("cls") not in CLASSES:
                    raise ValueError(f"{path}: `{key}` has unknown cls {raw.get('cls')!r}")
                if not isinstance(raw.get("label"), str) or not raw.get("what"):
                    raise ValueError(f"{path}: `{key}` needs a label and a `what`")
                entries[str(key).lower()] = Entry(
                    label=raw["label"], cls=raw["cls"], what=raw.get("what") or "", why=raw.get("why") or ""
                )
        return cls(entries)

    def lookup(self, name: str, default_cls: str = "field") -> Entry:
        """The entry for ``name``; unknown names get a readable label and no explanation."""
        for key in (name.lower(), normalise(name)):
            entry = self._entries.get(key)
            if entry is not None:
                return entry
        entry = self._squashed.get(re.sub(r"[_\- ]", "", normalise(name)))
        if entry is not None:
            return entry
        label = humanise(name)
        return Entry(label=label.lower() if default_cls == "field" else label, cls=default_cls, known=False)

    def party(self, party: str) -> Entry:
        """The entry for an item's ``from`` or ``to``: ``stage:<name>``, ``peer:<addr>``, ``node``."""
        if party.startswith("stage:"):
            name = party[len("stage:") :]
            entry = self.lookup(name, "part")
            peer = re.match(r"^\d+-(\d+\.\d+\.\d+\.\d+:\d+)-\d+$", name)
            if peer:
                return Entry(f"connection {peer.group(1)}", "part", "Everything about one peer's connection.", "")
            return entry
        if party.startswith("peer:"):
            return Entry(party[len("peer:") :], "part", "Another node on the network.", "")
        if party == "node":
            return Entry("node", "part", "The node's own services: stores, ledger, network.", "")
        return Entry(party or "-", "part", "", "", known=False)


@lru_cache(maxsize=1)
def default() -> Glossary:
    return Glossary.load()
