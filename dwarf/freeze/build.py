"""Build a node image with the Freeze agent compiled in.

Freeze's own builder: it copies a source tree, adds the agent crate, applies the hook patches
of a recipe and runs a container build. It does not use or change DWARF's release builds or
measurement patches.
"""

from __future__ import annotations

import hashlib
import json
import re
import shlex
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
TARGETS = HERE / "targets"
IGNORED = shutil.ignore_patterns("target", ".git", ".DS_Store", "node_modules")


class BuildError(Exception):
    pass


@dataclass(frozen=True)
class Recipe:
    name: str
    directory: Path
    manifest: dict[str, Any]

    @property
    def implementation(self) -> str:
        return self.manifest["implementation"]

    @property
    def patches(self) -> list[Path]:
        return [self.directory / name for name in self.manifest["patches"]]

    @property
    def crate(self) -> Path:
        return TARGETS / self.implementation / "crate" / self.manifest["crate"]

    @property
    def crates(self) -> list[Path]:
        """Every crate the recipe adds to the workspace: the agent, and what it is built from."""
        names = self.manifest.get("crates") or [self.manifest["crate"]]
        # A recipe may bring its own copy of the crates, when its revision needs them different.
        home = self.directory / self.manifest["crate_dir"] if self.manifest.get("crate_dir") else TARGETS / self.implementation / "crate"
        return [home / name for name in names]

    @property
    def traced(self) -> list[str]:
        """The workspace crates whose every function gets the tracing attribute at build time."""
        return list(self.manifest.get("trace", []))

    def digest(self) -> str:
        """Identifies exactly what gets added to the source: the patches, the crates and the manifest."""
        sha = hashlib.sha256()
        files = sorted(self.patches)
        for crate in self.crates:
            files += sorted(p for p in crate.rglob("*") if p.is_file() and "target" not in p.parts)
        sha.update(json.dumps(self.manifest, sort_keys=True).encode())
        for path in files:
            sha.update(path.name.encode() + b"\0" + path.read_bytes())
        return sha.hexdigest()


def recipes(implementation: str = "amaru", data_root: Path | None = None) -> list[Recipe]:
    """The stored patch series: the ones shipped, then the ones ported on this machine."""
    folders = [TARGETS / implementation / "patches"]
    if data_root is not None:
        folders.append(ported_dir(data_root, implementation))
    found = []
    for folder in folders:
        for manifest in sorted(folder.glob("*/manifest.json")):
            found.append(Recipe(manifest.parent.name, manifest.parent, json.loads(manifest.read_text(encoding="utf-8"))))
    return found


def ported_dir(data_root: Path, implementation: str = "amaru") -> Path:
    """Where patch series made by the applier on this machine are kept."""
    return data_root / "recipes" / implementation


def _applies(recipe: Recipe, source: Path) -> bool:
    for patch in recipe.patches:
        check = subprocess.run(["git", "apply", "--check", str(patch)], cwd=source, capture_output=True, text=True)
        if check.returncode != 0:
            return False
    return True


def choose_recipe(source: Path, wanted: str | None = None, implementation: str = "amaru",
                  data_root: Path | None = None) -> Recipe:
    """The named recipe, else the one whose patches apply to ``source`` (same-named first)."""
    available = recipes(implementation, data_root)
    if not available:
        raise BuildError(f"no Freeze recipes for {implementation}")
    if wanted:
        for recipe in available:
            if recipe.name == wanted:
                return recipe
        raise BuildError(f"no recipe named {wanted!r}; available: {', '.join(r.name for r in available)}")
    ordered = sorted(available, key=lambda recipe: (recipe.name != source.name, recipe.name), reverse=False)
    for recipe in ordered:
        if _applies(recipe, source):
            return recipe
    raise BuildError(
        f"no Freeze recipe applies to {source}. Tried: {', '.join(r.name for r in ordered)}. "
        "The hooks need porting to this revision: `freeze-debugger port --source <tree>`."
    )


def tree_digest(source: Path) -> str:
    sha = hashlib.sha256()
    for path in sorted(p for p in source.rglob("*") if p.is_file() and "target" not in p.parts and ".git" not in p.parts):
        sha.update(str(path.relative_to(source)).encode() + b"\0")
        sha.update(hashlib.sha256(path.read_bytes()).digest())
    return sha.hexdigest()


def instrument(source: Path, recipe: Recipe, destination: Path, trace: bool = True) -> dict[str, int]:
    """Copy ``source`` to ``destination`` and add the Freeze agent and hooks to the copy.

    With ``trace``, every function of the crates the recipe lists also gets the tracing
    attribute; that is not part of the stored patch, which stays small enough to review.
    Returns how many functions were marked in each crate.
    """
    if destination.exists():
        shutil.rmtree(destination)
    shutil.copytree(source, destination, ignore=IGNORED, symlinks=True)
    for crate in recipe.crates:
        shutil.copytree(crate, destination / "crates" / crate.name, ignore=IGNORED)
    for patch in recipe.patches:
        applied = subprocess.run(
            ["git", "apply", "--whitespace=nowarn", str(patch)], cwd=destination, capture_output=True, text=True
        )
        if applied.returncode != 0:
            raise BuildError(f"{patch.name} does not apply to {source}:\n{applied.stderr.strip()}")
    if trace and recipe.traced:
        from . import instrument as instrumenting

        # Functions inside macros are marked only in the files a checked run found safe for this recipe.
        safe = instrumenting.macro_safe_files(destination, recipe.traced, recipe.directory / "manifest.json")
        return instrumenting.add_tracing(destination, recipe.traced, macro_files=safe)
    return {}


def keep_debug_info(tree: Path) -> None:
    """Make the build keep debug information: full for the workspace's own crates, line numbers
    for everything else, which keeps the binary and the build time within reason."""
    manifest = tree / "Cargo.toml"
    text = manifest.read_text(encoding="utf-8")
    names = []
    for crate in sorted((tree / "crates").glob("*/Cargo.toml")):
        found = re.search(r'^\s*name\s*=\s*"([^"]+)"', crate.read_text(encoding="utf-8"), re.MULTILINE)
        if found:
            names.append(found.group(1))
    block = re.search(r"^\[profile\.dev\]\n(?:(?!\[).*\n?)*", text, re.MULTILINE)
    if block:
        body = re.sub(r"^debug\s*=.*\n", "", block.group(0), flags=re.MULTILINE).rstrip("\n") + '\ndebug = "line-tables-only"\n\n'
        text = text[:block.start()] + body + text[block.end():]
    else:
        text += '\n[profile.dev]\ndebug = "line-tables-only"\n'
    text += "\n# Added by `freeze-debugger build --debug-info`.\n"
    for name in names:
        if f'[profile.dev.package.{name}]' not in text and f'[profile.dev.package."{name}"]' not in text:
            text += f"[profile.dev.package.{name}]\ndebug = true\n"
    manifest.write_text(text, encoding="utf-8")


def build(
    source: Path,
    data_root: Path,
    recipe_name: str | None = None,
    tag: str | None = None,
    platform: str | None = None,
    log: Any = print,
    static: bool = False,
    debug_info: bool = False,
) -> dict[str, Any]:
    """Build the image and return its build record.

    ``static`` produces a fully static x86-64 binary in an amd64 image, which is what DWARF's
    devnet containers need; otherwise the image is built for ``platform`` (default: this machine).
    ``debug_info`` keeps what a source-level debugger needs in the binary (the node's own code
    in full, its dependencies as line numbers) and also builds the tools image for
    `freeze-debugger source`.
    """
    source = source.resolve()
    if not (source / "Cargo.toml").is_file():
        raise BuildError(f"{source} does not look like an Amaru source tree (no Cargo.toml)")
    recipe = choose_recipe(source, recipe_name, data_root=data_root)
    source_digest, recipe_digest = tree_digest(source), recipe.digest()
    flavour = ("static-amd64" if static else (platform or "native")) + ("-dbg" if debug_info else "")
    build_id = hashlib.sha256(f"{source_digest}:{recipe_digest}:{flavour}".encode()).hexdigest()[:16]
    suffix = ("-static" if static else "") + ("-dbg" if debug_info else "")
    tag = tag or f"dwarf/freeze-{recipe.implementation}:{source.name.lower()}-{recipe_digest[:12]}{suffix}"
    context = data_root / "builds" / build_id
    context.mkdir(parents=True, exist_ok=True)
    log(f"recipe {recipe.name} · source {source.name} · image {tag}")
    traced = instrument(source, recipe, context / "src")
    if traced:
        log(f"functions that can be traced: {sum(traced.values()):,} in {', '.join(traced)}")
    if debug_info:
        keep_debug_info(context / "src")
        shutil.copy(HERE / "lldb_driver.py", context / "lldb_driver.py")

    dockerfile = HERE / "docker" / f"{recipe.implementation}-freeze{'-static' if static else ''}.Dockerfile"
    command = ["docker", "build", "-f", str(dockerfile), "-t", tag]
    labels = {
        "dwarf.freeze": "1",
        "dwarf.freeze.implementation": recipe.implementation,
        "dwarf.freeze.recipe": recipe.name,
        "dwarf.freeze.recipe-sha256": recipe_digest,
        "dwarf.freeze.source-tree-sha256": source_digest,
        # How this revision's node is started and bootstrapped; the launcher reads these.
        "dwarf.freeze.capabilities": ",".join(recipe.manifest.get("capabilities", [])),
        "dwarf.freeze.run": " ".join(recipe.manifest.get("run", ["run"])),
        "dwarf.freeze.bootstrap": " ".join(recipe.manifest.get("bootstrap", ["bootstrap"])),
    }
    for key, value in labels.items():
        command += ["--label", f"{key}={value}"]
    command += [
        "--build-arg", f"TOOLCHAIN={recipe.manifest['toolchain']}",
        "--build-arg", f"BUILD_COMMAND={shlex.join(recipe.manifest['build'])}",
        "--build-arg", f"BINARY={recipe.manifest['binary']}",
        # The node claims only what this recipe gave it.
        "--build-arg", f"CAPABILITIES={','.join(recipe.manifest.get('capabilities', []))}",
    ]
    if static:
        command += ["--platform", "linux/amd64"]
    elif platform:
        command += ["--platform", platform]
    command.append(str(context))
    started = time.time()
    if subprocess.run(command).returncode != 0:
        raise BuildError("the container build failed (output above)")
    if debug_info:
        from .source import tools_image

        tools = [part if part != tag else tools_image(tag) for part in command]
        if subprocess.run([*tools[:2], "--target", "debugger", *tools[2:]]).returncode != 0:
            raise BuildError("building the tools image for source stepping failed (output above)")
    image_id = subprocess.run(
        ["docker", "image", "inspect", "--format", "{{.Id}}", tag], capture_output=True, text=True, check=True
    ).stdout.strip()
    record = {
        "schema": "dwarf-freeze-build-v1",
        "image": tag,
        "image_id": image_id,
        "implementation": recipe.implementation,
        "recipe": recipe.name,
        "recipe_sha256": recipe_digest,
        "source": str(source),
        "source_tree_sha256": source_digest,
        "platform": "linux/amd64" if static else platform,
        "static": static,
        "debug_info": debug_info,
        "capabilities": recipe.manifest.get("capabilities", []),
        "built_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "seconds": round(time.time() - started),
    }
    (context / "build.json").write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    shutil.rmtree(context / "src", ignore_errors=True)
    return record


def builds(data_root: Path) -> list[dict[str, Any]]:
    """Records of past builds, newest first."""
    records = [json.loads(path.read_text(encoding="utf-8")) for path in (data_root / "builds").glob("*/build.json")]
    return sorted(records, key=lambda record: record["built_at"], reverse=True)
