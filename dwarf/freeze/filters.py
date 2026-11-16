"""Named filters: a condition saved once and used by name, in `search` and in the UI's History.

They live in one file, ``filters.json``, under the Freeze data folder, so every session and
every node shares them. A saved filter is used as ``@name`` wherever a condition is typed.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from . import conditions

_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")


class FilterError(ValueError):
    pass


class Filters:
    def __init__(self, directory: Path) -> None:
        self.path = directory / "filters.json"

    def all(self) -> dict[str, dict[str, Any]]:
        try:
            saved = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, json.JSONDecodeError) as error:
            raise FilterError(f"{self.path} cannot be read: {error}") from error
        return saved if isinstance(saved, dict) else {}

    def _write(self, saved: dict[str, dict[str, Any]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(saved, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        temporary.replace(self.path)

    def save(self, name: str, query: str, about: str = "") -> None:
        """Keep ``query`` under ``name``. The query must be a condition, so it means the same every time."""
        if not _NAME.match(name):
            raise FilterError(f"`{name}` is not a usable name: letters, digits, `_`, `.`, `-`, at most 64")
        query = query.strip()
        if query.startswith("@"):
            raise FilterError("a saved filter cannot refer to another saved filter")
        try:
            conditions.compile_condition(query)
        except conditions.ConditionError as error:
            raise FilterError(f"not a condition: {error}") from error
        saved = self.all()
        saved[name] = {"query": query, "about": about}
        self._write(saved)

    def delete(self, name: str) -> bool:
        saved = self.all()
        if name not in saved:
            return False
        del saved[name]
        self._write(saved)
        return True

    def resolve(self, text: str) -> str:
        """``@name`` becomes the saved condition; anything else is returned as it is."""
        text = text.strip()
        if not text.startswith("@"):
            return text
        name = text[1:].strip()
        saved = self.all()
        if name not in saved:
            known = ", ".join(sorted(saved)) or "none saved yet"
            raise FilterError(f"no saved filter `{name}` (have: {known})")
        return str(saved[name]["query"])

    def typed(self, text: str) -> tuple[str, str]:
        """What a line typed into a filter box asks for, as (what happened, condition to apply).

        ``@name``            use a saved filter
        ``@name = condition`` save the condition under that name, and use it
        ``@-name``           forget a saved filter
        ``@``                list the saved filters
        anything else        a condition, used as typed
        """
        text = text.strip()
        if not text.startswith("@"):
            return "", text
        body = text[1:].strip()
        if not body:
            saved = self.all()
            return ("saved filters: " + ", ".join(f"@{name}" for name in sorted(saved))) if saved else "no saved filters yet", ""
        if body.startswith("-"):
            name = body[1:].strip()
            return (f"forgot @{name}" if self.delete(name) else f"no saved filter `{name}`"), ""
        if "=" in body and _NAME.match(body.split("=", 1)[0].strip()):
            name, query = (part.strip() for part in body.split("=", 1))
            # `@slow = kind=proto_in` saves; `@slow` alone (no `=` after a plain name) is a lookup.
            self.save(name, query)
            return f"saved @{name}", query
        return "", self.resolve(text)
