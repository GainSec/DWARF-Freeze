"""The Freeze terminal UI: an intercepting proxy for a Cardano node.

Tabs: Intercept (hold, read, edit, forward, drop), History (everything that happened and how
the node reacted), State (what the node holds), Freeze points (where it stops).
"""

from __future__ import annotations

import collections
import json
import time
from typing import Any

from rich.markup import escape
from rich.text import Text
from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.css.query import NoMatches
from textual.screen import ModalScreen
from textual.widgets import Button, Checkbox, DataTable, Footer, Input, Label, Static, TabbedContent, TabPane, Tree
from textual.widgets.tree import TreeNode

from pathlib import Path

from . import cardano, cbor_tree, conditions, payloads, render, rules, runs, scenarios
from .client import FreezeClient, FreezeError
from .glossary import Entry, Glossary
from .glossary import default as default_glossary
from .session import Session
from . import views
from .views import item_view

COLOURS = {"field": "#9db8ff", "kind": "#ffd479", "part": "#7dffd6", "action": "#e0a6ff"}
ICE, RED, MUTED, FROST, OK = "#5fe0ff", "#ff3b4e", "#7fa3bd", "#bfefff", "#7dffd6"

HOLD_GROUPS = [
    ("from peers", ["proto_in"]),
    ("to peers", ["proto_out"]),
    ("raw bytes", ["wire_in", "wire_out"]),
    ("between parts", ["internal_input"]),
    ("actions", ["internal_action", "internal_result"]),
    ("store writes", ["store_write"]),
    ("timers", ["timer"]),
]
DEFAULT_HOLD = {"from peers", "to peers"}

# Terminal widths below which the Intercept tab rearranges itself instead of running off the right edge.
COMPACT_WIDTH, NARROW_WIDTH = 140, 100

SOURCE_NOT_ATTACHED = (
    "No debugger is attached, so there is nothing to show yet.\n\n"
    "Press A to attach one to the node. The source around the line the node stopped at appears here, "
    "the call stack and the local variables on the right.\n\n"
    "This needs a node image built with `freeze-debugger build --debug-info`. "
    "An image built without it has no source lines to step through, and A says so."
)
HISTORY_ROWS = 3000

TAB_FOCUS = {"intercept": "#held", "history": "#history-table", "state": "#state-keys", "points": "#points-table",
             "overview": "#overview-scroll", "payloads": "#pl-lists", "scenarios": "#sc-table", "nodes": "#nodes-table",
             "source": "#src-stack"}

# How the overview lays out the parts of an Amaru node: the path a block takes, then the rest.
# Parts not named here (per-connection protocol handlers and helpers) are summed on one line.
PIPELINE = ["track_peers", "select_chain", "fetch_blocks", "validate_block", "adopt_chain"]
SERVICES = ["manager", "peer_selection", "mempool", "block_source"]

KIND_WORDS = {
    "internal_input": "in",
    "internal_action": "does",
    "internal_result": "gets",
    "internal_state": "state",
    "wire_in": "bytes in",
    "wire_out": "bytes out",
    "proto_in": "recv",
    "proto_out": "send",
    "store_write": "write",
    "timer": "timer",
    "probe": "probe",
    "call": "call",
}


def styled(entry: Entry) -> Text:
    return Text(entry.label, style=COLOURS[entry.cls])


class PayloadTree(Tree[render.Node]):
    """A payload shown as a tree of plain-named, colour-coded fields."""

    def __init__(self, glossary: Glossary, **kwargs: Any) -> None:
        super().__init__("", **kwargs)
        self.glossary = glossary
        self.show_root = False
        self.guide_depth = 3
        self.changed: set[render.Path] = set()

    def show(
        self,
        payload: Any,
        changed: set[render.Path] | None = None,
        expand_depth: int = 2,
        names: dict[render.Path, str] | None = None,
        loose_bytes: bool = True,
    ) -> None:
        self.changed = changed or set()
        self.clear()
        root = render.build(payload, names=names, loose_bytes=loose_bytes)
        if root.leaf:
            self.root.add_leaf(self._label(root, "value"), data=root)
        else:
            self._fill(self.root, root, expand_depth)
        self.root.expand()

    def _fill(self, parent: TreeNode[render.Node], node: render.Node, expand_depth: int) -> None:
        for index, child in enumerate(node.children):
            name = child.name or f"[{index}]"
            if child.leaf:
                parent.add_leaf(self._label(child, name), data=child)
            else:
                branch = parent.add(self._label(child, name), data=child, expand=expand_depth > 0)
                self._fill(branch, child, expand_depth - 1)

    def _label(self, node: render.Node, name: str) -> Text:
        changed = any(node.path[: len(path)] == path or path[: len(node.path)] == node.path for path in self.changed)
        text = Text()
        if node.name:
            entry = self.glossary.lookup(node.name)
            text.append(entry.label, style=RED if changed and node.leaf else COLOURS[entry.cls])
        else:
            text.append(name, style=MUTED)
        if node.summary:
            text.append("  ")
            text.append(node.summary, style=RED if changed and node.leaf else (FROST if node.leaf else MUTED))
        if changed and node.leaf:
            text.append("  CHANGED", style=f"bold {RED}")
        return text

    def explain(self, node: render.Node | None) -> Entry | None:
        if node is None or not node.name:
            return None
        return self.glossary.lookup(node.name)

    def watch_hover_line(self, previous: int, line: int) -> None:
        super().watch_hover_line(previous, line)
        node = self.get_node_at_line(line) if line >= 0 else None
        entry = self.explain(node.data) if node is not None else None
        self.tooltip = entry.explanation if entry and entry.explanation else None


class EditScreen(ModalScreen[str | None]):
    """Ask for a replacement value."""

    BINDINGS = [Binding("escape", "dismiss(None)", "cancel")]

    def __init__(self, title: str, current: str, hint: str) -> None:
        super().__init__()
        self._title, self._current, self._hint = title, current, hint

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label(self._title, id="dialog-title")
            yield Input(value=self._current, id="dialog-input")
            yield Label(self._hint, id="dialog-hint")

    def on_mount(self) -> None:
        self.query_one(Input).focus()

    @on(Input.Submitted)
    def _submitted(self, event: Input.Submitted) -> None:
        self.dismiss(event.value)


class InjectScreen(ModalScreen["tuple[str, str] | None"]):
    """Ask where to deliver a new message and what it says."""

    BINDINGS = [Binding("escape", "dismiss(None)", "cancel")]

    def __init__(self, destinations: list[str], to: str, payload: str) -> None:
        super().__init__()
        self._destinations, self._to, self._payload = destinations, to, payload

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label("Deliver a new message to a part of the node", id="dialog-title")
            yield Input(value=self._to, placeholder="to, e.g. stage:select_chain-4", id="inject-to")
            yield Input(value=self._payload, placeholder="the message, as JSON", id="inject-payload")
            shown = ", ".join(name.removeprefix("stage:") for name in self._destinations[:40])
            yield Label(f"can receive: {shown}{' …' if len(self._destinations) > 40 else ''}\n"
                        "tab moves between the boxes · enter delivers · esc cancels", id="dialog-hint")

    def on_mount(self) -> None:
        self.query_one("#inject-payload" if self._to else "#inject-to", Input).focus()

    @on(Input.Submitted)
    def _submitted(self, _event: Input.Submitted) -> None:
        self.dismiss((self.query_one("#inject-to", Input).value.strip(), self.query_one("#inject-payload", Input).value.strip()))


class FreezeApp(App[None]):
    TITLE = "DWARF Freeze"
    CSS = f"""
    Screen {{ background: #050b14; color: #e4f6ff; }}
    #status {{ height: 1; background: #0a1a2b; color: #e4f6ff; padding: 0 1; }}
    #actions {{ height: 3; padding: 0 1; }}
    #holding {{ height: 3; padding: 0 1; }}
    #holding Label {{ padding: 1 1 0 0; color: {MUTED}; }}
    Button {{ min-width: 10; width: auto; margin: 0 1 0 0; background: #0d2034; color: #e4f6ff; border: tall #1d3a52; }}
    Button.primary {{ background: {ICE}; color: #081220; }}
    Button.on {{ color: {ICE}; border: tall {ICE}; }}
    #intercept-toggle {{ min-width: 24; }}
    Checkbox {{ background: #050b14; border: none; padding: 1 1 0 0; height: 3; }}
    .pane {{ border: round #1d3a52; }}
    .pane:focus-within {{ border: round {ICE}; }}
    #held {{ width: 34%; }}
    #item {{ width: 40%; }}
    #inspector {{ width: 26%; padding: 0 1; }}
    /* Below COMPACT_WIDTH columns the rows wrap and the held list goes above the item, so nothing is cut off. */
    #actions.compact {{ layout: grid; grid-size: 4; grid-gutter: 0 1; grid-rows: 1; height: auto; }}
    #actions.compact Button {{ height: 1; min-width: 0; width: 1fr; margin: 0; border: none; }}
    #actions.narrow {{ grid-size: 3; }}
    #holding.compact {{ layout: grid; grid-size: 4; grid-rows: 1; height: auto; }}
    #holding.compact Label {{ padding: 0 1 0 0; }}
    #holding.compact Checkbox {{ height: 1; width: 1fr; padding: 0 1 0 0; }}
    #intercept-panes.compact {{ layout: grid; grid-size: 2; grid-columns: 3fr 2fr; grid-rows: 7 1fr; }}
    #intercept-panes.compact #held {{ column-span: 2; width: 100%; height: 100%; }}
    #intercept-panes.compact #item, #intercept-panes.compact #inspector {{ width: 100%; height: 100%; }}
    DataTable {{ background: #081220; }}
    DataTable > .datatable--header {{ background: #0a1a2b; color: {ICE}; }}
    DataTable > .datatable--cursor {{ background: #12384f; }}
    Tree {{ background: #081220; }}
    Tree > .tree--cursor {{ background: #12384f; }}
    #history-table {{ height: 60%; }}
    #history-in, #history-did {{ width: 50%; }}
    #history-did {{ padding: 0 1; }}
    #state-keys {{ width: 30%; }}
    #state-value {{ width: 70%; }}
    #points-help {{ height: auto; padding: 0 1; color: {MUTED}; }}
    #points-table {{ height: 1fr; }}
    #probes-table {{ height: 2fr; }}
    #overview-scroll {{ padding: 1 1; }}
    #pl-help, #sc-help {{ height: auto; padding: 0 1; color: {MUTED}; }}
    #sc-table {{ width: 58%; }}
    #sc-detail {{ width: 42%; padding: 0 1; }}
    #pl-lists {{ width: 24%; }}
    #pl-values {{ width: 22%; }}
    #pl-right {{ width: 54%; }}
    #pl-rules {{ height: 35%; }}
    #pl-results {{ height: 65%; }}
    #dialog {{ width: 70; height: auto; padding: 1 2; background: #0c1826; border: round {ICE}; }}
    EditScreen, InjectScreen {{ align: center middle; }}
    InjectScreen #dialog {{ width: 110; }}
    #nodes-help {{ height: auto; padding: 0 1; color: {MUTED}; }}
    #nodes-table {{ height: 7; }}
    #nodes-held {{ height: 10; }}
    #nodes-feed {{ height: 1fr; }}
    #src-help {{ height: auto; padding: 0 1; color: {MUTED}; }}
    #src-code {{ width: 62%; padding: 0 1; }}
    #src-side {{ width: 38%; }}
    #src-stack {{ height: 45%; }}
    #src-vars {{ height: 55%; }}
    #dialog-title {{ color: {ICE}; }}
    #dialog-hint {{ color: {MUTED}; }}
    TabbedContent {{ height: 1fr; }}
    Footer {{ background: #0a1a2b; }}
    """

    BINDINGS = [
        Binding("f", "forward", "forward"),
        Binding("d", "drop", "drop"),
        Binding("space", "step", "step"),
        Binding("i", "toggle_intercept", "intercept on/off"),
        Binding("s", "toggle_resigning", "header/body repair"),
        Binding("N", "toggle_stale_negative", "allow stale negative control"),
        Binding("e", "edit", "edit"),
        Binding("u", "run_until", "run until…"),
        Binding("z", "freeze", "freeze"),
        Binding("g", "thaw", "run"),
        Binding("r", "revert", "undo edits"),
        Binding("1", "tab('intercept')", "Intercept", show=False),
        Binding("2", "tab('history')", "History", show=False),
        Binding("3", "tab('state')", "State", show=False),
        Binding("4", "tab('points')", "Freeze points", show=False),
        Binding("5", "tab('overview')", "Overview", show=False),
        Binding("6", "tab('payloads')", "Payloads", show=False),
        Binding("7", "tab('scenarios')", "Scenarios", show=False),
        Binding("8", "tab('nodes')", "Nodes", show=False),
        Binding("9", "tab('source')", "Source", show=False),
        Binding("p", "attack", "payloads"),
        Binding("O", "claim", "claim/release"),
        Binding("q", "quit", "quit"),
    ]

    def __init__(
        self,
        client: FreezeClient,
        session: Session | None = None,
        glossary: Glossary | None = None,
        store: payloads.Store | None = None,
    ) -> None:
        super().__init__()
        self.client = client
        self.session = session or Session()
        self.node_name: str | None = None
        # The devnet's Freeze nodes as {name: {"host", "port", "token"}}, when there are several.
        self.nodes: dict[str, dict[str, Any]] = {}
        self.switch_to: str | None = None
        # Gives a `source.Debugger` beside this node, or raises; set by whoever knows where the node runs.
        self.source_factory: Any = None
        self.debugger: Any = None
        self._source_events: collections.deque[dict[str, Any]] = collections.deque()
        self._source_busy = False
        self._others: dict[str, FreezeClient] = {}
        self._feed: collections.deque[tuple[float, str, dict[str, Any]]] = collections.deque(maxlen=2000)
        self._node_rows: dict[str, tuple[Any, ...]] = {}
        self._other_held: dict[tuple[str, int], dict[str, Any]] = {}
        self._held_rows: list[str] = []
        self._nodes_drawn = 0.0
        # Sampling: on a node producing more than a UI can read, only one item in `sample` is
        # asked for (held items always arrive). `node_rate` is what the node says it produces.
        self.sample = 1
        self.node_rate = 0.0
        self._stats_at = 0.0
        self.sampling = True
        self.peers_state: dict[str, Any] = (client.broker or {}).get("peers") or {}
        self.owner: int | None = (client.broker or {}).get("owner")
        self.glossary = glossary or default_glossary()
        if store is None:
            from .cli import data_root

            store = payloads.Store(data_root() / "payloads")
        self.store = store
        self.engine = rules.Engine(self.client, self.session, on_change=self._payloads_changed)
        self.current_list: str | None = None
        self._payloads_dirty = True
        self._scenarios: list[scenarios.Entry] = []
        self._scenarios_all = False
        self._scenario_filter = ""
        self._incoming: collections.deque[tuple[str, Any]] = collections.deque()
        self.intercept_on = False
        self.resigning: dict[str, Any] = {"enabled": False, "available": False}
        self.selected: int | None = None
        self.edits: dict[int, Any] = {}
        self.raw_edits: dict[int, bytes] = {}
        self.changed: dict[int, set[render.Path]] = {}
        # Per-edit authorization to emit deliberately stale validation material. This is never
        # inferred from an edit or from signing being unavailable: the operator must select it.
        self.stale_negative_controls: set[int] = set()
        self.history_filter: Any = None
        self.history_seq: int | None = None
        from . import notes as notes_module

        self.marks = notes_module.Notes(self.session.directory)
        self.compare_with: int | None = None
        self.state_path: str | None = None
        self.state_value: Any = None
        self.state_edit: Any = None
        self.state_changed: set[render.Path] = set()
        self.last_seq = 0
        self._client_timeout = client.timeout
        self.message = ""
        self._rate_mark = (time.monotonic(), 0)
        self.rate = 0.0

    # layout --------------------------------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield Static(id="status")
        with TabbedContent(initial="intercept"):
            with TabPane("Intercept", id="intercept"):
                with Horizontal(id="actions"):
                    yield Button("Forward", id="forward", classes="primary")
                    yield Button("Drop", id="drop")
                    yield Button("Step", id="step")
                    yield Button("Intercept is off", id="intercept-toggle")
                    yield Button("Re-signing unavailable", id="resign-toggle")
                    yield Button("Run until…", id="run-until")
                    yield Button("Undo edits", id="revert")
                with Horizontal(id="holding"):
                    yield Label("holding:")
                    for name, _kinds in HOLD_GROUPS:
                        yield Checkbox(name, value=name in DEFAULT_HOLD, id="hold-" + name.replace(" ", "-"))
                with Horizontal(id="intercept-panes"):
                    yield DataTable(id="held", classes="pane", cursor_type="row", zebra_stripes=False)
                    yield PayloadTree(self.glossary, id="item", classes="pane")
                    with VerticalScroll(id="inspector", classes="pane"):
                        yield Static(id="inspector-text")
            with TabPane("History", id="history"):
                yield Input(placeholder="filter, e.g.  kind=input and to~validate   (enter to apply, empty for all; @name uses a saved filter, @name = … saves one) · X export · b bookmark · m note · F finding · c compare · R send again", id="history-filter")
                yield DataTable(id="history-table", classes="pane", cursor_type="row")
                with Horizontal():
                    yield PayloadTree(self.glossary, id="history-in", classes="pane")
                    with VerticalScroll(id="history-did", classes="pane"):
                        yield Static(id="history-did-text")
            with TabPane("State", id="state"):
                with Horizontal():
                    yield Tree("what the node holds", id="state-keys", classes="pane")
                    with Vertical(id="state-value"):
                        with Horizontal(id="state-actions"):
                            yield Button("Apply edits", id="state-apply", classes="primary")
                            yield Button("Reload", id="state-reload")
                        yield PayloadTree(self.glossary, id="state-tree", classes="pane")
            with TabPane("Freeze points", id="points"):
                yield Static(
                    "Freeze the node whenever an item matches a condition. Examples:\n"
                    "  kind=input and to~validate_block      label=RollBackward      slot>=138760000\n"
                    "  to~select_chain and type~NewTip        txs#>10                 spot=block-check",
                    id="points-help",
                )
                yield Input(placeholder="new condition (enter to add; prefix with `log ` to only log, `once ` to fire once)", id="points-new")
                yield DataTable(id="points-table", classes="pane", cursor_type="row")
                yield DataTable(id="probes-table", classes="pane", cursor_type="row")
            with TabPane("Payloads", id="payloads"):
                yield Static(
                    "Lists of test values, and rules that apply them for you. In Intercept, select a field of a held item "
                    "and press p to run a list against it: one value per matching item, automatically.\n"
                    "n new list · a add value · e edit value · x delete · I import list file · E export list · "
                    "T load template · S stop all rules · C clear results · R save results",
                    id="pl-help",
                )
                with Horizontal():
                    yield DataTable(id="pl-lists", classes="pane", cursor_type="row")
                    yield DataTable(id="pl-values", classes="pane", cursor_type="row")
                    with Vertical(id="pl-right"):
                        yield DataTable(id="pl-rules", classes="pane", cursor_type="row")
                        yield DataTable(id="pl-results", classes="pane", cursor_type="row")
            with TabPane("Scenarios", id="scenarios"):
                yield Static(
                    "DWARF's scenarios. The ones marked ready or partly can be carried out from inside this node: "
                    "L loads the selected one as rules and starts them (results appear in Payloads) · "
                    "W writes it as a template file · A shows or hides the ones Freeze cannot do, with the reason.",
                    id="sc-help",
                )
                yield Input(placeholder="filter by words in the name, subject or target (enter to apply)", id="sc-filter")
                with Horizontal():
                    yield DataTable(id="sc-table", classes="pane", cursor_type="row")
                    with VerticalScroll(id="sc-detail", classes="pane"):
                        yield Static(id="sc-detail-text")
            with TabPane("Overview", id="overview"):
                with VerticalScroll(id="overview-scroll", classes="pane"):
                    yield Static(id="overview-text")
            with TabPane("Source", id="source"):
                yield Static(
                    "Step through the node's source lines. A attaches a debugger to the node (and detaches it) · "
                    "b break at FILE:LINE or a function · c continue · s step into · n next line · o out of this function · "
                    "x stop the node now. While the node is stopped here, its Freeze agent is stopped too: "
                    "the other tabs get no answers until you continue or detach.",
                    id="src-help",
                )
                with Horizontal():
                    with VerticalScroll(id="src-code", classes="pane"):
                        yield Static(SOURCE_NOT_ATTACHED, id="src-code-text")
                    with Vertical(id="src-side"):
                        yield DataTable(id="src-stack", classes="pane", cursor_type="row")
                        yield DataTable(id="src-vars", classes="pane", cursor_type="row")
            with TabPane("Nodes", id="nodes"):
                yield Static(
                    "Every Freeze node of this devnet, what each is holding, and what all of them did, merged by time. "
                    "enter moves this window to the selected node · Z freezes all · G runs all · "
                    "on a held item: f forward and run, space forward and stay frozen, d drop, enter opens it on its node to edit",
                    id="nodes-help",
                )
                yield DataTable(id="nodes-table", classes="pane", cursor_type="row")
                yield DataTable(id="nodes-held", classes="pane", cursor_type="row")
                yield DataTable(id="nodes-feed", classes="pane", cursor_type="row")
        yield Footer()

    def on_resize(self, event: Any) -> None:
        self._fit_width(event.size.width)

    def _fit_width(self, width: int) -> None:
        """Wrap the button and "holding" rows and stack the panes when the terminal is not wide enough for one row."""
        for widget in self.query("#actions, #holding, #intercept-panes"):
            widget.set_class(width < COMPACT_WIDTH, "compact")
            widget.set_class(width < NARROW_WIDTH, "narrow")

    def on_mount(self) -> None:
        self._fit_width(self.size.width)
        held = self.query_one("#held", DataTable)
        held.add_columns("#", "from → to", "what")
        held.border_title = "HELD"
        history = self.query_one("#history-table", DataTable)
        history.add_columns("#", "time", "", "from", "to", "what", "about", "node said")
        history.border_title = "HISTORY"
        self.query_one("#item", PayloadTree).border_title = "ITEM"
        self.query_one("#inspector").border_title = "INSPECTOR"
        self.query_one("#history-in", PayloadTree).border_title = "WHAT WENT IN"
        self.query_one("#history-did").border_title = "WHAT THE NODE DID"
        self.query_one("#state-keys").border_title = "STATE"
        self.query_one("#overview-scroll").border_title = "OVERVIEW"
        self.query_one("#state-tree", PayloadTree).border_title = "VALUE"
        lists = self.query_one("#pl-lists", DataTable)
        lists.add_columns("list", "values", "")
        lists.border_title = "PAYLOAD LISTS"
        values = self.query_one("#pl-values", DataTable)
        values.add_columns("#", "value")
        values.border_title = "VALUES"
        armed = self.query_one("#pl-rules", DataTable)
        armed.add_columns("rule", "when", "does", "progress", "")
        armed.border_title = "RULES"
        results = self.query_one("#pl-results", DataTable)
        results.add_columns("#", "rule", "item", "what", "value", "was → became", "node said")
        results.border_title = "RESULTS"
        catalogue = self.query_one("#sc-table", DataTable)
        catalogue.add_columns("can run", "about", "target", "scenario")
        catalogue.border_title = "DWARF SCENARIOS"
        self.query_one("#sc-detail").border_title = "SCENARIO"
        points = self.query_one("#points-table", DataTable)
        points.add_columns("id", "when", "then", "hits")
        points.border_title = "CONDITIONS  (x deletes the selected one)"
        probes = self.query_one("#probes-table", DataTable)
        probes.add_columns("reports", "reached", "probe", "where in the node")
        probes.border_title = "PROBES  named places inside the node's functions  (enter turns reporting on or off · a all on · n all off)"

        nodes = self.query_one("#nodes-table", DataTable)
        nodes.add_columns("node", "agent", "", "step", "held", "")
        nodes.border_title = "FREEZE NODES"
        everywhere = self.query_one("#nodes-held", DataTable)
        everywhere.add_columns("node", "#", "from → to", "what")
        everywhere.border_title = "HELD ON ANY NODE"
        feed = self.query_one("#nodes-feed", DataTable)
        feed.add_columns("time", "node", "#", "", "from", "to", "what")
        feed.border_title = "ALL NODES, BY TIME"
        stack = self.query_one("#src-stack", DataTable)
        stack.add_columns("#", "function", "line")
        stack.border_title = "CALL STACK"
        variables = self.query_one("#src-vars", DataTable)
        variables.add_columns("name", "value", "type")
        variables.border_title = "LOCALS"
        self.query_one("#src-code").border_title = "SOURCE"

        # A node on a public network may be catching up at tens of thousands of items a second,
        # more than this window can take in before it could even notice. Start with a sample
        # there; it is widened to everything within seconds if the node turns out to be quiet.
        network = str(self.client.hello.get("node", {}).get("network", ""))
        if self.sampling and self.client.supports("events:filter") and network and not network.startswith("testnet"):
            self._subscribe(50)
            self.node_rate, self._stats_at = 100_000.0, time.monotonic()
        self.client.on_event(lambda name, data: self._incoming.append((name, data)))
        try:
            status = self.client.status()
            self.session.sync(status, self.client.item)
            self.intercept_on = status.get("hold") is not None
            self.resigning = status.get("resigning") or {"enabled": False, "available": False}
            self.last_seq = status.get("seq", 0)
        except FreezeError as error:
            self.message = str(error)
        self._refresh_intercept_button()
        self._refresh_resign_button()
        self._refresh_held()
        self._refresh_status()
        self.set_interval(0.1, self._drain)
        self.set_interval(1.0, self._refresh_status)
        self.query_one("#held", DataTable).focus()

    # events from the node ------------------------------------------------------------------

    def _drain(self) -> None:
        """Apply events that arrived from the node since the last tick."""
        try:
            self._drain_events()
        except NoMatches:
            pass  # a tick can fire while the screen is being built or torn down

    def _drain_events(self) -> None:
        while self._source_events:
            self._source_event(self._source_events.popleft())
        if not self._incoming:
            return
        held_changed = False
        rows: list[dict[str, Any]] = []
        for _ in range(min(len(self._incoming), 5000)):
            name, data = self._incoming.popleft()
            self.session.record(name, data)
            if name == "item":
                self.last_seq = max(self.last_seq, data["seq"])
                rows.append(data)
                held_changed |= bool(data.get("held"))
                if data.get("held"):
                    self.engine.offer(data)
            elif name == "released":
                held_changed = True
            elif name in ("frozen", "thawed"):
                held_changed = True
            elif name == "log":
                self.message = f"{data.get('level', '')}: {data.get('message', '')}"
            elif name == "stats":
                self.last_seq = max(self.last_seq, (data or {}).get("seq", 0))
                self.node_rate = float((data or {}).get("items", 0))
                self._stats_at = time.monotonic()
            elif name == "peers":
                self.peers_state = data or {}
            elif name == "session":
                self.owner = (data or {}).get("owner")
            elif name == "closed":
                self.message = "connection to the node closed"
        if rows:
            self._append_history(rows)
        if held_changed:
            self._refresh_held()
        self._refresh_status()

    # status bar ----------------------------------------------------------------------------

    def _refresh_status(self) -> None:
        try:
            self._draw_status()
        except NoMatches:
            pass  # a tick can fire while the screen is being built or torn down

    def _draw_status(self) -> None:
        node = self.client.hello.get("node", {})
        text = Text()
        text.append("❄ DWARF FREEZE", style=f"bold {ICE}")
        text.append(f"   {node.get('implementation', 'node')} {node.get('version', '')} · {node.get('network', '')}   ")
        if self.node_name:
            text.append(f"{self.node_name}   ", style=f"bold {OK}")
        if self.debugger is not None and self.debugger.attached and not self.debugger.running:
            text.append(" STOPPED IN THE DEBUGGER ", style=f"bold white on {RED}")
        elif not self.client.connected:
            text.append(" DISCONNECTED ", style=f"bold white on {RED}")
        elif self.session.frozen:
            text.append(" FROZEN ", style=f"bold #081220 on {ICE}")
        else:
            text.append(" RUNNING ", style=f"bold #081220 on {OK}")
        text.append(f"   step {self.last_seq:,}   held {len(self.session.held)}")
        if self.sample > 1:
            text.append(f"   showing 1 in {self.sample}", style=f"bold {RED}")
        if self.resigning.get("available"):
            text.append(f"   re-sign {'on' if self.resigning.get('enabled') else 'off'}", style=ICE if self.resigning.get("enabled") else MUTED)
        if self.session.tampers:
            text.append(f"   {self.session.tampers} tamper{'s' if self.session.tampers != 1 else ''}", style=f"bold {RED}")
        broker = self.client.broker
        if broker:
            if self.peers_state.get("paused"):
                text.append("   peers paused", style=ICE)
            if self.owner is not None:
                mine = self.owner == broker.get("client")
                text.append("   only this window can change the node" if mine else f"   WATCHING (client {self.owner} has claimed the node; O takes it)",
                            style=OK if mine else f"bold {RED}")
        if self.message:
            text.append(f"   {self.message}", style=MUTED)
        self.query_one("#status", Static).update(text)
        marked_at, marked_seq = self._rate_mark
        elapsed = time.monotonic() - marked_at
        if elapsed >= 2.0:
            self.rate = (self.last_seq - marked_seq) / elapsed
            self._rate_mark = (time.monotonic(), self.last_seq)
            self._adjust_sampling()
        active = self.query_one(TabbedContent).active
        if active == "overview":
            self._refresh_overview()
        elif active == "nodes":
            self._refresh_nodes()
        elif active == "payloads" and (self._payloads_dirty or self.engine.attempts):
            self._refresh_payloads()

    def _adjust_sampling(self) -> None:
        """Ask for fewer items when the node produces more than this window can read, and for
        all of them again when it calms down. With a History filter set, everything matching it
        is asked for instead, so the filter never shows less than there is."""
        if not self.sampling or not self.client.connected or not self.client.supports("events:filter"):
            return
        # With a sample in force the node says what it produces; if it says nothing, little is being left out.
        produced = self.node_rate if time.monotonic() - self._stats_at < 3.0 else (self.rate if self.sample == 1 else 0.0)
        # About 400 items a second is what this window can list and still answer keys at once.
        wanted = self.sample
        if produced > 1500 and self.sample == 1:
            wanted = max(2, int(produced // 400))
        elif self.sample > 1:
            ideal = max(1, int(produced // 400))
            if ideal >= self.sample * 2 or ideal * 2 <= self.sample:
                wanted = ideal if produced > 800 else 1
        if wanted != self.sample:
            self._subscribe(wanted)

    def _subscribe(self, sample: int) -> None:
        try:
            if sample > 1 and self.history_filter is not None:
                self.client.subscribe(when=self.history_filter)
            else:
                self.client.subscribe(sample=sample)
        except FreezeError as error:
            self.message = str(error)
            return
        if sample != self.sample:
            self.message = (f"the node is producing about {self.node_rate or self.rate:,.0f} items a second: showing 1 in {sample} "
                            "(held items always arrive)") if sample > 1 else "showing every item again"
        self.sample = sample

    def _say(self, message: str) -> None:
        self.message = message
        self._refresh_status()

    # Intercept -----------------------------------------------------------------------------

    def _party(self, party: str) -> Text:
        return styled(self.glossary.party(party)) if party else Text("-", style=MUTED)

    def _what(self, item: dict[str, Any]) -> Text:
        message = cardano.item_message_name(item)
        if message is not None:
            return styled(self.glossary.lookup(message, "kind"))
        default_cls = "action" if item["kind"] in ("internal_action",) else "kind"
        entry = self.glossary.lookup(item.get("label") or item.get("type") or item["kind"], default_cls)
        return styled(entry)

    def _refresh_held(self) -> None:
        table = self.query_one("#held", DataTable)
        held = dict(self.session.held)
        table.clear()
        for seq in sorted(held):
            item = held[seq]
            route = Text.assemble(self._party(item.get("from", "")), (" → ", MUTED), self._party(item.get("to", "")))
            what = self._what(item)
            if seq in self.edits or seq in self.raw_edits:
                what.append("  CHANGED", style=f"bold {RED}")
            table.add_row(Text(str(seq), style=MUTED), route, what, key=str(seq))
        table.border_title = f"HELD  {len(held)} item{'s' if len(held) != 1 else ''}" + ("  ·  node frozen" if self.session.frozen else "")
        if self.selected not in held:
            self.selected = min(held) if held else None
        if self.selected is not None:
            try:
                table.move_cursor(row=table.get_row_index(str(self.selected)))
            except Exception:
                pass
        self._show_selected()

    def _show_selected(self) -> None:
        tree = self.query_one("#item", PayloadTree)
        inspector = self.query_one("#inspector-text", Static)
        item = self.session.held.get(self.selected) if self.selected is not None else None
        if item is None:
            tree.clear()
            tree.border_title = "ITEM"
            hint = (
                "Nothing is held.\n\nTurn Intercept on (i) to hold what you ticked above, press z to freeze the node "
                "right now, or add a condition under Freeze points."
            )
            inspector.update(Text(hint, style=MUTED))
            return
        seq = item["seq"]
        payload, names, raw = item_view(item, self.raw_edits.get(seq))
        if not raw:
            payload = self.edits.get(seq, payload)
        tree.show(payload, self.changed.get(seq), names=names, loose_bytes=not raw, expand_depth=3 if raw else 2)
        title = Text.assemble((f"ITEM {seq} · ", ""), self._what(item))
        tree.border_title = title.plain
        self._inspect(item, None)

    def _inspect(self, item: dict[str, Any], node: render.Node | None) -> None:
        text = Text()

        def heading(title: str) -> None:
            text.append(f"\n{title}\n" if text else f"{title}\n", style=ICE)

        def row(name: str, value: Text | str) -> None:
            text.append(f" {name:<10} ", style=MUTED)
            text.append(value if isinstance(value, Text) else Text(value))
            text.append("\n")

        heading("Item")
        row("what", self._what(item))
        row("from", self._party(item.get("from", "")))
        row("to", self._party(item.get("to", "")))
        row("kind", KIND_WORDS.get(item["kind"], item["kind"]))
        if item.get("cold_spots"):
            row("cold spot", Text(" ".join("◆ " + spot for spot in item["cold_spots"]), style=ICE))
        row("can", ", ".join(x for x, ok in (("edit", item.get("editable")), ("drop", item.get("droppable"))) if ok) or "forward only")

        heading("In plain words")
        message = cardano.item_message_name(item)
        what = self.glossary.lookup(message or item.get("label") or item.get("type") or "", "kind")
        inside = item.get("from", "") if item["kind"] in ("internal_action", "proto_out", "wire_out") else item.get("to", "")
        target = self.glossary.party(inside)
        said: list[str] = []
        for sentence in (what.what, what.why, target.what, target.why):
            if sentence and sentence not in said:
                said.append(sentence)
                text.append(f" {sentence}\n", style=MUTED)
        if not (what.what or target.what):
            text.append(" No description for this yet.\n", style=MUTED)

        if node is not None and node.name:
            entry = self.glossary.lookup(node.name)
            heading("Selected field")
            row("name", Text(entry.label, style=COLOURS[entry.cls]))
            row("raw name", node.name)
            if node.leaf:
                row("value", node.summary)
            if entry.what:
                text.append(f" {entry.what}\n", style=MUTED)
            if entry.why:
                text.append(f" {entry.why}\n", style=MUTED)

        seq = item["seq"]
        if seq in self.changed:
            heading("Your edits")
            original, _names, raw = item_view(item)
            current = item_view(item, self.raw_edits[seq])[0] if raw else self.edits[seq]
            for path in sorted(self.changed[seq], key=str):
                before = render.build(render.get_at(original, path)).summary
                after = render.build(render.get_at(current, path)).summary
                text.append(f" {'.'.join(str(step) for step in path)}\n", style=COLOURS["field"])
                text.append(f"   {before} → ", style=MUTED)
                text.append(f"{after}\n", style=RED)
            if raw and (commitment := cardano.block_body_commitment(item.get("type", ""), self.raw_edits[seq])):
                body_hash, body_size = commitment
                heading("Edited block commitment")
                row("body hash", body_hash)
                row("body size", f"{body_size:,} bytes")
                text.append(
                    " Put both values in the matching ChainSync header before replaying this body; "
                    "Freeze will reject a body edit without that pre-announced signed commitment.\n",
                    style=MUTED,
                )
            impact = self._edit_impact(item, seq)
            if impact is not None:
                heading("Dependency state")
                state = impact["state"]
                row("outcome", Text(state.replace("-", " ").upper(), style=RED if state != "auto-repair" else ICE))
                if impact.get("code"):
                    row("refusal code", impact["code"])
                if impact["damage"]:
                    row("damage", ", ".join(impact["damage"]))
                if impact["repairs"]:
                    row("repairs", ", ".join(impact["repairs"]))
                if impact["remaining"]:
                    row("remaining stale", ", ".join(impact["remaining"]))
                if impact["reason"]:
                    text.append(f" {impact['reason']}\n", style=RED)
        self.query_one("#inspector-text", Static).update(text)

    def _edit_impact(self, item: dict[str, Any], seq: int) -> dict[str, Any] | None:
        """The pre-emission dependency state for one local edit."""
        if seq in self.raw_edits and item.get("raw") is not None:
            return cardano.edit_impact(
                item.get("type", ""),
                bytes.fromhex(item["raw"]),
                self.raw_edits[seq],
                resigning=self.resigning,
                item_kind=item.get("kind"),
                stale_negative_control=seq in self.stale_negative_controls,
            )
        if seq in self.edits:
            return {
                "surface": item.get("kind", "typed"),
                "state": "auto-repair",
                "code": None,
                "damage": [],
                "repairs": ["typed value reconstruction"],
                "remaining": [],
                "reason": None,
            }
        return None

    @on(DataTable.RowHighlighted, "#held")
    def _held_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if event.row_key is not None and event.row_key.value is not None:
            seq = int(event.row_key.value)
            if seq != self.selected:
                self.selected = seq
                self._show_selected()

    @on(Tree.NodeHighlighted, "#item")
    def _field_highlighted(self, event: Tree.NodeHighlighted[render.Node]) -> None:
        item = self.session.held.get(self.selected) if self.selected is not None else None
        if item is not None:
            self._inspect(item, event.node.data)

    def _hold_predicate(self) -> dict[str, Any]:
        kinds: list[str] = []
        for name, group in HOLD_GROUPS:
            if self.query_one("#hold-" + name.replace(" ", "-"), Checkbox).value:
                kinds.extend(group)
        return {"op": "kind", "in": kinds}

    def _refresh_intercept_button(self) -> None:
        button = self.query_one("#intercept-toggle", Button)
        button.label = "❄ Intercept is on" if self.intercept_on else "Intercept is off"
        button.set_class(self.intercept_on, "on")

    def _refresh_resign_button(self) -> None:
        button = self.query_one("#resign-toggle", Button)
        available = bool(self.resigning.get("available"))
        enabled = available and bool(self.resigning.get("enabled"))
        button.label = (
            "❄ Header/body repair is on" if enabled else "Header/body repair is off"
        ) if available else "Header/body repair unavailable"
        button.disabled = not available
        button.set_class(enabled, "on")

    def _call(self, description: str, function: Any, *args: Any, **kwargs: Any) -> Any:
        try:
            result = function(*args, **kwargs)
            self._say(description)
            return result
        except FreezeError as error:
            self._say(f"{description}: {error}")
            return None

    def action_toggle_intercept(self) -> None:
        if self.intercept_on:
            self._call("intercept off", self.client.set_hold, None)
            self._call("intercept off; node running", self.client.thaw)
            self.intercept_on = False
        else:
            self._call("intercept on", self.client.set_hold, self._hold_predicate())
            self.intercept_on = True
        self._refresh_intercept_button()

    def action_toggle_resigning(self) -> None:
        if not self.resigning.get("available"):
            self._say(self.resigning.get("error", "re-signing is unavailable"))
            return
        result = self._call(
            "edited-header re-signing " + ("off" if self.resigning.get("enabled") else "on"),
            self.client.set_resigning,
            not bool(self.resigning.get("enabled")),
        )
        if isinstance(result, dict):
            self.resigning = result
        self._refresh_resign_button()
        self._refresh_status()

    def action_toggle_stale_negative(self) -> None:
        if self.selected is None:
            self._say("select an edited held item or a raw network item for a payload rule first")
            return
        seq = self.selected
        item = self.session.held.get(seq)
        if seq not in self.edits and seq not in self.raw_edits and (item is None or item.get("raw") is None):
            self._say("select an edited held item or a raw network item for a payload rule first")
            return
        if seq in self.stale_negative_controls:
            self.stale_negative_controls.remove(seq)
            self._say(f"item {seq}: stale negative-control mode removed")
        else:
            self.stale_negative_controls.add(seq)
            self._say(f"item {seq}: stale negative-control mode explicitly selected")
        if seq in self.edits or seq in self.raw_edits:
            self._show_selected()

    @on(Checkbox.Changed)
    def _hold_changed(self, _event: Checkbox.Changed) -> None:
        if self.intercept_on:
            self._call("holding updated", self.client.set_hold, self._hold_predicate())

    def _decide(self, seq: int, then_run: bool) -> None:
        """Forward an item, with the user's edits if there are any."""
        if seq in self.edits or seq in self.raw_edits:
            item = self.session.held.get(seq) or self.session.items.get(seq)
            impact = self._edit_impact(item, seq) if item is not None else None
            if impact is not None and impact["state"] == "refused":
                reason = impact["reason"] or "the edit has unsupported dependencies"
                self._say(f"item {seq} not emitted: {reason}; press N to select a stale negative control")
                self._show_selected()
                return
            sent = (
                self._call(
                    f"item {seq} forwarded with your changes",
                    self.client.replace,
                    seq,
                    raw=self.raw_edits[seq],
                    stale_negative_control=seq in self.stale_negative_controls,
                )
                if seq in self.raw_edits
                else self._call(f"item {seq} forwarded with your changes", self.client.replace, seq,
                                payload=views.as_sent(self.session.items.get(seq), self.edits[seq]))
            )
            if sent is None:
                return
            self.edits.pop(seq, None)
            self.raw_edits.pop(seq, None)
            self.changed.pop(seq, None)
            self.stale_negative_controls.discard(seq)
            if then_run:
                self._call(f"item {seq} forwarded with your changes; running", self.client.thaw)
        else:
            self._call(f"item {seq} forwarded", self.client.forward, seq, then_run)

    def action_forward(self) -> None:
        if self.selected is None:
            self._say("nothing is held")
        else:
            self._decide(self.selected, then_run=True)

    def action_step(self) -> None:
        if self.selected is None:
            self._say("nothing is held")
        else:
            self._decide(self.selected, then_run=False)

    def action_drop(self) -> None:
        if self.selected is None:
            self._say("nothing is held")
            return
        seq = self.selected
        if self._call(f"item {seq} dropped", self.client.drop, seq) is not None:
            self.edits.pop(seq, None)
            self.raw_edits.pop(seq, None)
            self.changed.pop(seq, None)
            self.stale_negative_controls.discard(seq)

    def action_freeze(self) -> None:
        self._call("freeze requested: the node stops at its next step", self.client.freeze)

    def action_thaw(self) -> None:
        self._call("running", self.client.thaw)

    def action_revert(self) -> None:
        if self.selected is not None and (self.selected in self.edits or self.selected in self.raw_edits):
            self.edits.pop(self.selected, None)
            self.raw_edits.pop(self.selected, None)
            self.changed.pop(self.selected, None)
            self.stale_negative_controls.discard(self.selected)
            self._refresh_held()
            self._say("edits undone")

    def action_edit(self) -> None:
        active = self.query_one(TabbedContent).active
        if active == "state":
            self._edit_state()
            return
        if active == "payloads":
            self._edit_payload_value()
            return
        if active != "intercept":
            return
        item = self.session.held.get(self.selected) if self.selected is not None else None
        tree = self.query_one("#item", PayloadTree)
        node = tree.cursor_node.data if tree.cursor_node is not None else None
        if item is None or node is None:
            self._say("select a field of a held item first")
            return
        if not item.get("editable"):
            self._say("this item cannot be edited")
            return
        if not node.leaf:
            self._say("select a single value to edit (open the group and pick a field)")
            return
        seq = item["seq"]
        payload, _names, raw = item_view(item, self.raw_edits.get(seq))
        if not raw:
            payload = self.edits.get(seq, payload)

        def done(text: str | None) -> None:
            if text is None:
                return
            try:
                value = render.parse_edit(text, node.value)
                if raw and node.path == () and render.is_bytes(value):
                    self.raw_edits[seq] = bytes.fromhex(value["$bytes"])
                elif raw:
                    current = self.raw_edits.get(seq) or bytes.fromhex(item["raw"])
                    self.raw_edits[seq] = cbor_tree.edit(current, node.path, value)
                else:
                    self.edits[seq] = render.set_at(payload, node.path, value)
            except ValueError as error:
                self._say(f"not changed: {error}")
                return
            self.changed.setdefault(seq, set()).add(node.path)
            self._refresh_held()
            self._say("changed locally; Forward or Step sends it to the node")

        self.push_screen(self._edit_dialog(node), done)

    def _edit_dialog(self, node: render.Node) -> EditScreen:
        entry = self.glossary.lookup(node.name) if node.name else None
        if render.is_bytes(node.value):
            current, hint = node.value["$bytes"], "bytes, as hex"
        elif render.is_byte_array(node.value):
            current, hint = bytes(node.value).hex(), "bytes, as hex"
        elif isinstance(node.value, bool):
            current, hint = "yes" if node.value else "no", "yes or no"
        elif isinstance(node.value, dict) and "$int" in node.value:
            current, hint = node.value["$int"], "a whole number"
        elif isinstance(node.value, int):
            current, hint = str(node.value), "a whole number"
        elif node.name == "at" and (self.session.items.get(self.selected) or {}).get("label") == "schedule":
            # A schedule's time is shown as the node wrote it; it is moved by saying how long from now.
            current, hint = "", "milliseconds from now, on the node's clock (which stands still while frozen)"
        elif isinstance(node.value, str):
            current, hint = node.value, "text"
        else:
            current, hint = json.dumps(node.value), "a value"
        return EditScreen(f"New value for {entry.label if entry else 'this value'}", current, f"{hint} · enter to change · esc to cancel")

    def action_run_until(self) -> None:
        def done(text: str | None) -> None:
            if not text:
                return
            try:
                predicate = conditions.compile_condition(text)
            except conditions.ConditionError as error:
                self._say(str(error))
                return
            self._call(f"running until: {text}", self.client.run_until, predicate)
            self._refresh_points()

        self.push_screen(
            EditScreen("Run until…", "", "a condition, e.g.  to~validate_block   label=RollBackward   slot>=138760000"), done
        )

    @on(Button.Pressed)
    def _button(self, event: Button.Pressed) -> None:
        actions = {
            "forward": self.action_forward,
            "drop": self.action_drop,
            "step": self.action_step,
            "intercept-toggle": self.action_toggle_intercept,
            "resign-toggle": self.action_toggle_resigning,
            "run-until": self.action_run_until,
            "revert": self.action_revert,
            "state-apply": self._apply_state,
            "state-reload": self._reload_state,
        }
        action = actions.get(event.button.id or "")
        if action:
            action()

    # History -------------------------------------------------------------------------------

    def _history_row(self, item: dict[str, Any]) -> tuple[Any, ...]:
        seq = item["seq"]
        clock = time.strftime("%H:%M:%S", time.localtime(item.get("t_ms", 0) / 1000))
        verdict = self.session.verdict(seq)
        said = Text(verdict, style=RED if verdict in ("dropped", "edited") else (ICE if verdict == "held" else MUTED))
        return (
            Text(f"{self.marks.badge(seq)}{seq}", style=RED if self.marks.badge(seq) else MUTED),
            Text(clock, style=MUTED),
            Text(KIND_WORDS.get(item["kind"], item["kind"]), style=MUTED),
            self._party(item.get("from", "")),
            self._party(item.get("to", "")),
            self._what(item),
            Text(cardano.about(item.get("type", ""), item.get("payload")) or render.one_line(item.get("payload"), 48), style=FROST),
            said,
        )

    def _history_keeps(self, item: dict[str, Any]) -> bool:
        if self.history_filter is None:
            return item["kind"] != "internal_state"
        return conditions.matches(self.history_filter, item)

    def _append_history(self, items: list[dict[str, Any]]) -> None:
        table = self.query_one("#history-table", DataTable)
        at_end = table.cursor_row >= table.row_count - 1
        for item in items:
            if self._history_keeps(item):
                table.add_row(*self._history_row(item), key=str(item["seq"]))
        while table.row_count > HISTORY_ROWS:
            first_key = next(iter(table.rows))
            table.remove_row(first_key)
        if at_end and table.row_count:
            table.move_cursor(row=table.row_count - 1)

    def _rebuild_history(self, keep: int | None = None) -> None:
        """Fill the table again; ``keep`` is the item the cursor should stay on."""
        table = self.query_one("#history-table", DataTable)
        table.clear()
        for item in self.session.recent(HISTORY_ROWS, self._history_keeps):
            table.add_row(*self._history_row(item), key=str(item["seq"]))
        if table.row_count:
            keys = [str(key.value) for key in table.rows]
            wanted = str(keep) if keep is not None and str(keep) in keys else None
            table.move_cursor(row=keys.index(wanted) if wanted else table.row_count - 1)

    @on(Input.Submitted, "#history-filter")
    def _history_filter_submitted(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        if text.startswith("@"):
            # Saved filters: `@name` uses one, `@name = condition` saves one, `@-name` forgets one, `@` lists them.
            from . import filters
            from .cli import data_root

            try:
                said, text = filters.Filters(data_root()).typed(text)
            except filters.FilterError as error:
                self._say(str(error))
                return
            if said:
                self._say(said)
            if not text:
                return
        if not text:
            self.history_filter = None
        else:
            try:
                self.history_filter = conditions.compile_condition(text)
            except conditions.ConditionError as error:
                self._say(str(error))
                return
        if self.sample > 1:
            self._subscribe(self.sample)
        self._rebuild_history()
        self.query_one("#history-table", DataTable).focus()

    @on(DataTable.RowHighlighted, "#history-table")
    def _history_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if event.row_key is None or event.row_key.value is None:
            return
        seq = int(event.row_key.value)
        if seq == self.history_seq:
            return
        self.history_seq = seq
        item = self.session.items.get(seq)
        if item is None:
            return
        release = self.session.releases.get(seq, {})
        tree = self.query_one("#history-in", PayloadTree)
        original, names, raw = item_view(item)
        if release.get("verdict") == "replace" and ("payload" in release or "raw" in release):
            edited = item_view(item, bytes.fromhex(release["raw"]))[0] if "raw" in release else release["payload"]
            tree.show(edited, views.diff_paths(original, edited), names=names, loose_bytes=not raw)
            tree.border_title = f"WHAT WENT IN · item {seq} · edited by you"
        else:
            tree.show(original, names=names, loose_bytes=not raw)
            tree.border_title = f"WHAT WENT IN · item {seq}"
        self.query_one("#history-did-text", Static).update(self._reaction(item))

    def _reaction(self, item: dict[str, Any]) -> Text:
        seq = item["seq"]
        text = Text()
        verdict = self.session.verdict(seq)
        if verdict == "dropped":
            text.append("You dropped this item; the node never saw it.\n", style=RED)
        elif verdict == "edited":
            text.append("You edited this item before the node saw it.\n", style=RED)
        elif verdict == "held":
            text.append("Still held: the node has not acted on it yet.\n", style=ICE)
        outcome = self.session.outcomes.get(seq)
        if isinstance(outcome, dict) and outcome.get("terminated"):
            text.append("The part of the node handling this stopped.\n", style=RED)
        followed = self.session.reaction(seq)
        if not followed and not text:
            text.append("Nothing followed from this (yet).\n", style=MUTED)
        for depth, child in followed:
            if child["kind"] == "internal_state":
                continue
            text.append("  " * depth + f"{child['seq']:>6} ", style=MUTED)
            text.append(f"{KIND_WORDS.get(child['kind'], child['kind']):<5} ", style=MUTED)
            text.append(self._what(child))
            if child.get("to") and child["kind"] == "internal_action" and child.get("to") != "node":
                text.append(" → ", style=MUTED)
                text.append(self._party(child["to"]))
            summary = render.one_line(child.get("payload"), 50)
            if summary and summary not in ("none", "(empty)"):
                text.append(f"  {escape(summary)}", style=FROST)
            child_verdict = self.session.verdict(child["seq"])
            if child_verdict:
                text.append(f"  {child_verdict}", style=RED if child_verdict != "held" else ICE)
            text.append("\n")
        return text

    # State ---------------------------------------------------------------------------------

    STATE_PAGE = 200

    def _load_state_keys(self) -> None:
        tree = self.query_one("#state-keys", Tree)
        tree.clear()
        tree.root.expand()
        try:
            namespaces = self.client.namespaces()
        except FreezeError as error:
            self._say(str(error))
            return
        for namespace in namespaces:
            # The node's parts are few and looked at often; stores can hold thousands of
            # entries, so those are read a page at a time, when their branch is opened.
            branch = tree.root.add(Text(namespace, style=COLOURS["part"]), data=("namespace", namespace), expand=False)
            if namespace == "stages":
                self._load_state_page(branch, namespace, None)
                branch.expand()
            else:
                branch.add_leaf(Text("…", style=MUTED), data=("more", namespace, None))
        if not namespaces:
            tree.root.add_leaf(Text("this node exposes no state yet", style=MUTED))

    @staticmethod
    def _state_label(namespace: str, entry: dict[str, Any]) -> Text:
        """A store entry in a line: chain entries by slot and hash, the rest by their key."""
        key, value = str(entry["key"]), entry.get("value")
        if namespace.startswith("chain/") and isinstance(value, dict) and "slot" in value and "hash" in value:
            label = Text(f"slot {value['slot']:,}" if isinstance(value["slot"], int) else f"slot {value['slot']}")
            if isinstance(value.get("height"), int):
                label.append(f"  block {value['height']:,}", style=FROST)
            label.append(f"  {str(value['hash'])[:12]}…", style=MUTED)
            if value.get("stored") is False:
                label.append("  header only", style=MUTED)
            elif isinstance(value.get("bytes"), int):
                label.append(f"  {value['bytes']:,} bytes", style=MUTED)
            return label
        return Text(key if len(key) <= 60 else f"{key[:28]}…{key[-28:]}")

    def _load_state_page(self, branch: Any, namespace: str, after: str | None) -> None:
        try:
            entries = self.client.state_list(namespace, after=after, limit=self.STATE_PAGE)
        except FreezeError as error:
            branch.add_leaf(Text(str(error), style=RED))
            return
        for entry in entries:
            if namespace == "stages":
                label = styled(self.glossary.lookup(entry["key"], "part"))
                label.append(f"  {entry['key']}", style=MUTED)
            else:
                label = self._state_label(namespace, entry)
            branch.add_leaf(label, data=f"{namespace}/{entry['key']}")
        if not entries and after is None:
            branch.add_leaf(Text("empty", style=MUTED))
        if len(entries) >= self.STATE_PAGE:
            branch.add_leaf(Text(f"more… (next {self.STATE_PAGE})", style=ICE), data=("more", namespace, str(entries[-1]["key"])))

    @on(Tree.NodeExpanded, "#state-keys")
    def _state_expanded(self, event: Tree.NodeExpanded[Any]) -> None:
        children = list(event.node.children)
        if len(children) == 1 and isinstance(children[0].data, tuple) and children[0].data[0] == "more" and children[0].data[2] is None:
            _more, namespace, _after = children[0].data
            children[0].remove()
            self._load_state_page(event.node, namespace, None)

    @on(Tree.NodeSelected, "#state-keys")
    def _state_selected(self, event: Tree.NodeSelected[Any]) -> None:
        data = event.node.data
        if isinstance(data, tuple) and data[0] == "more" and data[2] is not None and event.node.parent is not None:
            _more, namespace, after = data
            parent = event.node.parent
            event.node.remove()
            self._load_state_page(parent, namespace, after)
        elif isinstance(data, str):
            self.state_path = data
            self._reload_state()

    def _reload_state(self) -> None:
        if self.state_path is None:
            return
        value = self._call(f"loaded {self.state_path}", self.client.state_get, self.state_path)
        self.state_value = value
        self.state_edit = None
        self.state_changed = set()
        self._show_state()

    def _state_payload(self) -> Any:
        value = self.state_edit if self.state_edit is not None else self.state_value
        return value.get("payload", value) if isinstance(value, dict) and "payload" in value else value

    def _show_state(self) -> None:
        tree = self.query_one("#state-tree", PayloadTree)
        tree.show(self._state_payload(), self.state_changed, expand_depth=1)
        tree.border_title = f"VALUE · {self.state_path}" + ("  ·  edits not applied yet" if self.state_changed else "")

    def _edit_state(self) -> None:
        tree = self.query_one("#state-tree", PayloadTree)
        node = tree.cursor_node.data if tree.cursor_node is not None else None
        if node is None or not node.leaf or self.state_value is None:
            self._say("select a single value to edit")
            return
        payload = self._state_payload()

        def done(text: str | None) -> None:
            if text is None:
                return
            try:
                value = render.parse_edit(text, node.value)
            except ValueError as error:
                self._say(f"not changed: {error}")
                return
            edited = render.set_at(payload, node.path, value)
            wrapped = isinstance(self.state_value, dict) and "payload" in self.state_value
            self.state_edit = {**self.state_value, "payload": edited} if wrapped else edited
            self.state_changed.add(node.path)
            self._show_state()

        self.push_screen(self._edit_dialog(node), done)

    def _apply_state(self) -> None:
        if self.state_path is None or self.state_edit is None:
            self._say("no state edits to apply")
            return
        try:
            receipt = self.client.state_put(self.state_path, self.state_edit)
            self._say(f"wrote {self.state_path}")
        except FreezeError as error:
            if '"unsafe": true' not in str(error) or not isinstance(self.state_edit, dict):
                self._say(f"wrote {self.state_path}: {error}")
                return
            # Some of what the ledger holds can only be written by saying so: afterwards the
            # node's behaviour shows nothing but the edit.
            path, value = self.state_path, self.state_edit

            def confirmed(text: str) -> None:
                if text != "unsafe":
                    self._say("not written")
                    return
                written = self._call(f"wrote {path} (unsafe)", self.client.state_put, path, {**value, "unsafe": True})
                if written is not None:
                    self.session.tampers += 1
                    self.session.record("unsafe_write", {"path": path})
                    self._reload_state()

            self._ask(f"Write {path} anyway?", "", "after this the node's behaviour is evidence of nothing but this edit · type unsafe to write", confirmed)
            return
        if receipt is not None:
            self.session.tampers += 1
            if receipt.get("queued"):
                # A store that belongs to another thread of the node writes when that thread next can.
                self._say(f"{self.state_path}: queued; {receipt.get('applied', 'written by the node shortly')}. {receipt.get('scope', '')}")
            self._reload_state()

    # Freeze points -------------------------------------------------------------------------

    def _refresh_points(self) -> None:
        table = self.query_one("#points-table", DataTable)
        table.clear()
        try:
            for condition in self.client.conditions():
                then = condition["action"] + (" once" if condition.get("once") else "")
                table.add_row(str(condition["id"]), conditions.describe(condition["predicate"]), then, str(condition["hits"]), key=str(condition["id"]))
        except FreezeError as error:
            self._say(str(error))
        self._refresh_probes()

    def _refresh_probes(self, keep: str | None = None) -> None:
        table = self.query_one("#probes-table", DataTable)
        table.clear()
        try:
            probes = self.client.probes()
        except FreezeError:
            # A node built before probes existed, or another implementation without them.
            table.border_subtitle = "this node has no probes"
            return
        for probe in probes:
            table.add_row(Text("on", style=OK) if probe["enabled"] else Text("off", style=MUTED),
                          Text(f"{probe['hits']:,}", style=FROST if probe["hits"] else MUTED),
                          probe["name"], Text(str(probe["where"]), style=MUTED), key=probe["name"])
        table.border_subtitle = f"{sum(1 for probe in probes if probe['hits'])} of {len(probes)} reached"
        if keep is not None and table.row_count:
            names = [str(key.value) for key in table.rows]
            if keep in names:
                table.move_cursor(row=names.index(keep))

    @on(DataTable.RowSelected, "#probes-table")
    def _probe_toggled(self, event: DataTable.RowSelected) -> None:
        if event.row_key is None or event.row_key.value is None:
            return
        name = str(event.row_key.value)
        try:
            enabled = next((probe["enabled"] for probe in self.client.probes() if probe["name"] == name), False)
            (self.client.probe_disable if enabled else self.client.probe_enable)(name)
        except FreezeError as error:
            self._say(str(error))
            return
        self._say(f"{name} {'no longer reports' if enabled else 'now reports each time it is reached'}")
        self._refresh_probes(keep=name)

    @on(Input.Submitted, "#points-new")
    def _point_added(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        action, once = "freeze", False
        while True:
            if text.startswith("log "):
                action, text = "log", text[4:].strip()
            elif text.startswith("once "):
                once, text = True, text[5:].strip()
            else:
                break
        try:
            predicate = conditions.compile_condition(text)
        except conditions.ConditionError as error:
            self._say(str(error))
            return
        if self._call(f"condition added: {text}", self.client.add_condition, predicate, action, once) is not None:
            event.input.value = ""
            self._refresh_points()

    def on_key(self, event: Any) -> None:
        try:
            active = self.query_one(TabbedContent).active
        except NoMatches:
            return  # a key can arrive while the screen is being torn down
        if not isinstance(self.focused, Input) and len(self.screen_stack) == 1:
            if active in ("intercept", "history") and event.key == "n":
                event.stop()
                self._inject()
                return
            if active == "source" and event.key in ("A", "b", "c", "s", "n", "o", "x"):
                event.stop()
                self._source_key(event.key)
                return
            if active == "nodes" and event.key in ("f", "space", "d") and self.query_one("#nodes-held", DataTable).has_focus:
                event.stop()
                self._nodes_held_act(event.key)
                return
            if active == "nodes" and event.key in ("Z", "G"):
                event.stop()
                self._all_nodes(freeze=event.key == "Z")
                return
        if active == "payloads" and not isinstance(self.focused, Input) and len(self.screen_stack) == 1:
            if event.key in ("n", "a", "x", "I", "E", "T", "S", "C", "R"):
                event.stop()
                self._payload_key(event.key)
                return
        if not isinstance(self.focused, Input) and len(self.screen_stack) == 1:
            if active == "scenarios" and event.key in ("L", "W", "A"):
                event.stop()
                self._scenario_key(event.key)
                return
            if active == "history" and event.key == "X":
                event.stop()
                self._export_run()
                return
            if active == "history" and event.key in ("b", "m", "F", "c", "R"):
                event.stop()
                self._history_key(event.key)
                return
        if event.key in ("a", "n") and active == "points" and self.query_one("#probes-table", DataTable).has_focus:
            event.stop()
            try:
                changed = self.client.probe_enable("all") if event.key == "a" else self.client.probe_disable("all")
            except FreezeError as error:
                self._say(str(error))
            else:
                self._say(f"{changed} probe(s) turned {'on' if event.key == 'a' else 'off'}")
                self._refresh_probes()
            return
        if event.key == "x" and active == "points" and self.query_one("#points-table", DataTable).has_focus:
            table = self.query_one("#points-table", DataTable)
            if table.row_count:
                key = table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value
                self._call(f"condition {key} removed", self.client.remove_condition, int(key))
                self._refresh_points()

    def _history_key(self, key: str) -> None:
        """Marks and tools on the selected History item: b bookmark, m note, F finding, c compare, R send again."""
        from . import analysis, repeater

        seq = self.history_seq
        item = self.session.items.get(seq) if seq is not None else None
        if item is None:
            self._say("select an item first")
            return
        if key == "b":
            self._say(f"item {seq} bookmarked" if self.marks.toggle_bookmark(seq) else f"item {seq}: bookmark removed")
        elif key == "m":
            def save(text: str) -> None:
                self.marks.comment(seq, "" if text == "-" else text)
                self._say(f"item {seq}: note saved")
                self._rebuild_history(seq)

            self._ask(f"Note on item {seq}", self.marks.marks.get(seq, {}).get("comment", ""), "a note for yourself · - removes it", save)
            return
        elif key == "F":
            def save(text: str) -> None:
                severity, _, title = text.partition(":")
                if not title.strip() or severity.strip() not in ("info", "low", "medium", "high", "critical"):
                    severity, title = "medium", text
                self.marks.finding(seq, "" if title.strip() == "-" else title, severity.strip())
                self._say(f"item {seq}: finding saved; write the report with `freeze-debugger findings -o report.md`")
                self._rebuild_history(seq)

            self._ask(f"Finding about item {seq}", "medium: ", "severity: title (info, low, medium, high, critical) · - removes it", save)
            return
        elif key == "c":
            if self.compare_with is None or self.compare_with == seq:
                self.compare_with = seq
                self._say(f"item {seq} chosen; select another and press c to compare")
                return
            try:
                result = analysis.compare_items(self.session, self.compare_with, seq)
            except KeyError:
                self._say("that item is no longer in memory")
                self.compare_with = None
                return
            text = Text()
            text.append(f"Item {self.compare_with} against item {seq}: {len(result['fields'])} field(s) differ\n", style=ICE)
            for field in result["fields"][:30]:
                text.append(f"  {field['field']}: ", style=MUTED)
                text.append(f"{field['first']}  |  {field['second']}\n")
            text.append(f"node, item {self.compare_with}: {'; '.join(result['first']) or 'nothing recorded'}\n", style=FROST)
            text.append(f"node, item {seq}: {'; '.join(result['second']) or 'nothing recorded'}\n", style=FROST)
            self.query_one("#history-did-text", Static).update(text)
            self.compare_with = None
            return
        elif key == "R":
            def send(text: str) -> None:
                changes = []
                for part in ([] if text == "as it is" else text.split(";")):
                    field, separator, value = part.partition("=")
                    if not separator:
                        self._say("write changes as field=value; field=value")
                        return
                    changes.append((field.strip(), value.strip()))
                try:
                    step = repeater.plan(self.client, item, changes)
                    if step["how"] == "inject":
                        repeater.send(self.client, step)
                        self._say(f"item {seq} delivered again to {step['to']}")
                    else:
                        self.engine.arm(step["rule"])
                        self._say(f"item {seq} will take the place of the next message like it; result in Payloads")
                except Exception as error:  # noqa: BLE001 - whatever is wrong with the request is shown, not raised
                    self._say(str(error))

            self._ask(f"Send item {seq} again", "as it is", "or changes: slot=add:1; block_number=0", send)
            return
        self._rebuild_history(seq)

    # Inject --------------------------------------------------------------------------------

    def _inject(self) -> None:
        """Deliver a new message to a part of the node, starting from the selected item if it was an input."""
        active = self.query_one(TabbedContent).active
        seq = self.history_seq if active == "history" else self.selected
        item = self.session.items.get(seq) if seq is not None else None
        try:
            destinations = sorted(self.client.destinations())
        except FreezeError as error:
            self._say(str(error))
            return
        to, payload = "", ""
        if item is not None and item.get("kind") == "internal_input":
            to = item.get("to", "")
            payload = json.dumps(self.edits.get(seq, item.get("payload")), separators=(",", ":"))

        def deliver(answer: "tuple[str, str] | None") -> None:
            if answer is None:
                return
            target, text = answer
            if target and not target.startswith("stage:") and f"stage:{target}" in destinations:
                target = f"stage:{target}"
            if target not in destinations:
                self._say(f"`{target}` is not a part of the node that can receive a message")
                return
            try:
                message = json.loads(text)
            except ValueError as error:
                self._say(f"the message is not JSON: {error}")
                return
            try:
                self.client.inject(target, message)
            except FreezeError as error:
                self._say(f"not delivered: {error}")
                return
            self.session.tampers += 1
            self.session.record("injected", {"to": target, "payload": message})
            self._say(f"delivered to {target}")

        self.push_screen(InjectScreen(destinations, to, payload), deliver)

    # Source --------------------------------------------------------------------------------

    def _source_task(self, work: Any) -> None:
        """Run a debugger request off the UI thread; what it reports comes back as an event."""
        import threading

        if self._source_busy:
            self._say("the debugger is still busy with the last request")
            return
        self._source_busy = True

        def run() -> None:
            try:
                outcome = work()
                if outcome:
                    self._source_events.append({"event": "note", "text": outcome})
            except Exception as error:  # noqa: BLE001 - whatever went wrong is shown in the status bar
                self._source_events.append({"event": "note", "text": str(error)})
            finally:
                self._source_busy = False

        threading.Thread(target=run, name="freeze-source-request", daemon=True).start()

    def _source_key(self, key: str) -> None:
        debugger = self.debugger
        if key == "A":
            if debugger is not None and not debugger.gone:
                def leave() -> str:
                    debugger.close()
                    self._source_events.append({"event": "detached"})
                    return "detached; the node runs on"

                self._source_task(leave)
                return
            if self.source_factory is None:
                self._say("source stepping needs a node started by `devnet up` or `run` from an image built with --debug-info")
                return

            def attach() -> str:
                new = self.source_factory()
                new.on_event(self._source_events.append)
                new.start()
                self.debugger = new
                about = new.attach()
                return f"attached to process {about['pid']}; the node is stopped"

            self._say("attaching a debugger to the node (reading its symbols can take a while)…")
            self._source_task(attach)
            return
        if debugger is None or debugger.gone or not debugger.attached:
            self._say("press A to attach a debugger to the node first")
            return
        if key == "b":
            def add(text: str) -> None:
                place, _, number = text.rpartition(":")

                def work() -> str:
                    answer = (debugger.call("break", file=place, line=int(number)) if place and number.isdigit()
                              else debugger.call("break", function=text))
                    return f"breakpoint {answer['id']} set at {text} ({answer['places']} place(s))"

                self._source_task(work)

            self._ask("Stop the node at", "", "FILE:LINE, e.g. select_chain/mod.rs:120 · or a function name", add)
            return
        request = {"c": "continue", "s": "step", "n": "next", "o": "out", "x": "interrupt"}[key]
        self._source_task(lambda: debugger.call(request) and None)

    def _source_event(self, event: dict[str, Any]) -> None:
        name = event.get("event")
        if name == "note":
            self._say(event["text"])
        elif name == "running":
            self.client.timeout = self._client_timeout
            self.query_one("#src-code").border_subtitle = "the node is running"
        elif name in ("gone", "closed", "detached"):
            self.client.timeout = self._client_timeout
            self.query_one("#src-code").border_subtitle = "no debugger attached"
            if name != "detached":
                self._say(event.get("error") or "the debugger ended")
        elif name == "stopped":
            # The agent is stopped with the node: do not let other tabs wait long for it.
            self.client.timeout = 2.0
            self._show_stop(event)
        self._refresh_status()

    def _show_stop(self, stop: dict[str, Any]) -> None:
        from . import source as source_module

        at = stop.get("at") or {}
        code = Text()
        code.append(f"{stop.get('reason') or 'stopped'} · thread {stop.get('thread')} {stop.get('name', '')}\n", style=ICE)
        code.append(f"{at.get('function', '?')}\n", style=COLOURS["part"])
        stack = self.query_one("#src-stack", DataTable)
        stack.clear()
        for frame in stop.get("frames", []):
            stack.add_row(str(frame["index"]), frame["function"], f"{Path(frame['file']).name}:{frame['line']}" if frame["line"] else "")
        variables = self.query_one("#src-vars", DataTable)
        variables.clear()
        debugger = self.debugger
        if at.get("line") and debugger is not None:
            code.append(f"{at['file']}:{at['line']}\n\n", style=MUTED)
            try:
                lines = source_module.listing(debugger.call("source", file=at["file"], line=at["line"], around=18), at["line"])
                for number, text, current in lines:
                    code.append(f"{'→' if current else ' '} {number:>5}  {text}\n", style=f"bold {ICE}" if current else "")
                for value in debugger.call("vars", frame=at.get("index", 0)):
                    variables.add_row(value["name"], value["value"], value["type"])
            except source_module.SourceError as error:
                code.append(f"({error})\n", style=MUTED)
        else:
            code.append("\nno source line here (code built without debug information)\n", style=MUTED)
        self.query_one("#src-code-text", Static).update(code)
        self.query_one("#src-code").border_subtitle = "the node is stopped"

    # Nodes ---------------------------------------------------------------------------------

    def _other(self, name: str) -> FreezeClient | None:
        """A connection to another Freeze node of the devnet, opened when the Nodes tab first needs it."""
        client = self._others.get(name)
        if client is not None and client.connected:
            return client
        where = self.nodes[name]
        client = FreezeClient(where["host"], where["port"], token=where.get("token"), timeout=3.0, name="nodes view")
        try:
            client.connect()
        except (OSError, FreezeError):
            return None
        def heard(event: str, data: Any, name: str = name) -> None:
            if event == "item" and isinstance(data, dict):
                self._feed.append((data.get("t_ms", 0) / 1000, name, data))
                if data.get("held"):
                    self._other_held[(name, data["seq"])] = data
            elif event in ("released", "deferred") and isinstance(data, dict):
                self._other_held.pop((name, data.get("seq")), None)

        client.on_event(heard)
        try:
            # A sample is enough for the merged list; what is held always arrives.
            if client.supports("events:filter"):
                client.subscribe(sample=20)
            for seq in client.status().get("held", []):
                self._other_held[(name, seq)] = client.item(seq)
        except FreezeError:
            pass
        self._others[name] = client
        return client

    def _close_others(self) -> None:
        for client in self._others.values():
            client.close()
        self._others.clear()
        self._other_held.clear()

    def _held_everywhere(self) -> list[tuple[str, dict[str, Any]]]:
        """Every held item of every Freeze node, as (node, item), oldest first within a node."""
        here = self.node_name or "this node"
        rows = [(here, item) for _seq, item in sorted(self.session.held.items())]
        rows += [(name, item) for (name, _seq), item in sorted(self._other_held.items())]
        return rows

    def _nodes_held_act(self, key: str) -> None:
        """Forward (f), step (space) or drop (d) the selected held item, on whichever node holds it."""
        table = self.query_one("#nodes-held", DataTable)
        if not table.row_count:
            self._say("nothing is held on any node")
            return
        name, _, number = str(table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value).rpartition("#")
        seq = int(number)
        client = self.client if name == (self.node_name or "this node") else self._other(name)
        if client is None:
            self._say(f"{name} is not answering")
            return
        try:
            if key == "d":
                client.drop(seq)
            else:
                client.forward(seq, then_run=key == "f")
        except FreezeError as error:
            self._say(f"{name}, item {seq}: {error}")
            return
        self._other_held.pop((name, seq), None)
        if key == "d" and client is not self.client:
            self.session.tampers += 1
        self._say(f"{name}: item {seq} {'dropped' if key == 'd' else 'forwarded'}")
        self._refresh_nodes(now=True)

    def _refresh_nodes(self, now: bool = False) -> None:
        # The status bar is redrawn many times a second on a busy node; asking every node for its
        # state and redrawing the merged list that often would leave the UI no time for anything else.
        if not now and time.monotonic() - self._nodes_drawn < 1.5:
            return
        self._nodes_drawn = time.monotonic()
        table = self.query_one("#nodes-table", DataTable)
        here = self.node_name or "this node"
        rows: dict[str, tuple[Any, ...]] = {}
        names = list(self.nodes) or [here]
        for name in names:
            where = self.nodes.get(name, {"host": self.client.host, "port": self.client.port})
            address = f"{where['host']}:{where['port']}"
            if name == here:
                rows[name] = (name, address, "FROZEN" if self.session.frozen else "running", f"{self.last_seq:,}",
                              str(len(self.session.held)), "← this window")
                continue
            other = self._other(name)
            try:
                status = other.status() if other else None
            except FreezeError:
                status = None
            if status is None:
                rows[name] = (name, address, "not answering", "", "", "")
            else:
                rows[name] = (name, address, "FROZEN" if status.get("frozen") else "running", f"{status.get('seq', 0):,}",
                              str(len(status.get("held", []))), "enter: move here")
        if rows != self._node_rows:
            keep = table.cursor_row
            table.clear()
            for name, row in rows.items():
                table.add_row(*row, key=name)
            if table.row_count:
                table.move_cursor(row=min(keep, table.row_count - 1))
            self._node_rows = rows
        held = self._held_everywhere()
        keys = [f"{name}#{item['seq']}" for name, item in held]
        if keys != self._held_rows:
            everywhere = self.query_one("#nodes-held", DataTable)
            keep = everywhere.cursor_row
            everywhere.clear()
            for (name, item), key in zip(held, keys):
                route = Text.assemble(self._party(item.get("from", "")), " → ", self._party(item.get("to", "")))
                everywhere.add_row(Text(name, style=OK if name == here else COLOURS["part"]), str(item["seq"]), route, self._what(item), key=key)
            if everywhere.row_count:
                everywhere.move_cursor(row=min(keep, everywhere.row_count - 1))
            self._held_rows = keys
        merged = [(item.get("t_ms", 0) / 1000, here, item) for item in self.session.recent(120)]
        merged = sorted([*merged, *list(self._feed)[-240:]], key=lambda entry: entry[0])[-120:]
        feed = self.query_one("#nodes-feed", DataTable)
        feed.clear()
        for at, name, item in merged:
            feed.add_row(time.strftime("%H:%M:%S", time.localtime(at)) + f".{int(at * 1000) % 1000:03d}",
                         Text(name, style=OK if name == here else COLOURS["part"]), str(item.get("seq", "")),
                         KIND_WORDS.get(item.get("kind", ""), item.get("kind", "")), self._party(item.get("from", "")),
                         self._party(item.get("to", "")), self._what(item))
        if feed.row_count:
            feed.move_cursor(row=feed.row_count - 1)

    @on(DataTable.RowSelected, "#nodes-held")
    def _held_chosen(self, event: DataTable.RowSelected) -> None:
        """Open a held item where it can be edited: on its own node."""
        name, _, number = str(event.row_key.value).rpartition("#")
        if name == (self.node_name or "this node"):
            self.selected = int(number)
            self.action_tab("intercept")
            self._refresh_held()
        elif name in self.nodes:
            self.switch_to = name
            self.exit()

    @on(DataTable.RowSelected, "#nodes-table")
    def _node_chosen(self, event: DataTable.RowSelected) -> None:
        name = event.row_key.value
        if name and name in self.nodes and name != self.node_name:
            self.switch_to = name
            self.exit()

    def _all_nodes(self, freeze: bool) -> None:
        done = []
        for name in self.nodes or {}:
            client = self.client if name == self.node_name else self._other(name)
            if client is None:
                continue
            try:
                client.freeze() if freeze else client.thaw()
                done.append(name)
            except FreezeError as error:
                self._say(f"{name}: {error}")
                return
        if not self.nodes:
            self._call("frozen" if freeze else "running", self.client.freeze if freeze else self.client.thaw)
        else:
            self._say(f"{'frozen' if freeze else 'running'}: {', '.join(done)}")
        self._refresh_nodes(now=True)

    def action_claim(self) -> None:
        broker = self.client.broker
        if not broker:
            self._say("this window talks to the node directly; claiming needs the broker")
            return
        try:
            if self.owner == broker.get("client"):
                self.owner = self.client.release().get("owner")
                self._say("released: every window can change the node again")
            else:
                self.owner = self.client.claim(force=self.owner is not None).get("owner")
                self._say("claimed: only this window can change the node")
        except FreezeError as error:
            self._say(str(error))

    def on_unmount(self) -> None:
        self._close_others()
        if self.debugger is not None:
            self.debugger.close()

    # Payloads ------------------------------------------------------------------------------

    def _payloads_changed(self) -> None:
        self._payloads_dirty = True

    def _refresh_payloads(self) -> None:
        self._payloads_dirty = False
        lists = self.query_one("#pl-lists", DataTable)
        available = self.store.all()
        names = [item.name for item in available]
        if self.current_list not in names:
            self.current_list = names[0] if names else None
        lists.clear()
        for item in available:
            lists.add_row(Text(item.name, style=COLOURS["kind"]), str(len(item.values)),
                          Text("shipped" if item.builtin else "yours", style=MUTED), key=item.name)
        if self.current_list is not None:
            lists.move_cursor(row=names.index(self.current_list))
        self._refresh_values()

        armed = self.query_one("#pl-rules", DataTable)
        armed.clear()
        for index, rule in enumerate(self.engine.rules):
            total = rule.total
            progress = f"{min(rule.cursor, total)}/{total}" if total is not None else f"{rule.cursor}"
            does = "drop" if rule.action == "drop" else "forward" if rule.action == "forward" else f"set {views.path_text(tuple(rule.field), None) if isinstance(rule.field, (list, tuple)) else (rule.field or 'value')}"
            state = Text("running", style=ICE) if rule.armed else Text("done" if rule.finished else "stopped", style=MUTED)
            armed.add_row(Text(rule.name, style=COLOURS["action"]), rule.when, does, progress, state, key=str(index))
        armed.border_title = f"RULES  {sum(rule.armed for rule in self.engine.rules)} running"

        results = self.query_one("#pl-results", DataTable)
        at_end = results.cursor_row >= results.row_count - 1
        results.clear()
        for index, attempt in enumerate(self.engine.attempts):
            said = self.engine.reaction_of(attempt)
            telling = said.startswith(("error", "rejected", "THE NODE", "not applied")) or "hostile" in said
            change = f"{attempt.before} → {attempt.after}" if attempt.action == "replace" and not attempt.error else attempt.action
            results.add_row(str(attempt.number), attempt.rule, str(attempt.seq),
                            styled(self.glossary.lookup(attempt.what, "kind")) if attempt.what else Text(""),
                            Text(attempt.value, style=FROST), Text(change, style=RED),
                            Text(said, style=RED if telling else MUTED), key=str(index))
        results.border_title = f"RESULTS  {len(self.engine.attempts)}"
        if at_end and results.row_count:
            results.move_cursor(row=results.row_count - 1)

    def _refresh_values(self) -> None:
        table = self.query_one("#pl-values", DataTable)
        row = table.cursor_row
        table.clear()
        if self.current_list is None:
            table.border_title = "VALUES"
            return
        item = self.store.get(self.current_list)
        for index, value in enumerate(item.values):
            table.add_row(Text(str(index + 1), style=MUTED), Text(payloads.describe(value), style=FROST), key=str(index))
        table.border_title = f"VALUES · {item.name}" + ("  (shipped; editing makes your own copy)" if item.builtin else "")
        if item.values:
            table.move_cursor(row=min(max(row, 0), len(item.values) - 1))
        if item.description:
            table.border_subtitle = item.description[:80]

    @on(DataTable.RowHighlighted, "#pl-lists")
    def _list_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if event.row_key is not None and event.row_key.value is not None and event.row_key.value != self.current_list:
            self.current_list = event.row_key.value
            self._refresh_values()

    def _ask(self, title: str, default: str, hint: str, then: Any) -> None:
        def done(text: str | None) -> None:
            if text is not None and text.strip():
                then(text.strip())

        self.push_screen(EditScreen(title, default, hint + " · enter to accept · esc to cancel"), done)

    def _edit_list(self, change: Any) -> None:
        """Apply ``change(values)`` to the selected list and save it as the user's own."""
        if self.current_list is None:
            self._say("make a list first (n)")
            return
        item = self.store.get(self.current_list)
        values = list(item.values)
        change(values)
        self.store.save(payloads.PayloadList(item.name, values, item.description))
        self._refresh_payloads()

    def _payload_key(self, key: str) -> None:
        values = self.query_one("#pl-values", DataTable)
        in_values = self.focused is values
        if key == "n":
            def create(name: str) -> None:
                self.store.save(payloads.PayloadList(name, []))
                self.current_list = name
                self._refresh_payloads()
                self._say(f"list `{name}` created; press a to add values")

            self._ask("Name of the new list", "", "a short name", create)
        elif key == "a":
            self._ask("Add a value", "", "a literal (123, 0xdead, \"text\") or a transform (add:1, trunc:4, flip:0, zero)",
                      lambda text: self._edit_list(lambda items: items.append(text)))
        elif key == "x" and in_values and self.current_list is not None and values.row_count:
            row = values.cursor_row
            self._edit_list(lambda items: items.pop(row))
        elif key == "x" and self.focused is self.query_one("#pl-rules", DataTable) and self.engine.rules:
            self.engine.remove(self.engine.rules[self.query_one("#pl-rules", DataTable).cursor_row])
            self._refresh_payloads()
        elif key == "x" and self.current_list is not None:
            name = self.current_list
            self._say(f"list `{name}` deleted" if self.store.delete(name) else f"`{name}` is a shipped list and stays")
            self._refresh_payloads()
        elif key == "I":
            def bring(path: str) -> None:
                try:
                    items = self.store.import_file(Path(path).expanduser())
                except (OSError, ValueError) as error:
                    self._say(f"not imported: {error}")
                    return
                self.current_list = items[0].name if items else self.current_list
                self._refresh_payloads()
                self._say(f"imported {', '.join(item.name for item in items)}")

            self._ask("Import payload list(s) from a file", "", ".txt (one per line), .csv, .json or .yaml", bring)
        elif key == "E" and self.current_list is not None:
            name = self.current_list

            def send(path: str) -> None:
                try:
                    self.store.export(name, Path(path).expanduser())
                    self._say(f"`{name}` written to {path}")
                except OSError as error:
                    self._say(f"not written: {error}")

            self._ask(f"Export `{name}` to a file", f"{name.replace(' ', '-')}.yaml", ".yaml or .json", send)
        elif key == "T":
            def load(path: str) -> None:
                try:
                    template = rules.load_template(Path(path).expanduser(), self.store)
                    self.engine.arm_template(template)
                    self.engine.sweep()
                except (OSError, ValueError, FreezeError) as error:
                    self._say(f"template not loaded: {error}")
                    return
                self._refresh_payloads()
                self._say(f"template `{template.name}`: {len(template.rules)} rule(s) running")

            self._ask("Load a template and start its rules", "", "a .yaml or .json template file", load)
        elif key == "S":
            self.engine.stop()
            self._refresh_payloads()
            self._say("all rules stopped")
        elif key == "C":
            self.engine.attempts.clear()
            self._refresh_payloads()
        elif key == "R":
            def save(path: str) -> None:
                try:
                    rules.write_report(self.engine.report(), Path(path).expanduser())
                    self._say(f"results written to {path}")
                except OSError as error:
                    self._say(f"not written: {error}")

            self._ask("Save the results to a file", "freeze-results.json", "a .json file", save)

    def _edit_payload_value(self) -> None:
        values = self.query_one("#pl-values", DataTable)
        if self.current_list is None or not values.row_count:
            self._say("select a value to edit, or press a to add one")
            return
        row = values.cursor_row
        current = payloads.describe(self.store.get(self.current_list).values[row])

        def change(text: str) -> None:
            self._edit_list(lambda items: items.__setitem__(row, text))

        self._ask("Change the value", current, "a literal or a transform", change)

    def action_attack(self) -> None:
        """Run a payload list against the selected field of the selected held item."""
        if self.query_one(TabbedContent).active != "intercept":
            return
        item = self.session.held.get(self.selected) if self.selected is not None else None
        tree = self.query_one("#item", PayloadTree)
        node = tree.cursor_node.data if tree.cursor_node is not None else None
        if item is None or node is None or not node.leaf:
            self._say("select a single field of a held item first")
            return
        if not item.get("editable"):
            self._say("this item cannot be edited")
            return
        names = [entry.name for entry in self.store.all()]

        def start(name: str) -> None:
            matches = [candidate for candidate in names if candidate == name] or [c for c in names if c.lower().startswith(name.lower())]
            if not matches:
                self._say(f"no payload list named `{name}`")
                return
            chosen = self.store.get(matches[0])
            if not chosen.values:
                self._say(f"`{chosen.name}` has no values yet")
                return
            rule = rules.rule_for_item(item, node.path, chosen.values, chosen.name)
            rule.stale_negative_control = item["seq"] in self.stale_negative_controls
            try:
                self.engine.arm(rule)
                self.engine.sweep()
            except FreezeError as error:
                self._say(f"not started: {error}")
                return
            self.stale_negative_controls.discard(item["seq"])
            self._say(f"running `{chosen.name}` ({len(chosen.values)} values) against this field; results are in Payloads (6)")

        self._ask("Run which payload list against this field?", self.current_list or (names[0] if names else ""),
                  "lists: " + ", ".join(names), start)

    # Scenarios -----------------------------------------------------------------------------

    def _refresh_scenarios(self) -> None:
        table = self.query_one("#sc-table", DataTable)
        if not self._scenarios:
            self._scenarios = scenarios.catalog()
        words = self._scenario_filter.lower().split()
        table.clear()
        shown = 0
        for entry in self._scenarios:
            if entry.status == scenarios.NO and not self._scenarios_all:
                continue
            text = f"{entry.id} {entry.title} {entry.subject} {entry.implementation}".lower()
            if any(word not in text for word in words):
                continue
            style = {scenarios.READY: OK, scenarios.PARTLY: COLOURS["kind"], scenarios.NO: MUTED}[entry.status]
            table.add_row(Text(entry.status, style=style), Text(entry.subject, style=COLOURS["part"]),
                          Text(entry.implementation, style=MUTED), entry.id, key=entry.id)
            shown += 1
        counts = scenarios.summary(self._scenarios)
        table.border_title = (f"DWARF SCENARIOS  {shown} shown · {counts['ready']} ready · {counts['partly']} partly · "
                              f"{counts['no']} not possible from inside a node")
        self._show_scenario()

    def _selected_scenario(self) -> scenarios.Entry | None:
        table = self.query_one("#sc-table", DataTable)
        if not table.row_count:
            return None
        key = table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value
        return next((entry for entry in self._scenarios if entry.id == key), None)

    def _show_scenario(self) -> None:
        entry = self._selected_scenario()
        text = Text()
        if entry is None:
            text.append("No scenario matches.", style=MUTED)
            self.query_one("#sc-detail-text", Static).update(text)
            return
        text.append(f"{entry.title}\n", style=f"bold {FROST}")
        text.append(f"{entry.id}\n\n", style=MUTED)
        for name, value in (("target", entry.implementation), ("runs as", entry.runtime), ("about", entry.subject),
                            ("threats", ", ".join(entry.threats) or "-"), ("risks", ", ".join(entry.risks) or "-")):
            text.append(f" {name:<9}", style=MUTED)
            text.append(f"{value}\n")
        text.append("\nWhat DWARF does\n", style=ICE)
        text.append(" " + ", ".join(step.get("primitive", "") for step in entry.primitives) + "\n", style=MUTED)
        text.append("What DWARF checks\n", style=ICE)
        text.append(" " + (", ".join(entry.assertions) or "-") + "\n", style=MUTED)
        text.append("\nFrom inside this node: ", style=ICE)
        text.append(entry.status + "\n", style={scenarios.READY: OK, scenarios.PARTLY: COLOURS["kind"], scenarios.NO: RED}[entry.status])
        if entry.reason:
            text.append(f" {entry.reason}\n", style=MUTED)
        if entry.status != scenarios.NO:
            try:
                template = scenarios.template_for(entry)
            except rules.RuleError as error:
                text.append(f" {error}\n", style=RED)
            else:
                text.append("\nRules Freeze would run\n", style=ICE)
                for rule in template.rules:
                    text.append(f" {rule.name}\n", style=COLOURS["action"])
                    does = {"drop": "drop it", "forward": "let it through", "replay": "send an earlier one in its place"}.get(
                        rule.action, f"set {rule.field} to {', '.join(payloads.describe(v) for v in rule.values[:3])}"
                        + (" …" if len(rule.values) > 3 else ""))
                    text.append(f"   when {rule.when}\n   " + (f"after {rule.after} have gone by, " if rule.after else "") + does + "\n", style=MUTED)
                    if rule.expect:
                        text.append(f"   expect the node to: {' / '.join(rule.expect)}\n", style=MUTED)
        for line in entry.notes[:3]:
            text.append(f"\n DWARF's own caveat: {line}", style=MUTED)
        self.query_one("#sc-detail-text", Static).update(text)

    @on(DataTable.RowHighlighted, "#sc-table")
    def _scenario_highlighted(self, _event: DataTable.RowHighlighted) -> None:
        self._show_scenario()

    @on(Input.Submitted, "#sc-filter")
    def _scenario_filter_submitted(self, event: Input.Submitted) -> None:
        self._scenario_filter = event.value.strip()
        self._refresh_scenarios()
        self.query_one("#sc-table", DataTable).focus()

    def _scenario_key(self, key: str) -> None:
        if key == "A":
            self._scenarios_all = not self._scenarios_all
            self._refresh_scenarios()
            return
        entry = self._selected_scenario()
        if entry is None:
            return
        try:
            template = scenarios.template_for(entry)
        except rules.RuleError as error:
            self._say(str(error))
            return
        if key == "L":
            try:
                self.engine.arm_template(template)
                self.engine.sweep()
            except FreezeError as error:
                self._say(f"not started: {error}")
                return
            self._say(f"`{entry.id}`: {len(template.rules)} rule(s) running; results are in Payloads (6)")
        elif key == "W":
            def write(path: str) -> None:
                try:
                    rules.save_template(template, Path(path).expanduser())
                    self._say(f"template written to {path}")
                except OSError as error:
                    self._say(f"not written: {error}")

            self._ask("Write this scenario as a template file", f"{entry.id}.yaml", ".yaml or .json", write)

    def _export_run(self) -> None:
        """Write this session, up to the selected History row, as a run file."""
        if self.session.directory is None:
            self._say("this session is not being recorded, so there is nothing to export")
            return
        table = self.query_one("#history-table", DataTable)
        until = None
        if table.row_count and table.cursor_row < table.row_count - 1:
            until = int(table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value)
        directory = self.session.directory

        def write(path: str) -> None:
            try:
                manifest = runs.export_run(directory, Path(path).expanduser(), until)
            except (runs.RunError, OSError) as error:
                self._say(f"not exported: {error}")
                return
            upto = f" up to item {until}" if until is not None else ""
            self._say(f"run written to {path}: {manifest['items']:,} items{upto}, {manifest['interventions']} intervention(s)")

        self._ask("Export this run" + (f" up to item {until}" if until is not None else " (everything so far)"),
                  f"freeze-run-{directory.name}.tar.gz", "a .tar.gz file; select an earlier History row first to export only up to it", write)

    # Overview ------------------------------------------------------------------------------

    def _overview(self) -> Text:
        """The node at a glance: its peers, its parts, where it is stopped."""
        session, text = self.session, Text()
        node = self.client.hello.get("node", {})
        held_at = session.held_at()

        text.append(" NODE   ", style=ICE)
        text.append(f"{node.get('implementation', 'node')} {node.get('version', '')} · {node.get('network', '')}   ")
        if session.frozen:
            text.append(" FROZEN ", style=f"bold #081220 on {ICE}")
            text.append(f"  {len(session.held)} held", style=ICE)
        else:
            text.append(" RUNNING ", style=f"bold #081220 on {OK}")
            text.append(f"  {self.rate:,.0f} steps/s", style=MUTED)
        text.append(f"   step {self.last_seq:,}\n\n")

        text.append(" PEERS", style=ICE)
        text.append(f"{'':<34}{'heard':>8}{'said':>8}   last heard\n", style=MUTED)
        now = time.time()
        if not session.peers:
            text.append("   none seen yet\n", style=MUTED)
        for address in sorted(session.peers):
            peer = session.peers[address]
            if peer["hostile"]:
                mark, style, note = "✗", RED, "marked hostile"
            elif peer["closed"]:
                mark, style, note = "○", MUTED, "hung up"
            else:
                mark, style, note = "●", OK, ""
            text.append(f"   {mark} ", style=style)
            text.append(f"{address:<22}", style=COLOURS["part"] if not peer["closed"] else MUTED)
            text.append(f"{note:<14}", style=style)
            text.append(f"{peer['received']:>8,}{peer['sent']:>8,}   ")
            last = peer["last"]
            if isinstance(last, dict):
                name = cardano.item_message_name(last) or last.get("label", "")
                text.append(self.glossary.lookup(name, "kind").label, style=COLOURS["kind"])
                payload = last.get("payload") if last.get("payload") is not None else item_view(last)[0]
                about = cardano.about(last.get("type", ""), payload)
                if about:
                    text.append(f"  {about}", style=FROST)
                if peer["last_at"]:
                    text.append(f"  {max(0, now - peer['last_at']):.0f}s ago", style=MUTED)
            elif peer["bytes_in"] or peer["bytes_out"]:
                text.append(f"{peer['bytes_in']:,} bytes in · {peer['bytes_out']:,} out", style=MUTED)
            text.append("\n")

        def line(key: str, arrow: bool = False) -> None:
            part = session.parts.get(key, {"handled": 0, "actions": 0, "last": "", "stages": 0})
            held = held_at.get(key, 0)
            entry = self.glossary.lookup(key, "part")
            text.append("   ↓\n" if arrow else "", style=MUTED)
            text.append(" ❄ " if held else "   ", style=f"bold {ICE}")
            text.append(f"{entry.label:<22}", style=f"bold {ICE}" if held else COLOURS["part"])
            text.append(f"{(str(held) + ' held') if held else '':<10}", style=ICE)
            text.append(f"{part['handled']:>8,}{part['actions']:>9,}   ")
            if part["last"]:
                text.append(self.glossary.lookup(part["last"], "action").label, style=COLOURS["action"])
            text.append("\n")

        text.append("\n INSIDE THE NODE", style=ICE)
        text.append(f"{'':<19}{'handled':>8}{'actions':>9}   last did\n", style=MUTED)
        for index, key in enumerate(PIPELINE):
            line(key, arrow=index > 0)
        text.append("\n")
        for key in SERVICES:
            line(key)
        others = sorted(key for key in set(session.parts) | set(held_at) if key not in PIPELINE and key not in SERVICES)
        if others:
            handled = sum(session.parts.get(key, {}).get("handled", 0) for key in others)
            actions = sum(session.parts.get(key, {}).get("actions", 0) for key in others)
            held = sum(held_at.get(key, 0) for key in others)
            live = sum(session.parts.get(key, {}).get("stages", 0) for key in others)
            text.append(" ❄ " if held else "   ", style=f"bold {ICE}")
            text.append(f"{'connections & helpers':<22}", style=f"bold {ICE}" if held else COLOURS["part"])
            text.append(f"{(str(held) + ' held') if held else '':<10}", style=ICE)
            text.append(f"{handled:>8,}{actions:>9,}   ")
            stopped = [self.glossary.lookup(key, "part").label for key in others if held_at.get(key)]
            if stopped:
                text.append("stopped in " + ", ".join(stopped) + " · ", style=ICE)
            text.append(f"{live} running: ", style=MUTED)
            names = [self.glossary.lookup(key, "part").label for key in others if session.parts.get(key, {}).get("stages", 0)]
            text.append(" · ".join(names[:8]) + (" …" if len(names) > 8 else ""), style=MUTED)
            text.append("\n")
        if session.tampers:
            text.append(f"\n You have changed, dropped or injected {session.tampers} item{'s' if session.tampers != 1 else ''} this session.\n", style=RED)
        return text

    def _refresh_overview(self) -> None:
        self.query_one("#overview-text", Static).update(self._overview())

    # tabs ----------------------------------------------------------------------------------

    def action_tab(self, name: str) -> None:
        # Focus must move with the tab: the tab that holds the focused widget is shown again otherwise.
        self.set_focus(None)
        self.query_one(TabbedContent).active = name
        target = TAB_FOCUS.get(name)
        if target:
            self.query_one(target).focus()

    @on(TabbedContent.TabActivated)
    def _tab_activated(self, event: TabbedContent.TabActivated) -> None:
        pane = event.pane.id
        if pane != "nodes":
            self._close_others()
        if pane == "history":
            if self.query_one("#history-table", DataTable).row_count == 0:
                self._rebuild_history()
        elif pane == "state":
            self._load_state_keys()
        elif pane == "points":
            self._refresh_points()
        elif pane == "overview":
            self._refresh_overview()
        elif pane == "nodes":
            self._refresh_nodes(now=True)
        elif pane == "payloads":
            self._refresh_payloads()
        elif pane == "scenarios":
            self._refresh_scenarios()

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        """Single-letter commands must not fire while typing into a text box."""
        if action == "toggle_resigning":
            try:
                # On the Source tab `s` steps into a function.
                if self.query_one(TabbedContent).active == "source":
                    return False
            except NoMatches:
                pass
        if action in ("forward", "step", "drop"):
            try:
                # On the Nodes tab these act on the held item selected there, whichever node holds it.
                if self.query_one(TabbedContent).active == "nodes":
                    return False
            except NoMatches:
                pass
        if isinstance(self.focused, Input) and action in (
            "forward", "drop", "step", "toggle_intercept", "edit", "run_until", "freeze", "thaw", "revert", "tab", "quit",
            "attack", "claim",
        ):
            return False
        return True
