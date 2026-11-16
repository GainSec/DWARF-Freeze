"""Commands for working on recorded sessions and the tooling around a test:
search, coverage, compare, marks and findings, repeat, snapshots and resource watch."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Callable


def _line(item: dict[str, Any], badge: str = "") -> str:
    from . import cardano, render

    what = cardano.item_message_name(item) or item.get("label", "")
    return (f"{badge:>3} {item['seq']:>8}  {item['kind']:<16} {what:<26.26} {item.get('from', '') or '-':<26.26} → "
            f"{item.get('to', '') or '-':<26.26} {render.one_line(item.get('payload'), 60)}")


def register(commands: Any, connection: Callable[[Any], None], session_dir: Callable[[str | None], Path],
             connect: Callable[[argparse.Namespace], Any], recorded: Callable[..., Any], data_root: Callable[[], Path]) -> None:
    def load(args: argparse.Namespace):
        from . import notes

        directory = session_dir(args.session)
        return directory, notes.load_session(directory), notes.Notes(directory)

    def cmd_search(args: argparse.Namespace) -> int:
        from . import analysis

        from . import filters

        try:
            query = filters.Filters(data_root()).resolve(args.query)
        except filters.FilterError as error:
            print(f"freeze-debugger: {error}", file=sys.stderr)
            return 2
        _directory, session, marks = load(args)
        found = analysis.search(session, query, args.limit)
        for item in found:
            print(_line(item, marks.badge(item["seq"])))
        print(f"{len(found)} item(s)" + (" (limit reached)" if len(found) >= args.limit else ""))
        return 0

    def cmd_filters(args: argparse.Namespace) -> int:
        from . import filters

        store = filters.Filters(data_root())
        try:
            if args.filters_command == "save":
                store.save(args.name, args.query, args.about or "")
                print(f"saved @{args.name}")
            elif args.filters_command == "delete":
                if not store.delete(args.name):
                    print(f"freeze-debugger: no saved filter `{args.name}`", file=sys.stderr)
                    return 1
                print(f"forgot @{args.name}")
            else:
                saved = store.all()
                for name in sorted(saved):
                    about = f"   # {saved[name]['about']}" if saved[name].get("about") else ""
                    print(f"@{name:<24} {saved[name]['query']}{about}")
                if not saved:
                    print("no saved filters; add one with `freeze-debugger filters save NAME CONDITION`")
        except filters.FilterError as error:
            print(f"freeze-debugger: {error}", file=sys.stderr)
            return 2
        return 0

    def cmd_probes(args: argparse.Namespace) -> int:
        client = connect(args)
        try:
            for name in args.enable or []:
                print(f"{client.probe_enable(name)} probe(s) enabled by `{name}`")
            for name in args.disable or []:
                print(f"{client.probe_disable(name)} probe(s) disabled by `{name}`")
            probes = client.probes()
            has_probes = client.supports("probes") or bool(probes)
        finally:
            client.close()
        if not has_probes:
            print("this node was built without probes")
            return 0
        reached = [probe for probe in probes if probe["hits"]]
        shown = reached if args.hit else [probe for probe in probes if not probe["hits"]] if args.missed else probes
        for probe in shown:
            mark = "on " if probe["enabled"] else "off"
            print(f"  {mark} {probe['hits']:>10,}  {probe['name']:<44} {probe['where']}")
        print(f"{len(reached)} of {len(probes)} probes reached")
        return 0

    def cmd_trace(args: argparse.Namespace) -> int:
        client = connect(args)
        try:
            for prefix in args.enable or []:
                print(f"{client.trace_enable(prefix)} function(s) now traced by `{prefix}`")
            for prefix in args.disable or []:
                print(f"{client.trace_disable(prefix)} function(s) no longer traced by `{prefix}`")
            for prefix in args.hold or []:
                print(f"{client.trace_hold(prefix)} function(s) now hold the node when entered, by `{prefix}`")
            for prefix in args.unhold or []:
                print(f"{client.trace_unhold(prefix)} function(s) no longer hold the node, by `{prefix}`")
            traced = client.traces(args.prefix or "")
        finally:
            client.close()
        print("traced: " + (", ".join(traced["enabled"]) or "nothing"))
        if traced.get("holding"):
            print("holding the node on entry: " + ", ".join(traced["holding"]))
        sites = traced.get("sites", [])
        shown = [site for site in sites if site["hits"]] if args.hit else sites
        for site in sorted(shown, key=lambda site: -site["hits"])[: args.top]:
            print(f"  {'on ' if site['enabled'] else 'off'} {site['hits']:>12,}  {site['name']}")
        print(f"{len(sites):,} function(s) seen so far" + (f" under `{args.prefix}`" if args.prefix else "")
              + "; a function is listed once it has been called")
        return 0

    def cmd_coverage(args: argparse.Namespace) -> int:
        from . import analysis, notes, resources

        directory, session, _marks = load(args)
        print(f"session {directory.name}: {len(session.items):,} items")
        reached = analysis.coverage(session)
        titles = {"parts": "Parts of the node that handled something", "effects": "Effects run", "messages": "Protocol messages",
                  "errors": "Errors returned", "rejections": "Rejections",
                  "probes": "Probes reported (only those enabled; `freeze-debugger probes` has every hit)",
                  "calls": "Function calls reported (only those traced; `freeze-debugger trace` has every count)"}
        for name, title in titles.items():
            print(f"\n{title} ({len(reached[name])}):")
            for label, count in reached[name].most_common(args.top):
                print(f"  {count:>8,}  {label}")
        queues = resources.backlog(session)
        if queues:
            print("\nSent but not yet handled (queues):")
            for stage, count in queues[: args.top]:
                print(f"  {count:>8,}  {stage}")
        if args.against:
            other = notes.load_session(session_dir(args.against))
            print(f"\nCompared with {args.against}:")
            for name, sides in analysis.compare_coverage(session, other).items():
                for side, labels in sides.items():
                    if labels:
                        print(f"  {titles[name]}, {'only here' if side == 'only_first' else 'only there'}: {', '.join(labels[:12])}")
        return 0

    def cmd_compare(args: argparse.Namespace) -> int:
        from . import analysis, notes

        if args.items:
            directory, session, _marks = load(args)
            other = notes.load_session(session_dir(args.other)) if args.other else None
            try:
                result = analysis.compare_items(session, args.items[0], args.items[1], other)
            except KeyError as error:
                print(f"freeze-debugger: {error.args[0]}", file=sys.stderr)
                return 1
            print(f"{len(result['fields'])} field(s) differ")
            for field in result["fields"][: args.top]:
                print(f"  {field['field']:<40} {field['first']}  |  {field['second']}")
            print(f"node, first:  {'; '.join(result['first']) or 'nothing recorded'}")
            print(f"node, second: {'; '.join(result['second']) or 'nothing recorded'}")
            return 0
        if len(args.results) != 2:
            print("freeze-debugger: give two result files (from `template -o` / `scenarios run -o`), or --items A B", file=sys.stderr)
            return 1
        first, second = (json.loads(Path(path).read_text(encoding="utf-8")) for path in args.results)
        rows = analysis.compare_results(first, second)
        for row in rows:
            mark = "  " if row["same"] else "≠ "
            print(f"{mark}{row['rule']} #{row['number']}  {row['value']}")
            if not row["same"]:
                print(f"     first:  {row['first']}\n     second: {row['second']}")
        different = sum(not row["same"] for row in rows)
        print(f"{len(rows)} attempt(s), {different} where the two disagree")
        return 1 if different and args.strict else 0

    def cmd_mark(args: argparse.Namespace) -> int:
        from . import notes

        directory, session, marks = load(args)
        if args.seq not in session.items:
            print(f"freeze-debugger: item {args.seq} is not in session {directory.name}", file=sys.stderr)
            return 1
        try:
            if args.bookmark:
                print("bookmarked" if marks.toggle_bookmark(args.seq) else "bookmark removed")
            if args.comment is not None:
                marks.comment(args.seq, args.comment)
                print("comment saved" if args.comment.strip() else "comment removed")
            if args.finding is not None:
                marks.finding(args.seq, args.finding, args.severity, args.detail or "")
                print("finding saved" if args.finding.strip() else "finding removed")
        except notes.NotesError as error:
            print(f"freeze-debugger: {error}", file=sys.stderr)
            return 1
        return 0

    def cmd_findings(args: argparse.Namespace) -> int:
        from . import notes

        directory, session, marks = load(args)
        if args.output:
            count = notes.report(directory, Path(args.output), args.title or "")
            print(f"wrote {args.output} ({count} finding(s)) and {Path(args.output).with_suffix('.evidence.json')}")
            return 0
        for seq, mark in sorted(marks.marks.items()):
            item = session.items.get(seq)
            if item is None:
                continue
            print(_line(item, marks.badge(seq)))
            if "finding" in mark:
                print(f"{'':>13}finding ({mark['finding']['severity']}): {mark['finding']['title']}")
            if "comment" in mark:
                print(f"{'':>13}note: {mark['comment']}")
        print(f"{len(marks.findings())} finding(s); ! finding, * bookmark, # comment")
        return 0

    def cmd_repeat(args: argparse.Namespace) -> int:
        from . import repeater, rules

        _directory, past, _marks = load(args)
        item = past.items.get(args.seq)
        if item is None:
            print(f"freeze-debugger: item {args.seq} is not in that session", file=sys.stderr)
            return 1
        changes = []
        for text in args.set or []:
            field, separator, value = text.partition("=")
            if not separator:
                print(f"freeze-debugger: --set wants field=value, got {text!r}", file=sys.stderr)
                return 1
            changes.append((field.strip(), value.strip()))
        client = connect(args)
        session, directory = recorded(client)
        try:
            step = repeater.plan(
                client,
                item,
                changes,
                args.times,
                stale_negative_control=args.stale_negative_control,
            )
            if step["how"] == "inject":
                print(f"delivered {repeater.send(client, step)} time(s) to {step['to']}")
                rows = []
            else:
                print(f"{step['bytes']} bytes will take the place of the next {args.times} message(s) matching: {step['rule'].when}")
                engine = rules.Engine(client, session)
                engine.arm(step["rule"])
                try:
                    rows = engine.run(timeout=args.wait, log=lambda line: print("  " + line))
                finally:
                    engine.stop()
            for row in rows:
                print(f"#{row['number']}  item {row['seq']}  {row['node_said']}")
        except (repeater.RepeatError, ValueError) as error:
            print(f"freeze-debugger: {error}", file=sys.stderr)
            return 1
        finally:
            client.close()
            session.close()
        print(f"session recorded as {directory.name}")
        return 0

    search = commands.add_parser("search", help="find items in a recorded session by condition or text")
    search.add_argument("query", help="a condition (kind=proto_in and slot>100), a saved filter (@name) or any text")
    search.add_argument("--session", help="default: the most recent")
    search.add_argument("--limit", type=int, default=200)
    search.set_defaults(run=cmd_search)

    saved = commands.add_parser("filters", help="conditions saved under a name, used as @name in search and in History")
    saved_commands = saved.add_subparsers(dest="filters_command")
    saved_commands.add_parser("list", help="show the saved filters (the default)")
    keep = saved_commands.add_parser("save", help="save a condition under a name")
    keep.add_argument("name")
    keep.add_argument("query", help="a condition, e.g. 'kind=proto_in and slot>100'")
    keep.add_argument("--about", help="a note on what it is for")
    forget = saved_commands.add_parser("delete", help="forget a saved filter")
    forget.add_argument("name")
    saved.set_defaults(run=cmd_filters)

    probes = commands.add_parser("probes", help="named places inside the node's functions: which were reached, and which report items")
    connection(probes)
    probes.add_argument("--hit", action="store_true", help="only the probes that were reached")
    probes.add_argument("--missed", action="store_true", help="only the probes that were never reached")
    probes.add_argument("--enable", action="append", metavar="NAME",
                        help="report an item each time these probes are reached: a probe, a prefix ending in * (consensus.*), or all; repeatable")
    probes.add_argument("--disable", action="append", metavar="NAME", help="stop reporting them (hits are still counted); repeatable")
    probes.set_defaults(run=cmd_probes)

    trace = commands.add_parser("trace", help="every function of the node: which were called, and which report each call")
    connection(trace)
    trace.add_argument("--enable", action="append", metavar="PREFIX",
                       help="report each call of the functions whose full name starts with this, e.g. amaru_consensus::stages::select_chain; repeatable")
    trace.add_argument("--disable", action="append", metavar="PREFIX", help="stop reporting them (`all` for everything); repeatable")
    trace.add_argument("--hold", action="append", metavar="PREFIX",
                       help="hold the node each time one of these functions is entered (where the node can wait there); repeatable")
    trace.add_argument("--unhold", action="append", metavar="PREFIX", help="stop holding (`all` for everything); repeatable")
    trace.add_argument("--prefix", help="only list functions whose full name starts with this")
    trace.add_argument("--hit", action="store_true", help="only functions that were called")
    trace.add_argument("--top", type=int, default=40, help="how many to list, most called first (default 40)")
    trace.set_defaults(run=cmd_trace)

    cover = commands.add_parser("coverage", help="what a recorded session reached: parts, effects, messages, errors, queues")
    cover.add_argument("--session", help="default: the most recent")
    cover.add_argument("--against", help="another session: show what only one of the two reached")
    cover.add_argument("--top", type=int, default=25)
    cover.set_defaults(run=cmd_coverage)

    compare = commands.add_parser("compare", help="two result files attempt by attempt, or two items field by field")
    compare.add_argument("results", nargs="*", help="two result files written with -o")
    compare.add_argument("--items", nargs=2, type=int, metavar=("A", "B"), help="compare two items instead")
    compare.add_argument("--session", help="session of the items (default: the most recent)")
    compare.add_argument("--other", help="session of the second item, if different")
    compare.add_argument("--top", type=int, default=40)
    compare.add_argument("--strict", action="store_true", help="exit 1 if the two disagree anywhere")
    compare.set_defaults(run=cmd_compare)

    mark = commands.add_parser("mark", help="bookmark, comment on, or record a finding about an item")
    mark.add_argument("seq", type=int)
    mark.add_argument("--session", help="default: the most recent")
    mark.add_argument("--bookmark", action="store_true", help="toggle a bookmark")
    mark.add_argument("--comment", help="a note on the item (empty removes it)")
    mark.add_argument("--finding", help="title of a finding (empty removes it)")
    mark.add_argument("--severity", default="medium", help="info, low, medium, high or critical")
    mark.add_argument("--detail", help="what the finding means")
    mark.set_defaults(run=cmd_mark)

    findings = commands.add_parser("findings", help="list a session's marks, or write its findings as a report")
    findings.add_argument("--session", help="default: the most recent")
    findings.add_argument("-o", "--output", help="write a Markdown report here, with the evidence as JSON beside it")
    findings.add_argument("--title")
    findings.set_defaults(run=cmd_findings)

    repeat = commands.add_parser("repeat", help="send an item from a recorded session again, changed or not")
    repeat.add_argument("seq", type=int)
    repeat.add_argument("--session", help="where the item is (default: the most recent)")
    repeat.add_argument("--set", action="append", metavar="FIELD=VALUE", help="change a field first; repeatable; values may be transforms (add:1)")
    repeat.add_argument("--times", type=int, default=1)
    repeat.add_argument(
        "--stale-negative-control",
        action="store_true",
        help="deliberately emit an edited message with stale/invalid validation material",
    )
    repeat.add_argument("--wait", type=float, default=300.0, help="seconds to wait for a message to take the place of")
    connection(repeat)
    repeat.set_defaults(run=cmd_repeat)
