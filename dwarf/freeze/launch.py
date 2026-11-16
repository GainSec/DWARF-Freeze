"""Start and stop a standalone Freeze node in a container.

Freeze's own launcher. It does not change DWARF's topology generator; it only runs containers.
"""

from __future__ import annotations

import json
import secrets
import socket
import subprocess
import time
from pathlib import Path
from typing import Any

AGENT_PORT = 7373


class LaunchError(Exception):
    pass


def _docker(*args: str, check: bool = True, capture: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(["docker", *args], capture_output=capture, text=True)
    if check and result.returncode != 0:
        raise LaunchError(f"docker {' '.join(args[:2])} failed: {(result.stderr or '').strip()}")
    return result


def container_name(network: str) -> str:
    return f"dwarf-freeze-{network.replace('_', '-')}"


def run_directory(data_root: Path, name: str) -> Path:
    return data_root / "run" / name


def exists(name: str) -> bool:
    return _docker("container", "inspect", name, check=False).returncode == 0


def subcommand(image: str, what: str) -> list[str]:
    """The words that start (`run`) or bootstrap (`bootstrap`) the node in this image. Later
    revisions of the node moved them under another command; the build records which."""
    found = _docker("image", "inspect", "--format", f'{{{{ index .Config.Labels "dwarf.freeze.{what}" }}}}', image, check=False)
    words = found.stdout.strip().split() if found.returncode == 0 else []
    return words or [what]


def bootstrap(image: str, network: str, volume: str, log: Any = print) -> None:
    """Download the snapshots a fresh node needs. Skipped when the volume already has a ledger."""
    probe = _docker("run", "--rm", "-v", f"{volume}:/data", "--entrypoint", "sh", image, "-c",
                    f"test -d /data/ledger.{network}.db", check=False)
    if probe.returncode == 0:
        return
    log(f"bootstrapping {network} (downloads snapshots; a few minutes)…")
    result = subprocess.run(["docker", "run", "--rm", "-v", f"{volume}:/data", image, *subcommand(image, "bootstrap"), "--network", network])
    if result.returncode != 0:
        raise LaunchError("bootstrap failed (output above)")


def start(
    image: str,
    network: str,
    data_root: Path,
    port: int = AGENT_PORT,
    peers: list[str] | None = None,
    frozen: bool = True,
    extra_args: list[str] | None = None,
    log: Any = print,
    brokered: bool = True,
    record: str = "held",
) -> dict[str, Any]:
    """Start the node and return how to reach it: host, port, token, container.

    ``port`` is the agent's. With ``brokered`` a broker is started in front of it, which records
    the session and lets one client claim it; ``broker_port`` is where it listens. ``record`` is
    what it keeps on disk: a node on a public network produces far too much to keep all of it,
    so the default is what the clients are sent (held items and verdicts always).
    """
    name = container_name(network)
    if exists(name):
        raise LaunchError(f"container {name} already exists; `freeze-debugger stop --network {network}` first")
    volume = f"{name}-data"
    bootstrap(image, network, volume, log)
    token = secrets.token_urlsafe(24)
    directory = run_directory(data_root, name)
    directory.mkdir(parents=True, exist_ok=True)
    token_file = directory / "token"
    token_file.touch(mode=0o600)
    token_file.chmod(0o600)
    token_file.write_text(token, encoding="utf-8")

    command = [
        "run", "-d", "--name", name,
        "--label", "dwarf.freeze.run=1",
        "-p", f"127.0.0.1:{port}:{AGENT_PORT}",
        "-v", f"{volume}:/data",
        "-e", f"DWARF_FREEZE_LISTEN=0.0.0.0:{AGENT_PORT}",
        "-e", f"DWARF_FREEZE_TOKEN={token}",
        "-e", "AMARU_NO_TUI=true",
    ]
    if frozen:
        command += ["-e", "DWARF_FREEZE_START=frozen"]
    command += [image, *subcommand(image, "run"), "--network", network]
    for peer in peers or []:
        command += ["--peer-address", peer]
    command += extra_args or []
    _docker(*command)
    info = {"container": name, "host": "127.0.0.1", "port": port, "token_file": str(token_file), "image": image,
            "network": network, "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    if brokered:
        from . import broker

        info["broker_port"] = port + broker.OFFSET
        broker.start(directory, name, ["--port", str(port), "--listen", str(info["broker_port"]), "--token-file", str(token_file),
                                       "--sessions", str(data_root / "sessions"), "--record", record])
    (directory / "run.json").write_text(json.dumps(info, indent=2) + "\n", encoding="utf-8")
    return {**info, "token": token}


def wait_ready(host: str, port: int, container: str, timeout: float = 120.0) -> None:
    """Wait until the agent accepts connections; fail early if the container died."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        state = _docker("inspect", "--format", "{{.State.Running}}", container, check=False)
        if state.returncode != 0 or state.stdout.strip() != "true":
            tail = _docker("logs", "--tail", "20", container, check=False)
            raise LaunchError(f"the node stopped during start-up:\n{(tail.stdout + tail.stderr).strip()}")
        try:
            with socket.create_connection((host, port), timeout=1):
                return
        except OSError:
            time.sleep(0.5)
    raise LaunchError(f"the agent did not start listening on {host}:{port} within {timeout:.0f}s")


def stop(network: str, data_root: Path, remove_data: bool = False) -> bool:
    from . import broker

    name = container_name(network)
    broker.stop(run_directory(data_root, name), name)
    found = exists(name)
    if found:
        _docker("rm", "-f", name)
    if remove_data:
        _docker("volume", "rm", "-f", f"{name}-data", check=False)
    run_file = run_directory(data_root, name) / "run.json"
    if run_file.exists():
        run_file.unlink()
    return found


def current(network: str, data_root: Path, direct: bool = False) -> dict[str, Any] | None:
    """Connection details of the running node for ``network``, if there is one.

    ``port`` is where to connect: the node's broker when one is running, unless ``direct`` asks
    for the agent itself. ``agent_port`` is always the agent's.
    """
    from . import broker

    name = container_name(network)
    run_file = run_directory(data_root, name) / "run.json"
    if not run_file.exists() or not exists(name):
        return None
    info = json.loads(run_file.read_text(encoding="utf-8"))
    info["token"] = Path(info["token_file"]).read_text(encoding="utf-8").strip()
    info["agent_port"] = info["port"]
    if not direct and info.get("broker_port") and broker.running(run_directory(data_root, name), name) is not None:
        info["port"] = info["broker_port"]
    return info


def restart(network: str, data_root: Path, frozen: bool = True, log: Any = print) -> dict[str, Any]:
    """Stop the node for ``network`` and start it again from its stored data, with the same image and port."""
    info = current(network, data_root)
    if info is None:
        raise LaunchError(f"no Freeze node is running for {network}; start one with `freeze-debugger run`")
    stop(network, data_root)
    started = start(info["image"], network, data_root, info["agent_port"], frozen=frozen, log=log,
                    brokered=bool(info.get("broker_port")))
    wait_ready(started["host"], started["port"], started["container"])
    return {**started, "port": reachable(started)}


def reachable(info: dict[str, Any], timeout: float = 30.0) -> int:
    """The port to connect to once the node is up: its broker's when that answers, else the agent's."""
    from .client import FreezeClient, FreezeError

    if not info.get("broker_port"):
        return info["port"]
    deadline = time.time() + timeout
    while time.time() < deadline:
        client = FreezeClient(info["host"], info["broker_port"], token=info.get("token"), timeout=3.0)
        try:
            client.connect()
            return info["broker_port"]
        except (OSError, FreezeError):
            time.sleep(0.5)
        finally:
            client.close()
    return info.get("agent_port", info["port"])
