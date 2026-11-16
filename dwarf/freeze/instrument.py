"""Changes made to a node's source tree before a Freeze build.

``trace`` puts ``#[amaru_freeze::trace_fn]`` in front of every function with a body in the
chosen crates, so that a debugger can switch on reports of their calls (``trace.enable``).
It works on the text of the sources, without compiling them:

- only files under a crate's ``src/`` are touched, and not the ones that are a
  ``#[cfg(test)] mod``;
- a ``const fn``, a function without a body, a test and anything inside a ``#[cfg(test)]``
  block is left alone;
- a function inside ``impl Type``, ``impl Trait for Type`` or ``trait Trait`` is given
  ``in = "Type"``, which becomes part of its path;
- the crate gains a dependency on ``amaru-freeze`` if it has none. A crate that is also built
  for targets the agent is not built for keeps the dependency under its ``[target.'cfg(..)']``
  table, and its attributes are wrapped in ``cfg_attr`` with the same condition.

Functions inside macros are marked only in files known to still compile with them (see
``add_tracing``): either a ``check`` function compiles the tree after each crate and the files
it blames go back to the rule above, or the files come from the ``trace-macros.json`` such a
run left behind. Two places are looked at:

- the bodies of ``macro_rules!`` definitions, where the attribute becomes part of what the
  macro expands to. Its path is resolved where the macro is used, so a ``#[macro_export]``
  macro is marked only when every crate of the tree using it is one of the chosen crates;
- the arguments of a macro used where an item may stand, when a ``macro_rules!`` of that
  name is defined somewhere in the tree. Any other macro (``tokio::select!``, ``quote!``,
  ``proptest!`` ...) is left alone.

Inside a macro body the owner (``in = "Type"``) is given only when the ``impl`` is written out
in the same body without metavariables, and a function is skipped whenever a metavariable
could bring in something unknown (attributes, qualifiers, a body).

Running it twice changes nothing the second time.

    python -m dwarf.freeze.instrument trace --source TREE --crate amaru-consensus --crate amaru-ledger
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path

ATTRIBUTE = "amaru_freeze::trace_fn"
DEPENDENCY = "amaru-freeze"

# A crate with a dependency table for this condition is also built where the agent is not.
PORTABLE_TABLE = re.compile(
    r"""^\[target\.(['"])cfg\((?P<condition>all\(not\(target_family="wasm"\),not\(target_arch="riscv32"\)\))\)\1"""
    r"""\.dependencies\]$"""
)
TARGET_TABLE = re.compile(r"""^\[target\.(['"])cfg\((?P<condition>.*)\)\1\.dependencies\]$""")

QUALIFIERS = {"pub", "const", "async", "unsafe", "extern", "default", "safe"}
NOT_A_MACRO = {"if", "while", "match", "return", "in", "else", "break", "yield", "mut", "move", "as", "let"}
TEST_ATTRIBUTE = re.compile(r"^(\w+::)*(test|test_case|rstest|bench|proptest)\b")
# Built-in attributes that mark the function itself rather than rewrite its tokens: the mark is
# lost if another attribute rewrites the function after them, so ours goes in front of them.
MUST_COME_LAST = re.compile(r"^(define_opaque)\b")
OPENING = {"(": ")", "[": "]", "{": "}"}
CLOSING = {")", "]", "}"}

MACRO_FILE = "trace-macros.json"
# What a metavariable may stand for in the header of a function for the function to be marked.
HEADER_FRAGMENTS = {"ty", "path", "ident", "lifetime", "vis"}
NOT_A_CRATE = {"crate", "self", "super"}
SKIPPED_DIRECTORIES = {"target", "node_modules"}


class InstrumentError(Exception):
    """The tree cannot be instrumented as asked."""


# --------------------------------------------------------------------------------------------
# Rust source as tokens


@dataclass(frozen=True)
class Token:
    kind: str  # "ident", "lifetime", "literal" or "punct"
    text: str
    start: int


_RAW_STRING = re.compile(r'(?:br|cr|r)(#*)"')
_IDENT = re.compile(r"(?:r#)?[A-Za-z_][A-Za-z0-9_]*")
_NUMBER = re.compile(r"[0-9][0-9A-Za-z_]*")
_LIFETIME = re.compile(r"'[A-Za-z_][A-Za-z0-9_]*")


def tokenize(source: str) -> list[Token]:
    """The tokens of a Rust file, without its comments and white space."""
    tokens: list[Token] = []
    position, length = 0, len(source)
    while position < length:
        character = source[position]
        if character.isspace():
            position += 1
        elif source.startswith("//", position):
            end = source.find("\n", position)
            position = length if end < 0 else end
        elif source.startswith("/*", position):
            position = _after_block_comment(source, position)
        elif raw := _RAW_STRING.match(source, position):
            closing = '"' + raw.group(1)
            end = source.find(closing, raw.end())
            end = length if end < 0 else end + len(closing)
            tokens.append(Token("literal", source[position:end], position))
            position = end
        elif character == '"':
            end = _after_quoted(source, position, '"')
            tokens.append(Token("literal", source[position:end], position))
            position = end
        elif character == "'":
            if _is_char_literal(source, position):
                end = _after_quoted(source, position, "'")
                tokens.append(Token("literal", source[position:end], position))
            else:
                end = (_LIFETIME.match(source, position) or re.compile("'").match(source, position)).end()
                tokens.append(Token("lifetime", source[position:end], position))
            position = end
        elif ident := _IDENT.match(source, position):
            tokens.append(Token("ident", ident.group(), position))
            position = ident.end()
        elif number := _NUMBER.match(source, position):
            tokens.append(Token("literal", number.group(), position))
            position = number.end()
        else:
            width = 2 if source[position : position + 2] in ("->", "=>", "::") else 1
            tokens.append(Token("punct", source[position : position + width], position))
            position += width
    return tokens


def _after_block_comment(source: str, position: int) -> int:
    depth, position = 1, position + 2
    while depth and position < len(source):
        if source.startswith("/*", position):
            depth, position = depth + 1, position + 2
        elif source.startswith("*/", position):
            depth, position = depth - 1, position + 2
        else:
            position += 1
    return position


def _after_quoted(source: str, position: int, quote: str) -> int:
    position += 1
    while position < len(source) and source[position] != quote:
        position += 2 if source[position] == "\\" else 1
    return min(position + 1, len(source))


def _is_char_literal(source: str, position: int) -> bool:
    following = source[position + 1 : position + 3]
    return following.startswith("\\") or (len(following) == 2 and following[1] == "'")


def _matching(tokens: list[Token], index: int) -> int:
    """The index of the delimiter closing the one at ``index``, or the last token."""
    depth = 0
    for position in range(index, len(tokens)):
        text = tokens[position].text
        if text in OPENING:
            depth += 1
        elif text in CLOSING:
            depth -= 1
            if depth == 0:
                return position
    return len(tokens) - 1


def _body_or_end(tokens: list[Token], index: int) -> int | None:
    """From inside an item's header: the index of the ``{`` opening its body, or ``None`` when a
    ``;`` ends the item first."""
    position = index
    while position < len(tokens):
        text = tokens[position].text
        if text in ("(", "["):
            position = _matching(tokens, position)
        elif text == "{":
            return position
        elif text in (";", "}"):
            return None
        position += 1
    return None


# --------------------------------------------------------------------------------------------
# Finding the functions


@dataclass
class _Scope:
    owner: str | None = None
    skipped: bool = False
    items: bool = False  # a place where items stand: a file, a module, an ``impl``, a ``trait``


@dataclass
class _Insertion:
    offset: int
    owner: str | None
    macro: bool = False


@dataclass
class _Scan:
    insertions: list[_Insertion] = field(default_factory=list)
    test_modules: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class _Attribute:
    text: str  # what is between the brackets, without white space
    start: int


@dataclass(frozen=True)
class Macros:
    """What a file may assume about macros.

    ``known`` are the names with a ``macro_rules!`` definition somewhere in the tree, ``crates``
    the crates of the tree as they are written in a path, and ``bodies`` the macros defined in
    this file's crate whose bodies may carry the attribute."""

    known: frozenset[str] = frozenset()
    crates: frozenset[str] = frozenset()
    bodies: frozenset[str] = frozenset()


@dataclass(frozen=True)
class _Context:
    macros: Macros | None = None
    inside: str | None = None  # "body" of a macro_rules!, "arguments" of an invocation
    fragments: dict[str, str] = field(default_factory=dict)  # metavariable -> fragment kind


def _is_test_only(attributes: list[_Attribute]) -> bool:
    for attribute in (attribute.text for attribute in attributes):
        if attribute == "cfg(test)":
            return True
        if attribute.startswith("cfg(all(") and "any(" not in attribute and re.search(r"[(,]test[,)]", attribute):
            return True
    return False


def _impl_owner(header: list[Token]) -> str | None:
    """The name of the type an ``impl`` is for, from the tokens between ``impl`` and ``{``."""
    if any(token.text == "$" for token in header):
        return None
    position = 0
    if header and header[0].text == "<":
        depth = 0
        for position, token in enumerate(header):
            depth += {"<": 1, ">": -1}.get(token.text, 0)
            if depth == 0:
                break
        position += 1
    depth, start, end = 0, position, len(header)
    for index in range(position, len(header)):
        text = header[index].text
        if text in ("<", "(", "["):
            depth += 1
        elif text in (">", ")", "]"):
            depth -= 1
        elif depth == 0 and text == "where":
            end = index
            break
        elif depth == 0 and text == "for":
            start = index + 1
    name = None
    for token in header[start:end]:
        if token.kind == "lifetime" or token.text in ("&", "*", "mut", "dyn", "const", "(", "[", "::", "!"):
            if name is not None and token.text != "::":
                break
            continue
        if token.kind != "ident":
            break
        name = token.text
    return name


def scan(source: str, macros: Macros | None = None) -> _Scan:
    """Where the attribute goes in one file, and which ``#[cfg(test)] mod name;`` it declares.

    Functions inside macros are looked for only when ``macros`` is given; their insertions
    have ``macro`` set."""
    tokens = tokenize(source)
    found = _Scan()
    _walk(tokens, 0, len(tokens), _Scope(items=True), _Context(macros), found)
    return found


def _walk(tokens: list[Token], begin: int, end: int, top: _Scope, context: _Context, found: _Scan) -> None:
    """Scan the tokens from ``begin`` up to ``end``, which hold whole items or statements."""
    scopes = [top]
    opening: dict[int, _Scope] = {}
    attributes: list[_Attribute] = []
    index = begin
    while index < end:
        token = tokens[index]
        following = tokens[index + 1] if index + 1 < end else None
        after = tokens[index + 2] if index + 2 < end else None
        scope = scopes[-1]

        if token.text == "#" and following and following.text in ("[", "!"):
            inner = following.text == "!"
            start = index + (2 if inner else 1)
            if start < end and tokens[start].text == "[":
                close = _matching(tokens, start)
                if not inner:
                    text = "".join(part.text for part in tokens[start + 1 : close])
                    attributes.append(_Attribute(text, token.start))
                index = close + 1
                continue

        elif token.kind == "ident" and following and following.text == "!" and token.text not in NOT_A_MACRO:
            start = index + 2
            name = None
            if start < end and tokens[start].kind == "ident":
                name = tokens[start].text
                start += 1
            if start < end and tokens[start].text in OPENING:
                close = _matching(tokens, start)
                if context.macros is not None and not scope.skipped and not _is_test_only(attributes):
                    if token.text == "macro_rules" and name is not None:
                        if context.inside is None and name in context.macros.bodies:
                            _walk_rules(tokens, start, close, scope, context, found)
                    elif name is None and _is_item_macro(tokens, begin, index, scope, context):
                        inner_scope = _Scope(scope.owner, items=True)
                        inner_context = _Context(context.macros, "arguments")
                        _walk(tokens, start + 1, close, inner_scope, inner_context, found)
                index = close + 1
                attributes = []
                continue

        elif token.text == "mod" and following and after and (following.kind == "ident" or following.text == "$"):
            brace = index + (2 if following.kind == "ident" else 3)
            if following.kind == "ident" and after.text == ";":
                if _is_test_only(attributes):
                    found.test_modules.append(following.text)
            elif brace < end and tokens[brace].text == "{" and (following.kind == "ident" or after.kind == "ident"):
                opening[brace] = _Scope(scope.owner, scope.skipped or _is_test_only(attributes), items=True)

        elif token.text == "impl" and (body := _body_or_end(tokens, index + 1)) is not None and body < end:
            owner = _impl_owner(tokens[index + 1 : body])
            opening[body] = _Scope(owner, scope.skipped or _is_test_only(attributes), items=True)
            index = body
            continue

        elif token.text == "trait" and following and following.kind == "ident":
            if (body := _body_or_end(tokens, index + 2)) is not None and body < end:
                opening[body] = _Scope(following.text, scope.skipped or _is_test_only(attributes), items=True)
                index = body
                continue

        elif token.text == "fn" and following and (
            following.kind == "ident"
            or (context.inside == "body" and following.text == "$" and after is not None and after.kind == "ident")
        ):
            named = following.kind == "ident"
            header = index + (2 if named else 3)
            body = _body_or_end(tokens, header)
            if body is None or body >= end or (context.inside and not _is_plain_header(tokens, header, body, context)):
                attributes = []
                index = header
                continue
            first = _first_qualifier(tokens, index)
            qualifiers = {part.text for part in tokens[first:index]}
            wanted = not (
                scope.skipped
                or "const" in qualifiers
                or _is_test_only(attributes)
                or any(ATTRIBUTE in attribute.text or TEST_ATTRIBUTE.match(attribute.text) for attribute in attributes)
            )
            if wanted and context.inside:
                start = _start_in_macro(tokens, begin, first, attributes, context)
                wanted = start is not None
                first = first if start is None else start
            if wanted:
                last = [attribute.start for attribute in attributes if MUST_COME_LAST.match(attribute.text)]
                offset = min(last, default=tokens[first].start)
                found.insertions.append(_Insertion(offset, scope.owner, macro=context.inside is not None))
            if named:
                name = following.text.removeprefix("r#")
                opening[body] = _Scope(f"{scope.owner}::{name}" if scope.owner else name, scope.skipped)
            else:
                opening[body] = _Scope(None, scope.skipped)
            index = body
            continue

        if token.text == "{":
            scopes.append(opening.pop(index, None) or _Scope(scope.owner, scope.skipped))
            attributes = []
        elif token.text == "}":
            if len(scopes) > 1:
                scopes.pop()
            attributes = []
        elif token.text == ";":
            attributes = []
        index += 1


def _walk_rules(tokens: list[Token], start: int, close: int, scope: _Scope, context: _Context, found: _Scan) -> None:
    """Scan what each rule of a ``macro_rules!`` expands to; ``start`` and ``close`` are the
    delimiters around its rules."""
    position = start + 1
    while position < close:
        if tokens[position].text not in OPENING:
            return
        matcher_end = _matching(tokens, position)
        body = matcher_end + 2
        if body >= close or tokens[matcher_end + 1].text != "=>" or tokens[body].text not in OPENING:
            return
        body_end = _matching(tokens, body)
        fragments = {}
        for at in range(position, matcher_end - 2):
            if tokens[at].text == "$" and tokens[at + 1].kind == "ident" and tokens[at + 2].text == ":":
                fragments[tokens[at + 1].text] = tokens[at + 3].text
        inner = _Context(context.macros, "body", fragments)
        # Where the macro is used is not known here, so neither is the owner of its functions.
        _walk(tokens, body + 1, body_end, _Scope(None, items=True), inner, found)
        position = body_end + 1
        if position < close and tokens[position].text == ";":
            position += 1


def _is_item_macro(tokens: list[Token], begin: int, index: int, scope: _Scope, context: _Context) -> bool:
    """Whether the macro named at ``index`` is one of the tree's own, used where an item stands."""
    if context.inside == "body" or not scope.items or tokens[index].text not in context.macros.known:
        return False
    first = index
    while first - 2 >= begin and tokens[first - 1].text == "::" and tokens[first - 2].kind == "ident":
        first -= 2
    if first > begin and tokens[first - 1].text in ("::", "$"):
        return False
    if first < index and tokens[first].text not in NOT_A_CRATE and tokens[first].text not in context.macros.crates:
        return False
    return first == begin or tokens[first - 1].text in (";", "}", "{", "]")


def _is_plain_header(tokens: list[Token], header: int, body: int, context: _Context) -> bool:
    """Whether the ``{`` at ``body`` is sure to open the body of the function inside a macro
    whose header starts at ``header``: no metavariable in between may stand for a body."""
    position = header
    while position < body:
        text = tokens[position].text
        if text in ("(", "["):
            position = _matching(tokens, position)
        elif text == "fn":
            return False
        elif text == "$":
            name = tokens[position + 1].text
            if name != "crate" and context.fragments.get(name) not in HEADER_FRAGMENTS:
                return False
        position += 1
    return True


def _start_in_macro(
    tokens: list[Token], begin: int, first: int, attributes: list[_Attribute], context: _Context
) -> int | None:
    """The index of the token a function inside a macro starts at, or ``None`` when a
    metavariable in front of it makes it uncertain what the function is."""
    if any("$" in attribute.text for attribute in attributes):
        return None
    while first - 2 >= begin:
        previous, before = tokens[first - 1], tokens[first - 2]
        if previous.kind == "ident" and before.text == "$":
            if context.fragments.get(previous.text) != "vis":
                return None
            first -= 2
        elif previous.text in ("*", "+", "?") and before.text == ")":
            return None
        else:
            break
    return first


def _first_qualifier(tokens: list[Token], fn_index: int) -> int:
    """The index of the first of the words in front of ``fn``: ``pub(crate) async unsafe extern "C"``."""
    first = fn_index
    while first > 0:
        previous = tokens[first - 1]
        if previous.kind == "ident" and previous.text in QUALIFIERS:
            first -= 1
        elif previous.kind == "literal" and first > 1 and tokens[first - 2].text == "extern":
            first -= 1
        elif previous.text == ")":
            depth, position = 0, first - 1
            while position >= 0:
                depth += {")": 1, "(": -1}.get(tokens[position].text, 0)
                if depth == 0:
                    break
                position -= 1
            if position < 1 or tokens[position - 1].text != "pub":
                break
            first = position - 1
        else:
            break
    return first


def instrument_source(source: str, condition: str | None = None) -> tuple[str, int]:
    """``source`` with the attribute on every function outside macros that should carry it, and
    how many were added. ``condition`` is the ``cfg`` expression the attribute must be limited
    to, if any."""
    return _insert(source, scan(source).insertions, condition)


def instrument_macros(source: str, macros: Macros, condition: str | None = None) -> tuple[str, int]:
    """``source`` with the attribute on the functions inside macros that may carry it, and how
    many were added. The functions outside macros are left as they are."""
    return _insert(source, [insertion for insertion in scan(source, macros).insertions if insertion.macro], condition)


def _insert(source: str, insertions: list[_Insertion], condition: str | None) -> tuple[str, int]:
    parts: list[str] = []
    position = 0
    insertions = sorted(insertions, key=lambda insertion: insertion.offset)
    for insertion in insertions:
        arguments = f'(in = "{insertion.owner}")' if insertion.owner else ""
        attribute = f"{ATTRIBUTE}{arguments}"
        attribute = f"#[cfg_attr({condition}, {attribute})]" if condition else f"#[{attribute}]"
        line_start = source.rfind("\n", 0, insertion.offset) + 1
        indent = source[line_start : insertion.offset]
        separator = f"\n{indent}" if not indent.strip() else " "
        parts.append(source[position : insertion.offset])
        parts.append(attribute + separator)
        position = insertion.offset
    parts.append(source[position:])
    return "".join(parts), len(insertions)


# --------------------------------------------------------------------------------------------
# Crates


def crate_dir(tree: Path, crate: str) -> Path:
    direct = tree / "crates" / crate
    if (direct / "Cargo.toml").is_file():
        return direct
    for manifest in sorted(tree.glob("crates/**/Cargo.toml")):
        if re.search(rf'^name\s*=\s*"{re.escape(crate)}"\s*$', manifest.read_text(encoding="utf-8"), re.M):
            return manifest.parent
    raise InstrumentError(f"no crate `{crate}` under {tree / 'crates'}")


def _dependency_line(tree: Path, directory: Path) -> str:
    root = tree / "Cargo.toml"
    if root.is_file() and re.search(rf"^{DEPENDENCY}\s*=", root.read_text(encoding="utf-8"), re.M):
        return f"{DEPENDENCY}.workspace = true"
    target = crate_dir(tree, DEPENDENCY)
    levels = len(directory.relative_to(tree).parts)
    return f'{DEPENDENCY} = {{ path = "{"../" * levels}{target.relative_to(tree).as_posix()}" }}'


def ensure_dependency(tree: Path, directory: Path, write: bool = True) -> str | None:
    """Make the crate in ``directory`` depend on the agent crate. Returns the ``cfg`` condition
    its attributes must carry, or ``None`` when the dependency holds for every target."""
    manifest = directory / "Cargo.toml"
    lines = manifest.read_text(encoding="utf-8").split("\n")
    table, portable, plain = "", None, None
    for number, line in enumerate(lines):
        compact = "".join(line.split())
        if compact.startswith("["):
            table = compact
            if PORTABLE_TABLE.match(compact):
                portable = number
            elif compact == "[dependencies]":
                plain = number
        elif re.match(rf"{DEPENDENCY}\b\s*[.=]", line) and table.endswith("dependencies]"):
            if table in ("[dev-dependencies]", "[build-dependencies]"):
                continue
            gated = TARGET_TABLE.match(table)
            return _spaced(gated.group("condition")) if gated else None

    dependency = _dependency_line(tree, directory)
    if portable is not None:
        lines.insert(portable + 1, dependency)
        condition = _spaced(PORTABLE_TABLE.match("".join(lines[portable].split())).group("condition"))
    elif plain is not None:
        lines.insert(plain + 1, dependency)
        condition = None
    else:
        lines += ["", "[dependencies]", dependency, ""]
        condition = None
    if write:
        manifest.write_text("\n".join(lines), encoding="utf-8")
    return condition


def _spaced(condition: str) -> str:
    return condition.replace("=", " = ").replace(",", ", ")


def _test_module_files(source_dir: Path) -> set[Path]:
    """The files that make up the ``#[cfg(test)] mod name;`` modules declared under ``source_dir``."""
    excluded: set[Path] = set()
    for path in sorted(source_dir.rglob("*.rs")):
        home = path.parent if path.name in ("mod.rs", "lib.rs", "main.rs") else path.parent / path.stem
        for name in scan(path.read_text(encoding="utf-8")).test_modules:
            excluded.add(home / f"{name}.rs")
            excluded.add(home / name)
    return excluded


def _source_files(directory: Path, crate: str) -> list[Path]:
    """The files of a crate that may be instrumented."""
    source_dir = directory / "src"
    if not source_dir.is_dir():
        raise InstrumentError(f"crate `{crate}` has no src/ directory")
    excluded = _test_module_files(source_dir)
    return [
        path
        for path in sorted(source_dir.rglob("*.rs"))
        if path not in excluded and not any(parent in excluded for parent in path.parents)
    ]


def trace_crate(tree: Path, crate: str, write: bool = True) -> int:
    """Instrument the functions of one crate that are outside macros. Returns how many
    functions gained the attribute."""
    directory = crate_dir(tree, crate)
    files = _source_files(directory, crate)
    condition = ensure_dependency(tree, directory, write)
    added = 0
    for path in files:
        source = path.read_text(encoding="utf-8")
        instrumented, count = instrument_source(source, condition)
        if count and write:
            path.write_text(instrumented, encoding="utf-8")
        added += count
    return added


# --------------------------------------------------------------------------------------------
# Macros of the tree


@dataclass(frozen=True)
class MacroDefinition:
    name: str
    file: str  # relative to the tree
    crate: str | None  # the directory of its crate relative to the tree
    exported: bool


def _rust_files(tree: Path) -> list[Path]:
    files = []
    for path in sorted(tree.rglob("*.rs")):
        parts = path.relative_to(tree).parts[:-1]
        if not any(part in SKIPPED_DIRECTORIES or part.startswith(".") for part in parts):
            files.append(path)
    return files


def _crate_of(tree: Path, path: Path) -> str | None:
    for parent in path.parents:
        if (parent / "Cargo.toml").is_file():
            return parent.relative_to(tree).as_posix()
        if parent == tree:
            break
    return None


def workspace_macros(tree: Path) -> dict[str, list[MacroDefinition]]:
    """Every ``macro_rules!`` definition in the Rust files of ``tree``, by name."""
    tree = Path(tree)
    found: dict[str, list[MacroDefinition]] = {}
    for path in _rust_files(tree):
        source = path.read_text(encoding="utf-8", errors="replace")
        if "macro_rules" not in source:
            continue
        tokens = tokenize(source)
        for index in range(len(tokens) - 2):
            opens = tokens[index].text == "macro_rules" and tokens[index + 1].text == "!"
            if not opens or tokens[index + 2].kind != "ident":
                continue
            exported, at = False, index
            while at >= 1 and tokens[at - 1].text == "]":  # the attributes in front of it
                depth = 0
                while at >= 1:
                    at -= 1
                    depth += {"]": 1, "[": -1}.get(tokens[at].text, 0)
                    if depth == 0:
                        break
                exported = exported or any(part.text == "macro_export" for part in tokens[at:index])
                if at >= 1 and tokens[at - 1].text == "#":
                    at -= 1
                else:
                    break
            name = tokens[index + 2].text
            definition = MacroDefinition(name, path.relative_to(tree).as_posix(), _crate_of(tree, path), exported)
            found.setdefault(name, []).append(definition)
    return found


def _crate_names(tree: Path) -> frozenset[str]:
    """The crates of the tree, as their names are written in a path."""
    names = set()
    for manifest in sorted(tree.rglob("Cargo.toml")):
        parts = manifest.relative_to(tree).parts[:-1]
        if any(part in SKIPPED_DIRECTORIES or part.startswith(".") for part in parts):
            continue
        package = re.search(r'^\[package\]\s*$(.*?)(?=^\[|\Z)', manifest.read_text(encoding="utf-8"), re.M | re.S)
        name = package and re.search(r'^name\s*=\s*"([^"]+)"', package.group(1), re.M)
        if name:
            names.add(name.group(1).replace("-", "_"))
    return frozenset(names)


class _Tree:
    """What macro marking needs to know of a tree and the crates being traced."""

    def __init__(self, tree: Path, crates: list[str]):
        self.tree = tree
        self.definitions = workspace_macros(tree)
        self.crate_names = _crate_names(tree)
        self.directories = {crate: crate_dir(tree, crate) for crate in crates}
        self.conditions = {
            directory.relative_to(tree).as_posix(): ensure_dependency(tree, directory, write=False)
            for directory in self.directories.values()
        }
        self._users: dict[str, set[str | None]] = {}
        self._sources: list[tuple[str | None, str]] | None = None

    def users(self, name: str) -> set[str | None]:
        """The crates with a file in which the macro ``name`` seems to be used."""
        if name not in self._users:
            if self._sources is None:
                self._sources = [
                    (_crate_of(self.tree, path), path.read_text(encoding="utf-8", errors="replace"))
                    for path in _rust_files(self.tree)
                ]
            used = re.compile(rf"\b{re.escape(name)}\s*!")
            self._users[name] = {crate for crate, source in self._sources if name in source and used.search(source)}
        return self._users[name]

    def macros(self, crate: str) -> Macros:
        """What the files of ``crate`` may assume. The attribute in a macro body is resolved in
        the crate using the macro, which must therefore be a traced crate under the same
        condition."""
        home = self.directories[crate].relative_to(self.tree).as_posix()
        bodies = set()
        for name, definitions in self.definitions.items():
            here = [definition for definition in definitions if definition.crate == home]
            if not here:
                continue
            if any(definition.exported for definition in here):
                users = self.users(name) - {home}
                if any(user not in self.conditions or self.conditions[user] != self.conditions[home] for user in users):
                    continue
            bodies.add(name)
        return Macros(frozenset(self.definitions), self.crate_names, frozenset(bodies))

    def candidates(self, crate: str) -> dict[str, tuple[Path, str, int]]:
        """The files of ``crate`` with functions inside macros to mark: per path relative to the
        tree, the file, its text once marked and how many functions that marks."""
        directory = self.directories[crate]
        macros = self.macros(crate)
        condition = self.conditions[directory.relative_to(self.tree).as_posix()]
        found = {}
        for path in _source_files(directory, crate):
            source = path.read_text(encoding="utf-8")
            if "!" not in source:
                continue
            marked, count = instrument_macros(source, macros, condition)
            if count:
                found[path.relative_to(self.tree).as_posix()] = (path, marked, count)
        return found


def macro_safe_files(tree: Path, crates: list[str], path: Path | None = None) -> set[str]:
    """The files of ``crates`` whose functions inside macros are known to compile once marked,
    as paths relative to ``tree``.

    They are read from ``trace-macros.json`` (``{"files": [...]}``, as ``save_macro_files``
    writes it). ``path`` is that file, the directory holding it, or a file next to it such as
    the recipe's manifest; without ``path`` it is looked for at the root of ``tree``. No file
    means no macro is touched."""
    tree = Path(tree)
    location = tree if path is None else Path(path)
    if location.is_dir():
        location = location / MACRO_FILE
    elif location.name != MACRO_FILE and not (location.suffix == ".json" and "macros" in location.name):
        location = location.parent / MACRO_FILE
    if not location.is_file():
        return set()
    try:
        listed = json.loads(location.read_text(encoding="utf-8"))["files"]
    except (ValueError, KeyError, TypeError) as error:
        raise InstrumentError(f"{location} is not a list of files: {error}") from error
    if not isinstance(listed, list) or not all(isinstance(name, str) for name in listed):
        raise InstrumentError(f"{location}: `files` must be a list of paths")
    sources = [crate_dir(tree, crate).relative_to(tree).as_posix() + "/src/" for crate in crates]
    return {name for name in listed if any(name.startswith(source) for source in sources)}


def save_macro_files(path: Path, macros: dict) -> None:
    """Write the result of a checked ``add_tracing`` (its ``macros`` argument) as the
    ``trace-macros.json`` a later run without a check reads with ``macro_safe_files``."""
    content = {"files": sorted(macros["files"]), "functions": sum(macros["files"].values())}
    Path(path).write_text(json.dumps(content, indent=2) + "\n", encoding="utf-8")


_ANSI = re.compile(r"\x1b\[[0-9;]*m")
_SHORT_ERROR = re.compile(r"^(?P<path>[^\s:]+\.rs):\d+:\d+: error")
_ARROW = re.compile(r"^\s*(?:-->|:::)\s+(?P<path>[^\s:]+\.rs):\d+:\d+")
_DIAGNOSTIC = re.compile(r"^(error|warning|note|help)\b")


def error_files(output: str) -> dict[str, str]:
    """The files a compiler's output locates errors in, each with the first error found there.
    Both the usual and the ``--message-format=short`` output of cargo are understood."""
    found: dict[str, str] = {}
    current = None
    for line in _ANSI.sub("", output).splitlines():
        if short := _SHORT_ERROR.match(line):
            found.setdefault(short.group("path"), line.strip())
            current = None
        elif _DIAGNOSTIC.match(line):
            current = line.strip() if line.startswith("error") else None
        elif current and (arrow := _ARROW.match(line)):
            found.setdefault(arrow.group("path"), current)
    return found


def _blamed(output: str, files: Iterable[str]) -> dict[str, str]:
    """Which of ``files`` (relative to the tree) the compiler's output locates errors in."""
    blamed = {}
    for path, reason in error_files(output).items():
        path = path.replace("\\", "/").removeprefix("./")
        for name in files:
            if path == name or path.endswith("/" + name) or name.endswith("/" + path):
                blamed.setdefault(name, reason)
    return blamed


def _errors(output: str, limit: int = 5) -> str:
    lines = [line.strip() for line in _ANSI.sub("", output).splitlines() if re.match(r"\S*:? ?error", line.strip())]
    return "\n".join(lines[:limit]) or output.strip()[-2000:]


Check = Callable[[Path], tuple[bool, str]]


def add_tracing(
    tree: Path,
    crates: list[str],
    check: Check | None = None,
    macro_files: set[str] | None = None,
    macros: dict | None = None,
    write: bool = True,
) -> dict[str, int]:
    """Put the tracing attribute on every function of ``crates`` in the source tree ``tree``.

    Returns, per crate, how many functions gained the attribute by this call.

    The functions outside macros are marked first. Those inside macros are then marked

    - with ``check``, a function compiling ``tree`` and returning whether that worked and what
      the compiler said: crate by crate, in every file that has some (or only those also in
      ``macro_files``, if given). A file the compiler then locates an error in goes back to
      what it was before its macros were marked, and the crate is checked again. An error
      anywhere else, or a tree not compiling before any macro is marked, raises
      ``InstrumentError`` with the macros of the crate at hand unmarked;
    - without ``check``: in the files of ``macro_files`` only, the list such a run produced. It
      defaults to ``macro_safe_files(tree, crates)``, which is empty without a
      ``trace-macros.json`` in the tree.

    ``macros``, if given, is filled with ``files`` (per file relative to the tree, how many
    functions inside macros were marked by this call), ``reverted`` (per file put back, the
    compiler's first error in it) and ``functions`` (the total of ``files``).
    """
    tree = Path(tree)
    if check is not None and not write:
        raise InstrumentError("a check needs the changes to be written")
    counts = {crate: trace_crate(tree, crate, write) for crate in crates}
    kept: dict[str, int] = {}
    reverted: dict[str, str] = {}

    if check is None:
        allowed = macro_safe_files(tree, crates) if macro_files is None else set(macro_files)
        known = _Tree(tree, crates) if allowed else None
        for crate in crates if allowed else []:
            for name, (path, marked, count) in known.candidates(crate).items():
                if name in allowed:
                    if write:
                        path.write_text(marked, encoding="utf-8")
                    kept[name] = count
                    counts[crate] += count
    else:
        compiles, output = check(tree)
        if not compiles:
            raise InstrumentError(f"the tree does not compile before any macro is marked:\n{_errors(output)}")
        known = _Tree(tree, crates)
        for crate in crates:
            pending = known.candidates(crate)
            if macro_files is not None:
                pending = {name: found for name, found in pending.items() if name in macro_files}
            if not pending:
                continue
            before = {name: path.read_text(encoding="utf-8") for name, (path, _, _) in pending.items()}
            for path, marked, _ in pending.values():
                path.write_text(marked, encoding="utf-8")
            while True:
                compiles, output = check(tree)
                if compiles:
                    break
                blamed = _blamed(output, pending)
                if not blamed:
                    for name, (path, _, _) in pending.items():
                        path.write_text(before[name], encoding="utf-8")
                    raise InstrumentError(
                        f"marking the macros of `{crate}` breaks the build outside the files marked:\n{_errors(output)}"
                    )
                for name, reason in blamed.items():
                    pending.pop(name)[0].write_text(before[name], encoding="utf-8")
                    reverted[name] = reason
            for name, (_, _, count) in pending.items():
                kept[name] = count
                counts[crate] += count

    if macros is not None:
        macros.update(files=kept, reverted=reverted, functions=sum(kept.values()))
    return counts


# --------------------------------------------------------------------------------------------
# Command line


def _trace_command(arguments: argparse.Namespace) -> int:
    crates = [name for given in arguments.crate for name in given.split(",") if name]
    macros: dict = {}
    allowed = None if arguments.macros is None else macro_safe_files(arguments.source, crates, arguments.macros)
    counts = add_tracing(arguments.source, crates, macro_files=allowed, macros=macros, write=not arguments.dry_run)
    result = {"functions": counts, "total": sum(counts.values()), "written": not arguments.dry_run}
    if macros["files"]:
        result["in_macros"] = macros["files"]
    json.dump(result, sys.stdout)
    sys.stdout.write("\n")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m dwarf.freeze.instrument", description=__doc__.split("\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)

    trace = commands.add_parser("trace", help="add the tracing attribute to every function of some crates")
    trace.add_argument("--source", type=Path, required=True, help="root of the node's source tree")
    trace.add_argument("--crate", action="append", required=True, help="crate name; repeat or separate with commas")
    trace.add_argument("--macros", type=Path, help=f"a {MACRO_FILE} listing the files whose macros may be marked")
    trace.add_argument("--dry-run", action="store_true", help="count the functions without changing any file")
    trace.set_defaults(run=_trace_command)
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    try:
        return arguments.run(arguments)
    except InstrumentError as error:
        print(f"instrument: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
