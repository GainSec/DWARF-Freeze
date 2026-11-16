"""A closed local devnet with one Freeze node in it.

Nothing here leaves the machine: three block-producing cardano-nodes feed two Amaru relays
on a private test network, and the first Amaru relay runs the Freeze build. Dropping,
editing and injecting traffic affects only these containers.

The devnet itself is one of DWARF's compose files, used exactly as it is. Freeze adds a
compose override beside it; no DWARF code or file is changed.

Which devnet, and which node in it becomes the Freeze node, is a *profile*: a small YAML
file. Some ship with Freeze (``profiles/``); yours go in ``<data root>/profiles/``. Any
compose file whose Amaru services start through DWARF's ``amaru-relay-bootstrap`` wrapper
can be used directly with ``--compose FILE --target SERVICE``.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import shlex
import subprocess
import time
from dataclasses import asdict, dataclass
from dataclasses import field as dc_field
from pathlib import Path
from typing import Any

import yaml

REPO = Path(__file__).resolve().parents[2]
BASELINE = REPO / "antithesis" / "cardano_amaru_relay_bootstrap_control" / "docker-compose.yaml"
PROJECT = "dwarf-freeze-devnet"
TARGET = "amaru-relay-1"
AGENT_PORT = 7373
NETWORK = "testnet_42"
SNAPSHOT_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")


class DevnetError(Exception):
    pass


def directory(data_root: Path) -> Path:
    return data_root / "devnet"


PEER_COOLDOWN = 5
SHIPPED = Path(__file__).resolve().parent / "profiles"
WRAPPER = "amaru-relay-bootstrap"


@dataclass
class Profile:
    """Which devnet to run and which of its nodes is the Freeze node."""

    name: str
    compose: list[str]
    target: str = TARGET
    about: str = ""
    network: str = NETWORK
    # The variable that tells the target's start-up wrapper which node binary to run.
    binary_env: str = "AMARU_BIN"
    environment: dict[str, str] = dc_field(default_factory=dict)
    peer_cooldown: int = PEER_COOLDOWN
    resign: bool = False
    # More services of the same devnet that also run the Freeze build, each with its own agent port.
    also: list[str] = dc_field(default_factory=list)
    # Extra variables for one service, e.g. the peer a second Freeze node should follow.
    node_environment: dict[str, dict[str, str]] = dc_field(default_factory=dict)
    origin: str = "shipped"

    @property
    def nodes(self) -> list[str]:
        """Every Freeze node of this devnet: the main one first."""
        return [self.target, *[name for name in self.also if name != self.target]]

    @property
    def files(self) -> list[Path]:
        return [Path(path) for path in self.compose]


def _resolve(path: str, beside: Path) -> str:
    """A compose path from a profile: absolute, inside the DWARF repository, or beside the profile."""
    candidate = Path(path).expanduser()
    if candidate.is_absolute():
        return str(candidate)
    for base in (REPO, beside):
        if (base / candidate).is_file():
            return str((base / candidate).resolve())
    return str(REPO / candidate)


def load_profile(path: Path, origin: str = "yours") -> Profile:
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as error:
        raise DevnetError(f"cannot read the profile {path}: {error}") from error
    if not isinstance(document, dict) or not document.get("compose"):
        raise DevnetError(f"{path}: a profile needs at least `compose:` (a compose file) and `target:` (a service in it)")
    compose = document["compose"] if isinstance(document["compose"], list) else [document["compose"]]
    known = {"name", "compose", "target", "about", "network", "binary_env", "environment", "peer_cooldown", "resign", "also",
             "node_environment"}
    unknown = sorted(set(document) - known)
    if unknown:
        raise DevnetError(f"{path}: unknown setting(s) {', '.join(unknown)}; known: {', '.join(sorted(known))}")
    return Profile(
        name=str(document.get("name") or path.stem),
        compose=[_resolve(str(entry), path.parent) for entry in compose],
        target=str(document.get("target") or TARGET),
        about=str(document.get("about") or ""),
        network=str(document.get("network") or NETWORK),
        binary_env=str(document.get("binary_env") or "AMARU_BIN"),
        environment={str(k): str(v) for k, v in (document.get("environment") or {}).items()},
        peer_cooldown=int(document.get("peer_cooldown", PEER_COOLDOWN)),
        resign=bool(document.get("resign", False)),
        also=[str(name) for name in (document.get("also") or [])],
        node_environment={str(service): {str(k): str(v) for k, v in (values or {}).items()}
                          for service, values in (document.get("node_environment") or {}).items()},
        origin=origin,
    )


def profiles(data_root: Path | None = None) -> list[Profile]:
    """Every profile: the shipped ones, then yours (which win on a shared name)."""
    found: dict[str, Profile] = {}
    for folder, origin in ((SHIPPED, "shipped"), (data_root / "profiles" if data_root else None, "yours")):
        if folder is None or not folder.is_dir():
            continue
        for path in sorted(folder.glob("*.y*ml")):
            profile = load_profile(path, origin)
            found[profile.name] = profile
    return list(found.values())


def targets(compose: Path) -> list[str]:
    """The services in a compose file that can become the Freeze node."""
    try:
        document = yaml.safe_load(compose.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as error:
        raise DevnetError(f"cannot read {compose}: {error}") from error
    services_ = (document or {}).get("services") or {}
    return [name for name, service in services_.items()
            if isinstance(service, dict) and service.get("entrypoint") in (WRAPPER, [WRAPPER])]


def topologies() -> list[tuple[Path, list[str]]]:
    """DWARF's own devnet layouts that a Freeze node fits into, with the nodes it can replace."""
    found = []
    for compose in sorted((REPO / "antithesis").glob("*/docker-compose.yaml")):
        try:
            names = targets(compose)
        except DevnetError:
            continue
        if names:
            found.append((compose, names))
    return found


def find_profile(name: str | None, data_root: Path | None = None, compose: str | None = None, target: str | None = None,
                 also: list[str] | None = None) -> Profile:
    """The profile to run: by name, from a profile file, or made on the spot from a compose file."""
    if compose:
        path = Path(compose).expanduser().resolve()
        if not path.is_file():
            raise DevnetError(f"no such compose file: {path}")
        profile = Profile(name=path.parent.name, compose=[str(path)], origin="command line",
                          about=f"{path.parent.name}/{path.name}")
    elif name and (name.endswith((".yaml", ".yml")) or "/" in name):
        profile = load_profile(Path(name).expanduser())
    else:
        wanted = name or "relay"
        available = {profile.name: profile for profile in profiles(data_root)}
        if wanted not in available:
            raise DevnetError(f"no profile named `{wanted}`; there are: {', '.join(available)}")
        profile = available[wanted]
    if target:
        profile.target = target
    if also:
        profile.also = [name for name in dict.fromkeys([*profile.also, *also]) if name != profile.target]
    for file in profile.files:
        if not file.is_file():
            raise DevnetError(f"profile `{profile.name}`: compose file missing: {file}")
    usable = targets(profile.files[0])
    if profile.target not in usable and profile.binary_env == "AMARU_BIN":
        hint = f"; services that can: {', '.join(usable)}" if usable else "; this layout has no service started by DWARF's Amaru wrapper"
        raise DevnetError(f"profile `{profile.name}`: `{profile.target}` cannot be replaced by a Freeze node{hint}")
    for extra in profile.also:
        if extra not in usable:
            raise DevnetError(f"profile `{profile.name}`: `{extra}` cannot be a Freeze node; services that can: {', '.join(usable)}")
    return profile


def node_ports(profile: Profile | None, port: int = AGENT_PORT) -> dict[str, int]:
    """The local agent port of each Freeze node: the main one on ``port``, the others on the ports after it."""
    names = profile.nodes if profile else [TARGET]
    return {name: port + index for index, name in enumerate(names)}


def active(data_root: Path) -> Profile:
    """The profile of the devnet that Freeze last started; the default one if none."""
    saved = directory(data_root) / "profile.json"
    if saved.exists():
        return Profile(**json.loads(saved.read_text(encoding="utf-8")))
    return Profile(name="relay", compose=[str(BASELINE)])


def override(image: str, token: str, port: int = AGENT_PORT, frozen: bool | list[str] = False, cooldown: int = PEER_COOLDOWN,
             profile: Profile | None = None, resign: bool | None = None, capabilities: str | None = None,
             run_words: str | None = None) -> dict[str, Any]:
    """The compose override that puts the Freeze build into the profile's target node.

    ``capabilities`` is what the image was built with (its label); the node then claims only that.
    ``run_words`` is how this revision of the node is started when that is no longer `run`
    (its label, for example `node run`). DWARF's wrapper says `amaru run …`, so the node's place
    is then taken by a two-line launcher that passes everything on under the new words.

    ``cooldown`` is how many seconds the node keeps a peer out after deciding it misbehaved.
    Amaru's default is 600; a tampered message usually earns that verdict, and the relay has
    one upstream, so the default would leave it without traffic for ten minutes after each test.
    """
    resign = profile.resign if resign is None and profile else (True if resign is None else resign)
    services_: dict[str, Any] = {
        "freeze-target-extract": {
            "image": image,
            "platform": "linux/amd64",
            "entrypoint": ["/bin/sh", "-ec"],
            "command": [extract_script(run_words)],
            "volumes": ["freeze-target-bin:/target"],
            "restart": "no",
        },
    }
    for service, local_port in node_ports(profile, port).items():
        environment = {
            "AMARU_PEER_REMOVAL_COOLDOWN_SECS": str(cooldown),
            # The relay's wrapper runs this binary for both its bootstrap and the node itself.
            (profile.binary_env if profile else "AMARU_BIN"): "/target/amaru",
            "AMARU_MIGRATE_CHAIN_DB": "true",
            "DWARF_FREEZE_LISTEN": f"0.0.0.0:{AGENT_PORT}",
            "DWARF_FREEZE_TOKEN": token,
            "DWARF_FREEZE_RESIGN": "1" if resign else "0",
            "DWARF_FREEZE_DEVNET_CONFIG": "/freeze-producers",
        }
        # `frozen` is yes/no for every Freeze node, or the names of the ones to hold at their first step.
        if frozen is True or (not isinstance(frozen, bool) and service in frozen):
            environment["DWARF_FREEZE_START"] = "frozen"
        if capabilities:
            environment["DWARF_FREEZE_CAPABILITIES"] = capabilities
        if profile:
            environment.update(profile.environment)
            environment.update(profile.node_environment.get(service, {}))
        services_[service] = {
            "environment": environment,
            "volumes": [
                "freeze-target-bin:/target:ro",
                "p1-configs:/freeze-producers/p1:ro",
                "p2-configs:/freeze-producers/p2:ro",
                "p3-configs:/freeze-producers/p3:ro",
            ],
            "ports": [f"127.0.0.1:{local_port}:{AGENT_PORT}"],
            "depends_on": {"freeze-target-extract": {"condition": "service_completed_successfully"}},
        }
    return {"services": services_, "volumes": {"freeze-target-bin": {}}}


def image_label(image: str, label: str) -> str:
    """One label of a local image; empty if the image or the label is not there."""
    found = subprocess.run(["docker", "image", "inspect", "--format", f'{{{{ index .Config.Labels "{label}" }}}}', image],
                           capture_output=True, text=True)
    return found.stdout.strip() if found.returncode == 0 else ""


def extract_script(run_words: str | None = None) -> str:
    """The shell line that puts the Freeze node where the devnet's wrapper will start it."""
    if not run_words or run_words.split() == ["run"]:
        return ("cp /usr/local/bin/amaru /target/.amaru.new && chmod 0755 /target/.amaru.new "
                "&& mv -f /target/.amaru.new /target/amaru")
    words = " ".join(shlex.quote(word) for word in run_words.split())
    launcher = f'#!/bin/sh\nif [ "$1" = run ]; then shift; exec /target/amaru.real {words} "$@"; fi\nexec /target/amaru.real "$@"\n'
    return ("cp /usr/local/bin/amaru /target/.amaru.new && chmod 0755 /target/.amaru.new "
            "&& mv -f /target/.amaru.new /target/amaru.real "
            f"&& printf {shlex.quote(launcher)} > /target/.amaru.new && chmod 0755 /target/.amaru.new "
            "&& mv -f /target/.amaru.new /target/amaru")


def _compose(data_root: Path, *args: str, capture: bool = False) -> subprocess.CompletedProcess[str]:
    command = [*_base(data_root), *args]
    environment = {**os.environ, "INTERNAL_NETWORK": "false"}
    return subprocess.run(command, env=environment, capture_output=capture, text=True)


def _base(data_root: Path) -> list[str]:
    profile = active(data_root)
    for file in profile.files:
        if not file.is_file():
            raise DevnetError(f"the devnet's compose file is missing: {file}")
    command = ["docker", "compose", "-p", PROJECT]
    for file in profile.files:
        command += ["-f", str(file)]
    return command + ["-f", str(directory(data_root) / "override.yml")]


def up(image: str, data_root: Path, port: int = AGENT_PORT, frozen: bool = False, cooldown: int | None = None,
       profile: Profile | None = None, resign: bool | None = None, brokered: bool = True,
       auto_pause: bool = True, record: str = "all", freeze_together: bool = False) -> dict[str, Any]:
    """Start the devnet. Returns how to reach the Freeze node's agent once it is running."""
    home = directory(data_root)
    home.mkdir(parents=True, exist_ok=True)
    previous = current(data_root)
    profile = profile or (active(data_root) if previous else find_profile(None, data_root))
    was = (previous.get("profile", "relay"), previous.get("target", "amaru-relay-1")) if previous else None
    if previous is not None and was == (profile.name, profile.target) and sorted(previous.get("nodes", {was[1]: 0})) != sorted(profile.nodes):
        raise DevnetError(
            f"the devnet that is up has the Freeze nodes {', '.join(sorted(previous.get('nodes', {was[1]: 0})))}; "
            "stop it with `freeze-debugger devnet down` before starting it with different ones"
        )
    if was is not None and was != (profile.name, profile.target):
        raise DevnetError(
            f"a devnet with profile `{was[0]}` (Freeze node {was[1]}) is "
            "already up; stop it with `freeze-debugger devnet down` before starting another"
        )
    cooldown = profile.peer_cooldown if cooldown is None else cooldown
    resign = profile.resign if resign is None else resign
    (home / "profile.json").write_text(json.dumps(asdict(profile), indent=2) + "\n", encoding="utf-8")
    TARGET = profile.target  # noqa: N806 - shadows the default for the rest of this function
    token_file = home / "token"
    if not token_file.exists():
        token_file.touch(mode=0o600)
        token_file.chmod(0o600)
        token_file.write_text(secrets.token_urlsafe(24), encoding="utf-8")
    token = token_file.read_text(encoding="utf-8").strip()
    capabilities = image_label(image, "dwarf.freeze.capabilities")
    run_words = image_label(image, "dwarf.freeze.run")
    (home / "override.yml").write_text(
        f"# Generated by `freeze-debugger devnet up`. Applied on top of the `{profile.name}` devnet.\n"
        + yaml.safe_dump(override(image, token, port, frozen, cooldown, profile, resign, capabilities, run_words), sort_keys=False),
        encoding="utf-8",
    )
    # `up` waits for the last service's precondition, which is the Amaru bootstrap (many
    # minutes), so it is left running on its own with its output in a log file.
    if _compose(data_root, "config", "--quiet", capture=True).returncode != 0:
        raise DevnetError("the devnet's compose files do not fit together: " + _compose(data_root, "config", "--quiet", capture=True).stderr.strip())
    with (home / "up.log").open("w", encoding="utf-8") as log_file:
        subprocess.Popen(
            [*_base(data_root), "up", "-d"],
            env={**os.environ, "INTERNAL_NETWORK": "false"}, stdout=log_file, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL, start_new_session=True,
        )
    deadline = time.time() + 180
    while time.time() < deadline:
        running = {row["service"] for row in services(data_root) if row["state"] == "running"}
        if all(name in running for name in profile.nodes):
            break
        time.sleep(3)
    else:
        raise DevnetError(f"{', '.join(profile.nodes)} did not all start; see {home / 'up.log'}")
    if previous is not None and previous.get("image") != image:
        # The new binary is copied into a shared volume, but a node that is already running
        # keeps the old one until it is restarted.
        _compose(data_root, "up", "-d", "--no-deps", "--force-recreate", "freeze-target-extract", capture=True)
        if _compose(data_root, "restart", *profile.nodes, capture=True).returncode != 0:
            raise DevnetError(f"could not restart {', '.join(profile.nodes)} with the new image")
    info = {"host": "127.0.0.1", "port": port, "token_file": str(token_file), "image": image, "project": PROJECT,
            "cooldown": cooldown, "profile": profile.name, "capabilities": capabilities, "run_words": run_words,
            "resign": resign, "nodes": node_ports(profile, port),
            "target": TARGET, "network": profile.network, "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            # Kept from the first start, so a second `up` on a running devnet does not ask for a new start snapshot.
            "frozen_start": bool(frozen) if previous is None else bool(previous.get("frozen_start"))}
    if previous is not None:
        info["started_at"] = previous.get("started_at", info["started_at"])
    if brokered:
        info["brokers"] = start_brokers(data_root, info, auto_pause, record, freeze_together)
    (home / "run.json").write_text(json.dumps(info, indent=2) + "\n", encoding="utf-8")
    return {**info, "token": token}


def start_brokers(data_root: Path, info: dict[str, Any], auto_pause: bool = True, record: str = "all",
                  freeze_together: bool = False) -> dict[str, int]:
    """One broker per Freeze node, each on the port ``broker.OFFSET`` above its agent's.

    Only the main node's broker pauses the other nodes with it; if every broker did, two Freeze
    nodes in different states would pause and resume the rest in turns.
    """
    from . import broker

    home = directory(data_root)
    ports = {}
    for name, agent_port in info["nodes"].items():
        ports[name] = agent_port + broker.OFFSET
        arguments = ["--port", str(agent_port), "--listen", str(ports[name]), "--token-file", info["token_file"],
                     "--sessions", str(data_root / "sessions"), "--record", record]
        arguments += ["--devnet-root", str(data_root)]
        if info.get("frozen_start"):
            arguments.append("--start-snapshot")
        if name == info["target"]:
            arguments += ["--pause-peers"] + ([] if auto_pause else ["--no-auto-pause"])
            for other, other_port in info["nodes"].items():
                if other != name:
                    arguments += ["--also", f"{other}=127.0.0.1:{other_port}"]
            if freeze_together:
                arguments.append("--freeze-together")
        broker.start(home, name, arguments)
    return ports


def stop_brokers(data_root: Path) -> None:
    from . import broker

    home = directory(data_root)
    for pid in home.glob("broker-*.pid"):
        broker.stop(home, pid.name[len("broker-"):-len(".pid")])


def current(data_root: Path, node: str | None = None, direct: bool = False) -> dict[str, Any] | None:
    """How to reach the devnet's Freeze node; with ``node``, that one of its Freeze nodes.

    ``port`` is where to connect: the node's broker when one is running, unless ``direct`` asks
    for the agent itself. ``agent_port`` is always the agent's.
    """
    run_file = directory(data_root) / "run.json"
    if not run_file.exists():
        return None
    info = json.loads(run_file.read_text(encoding="utf-8"))
    info["token"] = Path(info["token_file"]).read_text(encoding="utf-8").strip()
    info.setdefault("nodes", {info.get("target", TARGET): info["port"]})
    info["main_port"] = info["port"]
    if node is not None and node != info.get("target"):
        if node not in info["nodes"]:
            raise DevnetError(f"`{node}` is not a Freeze node of this devnet; they are: {', '.join(info['nodes'])}")
        info = {**info, "target": node, "port": info["nodes"][node]}
    info["agent_port"] = info["port"]
    if not direct and info["target"] in info.get("brokers", {}):
        from . import broker

        if broker.running(directory(data_root), info["target"]) is not None:
            info["port"] = info["brokers"][info["target"]]
            info["brokered"] = True
    return info


def services(data_root: Path) -> list[dict[str, Any]]:
    """Each container of the devnet with its state."""
    result = _compose(data_root, "ps", "-a", "--format", "json", capture=True)
    if result.returncode != 0:
        raise DevnetError(result.stderr.strip() or "could not list the devnet's containers")
    text = result.stdout.strip()
    if not text:
        return []
    rows = json.loads(text) if text.startswith("[") else [json.loads(line) for line in text.splitlines() if line.strip()]
    return [{"service": row.get("Service"), "state": row.get("State"), "status": row.get("Status")} for row in rows]


def agent_ready(info: dict[str, Any]) -> bool:
    """Whether the agent answers. A published port accepts connections before the node listens,
    so this asks the agent a question instead of only connecting."""
    from .client import FreezeClient, FreezeError

    client = FreezeClient(info["host"], info["port"], token=info.get("token"), timeout=3.0)
    try:
        client.connect()
        return True
    except (OSError, FreezeError):
        return False
    finally:
        client.close()


def target_log(data_root: Path, lines: int = 3, node: str | None = None) -> str:
    result = _compose(data_root, "logs", "--no-log-prefix", "--tail", str(lines), node or active(data_root).target, capture=True)
    return (result.stdout or result.stderr).strip()


def wait_ready(data_root: Path, timeout: float = 3600.0, log: Any = print, node: str | None = None) -> dict[str, Any]:
    """Wait until the Freeze node has bootstrapped and its agent accepts connections."""
    info = current(data_root, node)
    if info is None:
        raise DevnetError("the devnet is not running; start it with `freeze-debugger devnet up`")
    deadline, last = time.time() + timeout, ""
    TARGET = info.get("target", active(data_root).target)  # noqa: N806
    while time.time() < deadline:
        if agent_ready(info):
            return info
        states = {row["service"]: row["state"] for row in services(data_root)}
        if not states:
            raise DevnetError("the devnet has no containers; did it stop?")
        tail = target_log(data_root, 1, TARGET)
        if tail and tail != last:
            last = tail
            log(f"  {TARGET}: {tail[:160]}")
        time.sleep(10)
    raise DevnetError(f"the Freeze node was not ready within {timeout:.0f}s")


def restart_target(data_root: Path, frozen: bool = True, node: str | None = None) -> dict[str, Any]:
    """Restart a Freeze node from its stored data (the rest of the devnet keeps running)."""
    info = current(data_root)
    if info is None:
        raise DevnetError("the devnet is not running; start it with `freeze-debugger devnet up`")
    home = directory(data_root)
    profile = active(data_root)
    TARGET = _freeze_node(data_root, node)  # noqa: N806
    (home / "override.yml").write_text(
        f"# Generated by `freeze-debugger`. Applied on top of the `{profile.name}` devnet.\n"
        + yaml.safe_dump(
            override(
                info["image"], info["token"], info["main_port"], [TARGET] if frozen else False,
                info.get("cooldown", PEER_COOLDOWN), profile,
                resign=bool(info.get("resign", profile.resign)), capabilities=info.get("capabilities"),
                run_words=info.get("run_words"),
            ),
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    result = _compose(data_root, "up", "-d", "--no-deps", "--force-recreate", TARGET, capture=True)
    if result.returncode != 0:
        raise DevnetError(f"could not restart {TARGET}: {result.stderr.strip()}")
    return wait_ready(data_root, timeout=600, log=lambda _line: None, node=TARGET)


def peers(data_root: Path) -> list[str]:
    """The devnet's running services other than the Freeze node."""
    ours = active(data_root).nodes
    return [row["service"] for row in services(data_root) if row["service"] not in ours and row["state"] in ("running", "paused")]


def pause_peers(data_root: Path) -> None:
    """Stop every other node's clock-driven progress while the Freeze node is frozen."""
    ours = active(data_root).nodes
    names = [row["service"] for row in services(data_root) if row["service"] not in ours and row["state"] == "running"]
    if names and _compose(data_root, "pause", *names, capture=True).returncode != 0:
        raise DevnetError("could not pause the other nodes")


def resume_peers(data_root: Path) -> None:
    ours = active(data_root).nodes
    names = [row["service"] for row in services(data_root) if row["service"] not in ours and row["state"] == "paused"]
    if names and _compose(data_root, "unpause", *names, capture=True).returncode != 0:
        raise DevnetError("could not resume the other nodes")


def down(data_root: Path, keep_data: bool = False) -> None:
    if not (directory(data_root) / "override.yml").exists():
        return
    stop_brokers(data_root)
    arguments = ["down", "--remove-orphans"] + ([] if keep_data else ["--volumes"])
    if _compose(data_root, *arguments).returncode != 0:
        raise DevnetError("the devnet did not stop cleanly (output above)")
    for name in ("run.json", "profile.json"):
        if (directory(data_root) / name).exists():
            (directory(data_root) / name).unlink()


# snapshots -------------------------------------------------------------------------------


def _freeze_node(data_root: Path, node: str | None) -> str:
    """The Freeze node meant: the main one unless another is named."""
    profile = active(data_root)
    if node is None or node == profile.target:
        return profile.target
    if node not in profile.nodes:
        raise DevnetError(f"`{node}` is not a Freeze node of this devnet; they are: {', '.join(profile.nodes)}")
    return node


def _target_service(data_root: Path, node: str | None = None) -> dict[str, Any]:
    result = _compose(data_root, "config", "--format", "json", capture=True)
    if result.returncode != 0:
        raise DevnetError(result.stderr.strip() or "could not read the devnet's configuration")
    return json.loads(result.stdout)["services"][_freeze_node(data_root, node)]


def state_volume(data_root: Path, mount: str = "/srv/amaru", node: str | None = None) -> tuple[str, str]:
    """The volume holding a Freeze node's stored chain, and an image with a shell to copy it."""
    service = _target_service(data_root, node)
    for volume in service.get("volumes", []):
        if volume.get("target") == mount and volume.get("type") == "volume":
            return f"{PROJECT}_{volume['source']}", service["image"]
    raise DevnetError(f"the Freeze node has no volume mounted at {mount}")


def snapshots_dir(data_root: Path) -> Path:
    return directory(data_root) / "snapshots"


def snapshots(data_root: Path) -> list[dict[str, Any]]:
    folder = snapshots_dir(data_root)
    found = []
    for meta in sorted(folder.glob("*.json")) if folder.is_dir() else []:
        found.append(json.loads(meta.read_text(encoding="utf-8")))
    return found


def _copy(image: str, mounts: list[str], script: str) -> str:
    command = ["docker", "run", "--rm", "--entrypoint", "/bin/sh"]
    for mount in mounts:
        command += ["-v", mount]
    result = subprocess.run([*command, image, "-ec", script], capture_output=True, text=True)
    if result.returncode != 0:
        raise DevnetError(f"copying the node's data failed: {result.stderr.strip()[:300]}")
    return result.stdout


def _snapshot_name(name: str) -> str:
    if not SNAPSHOT_NAME.fullmatch(name):
        raise DevnetError("a snapshot needs a simple name: letters, digits, dot, underscore, and hyphen only")
    return name


def snapshot_save(data_root: Path, name: str, note: str = "", node: str | None = None) -> dict[str, Any]:
    """Stop a Freeze node, copy its stored chain aside, start it again."""
    name = _snapshot_name(name)
    info = current(data_root)
    if info is None:
        raise DevnetError("the devnet is not running")
    volume, image = state_volume(data_root, node=node)
    folder = snapshots_dir(data_root)
    folder.mkdir(parents=True, exist_ok=True)
    target = _freeze_node(data_root, node)
    if _compose(data_root, "stop", target, capture=True).returncode != 0:
        raise DevnetError(f"could not stop {target}")
    try:
        # The node's own image is used for the copy, so nothing has to be pulled; it has no tar,
        # hence a plain copy into a folder.
        destination = f"/to/{name}"
        size = _copy(
            image,
            [f"{volume}:/from:ro", f"{folder}:/to"],
            f"rm -rf -- {shlex.quote(destination)}"
            f" && mkdir -p -- {shlex.quote(destination)}"
            f" && cp -a -- {shlex.quote('/from/.')} {shlex.quote(destination + '/')}"
            f" && du -sk -- {shlex.quote(destination)} | cut -f1",
        )
    finally:
        _compose(data_root, "start", target, capture=True)
    meta = {"name": name, "note": note, "profile": info.get("profile", "relay"), "target": target, "image": info["image"],
            "bytes": int(size.strip() or 0) * 1024, "saved_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    (folder / f"{name}.json").write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
    return meta


def snapshot_restore(data_root: Path, name: str, frozen: bool = True, node: str | None = None) -> dict[str, Any]:
    """Put a saved chain back in place of a Freeze node's current one and restart the node."""
    name = _snapshot_name(name)
    folder = snapshots_dir(data_root)
    if not (folder / f"{name}.json").is_file() or not (folder / name).is_dir():
        raise DevnetError(f"no snapshot named `{name}`; there are: {', '.join(s['name'] for s in snapshots(data_root)) or 'none'}")
    saved_of = json.loads((folder / f"{name}.json").read_text(encoding="utf-8")).get("target")
    target = _freeze_node(data_root, node or (saved_of if saved_of in active(data_root).nodes else None))
    node = target
    if saved_of and saved_of != target:
        raise DevnetError(f"snapshot `{name}` is of {saved_of}, not of {target}; a node only takes its own chain back")
    volume, image = state_volume(data_root, node=node)
    if _compose(data_root, "stop", target, capture=True).returncode != 0:
        raise DevnetError(f"could not stop {target}")
    source = f"/from/{name}/."
    _copy(
        image,
        [f"{volume}:/to", f"{folder}:/from:ro"],
        f"find {shlex.quote('/to')} -mindepth 1 -maxdepth 1 -exec rm -rf -- {{}} +"
        f" && cp -a -- {shlex.quote(source)} {shlex.quote('/to/')}",
    )
    return restart_target(data_root, frozen=frozen, node=target)


START = "start"
"""The snapshot taken by itself before the node's first step, which `rewind` goes back to."""


def start_name(data_root: Path, node: str | None = None) -> str:
    """What a node's start snapshot is called: `start` for the main node, `start-<service>` for another."""
    target = _freeze_node(data_root, node)
    return START if target == active(data_root).target else f"{START}-{target}"


def start_snapshot_due(data_root: Path, node: str | None = None) -> bool:
    """Whether this run of the devnet was started frozen and this node has no snapshot of its start yet."""
    info = current(data_root, direct=True)
    if info is None or not info.get("frozen_start"):
        return False
    wanted = start_name(data_root, node)
    saved = next((meta for meta in snapshots(data_root) if meta["name"] == wanted), None)
    return saved is None or saved["saved_at"] < info.get("started_at", "")


def snapshot_delete(data_root: Path, name: str) -> None:
    name = _snapshot_name(name)
    folder = snapshots_dir(data_root)
    if (folder / name).is_dir():
        # The copy belongs to the container's user, so a container removes it.
        _, image = state_volume(data_root)
        _copy(image, [f"{folder}:/to"], f"rm -rf -- {shlex.quote(f'/to/{name}')}")
    if (folder / f"{name}.json").exists():
        (folder / f"{name}.json").unlink()


def target_container(data_root: Path, node: str | None = None) -> str:
    result = _compose(data_root, "ps", "-q", node or active(data_root).target, capture=True)
    container = result.stdout.strip().splitlines()[0] if result.stdout.strip() else ""
    if not container:
        raise DevnetError("the Freeze node's container is not running")
    return container
