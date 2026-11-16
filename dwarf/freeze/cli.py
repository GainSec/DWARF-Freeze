"""Command line for DWARF Freeze.

    freeze-debugger demo                      try the UI on a recording of a real node; needs no node
    freeze-debugger build --source DIR        build a node image with the Freeze agent compiled in
    freeze-debugger run --network preprod     start that node, frozen, and open the UI on it
    freeze-debugger attach                    open the UI on a running Freeze node
    freeze-debugger script decisions.py       intercept items with a local Python callback
    freeze-debugger watch                     print what the node does, one line per step
    freeze-debugger stop --network preprod    stop the node

A closed local devnet, where tampering touches nothing outside this machine:

    freeze-debugger build --static --source DIR
    freeze-debugger devnet up                 three producers, two Amaru relays, one of them Freeze
    freeze-debugger devnet profiles           other devnets to start with `devnet up --profile NAME`
    freeze-debugger devnet attach             wait until it is ready, then open the UI
    freeze-debugger devnet down
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

from .client import FreezeClient, FreezeError

DEFAULT_PORT = 7373


def data_root() -> Path:
    base = os.environ.get("DWARF_DATA_ROOT") or os.path.join(
        os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share"), "dwarf"
    )
    return Path(base) / "freeze"


def _token(args: argparse.Namespace) -> str | None:
    if args.token_file:
        return Path(args.token_file).read_text(encoding="utf-8").strip()
    return args.token or os.environ.get("DWARF_FREEZE_TOKEN") or None


def _connect(args: argparse.Namespace) -> FreezeClient:
    token, port = _token(args), args.port
    if token is None and getattr(args, "network", None) == "devnet":
        from . import devnet

        try:
            running = devnet.current(data_root(), getattr(args, "node", None), direct=getattr(args, "direct", False))
        except devnet.DevnetError as error:
            raise SystemExit(f"freeze-debugger: {error}") from error
        if running is not None:
            token, port = running["token"], running["port"]
    elif token is None and getattr(args, "network", None):
        from . import launch

        running = launch.current(args.network, data_root(), direct=getattr(args, "direct", False))
        if running is not None:
            token, port = running["token"], running["port"]
    client = FreezeClient(args.host, port, token=token, timeout=args.timeout)
    client.connect()
    return client


def cmd_build(args: argparse.Namespace) -> int:
    from . import build

    try:
        record = build.build(Path(args.source), data_root(), args.recipe, args.tag, args.platform, static=args.static,
                             debug_info=args.debug_info)
    except build.BuildError as error:
        print(f"freeze-debugger: {error}", file=sys.stderr)
        return 1
    print(f"built {record['image']} in {record['seconds']}s")
    return 0


def cmd_port(args: argparse.Namespace) -> int:
    """Bring the hooks to a revision of the node that has no stored patch series."""
    from . import applier, build

    source = Path(args.source).resolve()
    available = build.recipes()
    # The spec describes the hooks as written for one revision; that is the one to merge from.
    wanted = args.recipe or applier.load_spec()[1]["base"]
    recipe = next((entry for entry in available if entry.name == wanted), None)
    if recipe is None:
        print(f"freeze-debugger: no recipe named {wanted!r}; there are: {', '.join(r.name for r in available)}", file=sys.stderr)
        return 1
    base = Path(args.base) if args.base else build.HERE.parent / "testplease" / recipe.implementation / recipe.manifest["source"]
    if not (base / "Cargo.toml").is_file():
        print(f"freeze-debugger: the tree the hooks were written for is needed to merge from, and {base} is not it; "
              "give it with --base", file=sys.stderr)
        return 1
    output = build.TARGETS / recipe.implementation / "patches" if args.shipped else build.ported_dir(data_root(), recipe.implementation)
    print(f"porting the `{recipe.name}` hooks to {source.name}")
    try:
        if args.resolved:
            report = applier.finish(source, Path(args.resolved), output, recipe, args.name)
        else:
            keep = args.by_hand or args.keep
            report = applier.port(source, base, output, recipe, args.name, keep=Path(keep) if keep else None,
                                  by_hand=bool(args.by_hand))
    except (applier.PortError, build.BuildError) as error:
        print(f"freeze-debugger: {error}", file=sys.stderr)
        return 1
    if report.conflicts:
        print(f"both upstream and the hooks changed the same places in these files; they are in {args.by_hand} with the conflicts marked:")
        for group, files in report.conflicts.items():
            for file in files:
                print(f"  [{group}] {file}")
        print(f"resolve them there (an optional group can also be put back as upstream has it), then:\n"
              f"  freeze-debugger port --source {args.source} --resolved {args.by_hand}")
        return 2
    for line in report.lines():
        print(line)
    print(f"stored as recipe `{report.recipe}` in {output}; `freeze-debugger build --source {args.source}` now uses it")
    return 0


def _latest_image() -> str | None:
    from . import build

    records = [record for record in build.builds(data_root()) if not record.get("static")]
    return records[0]["image"] if records else None


def cmd_run(args: argparse.Namespace) -> int:
    from . import launch

    image = args.image or _latest_image()
    if image is None:
        print("freeze-debugger: no Freeze image yet; run `freeze-debugger build --source <amaru tree>` first", file=sys.stderr)
        return 1
    try:
        info = launch.start(image, args.network, data_root(), args.port, args.peer, not args.thawed, args.node_arg,
                            brokered=not args.no_broker, record=args.record)
        launch.wait_ready(info["host"], info["port"], info["container"])
    except launch.LaunchError as error:
        print(f"freeze-debugger: {error}", file=sys.stderr)
        return 1
    print(f"node {info['container']} is up{' and frozen' if not args.thawed else ''}; agent on 127.0.0.1:{info['port']}")
    if args.no_attach:
        print(f"attach with: freeze-debugger attach --network {args.network}")
        return 0
    client = FreezeClient(info["host"], launch.reachable(info), token=info["token"], timeout=15.0, name="ui")
    client.connect()

    def debugger(_name: str | None) -> Any:
        return _source_debugger(argparse.Namespace(network=args.network, node=None, timeout=60.0))

    code = _run_ui(client, record=True, source=debugger)
    print(f"the node is still running. Stop it with: freeze-debugger stop --network {args.network}")
    return code


def cmd_devnet(args: argparse.Namespace) -> int:
    from . import build, devnet

    root = data_root()
    try:
        if args.devnet_command == "up":
            image = args.image
            if image is None:
                static = [record for record in build.builds(root) if record.get("static")]
                if not static:
                    print("freeze-debugger: no static Freeze image yet; run "
                          "`freeze-debugger build --static --source <amaru tree>` first", file=sys.stderr)
                    return 1
                image = static[0]["image"]
            profile = None
            if args.profile or args.compose or args.target or devnet.current(root) is None:
                profile = devnet.find_profile(args.profile, root, args.compose, args.target, args.also)
            info = devnet.up(image, root, args.port, args.frozen, args.peer_cooldown, profile, args.resign,
                             brokered=not args.no_broker, auto_pause=not args.no_auto_pause, record=args.record,
                             freeze_together=args.freeze_together)
            print(f"devnet `{info['profile']}` started with {image} in {info['target']}.")
            print(f"Edited-header re-signing is {'on' if info['resign'] else 'off'}.")
            print("The Amaru relays first wait for three epochs of chain and bootstrap from it; allow 15 minutes or more.")
            print(f"Then: freeze-debugger devnet attach   (agent on 127.0.0.1:{info['port']})")
            for name, node_port in info["nodes"].items():
                if name != info["target"]:
                    print(f"      freeze-debugger devnet attach --node {name}   (agent on 127.0.0.1:{node_port})")
        elif args.devnet_command == "status":
            info = devnet.current(root)
            for row in devnet.services(root):
                print(f"  {row['service']:<24} {row['state']:<10} {row['status']}")
            if info is None:
                print("not started by Freeze")
            else:
                print(f"profile: {info.get('profile', 'relay')}")
                for name in info["nodes"]:
                    node = devnet.current(root, name)
                    print(f"Freeze agent in {name}: {'ready' if devnet.agent_ready(node) else 'not ready yet'} on 127.0.0.1:{node['port']}")
                    print(f"  {name}: {devnet.target_log(root, 1, name)[:200]}")
        elif args.devnet_command == "profiles":
            running = devnet.current(root)
            print("Profiles (start one with `freeze-debugger devnet up --profile NAME`):")
            for profile in devnet.profiles(root):
                mark = "  ← running" if running and running.get("profile") == profile.name else ""
                print(f"  {profile.name:<24} Freeze node: {profile.target:<16} [{profile.origin}]{mark}")
                print(f"  {'':<24} {profile.about}")
            print("\nDWARF layouts a Freeze node fits into (`--compose FILE --target SERVICE`):")
            for compose, names in devnet.topologies():
                print(f"  {compose.relative_to(devnet.REPO)}")
                print(f"  {'':<4}can be the Freeze node: {', '.join(names)}")
            print(f"\nYour own profiles go in {root / 'profiles'} as YAML: name, about, compose, target,")
            print("network, peer_cooldown, environment (extra variables for the Freeze node).")
        elif args.devnet_command == "nodes":
            info = devnet.current(root)
            if info is None:
                raise devnet.DevnetError("the devnet is not running; start it with `freeze-debugger devnet up`")
            for name in info["nodes"]:
                node = devnet.current(root, name)
                if not devnet.agent_ready(node):
                    print(f"  {name:<20} 127.0.0.1:{node['port']}   not ready yet")
                    continue
                client = FreezeClient(node["host"], node["port"], token=node["token"], timeout=15.0)
                client.connect()
                try:
                    # Freezing or thawing every node in one go keeps them in step with each other.
                    status = client.freeze() if args.freeze else client.thaw() if args.thaw else client.status()
                finally:
                    client.close()
                state = "FROZEN" if status.get("frozen") else "running"
                print(f"  {name:<20} 127.0.0.1:{node['port']}   {state:<8} step {status.get('seq', 0):,}   held {len(status.get('held', []))}")
        elif args.devnet_command == "attach":
            info = devnet.wait_ready(root, args.wait, node=args.node)

            def reach(name: str) -> FreezeClient:
                where = devnet.current(root, name, direct=args.direct)
                assert where is not None
                other = FreezeClient(where["host"], where["port"], token=where["token"], timeout=15.0, name="ui")
                other.connect()
                return other

            several = len(info.get("nodes", {})) > 1
            nodes = {}
            for name in info["nodes"] if several else []:
                where = devnet.current(root, name, direct=args.direct)
                nodes[name] = {"host": where["host"], "port": where["port"], "token": where["token"]}
            def debugger(name: str | None) -> Any:
                return _source_debugger(argparse.Namespace(network="devnet", node=name or info["target"], timeout=60.0))

            return _run_ui(reach(info["target"]), record=True, node=info["target"] if several else None, nodes=nodes,
                           reconnect=reach, source=debugger)
        elif args.devnet_command in ("pause-peers", "resume-peers"):
            pause = args.devnet_command == "pause-peers"
            info = devnet.current(root)
            if info is not None and info.get("brokered"):
                # Through the broker, so that it knows the state it is supposed to keep.
                with FreezeClient(info["host"], info["port"], token=info["token"], timeout=60.0, name="cli") as client:
                    client.peers_auto(False)
                    client.peers_pause() if pause else client.peers_resume()
                print("the broker no longer pauses the other nodes by itself; `devnet broker --auto-pause on` turns that back on")
            elif pause:
                devnet.pause_peers(root)
            else:
                devnet.resume_peers(root)
            print("the other nodes are paused" if pause else "the other nodes are running")
        elif args.devnet_command == "broker":
            from . import broker

            info = devnet.current(root, direct=True)
            if info is None:
                raise devnet.DevnetError("the devnet is not running; start it with `freeze-debugger devnet up`")
            home = devnet.directory(root)
            if args.action == "stop":
                devnet.stop_brokers(root)
                print("brokers stopped; clients now talk to the agents directly")
            elif args.action == "start":
                run_file = home / "run.json"
                saved = json.loads(run_file.read_text(encoding="utf-8"))
                saved["brokers"] = devnet.start_brokers(root, {**saved, "nodes": info["nodes"]}, args.auto_pause != "off")
                run_file.write_text(json.dumps(saved, indent=2) + "\n", encoding="utf-8")
            for name, port in (devnet.current(root, direct=True) or {}).get("brokers", {}).items():
                pid = broker.running(home, name)
                if pid is None:
                    print(f"  {name:<20} broker not running")
                    continue
                line = f"  {name:<20} broker on 127.0.0.1:{port} (pid {pid})"
                try:
                    with FreezeClient("127.0.0.1", port, token=info["token"], timeout=30.0, name="cli") as client:
                        if args.auto_pause and name == info["target"]:
                            client.peers_auto(args.auto_pause == "on")
                        about, who = client.peers_status(), client.who()
                    if about["available"]:
                        line += f"   pauses the other nodes: {'yes' if about['auto'] else 'no'}{', paused now' if about['paused'] else ''}"
                    line += f"   clients: {len(who['clients']) - 1}" + (f", claimed by client {who['owner']}" if who["owner"] else "")
                    if about.get("error"):
                        line += f"   last error: {about['error']}"
                except (OSError, FreezeError) as error:
                    line += f"   node not answering yet ({error})"
                print(line)
        elif args.devnet_command == "tx":
            from . import devnet_tx

            wallet = devnet_tx.Wallet(root, args.via, args.key)
            for number in range(args.count):
                if args.what == "pay":
                    print(f"payment sent: {devnet_tx.pay(wallet, args.lovelace)}")
                else:
                    locked, spent = devnet_tx.script(wallet, args.lovelace)
                    print(f"script output spent: {spent} (it was locked by {locked})")
        elif args.devnet_command == "timeline":
            from . import conditions

            info = devnet.current(root)
            if info is None:
                raise devnet.DevnetError("the devnet is not running; start it with `freeze-debugger devnet up`")
            wanted = conditions.compile_condition(args.when) if args.when else None
            merged = []
            for name in info["nodes"]:
                recorded = sorted((root / "sessions").glob(f"*-{name}/events.jsonl"))
                if not recorded:
                    print(f"  ({name}: nothing recorded yet)")
                    continue
                with recorded[-1].open("r", encoding="utf-8") as lines:
                    for line in lines:
                        if '"event":"item"' not in line:
                            continue
                        item = json.loads(line)["data"]
                        if wanted is None or conditions.matches(wanted, item):
                            merged.append((item.get("t_ms", 0), name, item))
            merged.sort(key=lambda entry: entry[0])
            for at, name, item in merged[-args.last:]:
                stamp = time.strftime("%H:%M:%S", time.localtime(at / 1000)) + f".{at % 1000:03d}"
                print(f"  {stamp}  {name:<16} {item['seq']:>8}  {item['kind']:<16} {item.get('from', ''):<28} → {item.get('to', ''):<28} {item.get('label', '')}")
        elif args.devnet_command == "snapshot":
            if args.snapshot_command == "save":
                meta = devnet.snapshot_save(root, args.name, args.note or "", node=args.node)
                print(f"saved `{meta['name']}` of {meta['target']} ({meta['bytes'] / 1e6:.1f} MB); that node was restarted")
            elif args.snapshot_command == "restore":
                devnet.snapshot_restore(root, args.name, frozen=not args.running, node=args.node)
                print(f"the Freeze node now has the chain from `{args.name}`" + ("" if args.running else ", and is frozen at its first step"))
            elif args.snapshot_command == "delete":
                devnet.snapshot_delete(root, args.name)
                print("deleted")
            else:
                for meta in devnet.snapshots(root):
                    print(f"  {meta['name']:<24} {meta['saved_at']}  {meta['bytes'] / 1e6:>8.1f} MB  {meta['profile']}/{meta['target']}  {meta.get('note', '')}")
        elif args.devnet_command == "stats":
            from . import resources

            container = devnet.target_container(root)
            samples = resources.watch(container, args.every, args.duration, lambda s: print(
                f"  {time.strftime('%H:%M:%S', time.localtime(s['at']))}  memory {s['memory_bytes'] / 1e6:>8.1f} MB   cpu {s['cpu_percent']:>6.1f}%   threads {s['pids']}", flush=True))
            print(resources.verdict(samples))
        elif args.devnet_command == "down":
            devnet.down(root, args.keep_data)
            print("devnet stopped")
    except devnet.DevnetError as error:
        print(f"freeze-debugger: {error}", file=sys.stderr)
        return 1
    return 0


def _session_dir(name: str | None) -> Path:
    from . import runs

    root = data_root() / "sessions"
    if name and Path(name).is_dir():
        return Path(name)
    if name and (root / name).is_dir():
        return root / name
    recorded = runs.sessions(root)
    if name or not recorded:
        raise FreezeError(f"no recorded session {name!r}" if name else "no recorded sessions yet")
    return recorded[0]["path"]


def cmd_sessions(args: argparse.Namespace) -> int:
    from . import runs

    recorded = runs.sessions(data_root() / "sessions")
    if not recorded:
        print("no recorded sessions yet (attach or run records one)")
    for entry in recorded:
        node = entry["node"]
        print(f"{entry['name']}  {entry['items']:>8,} items  {entry['tampers']:>3} tampers  "
              f"{node.get('implementation', '')} {node.get('network', '')}")
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    from . import runs

    try:
        source = _session_dir(args.session)
        output = Path(args.output) if args.output else Path(f"freeze-run-{source.name}.tar.gz")
        manifest = runs.export_run(source, output, args.until, args.note or "")
    except runs.RunError as error:
        print(f"freeze-debugger: {error}", file=sys.stderr)
        return 1
    upto = f" up to item {manifest['until']}" if manifest["until"] is not None else ""
    print(f"wrote {output}: {manifest['items']:,} items{upto}, {manifest['interventions']} intervention(s)")
    return 0


def _load_run(path_text: str):
    from . import runs

    path = Path(path_text)
    if not path.exists() and (data_root() / "runs" / path_text).is_dir():
        path = data_root() / "runs" / path_text
    return runs.load_run(path, data_root() / "runs")


def cmd_import(args: argparse.Namespace) -> int:
    from . import runs

    try:
        manifest, steps, directory = _load_run(args.archive)
    except runs.RunError as error:
        print(f"freeze-debugger: {error}", file=sys.stderr)
        return 1
    node = manifest.get("node", {})
    print(f"run {directory.name}: {node.get('implementation', 'node')} {node.get('version', '')} {node.get('network', '')}, "
          f"{manifest.get('items', 0):,} items, {len(steps)} intervention(s)")
    for number, step in enumerate(steps, start=1):
        print(f"  {number}. {runs.describe(step)}")
    print(f"stored in {directory}")
    print(f"look at it:   freeze-debugger open {directory.name}")
    print(f"do it again:  freeze-debugger ingest {directory.name} --network <devnet|preprod|...>")
    return 0


def cmd_open(args: argparse.Namespace) -> int:
    """Browse an imported run in the UI. Nothing is connected to a node."""
    from . import runs
    from .testing.fake_agent import FakeAgent, replay

    try:
        manifest, _steps, directory = _load_run(args.archive)
    except runs.RunError as error:
        print(f"freeze-debugger: {error}", file=sys.stderr)
        return 1
    events = runs.read_events(directory / "events.jsonl")
    node = dict(manifest.get("node", {}))
    node["implementation"] = f"{node.get('implementation', 'node')} (recorded run)"
    agent = FakeAgent(start_frozen=False, info=node)
    agent.start()
    replay(agent, events, delay=0.0, holdable=False)
    client = FreezeClient("127.0.0.1", agent.port, timeout=10)
    client.connect()
    try:
        return _run_ui(client, record=False)
    finally:
        agent.stop()


def cmd_ingest(args: argparse.Namespace) -> int:
    """Restart the node and repeat what a recorded run did to it."""
    from . import devnet, launch, runs

    try:
        manifest, steps, directory = _load_run(args.archive)
    except runs.RunError as error:
        print(f"freeze-debugger: {error}", file=sys.stderr)
        return 1
    print(f"run {directory.name}: {len(steps)} intervention(s)")
    try:
        if args.no_restart:
            client = _connect(args)
        elif args.network == "devnet":
            print("restarting the devnet's Freeze node, frozen…")
            if getattr(args, "snapshot", None):
                print(f"putting back the chain saved as `{args.snapshot}`…")
                info = devnet.snapshot_restore(data_root(), args.snapshot, frozen=True)
            else:
                info = devnet.restart_target(data_root(), frozen=True)
            client = FreezeClient(info["host"], info["port"], token=info["token"], timeout=args.timeout)
            client.connect()
        elif args.network:
            print(f"restarting the {args.network} node, frozen…")
            info = launch.restart(args.network, data_root(), frozen=True)
            client = FreezeClient(info["host"], info["port"], token=info["token"], timeout=args.timeout)
            client.connect()
        else:
            print("freeze-debugger: say which node with --network, or use --no-restart with --port/--token", file=sys.stderr)
            return 1
    except (devnet.DevnetError, launch.LaunchError) as error:
        print(f"freeze-debugger: {error}", file=sys.stderr)
        return 1
    replay_options = (
        {"stale_negative_control": True}
        if getattr(args, "stale_negative_control", False)
        else {}
    )
    done = runs.Replayer(client, steps, **replay_options).run(timeout=args.wait)
    failed = [entry for entry in done if not entry["outcome"].startswith(("edited", "dropped"))]
    print(f"{len(done) - len(failed)} of {len(steps)} intervention(s) repeated; the node is frozen at that point")
    if args.no_attach:
        client.close()
        return 1 if failed or len(done) < len(steps) else 0
    return _run_ui(client, record=True)


def _source_debugger(args: argparse.Namespace):
    """A debugger beside the node `args` names, not yet attached."""
    from . import devnet, launch, source

    if args.network in (None, "devnet"):
        info = devnet.current(data_root(), getattr(args, "node", None), direct=True)
        if info is None:
            raise source.SourceError("the devnet is not running; say which node with --network")
        container, image = devnet.target_container(data_root(), info["target"]), info["image"]
    else:
        info = launch.current(args.network, data_root())
        if info is None:
            raise source.SourceError(f"no node started by `freeze-debugger run --network {args.network}`")
        container, image = info["container"], info["image"]
    if not source.have_image(source.tools_image(image)):
        raise source.SourceError(
            f"the node's image {image} was built without debug information. Build one with "
            "`freeze-debugger build --debug-info` (add --static for the devnet) and start the node from it")
    return source.Debugger(source.command_for(container, image), timeout=args.timeout)


def _print_stop(debugger: Any, stop: dict[str, Any], around: int = 4) -> None:
    from . import source

    at = stop.get("at") or {}
    print(f"stopped: {stop.get('reason') or 'stopped'} · thread {stop.get('thread')} {stop.get('name', '')}")
    print(f"  in {at.get('function', '?')}")
    if at.get("line"):
        print(f"  at {at['file']}:{at['line']}")
        try:
            for number, text, current in source.listing(debugger.call("source", file=at["file"], line=at["line"], around=around), at["line"]):
                print(f"  {'→' if current else ' '} {number:>5}  {text}")
        except source.SourceError as error:
            print(f"  (source not shown: {error})")


def _source_command(debugger: Any, line: str, wait: float) -> bool:
    """Carry out one line typed at the `source` prompt. Returns False when it is time to leave."""
    word, _, rest = line.strip().partition(" ")
    rest = rest.strip()
    short = {"b": "break", "c": "continue", "s": "step", "n": "next", "o": "out", "w": "where", "v": "vars",
             "l": "list", "i": "interrupt", "q": "detach", "quit": "detach", "bt": "where"}
    word = short.get(word, word)
    if word == "detach":
        return False
    if word == "break":
        place, _, number = rest.rpartition(":")
        answer = debugger.call("break", file=place, line=int(number)) if place and number.isdigit() else debugger.call("break", function=rest)
        print(f"breakpoint {answer['id']} set ({answer['places']} place{'s' if answer['places'] != 1 else ''})")
    elif word == "breaks":
        for point in debugger.call("breaks"):
            print(f"  {point['id']:>3}  {point['file']}:{point['line']}  reached {point['hits']} time(s)")
    elif word == "unbreak":
        debugger.call("unbreak", **({"id": int(rest)} if rest else {}))
        print("removed")
    elif word in ("continue", "step", "next", "out", "interrupt"):
        debugger.call(word)
        try:
            _print_stop(debugger, debugger.wait_stopped(wait))
        except Exception as error:  # noqa: BLE001 - shown, the session goes on
            print(f"{error} (the node is running; `interrupt` stops it)")
    elif word == "where":
        stop = debugger.call("where")
        _print_stop(debugger, stop)
        for frame in stop["frames"]:
            print(f"  #{frame['index']:<3} {frame['function']}" + (f"   {frame['file']}:{frame['line']}" if frame["line"] else ""))
    elif word == "vars":
        # `vars` lists the locals; `vars NAME` opens one of them further; `vars N` is frame N.
        for value in debugger.call("vars", **({"frame": int(rest)} if rest.isdigit() else {"name": rest} if rest else {})):
            print(f"  {value['name']}: {value['type']} = {value['value']}")
    elif word == "threads":
        for thread in debugger.call("threads"):
            at = thread["at"]
            print(f"  {thread['thread']:>3} {thread['name']:<20} {at.get('function', '')}")
    elif word == "list":
        at = (debugger.stopped or {}).get("at") or {}
        if at.get("line"):
            from . import source

            for number, text, current in source.listing(debugger.call("source", file=at["file"], line=at["line"], around=int(rest or 12)), at["line"]):
                print(f"  {'→' if current else ' '} {number:>5}  {text}")
    elif word:
        print("break FILE:LINE | break FUNCTION | breaks | unbreak [N] | continue | step | next | out | interrupt | "
              "where | vars [FRAME or NAME] | threads | list [N] | detach")
    return True


def cmd_source(args: argparse.Namespace) -> int:
    """Step through the node's source lines with a debugger beside it."""
    from . import devnet, launch, source

    try:
        debugger = _source_debugger(args)
        debugger.start()
        about = debugger.attach()
    except (source.SourceError, devnet.DevnetError, launch.LaunchError) as error:
        print(f"freeze-debugger: {error}", file=sys.stderr)
        return 1
    print(f"attached to the node (process {about['pid']}, {about['threads']} threads, {about['source_files']:,} source files known). "
          "It is stopped, and so is its Freeze agent, until you `continue` or `detach`.")
    failed = False
    try:
        try:
            _print_stop(debugger, debugger.wait_stopped(args.timeout))
        except source.SourceError:
            pass
        for line in args.do or []:
            print(f"(source) {line}")
            try:
                if not _source_command(debugger, line, args.wait):
                    break
            except (source.SourceError, ValueError) as error:
                print(f"  {error}")
                failed = True
        while not args.do:
            try:
                line = input("(source) ")
            except (EOFError, KeyboardInterrupt):
                print()
                break
            try:
                if not _source_command(debugger, line, args.wait):
                    break
            except (source.SourceError, ValueError) as error:
                print(f"  {error}")
    finally:
        debugger.close()
        print("detached; the node runs on")
    return 1 if failed else 0


def cmd_rewind(args: argparse.Namespace) -> int:
    """Take the devnet's Freeze node back to an earlier item of a recorded session."""
    from . import devnet, runs

    root = data_root()
    try:
        info = devnet.current(root, args.node)
        if info is None:
            raise devnet.DevnetError("the devnet is not running; rewind works on the devnet's Freeze nodes")
        if args.snapshot is None:
            args.snapshot = devnet.start_name(root, args.node)
        if args.session:
            source = _session_dir(args.session)
        else:
            recorded = sorted(path.parent for path in (root / "sessions").glob("*/events.jsonl"))
            mine = [path for path in recorded if path.name.endswith(f"-{info['target']}")] or recorded
            if not mine:
                raise devnet.DevnetError("no recorded session to rewind in")
            source = mine[-1]
        if args.snapshot not in [meta["name"] for meta in devnet.snapshots(root)]:
            hint = " (it is saved by itself when the devnet is started with `devnet up --frozen`)" if args.snapshot.startswith(devnet.START) else ""
            raise devnet.DevnetError(f"no snapshot named `{args.snapshot}`{hint}")
        events = runs.read_events(source / "events.jsonl")
        about = json.loads((source / "session.json").read_text(encoding="utf-8")) if (source / "session.json").is_file() else {}
        goal = runs.target(events, args.to)
        if (about.get("first_step") or 0) > 50:
            print(f"note: this recording starts at step {about['first_step']}, not at the node's start; "
                  "what was changed before it is not known and is not repeated")
        print(f"session {source.name}: going back to item {args.to} ({goal['what'] or goal['when']}), "
              f"number {goal['before'] + 1} of its kind")
        print(f"putting back the chain saved as `{args.snapshot}` and restarting the node, frozen…")
        restarted = devnet.snapshot_restore(root, args.snapshot, frozen=True, node=args.node)
        client = FreezeClient(restarted["host"], restarted["port"], token=restarted["token"], timeout=args.timeout, name="rewind")
        client.connect()
        rewinder = runs.Rewinder(
            client,
            events,
            args.to,
            stale_negative_control=args.stale_negative_control,
        )
        print(f"repeating {len(rewinder.steps)} change(s) made before it, then running to it…")
        result = rewinder.run(timeout=args.wait, by_count=args.by_count)
    except (devnet.DevnetError, runs.RunError, FreezeError) as error:
        print(f"freeze-debugger: {error}", file=sys.stderr)
        return 1
    if result["reached"] is None and result.get("passed") is not None:
        print(f"not reached: the item went by as item {result['passed']} while the earlier changes were being repeated, "
              "because this run delivered it sooner than the recorded one did. The node is frozen where it got to.")
    elif result["reached"] is None:
        print(f"not reached: the node handled {result['seen']} item(s) of that kind"
              + ("" if args.by_count else " and none carried the same; `--by-count` stops at the same position instead")
              + ". The node is frozen where it got to.")
    else:
        print(f"the node is frozen with item {result['reached']} held: it corresponds to item {args.to} of the recording")
    if args.no_attach:
        client.close()
        return 0 if result["reached"] is not None else 1
    return _run_ui(client, record=True)


def cmd_template(args: argparse.Namespace) -> int:
    """Run a template against a node without the UI and print what happened."""
    from . import payloads, rules
    from .session import Session

    store = payloads.Store(data_root() / "payloads")
    try:
        template = rules.load_template(Path(args.template), store)
    except (OSError, ValueError) as error:
        print(f"freeze-debugger: {error}", file=sys.stderr)
        return 1
    client = _connect(args)
    session, directory = _recorded_session(client)
    engine = rules.Engine(client, session)
    print(f"template `{template.name}`: {len(template.rules)} rule(s)")
    try:
        if client.status().get("frozen"):
            print("the node is frozen, so nothing will reach the rules until it runs: "
                  "`freeze-debugger devnet nodes --thaw`, or `g` in the UI", file=sys.stderr)
        engine.arm_template(template)
        rows = engine.run(timeout=args.wait, log=lambda line: print("  " + line))
    finally:
        engine.stop()
        client.close()
        session.close()
    print(f"session recorded as {directory.name}")
    for row in rows:
        change = f"{row['before']} → {row['after']}" if row["action"] == "replace" else row["action"]
        print(f"{row['rule']} #{row['number']}  item {row['seq']}  {row['value']:<22} {change:<40} {row['node_said']}")
    if args.output:
        rules.write_report(rows, Path(args.output))
        print(f"results written to {args.output}")
    unfinished = [rule.name for rule in template.rules if not rule.finished and rule.total is not None]
    if unfinished:
        print(f"not finished before the wait ran out: {', '.join(unfinished)}")
    return 0


def cmd_scenarios(args: argparse.Namespace) -> int:
    """DWARF's scenarios: list them, show one, turn one into a template, or run one."""
    from . import payloads, rules, scenarios
    from .session import Session

    command = args.scenarios_command
    try:
        if command == "list":
            entries = scenarios.catalog()
            shown = [entry for entry in entries
                     if (args.all or entry.status != scenarios.NO)
                     and (not args.filter or args.filter.lower() in f"{entry.id} {entry.title} {entry.subject} {entry.implementation}".lower())]
            for entry in shown:
                print(f"{entry.status:<7} {entry.subject:<16} {entry.implementation:<20} {entry.id}")
                if entry.status == scenarios.NO and args.all:
                    print(f"        {entry.reason}")
            counts = scenarios.summary(entries)
            print(f"{len(entries)} scenarios: {counts['ready']} ready, {counts['partly']} partly, {counts['no']} not possible from inside a node"
                  + ("" if args.all else "  (use --all to list those too, with the reason)"))
            return 0
        if command == "lists":
            store = payloads.Store(data_root() / "payloads")
            for name, cases in scenarios.value_lists().items():
                store.save(payloads.PayloadList(name, [f"0x{text}" for text in cases.values()],
                                                "From DWARF: " + ", ".join(list(cases)[:6]) + (" …" if len(cases) > 6 else "")))
                print(f"saved `{name}` ({len(cases)} values)")
            return 0
        entry = scenarios.find(args.id)
        if command == "show":
            print(f"{entry.id}\n  {entry.title}\n  target {entry.implementation} · {entry.runtime} · about {entry.subject}")
            if entry.threats or entry.risks:
                print(f"  threats {', '.join(entry.threats) or '-'} · risks {', '.join(entry.risks) or '-'}")
            print(f"  workload: {', '.join(step.get('primitive', '') for step in entry.primitives)}")
            print(f"  asserts:  {', '.join(entry.assertions) or '-'}")
            print(f"  Freeze: {entry.status}" + (f" — {entry.reason}" if entry.reason else ""))
            if entry.status != scenarios.NO:
                for rule in scenarios.template_for(entry).rules:
                    print(f"    rule: {rule.name}\n          when {rule.when}"
                          + (f", after {rule.after}" if rule.after else "") + f", {rule.action}"
                          + (f" {rule.field} with {len(rule.values)} value(s)" if rule.action == "replace" else ""))
            return 0
        template = scenarios.template_for(entry)
        if command == "template":
            output = Path(args.output or f"{entry.id}.yaml")
            rules.save_template(template, output)
            print(f"wrote {output} ({len(template.rules)} rule(s))")
            return 0
        client = _connect(args)
        session, directory = _recorded_session(client)
        engine = rules.Engine(client, session)
        print(f"{entry.id}: {len(template.rules)} rule(s)")
        try:
            engine.arm_template(template)
            rows = engine.run(timeout=args.wait, log=lambda line: print("  " + line))
        finally:
            engine.stop()
            client.close()
            session.close()
        print(f"session recorded as {directory.name}")
        for row in rows:
            verdict = {True: "as expected", False: "NOT as expected", None: ""}[row["as_expected"]]
            print(f"{row['rule']} #{row['number']}  item {row['seq']}  {row['node_said']}  {verdict}")
        if args.output:
            rules.write_report(rows, Path(args.output))
        unexpected = [row for row in rows if row["as_expected"] is False]
        unfinished = [rule.name for rule in template.rules if not rule.finished]
        if unfinished:
            print(f"not finished before the wait ran out: {', '.join(unfinished)}")
        return 1 if unexpected or unfinished else 0
    except (KeyError, rules.RuleError) as error:
        print(f"freeze-debugger: {error.args[0] if error.args else error}", file=sys.stderr)
        return 1


def cmd_payloads(args: argparse.Namespace) -> int:
    from . import payloads

    store = payloads.Store(data_root() / "payloads")
    try:
        if args.payloads_command == "list":
            for item in store.all():
                print(f"{item.name:<28} {len(item.values):>4} values  {'shipped' if item.builtin else 'yours':<8} {item.description[:70]}")
        elif args.payloads_command == "show":
            for value in store.get(args.name).values:
                print(payloads.describe(value))
        elif args.payloads_command == "import":
            for item in store.import_file(Path(args.file)):
                print(f"imported `{item.name}` ({len(item.values)} values)")
        elif args.payloads_command == "export":
            store.export(args.name, Path(args.file))
            print(f"wrote {args.file}")
        elif args.payloads_command == "delete":
            print("deleted" if store.delete(args.name) else "that is a shipped list (or does not exist); nothing deleted")
    except (payloads.PayloadError, OSError) as error:
        print(f"freeze-debugger: {error}", file=sys.stderr)
        return 1
    return 0


def cmd_stop(args: argparse.Namespace) -> int:
    from . import launch

    try:
        found = launch.stop(args.network, data_root(), args.remove_data)
    except launch.LaunchError as error:
        print(f"freeze-debugger: {error}", file=sys.stderr)
        return 1
    print("stopped" if found else "no such node was running")
    return 0


def _recorded_session(client: FreezeClient, record: bool = True):
    """A session that keeps everything on disk, so it can be exported and repeated later."""
    import json

    from .session import Session

    directory = None
    recorded_by_broker = (client.broker or {}).get("session")
    if recorded_by_broker:
        # The broker already writes this session to disk; a second copy would only differ in where it starts.
        session = Session(None)
        session.directory = Path(recorded_by_broker)
        return session, session.directory
    if record:
        directory = data_root() / "sessions" / time.strftime("%Y%m%d-%H%M%S")
    session = Session(directory)
    if directory is not None:
        (directory / "session.json").write_text(json.dumps(
            {"node": client.hello.get("node", {}), "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
             "first_step": client.hello.get("status", {}).get("seq")}, indent=2) + "\n", encoding="utf-8")
    return session, directory


def _run_ui(client: FreezeClient, record: bool, node: str | None = None, nodes: dict[str, dict] | None = None,
            reconnect: Any = None, source: Any = None) -> int:
    """Open the UI. With several Freeze nodes, ``reconnect(name)`` gives a client for another one,
    and the UI is opened again on it when the Nodes tab asks to move there."""
    from .tui import FreezeApp

    while True:
        session, directory = _recorded_session(client, record)
        try:
            app = FreezeApp(client, session)
            # With several Freeze nodes in one devnet, the header says which one this window is on.
            app.node_name = node
            app.nodes = nodes or {}
            # `source(node)` gives a debugger beside that node, for the Source tab.
            app.source_factory = (lambda name=node: source(name)) if source is not None else None
            if client.broker:
                try:
                    # What happened before this window opened, so History starts filled.
                    app._incoming.extend((entry["event"], entry.get("data")) for entry in client.session_events(20000))
                except FreezeError:
                    pass
            app.run()
        finally:
            session.close()
            client.close()
        if directory is not None:
            print(f"session recorded in {directory}")
        if app.switch_to is None or reconnect is None:
            return 0
        node = app.switch_to
        client = reconnect(node)


def cmd_attach(args: argparse.Namespace) -> int:
    debugger = None
    if args.network:
        def debugger(_name: str | None) -> Any:
            return _source_debugger(argparse.Namespace(network=args.network, node=getattr(args, "node", None), timeout=60.0))

    return _run_ui(_connect(args), record=not args.no_record, source=debugger)


def cmd_demo(args: argparse.Namespace) -> int:
    from .testing.fake_agent import FakeAgent, load_capture, replay

    capture = Path(args.capture) if args.capture else Path(__file__).with_name("demo") / "amaru-preprod-sync.jsonl.gz"
    events = load_capture(capture)
    agent = FakeAgent(
        start_frozen=True,
        info={"implementation": "amaru (recording)", "version": "10.11.0", "network": "preprod", "kinds": {}},
    )
    agent.state["stages"] = {}
    for event in events:
        data = event["data"]
        if event["event"] == "item" and data["kind"] == "internal_state":
            name = data["from"].removeprefix("stage:")
            agent.state["stages"][name] = {"type": data["type"], "payload": data["payload"]}
    agent.start()
    replay(agent, events, delay=args.delay, loop=True)
    client = FreezeClient("127.0.0.1", agent.port, timeout=10)
    client.connect()
    try:
        return _run_ui(client, record=False)
    finally:
        agent.stop()


def cmd_watch(args: argparse.Namespace) -> int:
    from . import conditions, render
    from .glossary import default as glossary

    words = glossary()
    keep = conditions.compile_condition(args.only) if args.only else None
    client = _connect(args)
    node = client.hello.get("node", {})
    print(f"# {node.get('implementation')} {node.get('version')} {node.get('network')}  status={client.status()}")
    if client.supports("events:filter") and (keep is not None or args.sample > 1):
        # Let the node leave out what would not be printed anyway.
        client.subscribe(when=keep, sample=args.sample)
    try:
        for event in client.events():
            if event["event"] == "stats":
                data = event["data"]
                print(f"# step {data['seq']:,}: {data['items']:,} items in the last second, {data['dropped']:,} not shown")
                continue
            if event["event"] != "item":
                if event["event"] in ("frozen", "thawed", "log", "released"):
                    print(f"# {event['event']} {event['data']}")
                continue
            item = event["data"]
            if item["kind"] == "internal_state" or (keep is not None and not conditions.matches(keep, item)):
                continue
            what = words.lookup(item.get("label") or item["kind"], "kind").label
            route = f"{words.party(item.get('from', '')).label} → {words.party(item.get('to', '')).label}"
            held = "  HELD" if item.get("held") else ""
            print(f"{item['seq']:>8} {item['kind']:<16} {route:<44} {what:<24} {render.one_line(item.get('payload'), 70)}{held}")
    except KeyboardInterrupt:
        pass
    finally:
        client.close()
    return 0


def cmd_script(args: argparse.Namespace) -> int:
    from . import conditions, scripting

    print(
        "WARNING: SCRIPT CODE IS NOT SANDBOXED and runs with your full Python permissions.",
        file=sys.stderr,
    )
    try:
        keep = conditions.compile_condition(args.only) if args.only else None
        callback = scripting.load_callback(Path(args.script))
    except (conditions.ConditionError, scripting.ScriptError) as error:
        print(f"freeze-debugger script: {error}", file=sys.stderr)
        return 1

    client = _connect(args)
    try:
        session, directory = _recorded_session(client, record=not args.no_record)
    except BaseException:
        client.close()
        raise
    summary = scripting.ScriptSummary()
    code = 0
    try:
        scripting.run(client, callback, session, only=keep, summary=summary)
    except KeyboardInterrupt:
        print("freeze-debugger script: interrupted", file=sys.stderr)
        code = 130
    except scripting.ScriptError as error:
        print(f"freeze-debugger script: {error}", file=sys.stderr)
        code = 1
    finally:
        for resource, close in (("session", session.close), ("client", client.close)):
            try:
                close()
            except Exception as error:
                print(
                    f"freeze-debugger script: cleanup failed while closing {resource}: {error}",
                    file=sys.stderr,
                )
                if code != 130:
                    code = 1
        counts = " ".join(f"{action}={summary.actions[action]}" for action in summary.actions)
        print(f"actions: {counts}")
        if summary.filtered:
            print(f"filtered (forwarded without callback): {summary.filtered}")
        if summary.non_holdable:
            print(f"non-holdable (recorded, not intercepted): {summary.non_holdable}")
        print(f"session recorded in {directory}" if directory is not None else "session: not recorded")
        if summary.held_seq is not None:
            print(
                f"safety: item {summary.held_seq} remains held; node left frozen",
                file=sys.stderr,
            )
        elif summary.kept_frozen_for_safety:
            print(
                "safety: node left frozen; held-item status could not be confirmed",
                file=sys.stderr,
            )
        for error in summary.record_errors:
            print(f"freeze-debugger script: {error}", file=sys.stderr)
        for error in summary.restore_errors:
            print(f"freeze-debugger script: {error}", file=sys.stderr)
    return code


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="freeze-debugger", description="Interactive debugger for Cardano nodes built with freeze hooks.")
    commands = parser.add_subparsers(dest="command", required=True)

    def connection(sub: argparse.ArgumentParser) -> None:
        sub.add_argument("--host", default="127.0.0.1")
        sub.add_argument("--port", type=int, default=DEFAULT_PORT)
        sub.add_argument("--token", help="shared secret; defaults to $DWARF_FREEZE_TOKEN")
        sub.add_argument("--token-file", help="file holding the shared secret")
        sub.add_argument("--timeout", type=float, default=15.0)
        sub.add_argument("--network", help="use the node started by `freeze-debugger run` for this network, or `devnet`")
        sub.add_argument("--node", metavar="SERVICE", help="with --network devnet: which Freeze node, when the devnet has several")
        sub.add_argument("--direct", action="store_true", help="with --network: talk to the node's agent, not its broker")

    attach = commands.add_parser("attach", help="open the UI on a running Freeze node")
    connection(attach)
    attach.add_argument("--no-record", action="store_true", help="do not write the session to disk")
    attach.set_defaults(run=cmd_attach)

    build = commands.add_parser("build", help="build a node image with the Freeze agent compiled in")
    build.add_argument("--source", required=True, help="an Amaru source tree")
    build.add_argument("--recipe", help="which hook recipe to use; default: the first that applies")
    build.add_argument("--tag", help="image tag; default: derived from the source and recipe")
    build.add_argument("--platform", help="container platform, e.g. linux/amd64; default: this machine's")
    build.add_argument("--static", action="store_true", help="fully static x86-64 build, as the local devnet needs")
    build.add_argument("--debug-info", action="store_true",
                       help="keep what source-line stepping needs and build the tools image for `freeze-debugger source`")
    build.set_defaults(run=cmd_build)

    port = commands.add_parser("port", help="bring the hooks to a revision of the node that has no stored patch series")
    port.add_argument("--source", required=True, help="the source tree to bring them to")
    port.add_argument("--base", help="the tree the hooks were written for (default: DWARF's copy of that revision)")
    port.add_argument("--recipe", help="which stored hooks to start from (default: the one the spec describes)")
    port.add_argument("--name", help="what to call the result (default: the source folder's name)")
    port.add_argument("--shipped", action="store_true", help="store the result among the shipped recipes instead of in your data folder")
    port.add_argument("--keep", help="leave the hooked tree in this folder")
    port.add_argument("--by-hand", metavar="DIR", help="where both sides changed the same place, leave the tree in DIR with the conflicts marked")
    port.add_argument("--resolved", metavar="DIR", help="finish a port whose conflicts you resolved in DIR: compile it and store the result")
    port.set_defaults(run=cmd_port)

    run = commands.add_parser("run", help="start a Freeze node, frozen, and open the UI on it")
    run.add_argument("--network", required=True, help="mainnet, preprod, preview or testnet_<magic>")
    run.add_argument("--image", help="default: the most recently built Freeze image")
    run.add_argument("--port", type=int, default=DEFAULT_PORT, help="local port for the agent")
    run.add_argument("--peer", action="append", help="upstream peer host:port; repeatable; default: the network's")
    run.add_argument("--thawed", action="store_true", help="let the node run from the start")
    run.add_argument("--no-attach", action="store_true", help="start the node but do not open the UI")
    run.add_argument("--no-broker", action="store_true", help="do not start a broker in front of the node")
    run.add_argument("--record", choices=["all", "held", "none"], default="held",
                     help="what the broker keeps on disk: everything, what clients are sent (default; held items and verdicts always), or nothing")
    run.add_argument("--node-arg", action="append", help="extra argument passed to the node; repeatable")
    run.set_defaults(run=cmd_run)

    stop = commands.add_parser("stop", help="stop a node started by `run`")
    stop.add_argument("--network", required=True)
    stop.add_argument("--remove-data", action="store_true", help="also delete the node's chain and ledger data")
    stop.set_defaults(run=cmd_stop)

    devnet = commands.add_parser("devnet", help="a closed local devnet with one Freeze node in it")
    devnet_commands = devnet.add_subparsers(dest="devnet_command", required=True)
    devnet_up = devnet_commands.add_parser("up", help="start the devnet")
    devnet_up.add_argument("--image", help="default: the most recent static Freeze image")
    devnet_up.add_argument("--port", type=int, default=DEFAULT_PORT, help="local port for the agent")
    devnet_up.add_argument("--frozen", action="store_true", help="hold the Freeze node at its first step")
    devnet_up.add_argument("--profile", help="which devnet to run: a name from `devnet profiles`, or a profile file (default: relay)")
    devnet_up.add_argument("--compose", help="use this compose file instead of a profile")
    devnet_up.add_argument("--target", help="which service becomes the Freeze node (default: the profile's)")
    devnet_up.add_argument("--also", action="append", metavar="SERVICE",
                           help="another service that also runs the Freeze build, on the next agent port; repeatable")
    devnet_up.add_argument("--no-broker", action="store_true", help="do not start a broker for each Freeze node")
    devnet_up.add_argument("--freeze-together", action="store_true",
                           help="freeze and thaw the other Freeze nodes whenever the main one freezes and runs")
    devnet_up.add_argument("--record", choices=["all", "held", "none"], default="all",
                           help="what the brokers keep on disk: everything (default), what clients are sent, or nothing")
    devnet_up.add_argument("--no-auto-pause", action="store_true",
                           help="do not pause the other nodes while the Freeze node is frozen")
    devnet_up.add_argument("--peer-cooldown", type=int, default=None,
                           help="seconds the Freeze node keeps a misbehaving peer out (Amaru's own default is 600)")
    devnet_up.add_argument("--resign", action=argparse.BooleanOptionalAction, default=None,
                           help="re-sign edited producer headers with this controlled devnet's mounted keys (default: profile/on)")
    devnet_commands.add_parser("profiles", help="list the devnets you can start and the nodes Freeze can replace in them")
    devnet_commands.add_parser("status", help="show the devnet's containers and whether the Freeze node is ready")
    devnet_attach = devnet_commands.add_parser("attach", help="wait until the Freeze node is ready, then open the UI")
    devnet_attach.add_argument("--wait", type=float, default=3600.0, help="seconds to wait for the node")
    devnet_attach.add_argument("--node", metavar="SERVICE", help="which Freeze node, when the devnet has several (default: the main one)")
    devnet_attach.add_argument("--direct", action="store_true", help="talk to the node's agent, not its broker")
    devnet_nodes = devnet_commands.add_parser("nodes", help="list the devnet's Freeze nodes; freeze or thaw all of them at once")
    devnet_nodes.add_argument("--freeze", action="store_true", help="freeze every Freeze node")
    devnet_nodes.add_argument("--thaw", action="store_true", help="let every Freeze node run")
    devnet_broker = devnet_commands.add_parser("broker", help="the brokers between clients and the Freeze nodes: status, start, stop")
    devnet_broker.add_argument("action", nargs="?", default="status", choices=["status", "start", "stop"])
    devnet_broker.add_argument("--auto-pause", choices=["on", "off"], help="pause the other nodes while the Freeze node is frozen")
    devnet_tx = devnet_commands.add_parser(
        "tx", help="send a transaction on the devnet with its own funded key, so the ledger's rules and scripts run")
    devnet_tx.add_argument("what", choices=["pay", "script"], help="a plain payment, or lock and spend an output at a script that always succeeds")
    devnet_tx.add_argument("--lovelace", type=int, default=5_000_000)
    devnet_tx.add_argument("--count", type=int, default=1, help="how many, one after another; each waits for the one before to be in a block")
    devnet_tx.add_argument("--via", default="p1", metavar="SERVICE", help="the block producer whose cardano-cli and node are used")
    devnet_tx.add_argument("--key", type=int, default=1, help="which of the devnet's funded keys")
    devnet_timeline = devnet_commands.add_parser("timeline", help="what every Freeze node did, merged by time")
    devnet_timeline.add_argument("--last", type=int, default=60, help="how many items to print (default 60)")
    devnet_timeline.add_argument("--when", help="only items matching this condition")
    devnet_commands.add_parser("pause-peers", help="pause every other node")
    devnet_commands.add_parser("resume-peers", help="resume every other node")
    snapshot = devnet_commands.add_parser("snapshot", help="save the Freeze node's stored chain, or put a saved one back")
    snapshot_commands = snapshot.add_subparsers(dest="snapshot_command", required=True)
    snapshot_save = snapshot_commands.add_parser("save", help="stop the node, copy its chain aside, start it again")
    snapshot_save.add_argument("name")
    snapshot_save.add_argument("--note")
    snapshot_save.add_argument("--node", metavar="SERVICE", help="which Freeze node (default: the main one)")
    snapshot_commands.add_parser("list", help="saved snapshots")
    snapshot_restore = snapshot_commands.add_parser("restore", help="replace the node's chain with a saved one and restart it frozen")
    snapshot_restore.add_argument("name")
    snapshot_restore.add_argument("--running", action="store_true", help="do not hold the node at its first step")
    snapshot_restore.add_argument("--node", metavar="SERVICE", help="which Freeze node (default: the one the snapshot is of)")
    snapshot_delete = snapshot_commands.add_parser("delete", help="delete a snapshot")
    snapshot_delete.add_argument("name")
    stats = devnet_commands.add_parser("stats", help="watch the Freeze node's memory and CPU")
    stats.add_argument("--every", type=float, default=5.0, help="seconds between samples")
    stats.add_argument("--duration", type=float, default=60.0, help="seconds to watch for")
    devnet_down = devnet_commands.add_parser("down", help="stop the devnet")
    devnet_down.add_argument("--keep-data", action="store_true", help="keep the chain data volumes")
    devnet.set_defaults(run=cmd_devnet)

    sessions = commands.add_parser("sessions", help="list recorded sessions")
    sessions.set_defaults(run=cmd_sessions)

    export = commands.add_parser("export", help="write a recorded session, or its start, as one file")
    export.add_argument("--session", help="a session name or directory; default: the most recent")
    export.add_argument("--until", type=int, help="keep only up to this item number")
    export.add_argument("--note", help="a line of text stored with the run")
    export.add_argument("-o", "--output", help="default: freeze-run-<session>.tar.gz")
    export.set_defaults(run=cmd_export)

    import_run = commands.add_parser("import", help="unpack an exported run and show what it did")
    import_run.add_argument("archive", metavar="RUN", help="a .tar.gz written by `export`")
    import_run.set_defaults(run=cmd_import)

    open_run = commands.add_parser("open", help="browse an imported run in the UI, with no node")
    open_run.add_argument("archive", metavar="RUN", help="an exported file or the name of an imported run")
    open_run.set_defaults(run=cmd_open)

    ingest = commands.add_parser("ingest", help="restart the node and repeat what a recorded run did to it")
    ingest.add_argument("archive", metavar="RUN", help="an exported file or the name of an imported run")
    connection(ingest)
    ingest.add_argument("--snapshot", help="devnet only: first put back the chain saved under this name, so the run starts from the same state")
    ingest.add_argument("--no-restart", action="store_true", help="use the node as it is now")
    ingest.add_argument("--no-attach", action="store_true", help="do not open the UI afterwards")
    ingest.add_argument("--wait", type=float, default=900.0, help="seconds to wait for the matching items")
    ingest.add_argument(
        "--stale-negative-control",
        action="store_true",
        help="deliberately replay edits with stale/invalid validation material",
    )
    ingest.set_defaults(run=cmd_ingest)

    step_source = commands.add_parser(
        "source", help="step through the node's source lines (needs an image built with `build --debug-info`)")
    step_source.add_argument("--network", help="the node started by `run` for this network; default: the devnet's Freeze node")
    step_source.add_argument("--node", metavar="SERVICE", help="which Freeze node of the devnet")
    step_source.add_argument("--do", action="append", metavar="COMMAND",
                             help="run this command and leave, instead of asking; repeatable, e.g. --do 'break x.rs:10' --do continue --do vars")
    step_source.add_argument("--wait", type=float, default=60.0, help="seconds to wait for the node to stop after continue or a step")
    step_source.add_argument("--timeout", type=float, default=60.0)
    step_source.set_defaults(run=cmd_source)

    rewind = commands.add_parser("rewind", help="take the devnet's Freeze node back to an earlier item of a recorded session")
    rewind.add_argument("--to", type=int, required=True, metavar="ITEM", help="the item's number in the recording")
    rewind.add_argument("--session", help="which recording (default: the Freeze node's latest)")
    rewind.add_argument("--snapshot", help="the saved chain to start again from (default: the node's start snapshot)")
    rewind.add_argument("--node", metavar="SERVICE", help="which Freeze node (default: the main one)")
    rewind.add_argument("--by-count", action="store_true",
                        help="stop at the item in the same position among its like, even if it carries something else")
    rewind.add_argument("--wait", type=float, default=900.0, help="seconds to wait for the item")
    rewind.add_argument("--timeout", type=float, default=15.0)
    rewind.add_argument("--no-attach", action="store_true", help="do not open the UI afterwards")
    rewind.add_argument(
        "--stale-negative-control",
        action="store_true",
        help="deliberately replay edits with stale/invalid validation material",
    )
    rewind.set_defaults(run=cmd_rewind)

    from . import cli_more

    cli_more.register(commands, connection, _session_dir, _connect, _recorded_session, data_root)

    template = commands.add_parser("template", help="run a template of rules against a node without the UI")
    template.add_argument("template", help="a .yaml or .json template")
    connection(template)
    template.add_argument("--wait", type=float, default=600.0, help="seconds to wait for the rules to finish")
    template.add_argument("-o", "--output", help="write the results as JSON")
    template.set_defaults(run=cmd_template)

    scen = commands.add_parser("scenarios", help="DWARF's scenarios: list, show, convert to a template, or run")
    scen_commands = scen.add_subparsers(dest="scenarios_command", required=True)
    scen_list = scen_commands.add_parser("list", help="scenarios Freeze can carry out")
    scen_list.add_argument("--all", action="store_true", help="also list the ones it cannot, with the reason")
    scen_list.add_argument("--filter", help="only those mentioning this text")
    scen_show = scen_commands.add_parser("show", help="what a scenario does and the rules Freeze derives from it")
    scen_show.add_argument("id")
    scen_template = scen_commands.add_parser("template", help="write a scenario as a Freeze template file")
    scen_template.add_argument("id")
    scen_template.add_argument("-o", "--output", help="default: <id>.yaml")
    scen_run = scen_commands.add_parser("run", help="carry out a scenario's tampering on a node and judge the reaction")
    scen_run.add_argument("id")
    connection(scen_run)
    scen_run.add_argument("--wait", type=float, default=600.0, help="seconds to wait for the rules to finish")
    scen_run.add_argument("-o", "--output", help="write the results as JSON")
    scen_commands.add_parser("lists", help="save DWARF's test values as payload lists")
    scen.set_defaults(run=cmd_scenarios)

    lists = commands.add_parser("payloads", help="manage payload lists")
    lists_commands = lists.add_subparsers(dest="payloads_command", required=True)
    lists_commands.add_parser("list", help="every list, shipped and yours")
    show = lists_commands.add_parser("show", help="print a list's values")
    show.add_argument("name")
    bring = lists_commands.add_parser("import", help="import list(s) from .txt, .csv, .json or .yaml")
    bring.add_argument("file")
    send = lists_commands.add_parser("export", help="write a list to .yaml or .json")
    send.add_argument("name")
    send.add_argument("file")
    remove = lists_commands.add_parser("delete", help="delete one of your lists")
    remove.add_argument("name")
    lists.set_defaults(run=cmd_payloads)

    demo = commands.add_parser("demo", help="try the UI on a recording of a real node")
    demo.add_argument("--capture", help="a recorded event stream (.jsonl or .jsonl.gz)")
    demo.add_argument("--delay", type=float, default=0.002, help="seconds between replayed steps")
    demo.set_defaults(run=cmd_demo)

    watch = commands.add_parser("watch", help="print what the node does, one line per step")
    connection(watch)
    watch.add_argument("--only", help="a condition; print only matching steps")
    watch.add_argument("--sample", type=int, default=1, metavar="N", help="print one step in N (held ones always)")
    watch.set_defaults(run=cmd_watch)

    script = commands.add_parser(
        "script",
        help="intercept held items with an explicitly named local Python file",
        description="Run on_item(item) from an explicitly named local .py file for each intercepted item.",
        epilog=(
            "WARNING: SCRIPT CODE IS NOT SANDBOXED. It runs with your full Python permissions.\n\n"
            "The runner records all protocol events. Agent-defined non-holdable items, including "
            "internal_state, are recorded but cannot be intercepted."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    script.add_argument("script", metavar="SCRIPT", help="local .py file defining callable on_item(item)")
    connection(script)
    script.add_argument("--only", help="call on_item only for matching items; forward other held items unchanged")
    script.add_argument("--no-record", action="store_true", help="do not write the session to disk")
    script.set_defaults(run=cmd_script)
    return parser


LOCAL_ONLY = {"build", "port", "run", "stop", "devnet", "ingest", "rewind", "source"}
"""Commands that start containers or processes on this machine."""


def needs_posix(args: argparse.Namespace, platform: str = sys.platform) -> str | None:
    """On Windows only the client side is supported: watching and driving a node that runs
    elsewhere. Returns why a command is refused there, or None."""
    if not platform.startswith("win"):
        return None
    command = getattr(args, "command", None)
    if command in LOCAL_ONLY:
        return (f"`{command}` starts containers and processes the way Linux and macOS do, and is not supported on Windows. "
                "Run it inside WSL 2, or run the node elsewhere and use `attach --host HOST --port PORT --token-file FILE` from here")
    if getattr(args, "network", None):
        return "`--network` looks for a node started on this machine; on Windows give `--host`, `--port` and `--token-file` instead"
    return None


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    refused = needs_posix(args)
    if refused:
        print(f"freeze-debugger: {refused}", file=sys.stderr)
        return 1
    try:
        return args.run(args)
    except FreezeError as error:
        print(f"freeze-debugger: {error}", file=sys.stderr)
        return 1
    except OSError as error:
        print(f"freeze-debugger: cannot reach the node: {error}", file=sys.stderr)
        return 1
