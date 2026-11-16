"""Bring the Freeze hooks to a revision of the node that has no stored patch series.

The hooks are stored as a patch against one revision (the base). For another revision each
hooked file is merged three ways: what the base had, what the hooks made of it, and what the
other revision has. ``spec.yaml`` beside the patches groups the files by the capability they
give and says which groups a build cannot do without.

- A mandatory group that does not merge, or does not compile, stops the port.
- An optional group that does not merge, or whose files do not compile, is left as upstream
  has it; the capability is reported missing.

The result is stored as a patch series of its own, with a manifest, so later builds of that
revision are plain and reviewable.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import yaml

from . import build

LOCK = "Cargo.lock"


class PortError(Exception):
    pass


@dataclass
class Group:
    name: str
    files: list[str]
    mandatory: bool = False
    capabilities: list[str] = field(default_factory=list)
    about: str = ""


@dataclass
class Report:
    applied: list[str] = field(default_factory=list)
    dropped: dict[str, str] = field(default_factory=dict)
    missing: list[str] = field(default_factory=list)
    merged: int = 0
    copied: int = 0
    # Of the merged files, those whose hooks were placed by what surrounds them after a merge gave up.
    anchored: int = 0
    rounds: int = 0
    recipe: str = ""
    # With `by_hand`: the files left with conflict markers for a person to resolve, by group.
    conflicts: dict[str, list[str]] = field(default_factory=dict)

    def lines(self) -> list[str]:
        out = [f"hooks applied: {', '.join(self.applied) or 'none'} ({self.copied} file(s) taken as they are, {self.merged} merged{f', {self.anchored} of those by what surrounds the hooks' if self.anchored else ''})"]
        for name, why in self.dropped.items():
            out.append(f"left out: {name}: {why}")
        if self.missing:
            out.append(f"capabilities this build will not have: {', '.join(self.missing)}")
        return out


def load_spec(implementation: str = "amaru") -> tuple[list[Group], dict[str, Any]]:
    path = build.TARGETS / implementation / "spec.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    groups = [Group(name=entry["name"], files=list(entry["files"]), mandatory=bool(entry.get("mandatory")),
                    capabilities=list(entry.get("capabilities", [])), about=entry.get("about", "")) for entry in data["hooks"]]
    return groups, data


def patched_files(patch: Path) -> list[str]:
    """Every file a patch touches, in order."""
    found = []
    for line in patch.read_text(encoding="utf-8", errors="replace").splitlines():
        match = re.match(r"^diff --git a/(.+?) b/(.+)$", line)
        if match:
            found.append(match.group(2))
    return found


def check_spec(recipe: build.Recipe, groups: list[Group]) -> list[str]:
    """What is wrong with the spec for this recipe: files in no group, in two, or not in the patch."""
    touched = {name for patch in recipe.patches for name in patched_files(patch)} - {LOCK}
    inherited = set(recipe.manifest.get("inherited", []))
    listed: dict[str, str] = {}
    problems = []
    for group in groups:
        for name in group.files:
            if name in listed:
                problems.append(f"{name} is in both `{listed[name]}` and `{group.name}`")
            listed[name] = group.name
    problems += [f"{name} is hooked by the patch but in no group of the spec" for name in sorted(touched - set(listed))]
    problems += [f"{name} is declared inherited but is not in the spec" for name in sorted(inherited - set(listed))]
    problems += [f"{name} is declared inherited but the patch touches it" for name in sorted(inherited & touched)]
    problems += [f"{name} is in the spec but the patch does not touch it" for name in sorted(set(listed) - touched - inherited)]
    return problems


def _merge(theirs: Path, base: Path, ours: Path) -> tuple[bytes, int]:
    """Three-way merge of one file: ``theirs`` is the other revision's, ``ours`` the hooked base's."""
    result = subprocess.run(["git", "merge-file", "-p", str(theirs), str(base), str(ours)], capture_output=True)
    if result.returncode < 0 or result.returncode > 127:
        raise PortError(f"git merge-file failed on {theirs.name}: {result.stderr.decode(errors='replace').strip()}")
    return result.stdout, result.returncode


def apply_group(group: Group, source: Path, base: Path, hooked: Path, destination: Path, report: Report,
                by_hand: bool = False) -> str | None:
    """Put one group's hooks into ``destination``. Returns why it could not be done, if so.

    Nothing is written unless every file of the group can be. With ``by_hand``, a file changed
    in the same place by both sides is written with conflict markers, noted in the report, and
    the group counts as applied: a person finishes it.
    """
    writes: list[tuple[Path, bytes | Path]] = []
    merged = copied = anchored = 0
    marked: list[str] = []
    for name in group.files:
        ours, was, theirs = hooked / name, base / name, source / name
        if not ours.exists():
            if was.exists():
                return f"{name}: the hooks delete this file; that is not supported"
            continue
        if not was.exists():
            if theirs.exists() and theirs.read_bytes() != ours.read_bytes():
                return f"{name}: the hooks add this file, and the other revision already has a different one"
            writes.append((destination / name, ours))
            copied += 1
        elif not theirs.exists():
            return f"{name}: no longer exists in this revision"
        elif theirs.read_bytes() == was.read_bytes():
            writes.append((destination / name, ours))
            copied += 1
        else:
            content, conflicts = _merge(theirs, was, ours)
            if conflicts:
                # A merge gives up when lines next to a hook changed; the hook's own place may still be there.
                try:
                    placed, why = place_by_anchor(was.read_text(encoding="utf-8"), ours.read_text(encoding="utf-8"),
                                                  theirs.read_text(encoding="utf-8"))
                except UnicodeDecodeError:
                    placed, why = None, "not text"
                if placed is not None:
                    content, conflicts = placed.encode("utf-8"), 0
                    anchored += 1
            if conflicts and not by_hand:
                return f"{name}: {conflicts} place(s) changed both upstream and by the hooks ({why})"
            if conflicts:
                marked.append(name)
            writes.append((destination / name, content))
            merged += 1
    for target, content in writes:
        target.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, Path):
            shutil.copy2(content, target)
        else:
            target.write_bytes(content)
    report.merged += merged
    report.copied += copied
    report.anchored += anchored
    if marked:
        report.conflicts[group.name] = marked
    return None


_FUNCTION = re.compile(r"^\s*(?:pub(?:\([^)]*\))?\s+)?(?:default\s+)?(?:const\s+)?(?:async\s+)?(?:unsafe\s+)?(?:extern\s+\"[^\"]*\"\s+)?fn\s+(\w+)")


def _enclosing(lines: list[str], index: int) -> tuple[str, int] | None:
    """The function a line is in, as (name, indentation of its `fn` line), going by the nearest
    `fn` above that is indented less than the line."""
    own = len(lines[index]) - len(lines[index].lstrip()) if index < len(lines) and lines[index].strip() else None
    for above in range(min(index, len(lines) - 1), -1, -1):
        found = _FUNCTION.match(lines[above])
        if found:
            indent = len(lines[above]) - len(lines[above].lstrip())
            if own is None or indent < own or above == index:
                return found.group(1), indent
    return None


def _span(lines: list[str], name: str, indent: int) -> tuple[int, int] | None:
    """Where the function called ``name`` (declared at ``indent``) starts and ends in ``lines``;
    None unless there is exactly one."""
    starts = [number for number, line in enumerate(lines)
              if (found := _FUNCTION.match(line)) and found.group(1) == name and len(line) - len(line.lstrip()) == indent]
    if len(starts) != 1:
        return None
    end = len(lines)
    for number in range(starts[0] + 1, len(lines)):
        line = lines[number]
        if line.strip() and len(line) - len(line.lstrip()) <= indent and line.strip() != "}" and not line.lstrip().startswith(("{", ")", "where", "->")):
            end = number
            break
    return starts[0], end


def _find(block: list[str], lines: list[str], within: tuple[int, int]) -> int | None:
    """The one place inside ``within`` where ``block`` occurs in ``lines``, ignoring indentation."""
    wanted = [line.strip() for line in block]
    if not any(wanted):
        return None
    hits = [start for start in range(within[0], within[1] - len(wanted) + 1)
            if [line.strip() for line in lines[start:start + len(wanted)]] == wanted]
    return hits[0] if len(hits) == 1 else None


def place_by_anchor(base: str, hooked: str, theirs: str) -> tuple[str | None, str]:
    """Put the hooks of one file into another revision's version of it by what surrounds them.

    Each thing the hooks did to the base file is redone in ``theirs``: added lines go after the
    same line they followed in the base (or before the one they preceded), found inside the same
    function; changed or removed lines are changed where exactly those lines still stand. If a
    place cannot be found, or is found more than once, nothing is placed. Returns the new text,
    or None and why.
    """
    import difflib

    was, ours, new = base.splitlines(), hooked.splitlines(), theirs.splitlines()
    edits: list[tuple[int, int, list[str]]] = []
    for kind, i1, i2, j1, j2 in difflib.SequenceMatcher(None, was, ours, autojunk=False).get_opcodes():
        if kind == "equal":
            continue
        within = (0, len(new))
        function = _enclosing(was, min(i1, len(was) - 1)) if was else None
        if function:
            within = _span(new, *function) or within
        where = f"line {i1 + 1}" + (f" (in `{function[0]}`)" if function else "")
        if kind == "insert":
            placed = None
            for size in (1, 2, 3):
                before = [line for line in was[max(0, i1 - size):i1]]
                if len(before) == size and any(line.strip() for line in before):
                    found = _find(before, new, within)
                    if found is not None:
                        placed = found + size
                        break
                after = was[i1:i1 + size]
                if len(after) == size and any(line.strip() for line in after):
                    found = _find(after, new, within)
                    if found is not None:
                        placed = found
                        break
            if placed is None:
                return None, f"{where}: the lines around what the hooks add are gone or no longer unique"
            edits.append((placed, placed, ours[j1:j2]))
        else:
            found = _find(was[i1:i2], new, within)
            if found is None:
                return None, f"{where}: the lines the hooks change are gone, changed, or no longer unique"
            edits.append((found, found + (i2 - i1), ours[j1:j2]))
    edits.sort(key=lambda edit: edit[0], reverse=True)
    for index in range(len(edits) - 1):
        if edits[index + 1][1] > edits[index][0]:
            return None, "two of the hooks' changes would land on the same lines"
    for start, end, lines in edits:
        new[start:end] = lines
    return "\n".join(new) + ("\n" if theirs.endswith("\n") else ""), ""


MARKERS = (b"<<<<<<< ", b">>>>>>> ")


def unresolved(tree: Path, groups: list[Group]) -> list[str]:
    """Hooked files that still carry conflict markers."""
    found = []
    for group in groups:
        for name in group.files:
            path = tree / name
            if path.is_file() and any(line.startswith(MARKERS) for line in path.read_bytes().splitlines()):
                found.append(name)
    return found


def revert_group(group: Group, source: Path, destination: Path) -> None:
    for name in group.files:
        if (source / name).exists():
            shutil.copy2(source / name, destination / name)
        else:
            (destination / name).unlink(missing_ok=True)


def toolchain_of(tree: Path, default: str) -> str:
    """The Rust toolchain a tree asks for."""
    for name in ("rust-toolchain.toml", "rust-toolchain"):
        path = tree / name
        if path.is_file():
            found = re.search(r'channel\s*=\s*"([^"]+)"', path.read_text(encoding="utf-8"))
            if found:
                return found.group(1)
    return default


def errors_by_file(output: str) -> dict[str, list[str]]:
    """The compiler's errors (short format) by the file they are in."""
    found: dict[str, list[str]] = {}
    for line in output.splitlines():
        match = re.match(r"^(?:error[^:]*: )?([\w./-]+\.rs):(\d+):(\d+): error(?:\[\w+\])?: (.*)$", line.strip())
        if match:
            found.setdefault(match.group(1), []).append(f"line {match.group(2)}: {match.group(4)}")
    return found


def container_check(tree: Path, toolchain: str, implementation: str = "amaru", log: Callable[[str], None] = print) -> tuple[bool, str]:
    """Compile-check ``tree`` in a container holding its toolchain. The lock file the check
    arrives at is copied back, so the new crates are in it. Returns (passed, compiler output)."""
    dockerfile = build.HERE / "docker" / f"{implementation}-freeze.Dockerfile"
    image = f"dwarf/freeze-toolchain:{toolchain}"
    built = subprocess.run(["docker", "build", "-q", "-f", str(dockerfile), "--target", "toolchain", "--build-arg",
                            f"TOOLCHAIN={toolchain}", "-t", image, str(build.HERE / "docker")], capture_output=True, text=True)
    if built.returncode != 0:
        raise PortError(f"could not prepare the toolchain image: {built.stderr.strip()[-400:]}")
    # The tree is copied in from outside, so git would refuse it as someone else's without this.
    script = ("git config --global --add safe.directory /src && cd /src && git init -q . && git -c user.name=f -c user.email=f@localhost add -A && "
              "git -c user.name=f -c user.email=f@localhost commit -q -m port && "
              f"cargo check -p {implementation} --bin {implementation} --message-format=short 2>&1")
    name = f"dwarf-freeze-port-{int(time.time())}"
    created = subprocess.run(["docker", "create", "--name", name, "-e", "CARGO_TARGET_DIR=/target",
                              "-v", "dwarf-freeze-port-target:/target", "-v", "dwarf-freeze-port-registry:/usr/local/cargo/registry",
                              image, "sh", "-c", script], capture_output=True, text=True)
    if created.returncode != 0:
        raise PortError(f"could not create the check container: {created.stderr.strip()}")
    try:
        if subprocess.run(["docker", "cp", f"{tree}/.", f"{name}:/src"], capture_output=True).returncode != 0:
            raise PortError("could not copy the tree into the check container")
        log("  compiling (the first time takes a while)…")
        ran = subprocess.run(["docker", "start", "-a", name], capture_output=True, text=True)
        subprocess.run(["docker", "cp", f"{name}:/src/{LOCK}", str(tree / LOCK)], capture_output=True)
        return ran.returncode == 0, ran.stdout + ran.stderr
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)


def _tree_differs(first: Path, second: Path) -> bool:
    """Whether two crate folders hold different files (build output aside)."""
    def files(root: Path) -> dict[str, bytes]:
        return {path.relative_to(root).as_posix(): path.read_bytes() for path in root.rglob("*")
                if path.is_file() and "target" not in path.relative_to(root).parts}

    return not second.is_dir() or files(first) != files(second)


def write_series(source: Path, destination: Path, recipe: build.Recipe, name: str, output: Path, report: Report,
                 toolchain: str, groups: list[Group], crates: list[str]) -> Path:
    """Store what was done to ``source`` as a patch series with a manifest. Returns its folder."""
    with tempfile.TemporaryDirectory(prefix="freeze-series-") as scratch:
        work = Path(scratch) / "tree"
        shutil.copytree(source, work, ignore=build.IGNORED, symlinks=True)
        git = ["git", "-c", "user.name=freeze", "-c", "user.email=freeze@localhost", "-c", "core.autocrlf=false"]
        subprocess.run([*git, "init", "-q", "."], cwd=work, check=True)
        subprocess.run([*git, "add", "-A", "-f"], cwd=work, check=True)
        subprocess.run([*git, "commit", "-q", "-m", "upstream"], cwd=work, check=True)
        # Everything that differs from upstream belongs to the series, not only the files the spec
        # names: a port by hand may have had to touch a neighbouring file. The agent crates are
        # added by the build, not by the patch.
        added = {f"crates/{crate.name}" for crate in recipe.crates}
        for path in sorted(destination.rglob("*")):
            relative = path.relative_to(destination).as_posix()
            if not path.is_file() or path.is_symlink() or {"target", ".git"} & set(path.relative_to(destination).parts):
                continue
            if any(relative == crate or relative.startswith(crate + "/") for crate in added):
                continue
            original = source / relative
            if not original.is_file() or original.read_bytes() != path.read_bytes():
                (work / relative).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, work / relative)
        subprocess.run([*git, "add", "-A", "-f"], cwd=work, check=True)
        diff = subprocess.run([*git, "diff", "--cached", "--binary", "HEAD"], cwd=work, capture_output=True, check=True).stdout
        # Canonicalise blank context lines so generated patches are clean under
        # `git diff --check` and byte-reproducible with the stored series.
        diff = b"\n".join(b"" if line == b" " else line for line in diff.split(b"\n"))
    folder = output / name
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "0001-freeze-hooks.patch").write_bytes(diff)
    capabilities = [capability for capability in recipe.manifest.get("capabilities", []) if capability not in report.missing]
    manifest = {**recipe.manifest, "source": name, "toolchain": toolchain, "patches": ["0001-freeze-hooks.patch"],
                "capabilities": capabilities, "ported_from": recipe.name, "missing": report.missing,
                "left_out": report.dropped, "ported_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    if crates:
        manifest["crates"] = crates
    # The agent crates as this revision needs them, if they are not the shared ones.
    manifest.pop("crate_dir", None)
    own = folder / "crate"
    if own.exists():
        shutil.rmtree(own)
    if any(_tree_differs(crate, destination / "crates" / crate.name) for crate in recipe.crates):
        for crate in recipe.crates:
            shutil.copytree(destination / "crates" / crate.name, own / crate.name, ignore=build.IGNORED)
        manifest["crate_dir"] = "crate"
    (folder / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return folder


def _settle(source: Path, destination: Path, recipe: build.Recipe, groups: list[Group], spec: dict[str, Any], report: Report,
            name: str, output: Path, toolchain: str, check: Callable[[Path, str], tuple[bool, str]],
            log: Callable[[str], None]) -> Report:
    """Compile the hooked tree, leaving out optional groups that do not compile, and store the result."""
    by_file = {file: group for group in groups for file in group.files}
    while True:
        report.rounds += 1
        passed, output_text = check(destination, toolchain)
        if passed:
            break
        errors = errors_by_file(output_text)
        if not errors:
            raise PortError("the hooked tree does not compile, and the compiler named no file:\n" + output_text[-1500:])
        blamed: dict[str, str] = {}
        for file, messages in errors.items():
            group = by_file.get(file)
            if group is None and file.startswith(tuple(f"crates/{crate.name}/" for crate in recipe.crates)):
                raise PortError(f"the agent crate itself does not compile in this revision: {file}: {messages[0]}")
            if group is None:
                # An error outside the hooks is caused by one of them; which one cannot be told from here.
                raise PortError(f"{file} does not compile with the hooks in place ({messages[0]}); the port needs a person")
            if group.name in report.applied:
                blamed.setdefault(group.name, f"{file}: {messages[0]}")
        if not blamed:
            raise PortError("the hooked tree does not compile:\n" + output_text[-1500:])
        for group_name, why in blamed.items():
            group = next(entry for entry in groups if entry.name == group_name)
            if group.mandatory:
                raise PortError(f"`{group_name}` is needed and does not compile in this revision: {why}")
            revert_group(group, source, destination)
            report.applied.remove(group_name)
            report.dropped[group_name] = f"does not compile: {why}"
            log(f"  {group_name}: left out (does not compile: {why})")

    report.missing = sorted({capability for group in groups if group.name in report.dropped for capability in group.capabilities})
    folder = write_series(source, destination, recipe, name, output, report, toolchain, groups, spec.get("crates", []))
    report.recipe = folder.name
    return report


def finish(source: Path, resolved: Path, output: Path, recipe: build.Recipe, name: str | None = None,
           check: Callable[[Path, str], tuple[bool, str]] | None = None, log: Callable[[str], None] = print) -> Report:
    """Store a port whose conflicts a person resolved in ``resolved`` (a tree left by ``port(by_hand=True)``).

    A group counts as applied if any of its files differs from ``source``.
    """
    source, resolved = source.resolve(), resolved.resolve()
    groups, spec = load_spec(recipe.implementation)
    left = unresolved(resolved, groups)
    if left:
        raise PortError("these files still have conflict markers:\n  " + "\n  ".join(left))
    report = Report()
    for group in groups:
        def differs(file: str) -> bool:
            ours, theirs = resolved / file, source / file
            return ours.exists() != theirs.exists() or (ours.exists() and ours.read_bytes() != theirs.read_bytes())

        if any(differs(file) for file in group.files):
            report.applied.append(group.name)
        elif group.mandatory:
            raise PortError(f"`{group.name}` is needed, and {resolved} has none of its hooks")
        else:
            report.dropped[group.name] = "not in the resolved tree"
    toolchain = toolchain_of(source, recipe.manifest["toolchain"])
    check = check or (lambda tree, chain: container_check(tree, chain, recipe.implementation, log))
    return _settle(source, resolved, recipe, groups, spec, report, name or source.name, output, toolchain, check, log)


def port(source: Path, base: Path, output: Path, recipe: build.Recipe | None = None, name: str | None = None,
         check: Callable[[Path, str], tuple[bool, str]] | None = None, log: Callable[[str], None] = print,
         keep: Path | None = None, by_hand: bool = False) -> Report:
    """Port ``recipe``'s hooks from ``base`` (the tree they were written for) to ``source``.

    ``check(tree, toolchain)`` compiles a tree and returns (passed, compiler output); the
    default does it in a container. ``keep`` is a folder to leave the hooked tree in.
    With ``by_hand`` (needs ``keep``), places both sides changed are left in that tree with
    conflict markers; if there are any, nothing is compiled or stored and ``Report.conflicts``
    lists the files. ``finish`` takes it from there once they are resolved.
    """
    source, base = source.resolve(), base.resolve()
    recipe = recipe or next((entry for entry in build.recipes() if entry.name == base.name), None) or build.recipes()[0]
    groups, spec = load_spec(recipe.implementation)
    problems = check_spec(recipe, groups)
    if problems:
        raise PortError("the spec does not describe the patch:\n  " + "\n  ".join(problems))
    if not build._applies(recipe, base):
        raise PortError(f"{base} is not the tree the `{recipe.name}` hooks were written for: they do not apply to it")
    if by_hand and keep is None:
        raise PortError("resolving by hand needs a folder to leave the tree in")
    name = name or source.name
    toolchain = toolchain_of(source, recipe.manifest["toolchain"])
    check = check or (lambda tree, chain: container_check(tree, chain, recipe.implementation, log))
    report = Report()

    with tempfile.TemporaryDirectory(prefix="freeze-port-") as scratch:
        hooked = Path(scratch) / "hooked"
        build.instrument(base, recipe, hooked, trace=False)
        destination = keep or Path(scratch) / "ported"
        if destination.exists():
            shutil.rmtree(destination)
        shutil.copytree(source, destination, ignore=build.IGNORED, symlinks=True)
        for crate in recipe.crates:
            shutil.copytree(crate, destination / "crates" / crate.name, ignore=build.IGNORED)

        for group in groups:
            why = apply_group(group, source, base, hooked, destination, report, by_hand)
            if why is None:
                report.applied.append(group.name)
                marked = report.conflicts.get(group.name)
                log(f"  {group.name}: " + (f"{len(marked)} file(s) left for a person" if marked else "applied"))
            elif group.mandatory:
                raise PortError(f"`{group.name}` is needed and could not be brought over: {why}. "
                                "`--by-hand DIR` leaves the tree with the conflicts marked, for a person to resolve")
            else:
                report.dropped[group.name] = why
                log(f"  {group.name}: left out ({why})")
        if report.conflicts:
            return report
        return _settle(source, destination, recipe, groups, spec, report, name, output, toolchain, check, log)
