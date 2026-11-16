# DWARF Freeze: interactive node debugger — design

Status: design, pre-implementation. Scope of this document: the Amaru (Rust) target and
everything node-independent. The cardano-node (Haskell) target is built separately against
the protocol defined here.

## 1. Intent

DWARF today drives real nodes with workloads and records what happened. It cannot stop a
node mid-thought. Freeze adds that: start a node built from source with debugger hooks,
see everything it receives, sends and does, stop it at any step, read and change what it
holds or is about to process, then let it continue to a chosen point and stop again.

It is for security research. The working loop is: stop the node at an interesting moment,
tamper with a message, a stored value or the clock, step forward, and see whether the node
does the right thing.

The reference experience is an intercepting proxy (Burp Suite) rather than a source
debugger: the node is the "server", and everything going into it or moving inside it is an
item that can be held, read, edited, forwarded, dropped or replayed. Source-level depth
exists but is one tab away, never in the default view.

### Goals

1. Hold every step. Every stage input, every effect, every byte on the wire and every
   store write can be a stopping point. "Stop everywhere" is the baseline; named places
   that matter ("cold spots") are tagged on top of it.
2. Complete history. Every item and the node's reaction to it (verdict, reason, effects,
   store changes, log lines) is recorded for the whole session.
3. View and modify data: the item under the cursor, the chain store, the ledger store, the
   mempool, peer table and stage states.
4. Run-until. Conditions are written against Cardano things (slot, block number, peer, tx
   hash, cold spot), not addresses.
5. Readable by a security person who does not know Amaru. Plain labels (`vrf proof`, not
   `vrf_proof`), colour by kind, and an explanation for every name on hover or cursor.
6. Easy piping. Everything the UI does goes through one small protocol. A tool is any
   program that speaks it. Adding a security test must not require touching the UI or the
   node.
7. Any Amaru source. The instrumentation is first written by hand against the pinned
   source, then captured as a spec that an applier can put onto another Amaru revision,
   the way measurement patches are ported today.
8. One protocol for both nodes, so the cardano-node work reuses the UI, history, tools and
   glossary unchanged.

### Non-goals

- Compatibility with measurement taps. Freeze is a separate subsystem with separate
  builds and runs; it shares no code path with taps, measurement patches or release
  builds, and timing measurements taken under Freeze would be meaningless.
- Production or mainnet use. Freeze builds are for devnets under DWARF's control.
- Performance. A Freeze node may be several times slower than stock.
- Evidence-grade determinism in v1. A session is recorded faithfully, but replaying it is
  best-effort (see 9).

### Anti-goals

- A default view that needs Amaru knowledge to read.
- A hook design that breaks on every Amaru release.
- Silent tampering: every edit, drop and injection is shown in red and logged.
- A Freeze build that can be mistaken for a stock or instrumented build in DWARF records.

## 2. What the Amaru source gives us

Read from the pinned source, `dwarf/testplease/amaru/v10.11.20260925`.

- Amaru is a graph of message-passing stages run by `amaru-pure-stage`. In the tokio
  runtime (`crates/amaru-pure-stage/src/tokio.rs`) two functions carry everything:
  `run_stage_boxed` delivers every stage input, and `interpreter` executes every effect a
  stage asks for (send, call, clock, wait, schedule, timeouts, and about 100 named
  external effects covering store reads/writes, ledger validation, network I/O, mempool).
- Every message, state and effect is `SendData`: serialisable with a type tag, and
  convertible to a generic value and back into its concrete type
  (`SendData::deserialize_value`). So an item can be shown, edited as data and handed back
  typed, with no per-type code.
- The runtime already records inputs, states, effect suspensions and resumptions to a
  `TraceBuffer`.
- All network bytes pass through one resource, `ConnectionsResource`
  (`Arc<dyn ConnectionProvider>`), registered in
  `crates/amaru-node/src/stages/build_node.rs`. All mini-protocol messages are decoded in
  one function, `miniprotocol()` in `crates/amaru-protocols/src/protocol/miniprotocol.rs`.
- The chain store is a shared resource (`Arc<dyn ChainStore>`). The ledger state is owned
  by a dedicated "ledger" OS thread and reachable only through `LedgerRequest` messages
  (`crates/amaru-consensus/src/block_validator.rs`).
- Work outside the stage runtime that a runtime gate does not stop: the ledger thread
  (acts only on requests), a rewards thread, the "performance" thread, the TCP accept
  loop, the submit API, metrics. These matter for 6.4.

Consequence: most of "stop everywhere" is a handful of hooks at chokepoints, not thousands
of patches. Hand-placed hooks are needed only below the effect level: inside ledger rules,
header checks, decoders and the script evaluator.

## 3. Shape

```
 terminal UI ─┐                                     ┌─ recorder ─► session store
 tools ───────┼── FCP ──► freeze broker (Python) ───┤
 scripts ─────┘           one per session           ├─ Docker: pause peers, restart node, swap stores
                                │                   └─ reference resolver + Cardano decoding
                                │ FCP (TCP, token)
                                ▼
                          freeze agent (inside the node process)
                            ├─ runtime gate   (stage inputs, effects, state, timers)
                            ├─ wire gate      (raw bytes, decoded protocol messages)
                            ├─ state access   (chain store, ledger, mempool, stage state)
                            └─ probes         (cold spots, function entry/exit)
```

The **broker** is a long-lived process started by `freeze-debugger run`. It is the only
client of the agent. The UI, tools and scripts connect to the broker and speak the same
protocol; the broker forwards node requests to the agent and itself implements the
requests the agent cannot serve from inside the node (see 12.1).

Four deliverables, built in this order because each depends on the one before:

1. The protocol and the agent in Amaru.
2. The client: session recorder, Cardano decoding, the terminal UI, the tool interface.
3. Deep instrumentation: probes, cold spots, then the spec and applier.
4. DWARF integration as a separate subsystem: own builder, launcher and entry point.

## 4. Freeze Control Protocol (FCP) v1

This is the contract shared with the cardano-node work. It is specified in full in
`dwarf/freeze/spec/freeze-control-protocol-v1.md` (written as part of deliverable 1) with JSON
Schemas beside it. Summary:

**Transport.** Newline-delimited JSON over TCP. The agent listens on the address in
`DWARF_FREEZE_LISTEN` inside the container; the port is published on `127.0.0.1` only.
Every connection starts with `hello` carrying the shared token from `DWARF_FREEZE_TOKEN`;
without it the agent closes the connection. (A Unix socket in a bind mount is not
reachable from the host under Docker Desktop on macOS, which is a supported DWARF host.)
The broker accepts several clients; one holds the "driver" role (may change node
behaviour), the rest observe.

**Messages.** Request/response with ids, plus unsolicited events. Requests:

| Group | Requests |
|---|---|
| session | `hello` (versions, node kind, capabilities), `claim_driver`, `status` |
| flow | `freeze`, `thaw`, `step` (n, granularity), `run_until` (condition), `set_hold` (what gets held) |
| items | `item.get`, `item.forward`, `item.drop`, `item.replace` (edited payload), `item.inject` (new item to a destination) |
| conditions | `cond.add`, `cond.remove`, `cond.list` (each has a predicate and an action: freeze, log, snapshot, notify) |
| state | `state.namespaces`, `state.list`, `state.get`, `state.put`, `state.delete` |
| time | `clock.get`, `clock.skew`, `timer.list`, `timer.fire` |
| snapshots | `snapshot.save`, `snapshot.list`, `snapshot.restore` |
| probes | `probe.list`, `probe.enable`, `probe.disable` |

Events: `item` (a new item was observed, with `held: true|false`), `item.done` (the
node's reaction), `frozen`, `thawed`, `log`, `probe`, `state.changed`, `error`.

**Item.** The unit of everything:

```json
{ "seq": 48212, "t": "2026-11-12T10:42:02.113Z", "kind": "stage_input",
  "dir": "in", "from": "peer:10.0.0.12:3001", "to": "stage:validate_block",
  "type": "amaru_consensus::stages::validate_block::ValidateBlockMsg",
  "label": "Block", "cold_spots": ["block-check"],
  "payload": { ... }, "raw": "hex, when the item is bytes",
  "parent": 48211, "held": true }
```

Kinds: `stage_input`, `effect` (before it runs), `effect_result`, `stage_state`,
`wire_in`, `wire_out`, `proto_in`, `proto_out`, `store_write`, `timer`, `probe`.

**Payload encoding.** Lossless tagged JSON: CBOR/JSON scalars map directly; byte strings
are `{"$bytes": "<hex>"}`; integers outside the JSON-safe range are `{"$int": "<decimal>"}`;
CBOR tags are `{"$tag": n, "value": ...}`; maps with non-string keys are
`{"$map": [[k, v], ...]}`. An edited payload is sent back in the same form. The agent
never interprets Cardano meaning; that is the client's job (5.2).

**Predicates.** A small JSON expression tree evaluated in the agent so that running to a
far-away condition is cheap: `and`/`or`/`not`, `kind`, `to`, `from`, `type`, `cold_spot`,
`seq`, and field tests on the payload by path (`eq`, `ne`, `lt`, `gt`, `contains`,
`len_gt`). The client compiles the friendly forms ("the next block arrives",
`block.slot >= N`) into this.

**Capabilities.** `hello` returns which kinds, state namespaces and probes this node
build supports, so the UI greys out what a given node cannot do. This is how a shallower
cardano-node build is accommodated.

**State namespaces.** A tree: `chain/headers`, `chain/blocks`, `chain/nonces`,
`chain/best`, `ledger/utxo`, `ledger/accounts`, `ledger/pools`, `ledger/dreps`,
`ledger/proposals`, `ledger/votes`, `ledger/cc_members`, `ledger/pots`, `ledger/slots`,
`mempool/txs`, `peers`, `stages/<name>` (current stage state). Keys and values use the
payload encoding plus raw bytes.

## 5. The client

Python, in `dwarf/freeze/`, because DWARF is Python and tools should be trivial to write.

### 5.1 Session and recorder

`dwarf.freeze.client` is the protocol client (sync and asyncio). `dwarf.freeze.session`
records every event to an append-only JSONL file plus a SQLite index under the DWARF data
root (`.../dwarf/freeze-sessions/<id>/`), so History survives restarts, can be filtered
quickly, and can be exported as a DWARF evidence attachment.

### 5.2 Cardano decoding and the glossary

`dwarf.freeze.cardano` turns payloads and raw bytes into a labelled tree: blocks,
headers, transactions, certificates, mini-protocol messages. It also does the reverse
after an edit, and "re-seal": recomputing dependent hashes (transaction id, block body
hash) so the node does not reject an edited item for an uninteresting reason. Re-signing
(witnesses, KES) is offered when the devnet keys are available to DWARF, otherwise the
stale signature is shown as such.

`dwarf/freeze/glossary.yaml` maps every field, message type, stage, effect and cold spot
to: a plain label, a colour class (field, kind of thing, part of the node, action), one
sentence on what it is, and one on why it matters for security. Unknown names fall back
to the raw name with underscores removed. The glossary is node-independent where the
concept is (a block header is a block header) and has per-node sections for stage and
effect names.

### 5.3 Terminal UI

Python with Textual (mouse hover, tabs, tables, runs over SSH). Tabs, left to right:

| Tab | Purpose |
|---|---|
| **Intercept** (default) | Held items queue · selected item Pretty / CBOR / Hex, editable per field · Inspector (source, destination, selected field, what the node will do next, "in plain words"). Forward, Drop, Intercept on/off, Forward until…, Send to Repeater / Intruder. Checkboxes choose what is held: from peers, to peers, between stages, effects, store writes, timers. |
| **History** | Every item as a filterable table (seq, time, direction, peer, stage, type, about, size, node said, took, cold spot). Selecting a row shows "what went in" (with diff against the original if edited) and "what the node did". |
| **Repeater** | Take any item, edit, send again, read the reaction; keeps the list of sends. Each send can start from a saved snapshot. |
| **Intruder** | Run a mutation strategy over a chosen field, N tries, restoring a snapshot between tries; verdict table; stops on crash. |
| **State** | Browse and edit the state namespaces by meaning; pending edits applied explicitly; key history from the session. |
| **Freeze points** | Step size (cold spots only, stage messages, every effect, every function), cold-spot toggles, condition list. |
| **Internals** | The node's own view: stage tree with mailbox depths and timers, the timeline around the cursor, call stack from probes. |
| **Tools** | Registered tools and their runs. |

Presentation rules: names are shown through the glossary; field names, kinds of thing,
parts of the node and node actions each have a colour; anything the user changed is red;
hovering or moving the cursor onto a name shows its explanation in a tooltip and status
line. Mockups: `dwarf/freeze/docs/tui-mockups.html` (open in a browser). The Intercept, History
and Repeater screens are binding for layout, wording, colour rules and key bindings; the
other tabs are indicative.

### 5.4 Tools

A tool is a program that connects to the same socket and uses the same requests. DWARF
ships `dwarf.freeze.tooling`, a small Python helper (connect, wait for an item matching a
predicate, edit, forward, read reaction, snapshot/restore, report rows to the Tools tab).
Tools live in `dwarf/freeze-tools/` (shipped) and the DWARF data root (user). Built-in
tools are written on this interface, not on UI internals: message drop/delay/duplicate/
reorder, CBOR mutation strategies, clock skew, snapshot diff, export as evidence, export
as replayable script. A tool may also register glossary entries and decoders.

## 6. The agent in Amaru

A new workspace crate, `crates/amaru-freeze`, holds the gate, the socket server, payload
conversion, predicates and probes. It has no dependency on other Amaru crates so every
crate can call it. Other crates gain small call sites. The agent is inert unless
`DWARF_FREEZE_LISTEN` is set at start.

### 6.1 Runtime gate (`amaru-pure-stage`)

- `run_stage_boxed`, before the transition: report the input; if held, wait for forward,
  drop or replace. Replace uses `deserialize_value` against the original to get the typed
  message back.
- `interpreter`, before each effect: report it; if held, wait. After each effect: report
  the response; if held, allow replacing the response.
- After each transition: report the new stage state; expose it under `stages/<name>` and
  allow replacing it between transitions.
- Injection: `item.inject` sends a typed message to a named stage's mailbox, using a
  registry of message types by type tag (populated as stages are wired).
- A single global step lock gives a total order across stages while stepping, so "one
  step" is unambiguous. When thawed with nothing held, the lock is not taken.

### 6.2 Wire gate

- A `ConnectionProvider` wrapper registered in place of the real one: sees, and when held
  can edit, drop or delay, every chunk of raw bytes in and out, per connection.
- In `miniprotocol()`: report each decoded inbound message and each outbound message
  before encoding, with protocol name and peer; both editable.

### 6.3 State access

- Chain store: through the shared resource, plus the concrete RocksDB store's diagnostic
  iterators for listing.
- Ledger: a new `LedgerRequest` variant that runs a closure on the ledger thread, giving
  list/get/put/delete over each column and read access to the volatile state. Edits to
  the stable store go through the store's own transaction type.
- Mempool, peer table: through their resources.
- Store writes are reported as `store_write` items (and can be held) by wrapping the
  chain store resource and hooking the ledger's commit.

### 6.4 Time and the rest of the process

Freezing holds stages at the gate. To stop the node's own timers from all firing on thaw,
the runtime's sleeps and clock go through the agent: time spent frozen is subtracted, so
the node sees continuous time. Two modes, selectable per session:

- **Node time frozen** (default): timers and the clock effect do not advance while
  frozen. The node's idea of the current slot then lags its peers by the frozen time.
  DWARF pauses the other containers in the topology (cgroup freezer) for the same period
  so the peers neither time out nor race ahead; the remaining lag against wall-clock slot
  time is shown in the status bar and can be removed with `clock.skew`.
- **Slow node**: time is real; peers see a node that stopped answering. Useful in itself
  as a test.

Threads outside the stage system are left running; they act only on requests from stages
(ledger) or are harmless when idle (metrics). The accept loop continues to accept
sockets, whose bytes are then held at the wire gate.

### 6.5 Probes and cold spots

`amaru_freeze::probe!("name", fields…)` marks a place. It reports a `probe` item, can be
held like anything else (blocking wait on OS threads, async wait in stages), and can
offer named values for editing where they are plain data. Cold spots are probes or item
matchers with a tag; the initial set, from the source survey:

`wire-in`, `wire-out`, `cbor-decode`, `mux-frame`, `handshake`, `header-decode`,
`header-check` (and one per Praos assertion: VRF, leader stake, operational certificate,
KES), `nonce-update`, `future-slot`, `rollback`, `chain-choice`, `body-hash`,
`peer-verdict`, `block-check`, `tx-check` (and one per phase-one rule), `script-eval`,
`budget`, `fork-switch`, `epoch-boundary`, `store-write`, `tx-admit`, `chain-adopt`.

**Every function.** An attribute macro `#[amaru_freeze::trace]` wraps a function body
with entry/exit reporting (name, arguments that implement `Debug`, result or panic). An
applier step adds it to every function in selected crates, giving the "every function"
step size and the call stack in Internals. It is off at runtime unless enabled per crate
or per function, so the default cost is one atomic load per call.

**Every source line** is built separately, with a debugger beside the node: section 20.10.

### 6.6 Snapshots

`snapshot.save` (only while frozen) takes RocksDB checkpoints of the chain and ledger
stores and records the session position. `snapshot.restore` is performed by DWARF: stop
the node process, swap the store directories, start it again. In-memory state (peer
connections, mempool) is rebuilt by the node on start, not restored. Repeater and
Intruder use this to send each try from the same stored state.

## 7. Instrumentation spec and applier

After the hand-written hooks work on the pinned source:

- `dwarf/freeze/targets/amaru/spec.yaml` describes the instrumentation independent of
  line numbers: the crate to add; each hook as an anchor (file, enclosing function, a
  short pattern to find inside it) plus the code to insert; the crates to auto-trace; the
  cold-spot table.
- `dwarf/freeze/instrument.py` applies a spec to a source tree: adds the crate,
  applies each anchored hook, runs the auto-trace pass, and reports every anchor it could
  not place. A build with unplaced anchors is still usable; `hello` reports the missing
  capabilities.
- For each qualified revision the applied result is also stored as a patch series under
  `dwarf/freeze/targets/amaru/patches/<source-revision>/` with a manifest, so builds are
  reproducible and reviewable. This is Freeze's own format and loader; the measurement
  patch porter and its manifests are not used or changed.

## 8. DWARF integration: a separate subsystem

Freeze is self-contained. It does not modify the measurement-tap, measurement-patch,
release-build or run pipelines, and nothing in those pipelines learns about it. Existing
files are left as they are; Freeze reads DWARF's outputs but adds no branches to its code.

- **Own tree.** Code in `dwarf/freeze/` (Python package: client, session, decoding, UI,
  tooling, builder, launcher), tools in `dwarf/freeze-tools/`, node-side sources in
  `dwarf/freeze/targets/amaru/` (the `amaru-freeze` crate, `spec.yaml`, and per-revision
  patch series under `patches/<source-revision>/`). Nothing is placed under
  `dwarf/targets/`, so no measurement-recipe glob can pick it up.
- **Own entry point.** `dwarf/freeze-debugger` (executable, like `dwarf/cardano-profile`)
  with `build`, `run`, `attach`, `sessions`, `export`. `dwarf/profile_manager/cli.py` is
  not changed.
- **Own build.** `freeze-debugger build --source <amaru tree | git revision>` runs its own
  builder container (`dwarf/freeze/docker/amaru-freeze-builder.Dockerfile`): copy the
  source, apply the instrumentation (patch series for a known revision, otherwise the
  applier), `cargo build`, package as image
  `dwarf/freeze-amaru:<source-revision>-<recipe-digest>`. It writes a build record
  (source identity, recipe digest, image id) under the DWARF data root in
  `freeze/builds/`. The controller's `release_builds.py` variants stay `stock` and
  `instrumented`.
- **Own run.** `freeze-debugger run` takes a topology in one of two ways, neither of
  which edits DWARF's compose generator:
  1. *Overlay:* given a compose project DWARF already prepared, it writes a compose
     override file that swaps one Amaru service's image for the Freeze image and adds the
     `DWARF_FREEZE_LISTEN` environment and a bind-mounted socket directory, then brings
     the project up with both files.
  2. *Standalone:* its own small compose template (one Freeze Amaru node plus peers from
     stock images) for use without the rest of DWARF.
  A Freeze node starts frozen before its first stage input. Pausing the other containers
  while frozen is done by the launcher through Docker, not by DWARF's controller.
- **Records.** Sessions and exports live under `freeze/sessions/` in the DWARF data root.
  They are not DWARF runs, do not appear in run history, and can never be read as
  measurement evidence. An export is a plain archive (history, tamper log, build record)
  that can be attached to a finding by hand.
- **Dashboard.** No dashboard changes in this work. The protocol allows a browser front
  end later.
- **Dependencies.** Freeze's Python dependencies (Textual, cbor2) go in
  `dwarf/freeze/requirements.txt`, not in the framework requirements.

## 9. Known limits

- Replaying a recorded session from the start (true rewind) is not in v1. Going back is by
  snapshot restore, which loses in-memory state.
- Editing a typed message is limited to values the type can represent; the agent rejects
  an edit that does not deserialize and keeps the original. Arbitrary malformed input is
  done at the wire gate, where items are bytes.
- With node time frozen, slot time lags wall clock until skewed back.
- Auto-traced arguments are shown only where the type implements `Debug`.
- Editing volatile ledger state is read-only in v1; stable-store edits take effect on the
  next read.

## 10. Testing

- Agent: unit tests in `amaru-freeze`; runtime gate tests in `amaru-pure-stage` using
  small stage graphs (hold, edit, drop, inject, effect-result replace, time freeze);
  an end-to-end test that starts a real node binary against an in-memory or local peer
  and drives it through the socket.
- Client: protocol client against a scripted fake agent; decoder round-trips on real
  Conway blocks from `dwarf/corpora`; UI tests with Textual's pilot; tool tests on the
  fake agent.
- DWARF: tests for the Freeze builder and launcher (override-file generation) in
  `tests/test_freeze_*.py`; existing tests are not edited and the public-tree and README
  contract tests stay green.
- A devnet smoke scenario: start a mixed topology with one Freeze Amaru node, hold a
  block, edit a fee, forward, assert the node rejects it and the session records it.

## 11. Decisions made by the implementer (not requested by the user)

- Python + Textual for the UI; JSON lines for the protocol.
- A separate `amaru-freeze` crate, inert without `DWARF_FREEZE_LISTEN`.
- Requested by the user: Freeze is built as its own subsystem and leaves the tap, patch and
  release-build code untouched (section 8).
- Predicates evaluated in the agent; Cardano decoding in the client.
- Snapshot restore by restarting the node on swapped store directories.
- Source-line stepping deferred to an attachable native debugger.
- Names: "Freeze", "cold spot", "cryo-tube" (snapshot) in the UI; plain terms in the
  protocol.

## 12. Resolutions from design review

These settle questions a reviewer raised. Where they differ from earlier sections, this
section wins.

### 12.1 The broker owns the session

One broker process per session. It owns: the session identity and sequence numbers
(which continue across a node restart), the recorder, peer pause/unpause, snapshot
save/restore orchestration, reference resolution (12.3) and the tool registry. Headless
tools therefore get recording, restore and peer pause without a UI. `freeze-debugger
attach` connects a UI to a running broker; `freeze-debugger run` starts the topology and
a broker, then attaches. In FCP, `snapshot.restore`, `peers.pause`, `peers.resume` and
`session.*` are broker-served; everything else is forwarded. A second node kind only has
to implement the agent side.

### 12.2 Holding means the node is frozen

Default: when any item is held, the whole node is frozen (the global step lock is taken,
node time stops, peers are paused). Forward releases the held item, lets the node run,
and freezes again at the next item matching the hold filter. This keeps the node's
behaviour independent of how long a human looks at an item; in particular, call timeouts
between stages cannot fire because of a hold.

An explicit **flow mode** (`set_hold {scope: "item"}`) holds only matching items while
the rest of the node runs in real time, like a proxy. It is offered for tests where that
is the point, and the UI marks the session as "not frozen: timeouts are live".

### 12.3 Where blocks and transactions are edited

Stage messages in Amaru mostly carry references (points, hashes), not block bodies; for
example `ValidateBlockMsg` holds a tip and parent, and the block is read from the chain
store. Three layers carry real Cardano bytes, and these are the places to tamper:

1. `proto_in` / `proto_out`: the decoded mini-protocol message (a blockfetch `Block`, a
   chainsync header, a submitted transaction) before the node acts on it. **This is the
   canonical place to edit a block, header or transaction arriving from a peer.**
2. `wire_in` / `wire_out`: raw bytes, for malformed input the decoder would reject.
3. State values: `chain/blocks/<hash>`, `chain/headers/<hash>`, `mempool/txs/<id>`.

So that a stage-level item is still readable, the broker **resolves references**: a
per-node table (`dwarf/freeze/targets/amaru/references.yaml`) says which fields of which
message or effect types are block or header references; the broker fetches the object
through `state.get` and attaches it to the item as `resolved`. The UI shows the resolved
block inline under the stage item (as in the mockup), and an edit to it is applied as a
`state.put` before the item is forwarded. The agent stays free of Cardano knowledge.

Decoding splits accordingly: real Cardano CBOR (blocks, headers, transactions,
mini-protocol messages) is decoded by the node-independent `dwarf.freeze.cardano`;
node-internal payloads (Amaru's serde shapes) are shown as generic labelled trees using
the per-node glossary section.

The smoke scenario becomes: hold the blockfetch `Block` at `proto_in`, edit a fee,
re-seal, forward, assert the node rejects the block and marks the peer.

### 12.4 Peers during a freeze

`docker pause` stops peers from racing ahead but does not stop their clocks. After a
long freeze, peers may see a slot jump, drop connections and reconnect, and slots are
skipped. v1 accepts this: the status bar shows "peers paused 4m12s; expect reconnects",
and the session records connection resets as such, not as findings. Peers that are
themselves Freeze builds are frozen through their agents instead of paused, with node
time stopped, so a topology of Freeze nodes freezes cleanly. Clock virtualisation for
stock peers is out of scope.

### 12.5 The protocol and the cardano-node work

`dwarf/freeze/spec/freeze-control-protocol-v1.md`, its JSON Schemas and a scripted fake
agent (`dwarf/freeze/testing/fake_agent.py`) are written first and are the handoff to the
cardano-node work. The spec is "draft" until the Amaru agent passes the end-to-end test,
then "v1". Item kinds are generic where possible (`internal_input`, `internal_action`,
`internal_result`, `internal_state` instead of Amaru's stage vocabulary; the node reports
its own names for them in `hello`), and anything a node cannot do is expressed through
capabilities, not by changing the protocol.

### 12.6 What the applier must achieve

Mandatory hooks, without which a build is refused: adding the crate, the two runtime
gate sites, and agent start-up. All other hooks are optional and reported as missing
capabilities. The applier must be shown working on the pinned revision
(`v10.11.20260925`) and on one later upstream Amaru revision, each building and passing
the agent end-to-end test. Older revisions are not a target.

### 12.7 Hosts

Local Docker on Linux and Docker Desktop on macOS. Remote DWARF hosts are reached by the
user's own SSH port forward to the broker; Freeze does not manage SSH.

## 13. Payloads, rules and templates

Modelled on an intruder tool: lists of test values, applied automatically.

- **Payload list**: a named list of values. A value is a literal (`123`, `0xdead`, `"text"`)
  or a transform of what the field already holds (`add:1`, `trunc:4`, `flip:0`, `xorlast:01`,
  `zero`, `repeat:2`, …). Freeze ships four starter lists; a user's own live in the data root
  and win over a shipped list of the same name. Lists import from `.txt`, `.csv`, `.json`, `.yaml`.
- **Rule**: when an item matches a condition, act on it. Actions: `replace` a field with the
  next value of a list (one value per matching item, or cycling), `drop`, `forward`, or `replay`
  (send an earlier matching message in its place). `after: N` lets N matching items go by
  first. `field: "@message"` addresses the whole message as bytes. `expect:` lists phrases;
  the attempt is "as expected" if the node's reaction mentions any.
- **Template**: a file of rules, with any lists it brings along. Loading one arms all its rules.
- **Engine** (`freeze/rules.py`): for each armed rule it adds a freeze condition on the agent,
  so matching items are held even with Intercept off. When one arrives it acts, records an
  attempt, and lets the node run on with `thaw(keep=…)` so that another matching item arriving
  at that instant is not lost. A rule that has used its last value removes its condition.
- **Reaction**: what the node did after an attempt, read from the items that followed it:
  results carrying an error (named by the chain of names inside it, for example
  `Assert › KesSignature › InvalidKesSignature`), rejections, a peer marked hostile, a
  connection closed, or the node no longer answering. Attribution is by position in the item
  stream up to the next attempt, so with attempts in quick succession a reaction can be split
  between neighbouring rows.

The UI's Payloads tab edits lists and shows rules and results. In Intercept, `p` runs a list
against the selected field: Freeze derives the condition from the item (kind, protocol and
message number, or label and part) and arms a rule.

## 14. Runs: export, import, do again

- A session is recorded as `events.jsonl` under the data root. `export` writes it, whole or up
  to an item number, as one archive with a manifest and the list of **interventions**: every
  drop and edit, each stored as a condition describing the item's nature, how many items of
  that nature went by first, and the edit as field paths and values (or the whole replacement
  when the shape changed).
- `import` unpacks an archive and prints the interventions in sentences. `open` shows an
  imported run in the UI with no node.
- `ingest` restarts the node frozen and repeats the interventions. A live node does not
  produce the same items in the same order twice, so interventions are matched by nature and
  count, not by item number. Interventions on one nature keep their order; no order is imposed
  between natures. The node is left frozen after the last one.
- Limits: the restart keeps the node's stored chain, so it does not return to the state the
  recorded run began in (that needs snapshots, section 6.6, or a fresh devnet). Injections
  and State-tab edits are not recorded as events yet and are therefore not replayed.

## 15. DWARF scenarios

`freeze/scenarios.py` reads `dwarf/scenarios/*.yaml` (they are JSON) and DWARF's own value
tables, as text, without importing or running any DWARF code.

- Every scenario is listed with its target, subject, threat and risk references, workload
  and assertions, and a status: **ready**, **partly**, or **no** with the reason.
- Conversion is by workload primitive:
  - DWARF's man-in-the-middle proxy modes (`MODE_DEFAULTS`) map one to one: `replay_earlier`
    → `replay` after N; `corrupt_header_signature` → damage the last byte of `kes_signature`;
    `flip_payload_byte` / `flip_once` → `xorlast:01` on the whole message.
  - Fake-peer cases (`_HANDSHAKE_CASES`, `_BLOCKFETCH_CASES`, `_TXSUBMISSION_CASES`,
    `_INVALID_CBOR_CASES`, the tx-submission probe cases, the handshake case sets) become
    whole-message replacements of the next message of that protocol from a real peer.
  - Port drops become dropping a number of raw chunks (partly: DWARF drops for a time).
- Not convertible, with the stated reason: decoder-only fuzzing, measurement and evidence
  tooling, timed delay or loss, anything needing re-signed blocks or headers, stored-state
  corruption, forced clock or epoch, two-node differentials, load generators, topology games.
- Assertions are not ported one by one. Rejection-type assertions become an `expect` on the
  rule; whole-topology assertions (quorum, a reference node's verdict) cannot be seen from
  inside one node.
- DWARF's proxy modes only target cardano-node, so running them on Amaru through Freeze is
  new coverage, not a repeat.

## 16. Causes: which step followed from which

Problem: reactions were attributed to a tampered message by position in the stream (the
items after it, up to the next attempt). With attempts a fraction of a second apart, or any
other traffic interleaved, a reaction could land on the wrong row, and "no reaction" could
not be told from "accepted".

Fix: the node reports causality. Every item may carry `cause`, the `seq` of the item at
the root of its chain (spec section 3). The Amaru hooks keep two tables (`amaru-freeze`
`trace.rs`):

- per stage: the input it is handling and that input's cause;
- per message in flight: the step that sent it and that step's cause. A message is
  recognised on arrival by the address of its heap allocation, which does not change while
  it sits in a mailbox. Zero-sized messages share one address and are not tracked.

Rules: an input nobody is known to have sent (a timer, an injected message, bytes from the
network) starts its own chain. A decoded message from a peer (`proto_in`) starts a new
chain inside the stage that received it, so everything the node does with that message
names it. Sends, calls, scheduled messages and timeouts carry the cause to the receiving
stage; results of effects and state snapshots inherit it. `parent` is now also set on
inputs (the step that sent them).

Known gaps: a reply to a `call` returns as a call response, not an input, and stays in the
caller's chain (correct, but the callee's work on it is a separate branch only linked by
`parent`). A message that is sent but never delivered leaves an entry behind; the table is
emptied when it reaches 65 536 entries, after which messages already in flight arrive
without a cause. Work a stage does later on its own initiative (a timer) about an earlier
message is not linked to it.

Debugger side: `Session.caused`/`consequences`, `rules.reaction` (uses causes when the
session has seen any, the old positional guess otherwise) and History's "what the node did".
An attempt with consequences and no error reads `no error in N steps, reached …`.

## 17. Devnet profiles

A profile (`dwarf/freeze/profiles/*.yaml`, or `<data root>/profiles/`) names a compose
file, the service that becomes the Freeze node, the network, the peer cooldown and extra
environment for the node. `devnet up --profile NAME|FILE`, or `--compose FILE --target
SERVICE` without a profile; `devnet profiles` lists both profiles and the DWARF layouts
that fit. The active profile is saved beside the generated override so every later
command (`status`, `attach`, `ingest`, `pause-peers`, `down`) addresses the right node.

The binary is swapped through the `AMARU_BIN` variable that DWARF's `amaru-relay-bootstrap`
wrapper honours, so only services started by that wrapper qualify (three layouts today:
relay bootstrap control, KES security, mini-protocol security). Not covered: layouts that
bake Amaru into an image, DWARF's remote `profile-a…g` runtime profiles, more than one
Freeze node at once, and Haskell nodes as the target.

## 18. Re-signing edited headers

Eligible inbound headers can be re-signed only inside the controlled devnet, using the
generated producer material mounted read-only at fixed paths. The toggle defaults on for
shipped devnet profiles; explicit off mode retains stale-signature behavior as a negative
control. Slot, issuer, VRF certificate, and operational certificate are immutable: the
feature preserves the producer and leader-election result and only regenerates the KES
signature over an eligible edited header body.

Inbound typed BlockFetch `MsgBlock` responses also pass through the Freeze gate. This allows
transaction bodies, witnesses, auxiliary data, and invalid-transaction indices to be edited,
but a body mutation is emitted only when a previously announced re-signed ChainSync header
commits to its exact four-component Cardano body hash and body size. The body message must
still contain the original producer header; the coordinator substitutes the exact signed
header atomically after checking the original point, message shape, hash, and size. Since the
header is announced before the block is fetched, this is intentionally a two-pass/replay
workflow: inspect the edited body commitment, edit the matching header before replay, then
repeat the body edit.

Hash and size checks apply to every mapped response, including an unchanged body; there is no
size-mismatch exception. If replay created several edited mappings for one original point, the
typed BlockFetch pipeline retains both the edited range requested by consensus and the translated
producer range sent on the wire. At dispatch, it also snapshots the exact original-to-edited point
mapping for every announced edited header on the selected branch, including intermediate blocks.
The coordinator selects by that immutable request identity, the original point, and the exact
returned-body hash and size. Each peer pipeline receives a distinct active request identity.
Selection, substitution, verification, and consumption for that identity are one mutex-protected
transition: a repeated response on the same request is rejected, while a legitimate response from
another peer that was concurrently asked for the range retains its independent right to use the
same mapping. The global signed-header mapping remains available for later refetches; only the
per-request replay state is released at that pipeline's `NoBlocks` or `BatchDone`. An all-original
request has an empty snapshot, so unrelated edited mappings are neither substituted nor consumed.
If a snapshot expects a mapping that is missing, belongs to a finished request, or was already used
by that same request, the response is rejected. Only helper paths without typed request identity
use commitment matching. If a new request finds a broken rewritten-parent link because a mapping
is missing, the entire request is marked incomplete and rejected. Multiple helper-path matches are
rejected as ambiguous rather than selected by edited-header key order. No exact, unambiguous match
means no block is emitted.

This reseals the block envelope; it does not recreate transaction witnesses or make a mutated
transaction ledger-valid. Outbound scripted-peer messages, newly injected blocks/headers, and
chain-store writes remain unsigned. Header/body mappings are process-local and must be rebuilt
by replaying ChainSync after restart. Identity checks, KES periods, commitment preimages,
ChainSync ancestry, BlockFetch translation, failure behavior, and acceptance tests are specified
in `dwarf/freeze/docs/RESIGNING-DISCOVERY.md`.

## 19. Interactive tooling

| # | Feature | Where | State |
|---|---|---|---|
| 1 | Repeater | `repeater.py`, `repeat`, History `R` | built. Inputs are injected; peer messages replace the next like message |
| 2 | Snapshots | `devnet.py` (`snapshot_*`), `devnet snapshot`, `ingest --snapshot` | built, Freeze node only |
| 3 | Compare | `analysis.py`, `compare`, `coverage --against`, History `c` | built |
| 4 | Paired edits | rule action `capture`, values `$name` | built for copying values; no recomputed hashes |
| 5 | Timing | agent `item.defer`/`item.resume`, rule actions `delay`, `overtake` | built |
| 6 | Several Freeze nodes, scripted peer | `devnet.py` (`also`, `node_environment`, `node_ports`), `devnet nodes`, `--node`, profiles `relay-pair` and `scripted-peer` | built. The scripted peer is a second Freeze node upstream of the node under test |
| 7 | Chain and ledger stores in State | `amaru-node/src/freeze_stores.rs`, State tab paging in `tui.py` | built, read-only |
| 8 | Findings and reports | `notes.py`, `mark`, `findings`, History `F` | built |
| 9 | Search, bookmarks, notes | `analysis.search`, `notes.py`, `search`, History `b`/`m` | built, with saved filters (`filters.py`, `filters`, `@name`) |
| 10 | Coverage | `analysis.coverage` | built from the session; 43 function-level probes (`amaru-freeze/src/probes.rs`, `probes`, Freeze points tab) |
| 11 | Scripting hook | `scripting.py`, `examples/drop_large_fee.py` | built (added separately) |
| 12 | Resource watch | `resources.py`, `devnet stats`, queues in `coverage` | built |

How these are built:

- **6, several nodes:** a profile lists extra targets in `also`; the override gives each
  the Freeze binary and its own local agent port (main node on `--port`, the rest on the
  ports after it); `run.json` records `nodes`. `scripted-peer` points the node under test
  at the other Freeze node with `AMARU_PEER`, so rules on that node's outgoing messages
  reach it over the wire. Snapshots and `ingest` still act on the main node only.
- **7, stores:** one `StateProvider` per namespace, registered where the node opens its
  chain store. The chain namespaces walk the best chain from the anchor. The ledger store
  belongs to the ledger thread, so each request opens a read-only view of it; that view is
  the ledger as stored, behind the tip by the rollback window.
- **10, probes:** a registry in the agent crate with a central list (`KNOWN`) of every
  probe placed, checked against the source by a test. Hits are counted always; an item is
  reported only for enabled probes. Not probed: the ledger rules and script evaluator
  (`amaru-ledger` is also built for targets the agent crate does not support).

`item.defer` semantics are in the spec (section 6). A deferred item is not in `held`, so
the node can run; it is forwarded unchanged when its time comes or when enough matching
items have been admitted.

## 20. Second round: what was left, in build order

Ordered easiest first; an item names the earlier items it needs. Where this section
differs from earlier ones, this section wins.

| # | Item | Needs |
|---|---|---|
| 1 | Inject from the UI | – |
| 2 | Nodes view (several Freeze nodes in one UI) | – |
| 3 | Broker: peers paused with the node, one owner per session | – |
| 4 | Probes in the ledger rules and the script evaluator | – |
| 5 | Editing and skipping effects | – |
| 6 | Writing to the chain and ledger stores | – |
| 7 | Every-function tracing, call stack | 4 (the dependency-free probe crate) |
| 8 | Applier for other Amaru revisions | 7 (the applier adds the tracing) |
| 9 | Rewind | 1, snapshots |
| 10 | Source lines | – |

### 20.1 Inject from the UI

`n` in Intercept or History opens a dialog: where to (one of the agent's destinations)
and the payload as JSON, filled in from the selected item when that item is an input to a
part of the node. Sending calls `item.inject`; it is counted as tampering.

The limit "a stage must have handled one message first" goes: the runtime knows a stage's
message type when the stage is built, so it registers a decoder for that type then,
instead of keeping the first message as a pattern. Stages wired at run time (their type
is erased by then) still learn it from their first message.

### 20.2 Nodes view

A **Nodes** tab, present when the devnet has more than one Freeze node. It lists each
node (name, port, frozen or running, held, items seen) and, below, the last items of all
nodes merged by time. `Enter` moves the UI to the selected node; `Z` freezes and `G` runs
all of them. The UI still drives one node at a time: moving closes the current
connection and opens the UI again on the other node, so every tab shows that node.
`devnet timeline` prints the merged list for scripts.

### 20.3 Broker

`dwarf/freeze/broker.py`, one process per Freeze node, started by `devnet up` and `run`
and stopped by `down` and `stop`. It sits between clients and the agent and speaks FCP on
both sides, so a client cannot tell it from an agent except by `hello.broker`.

- **Forwarding.** Requests are passed to the agent with their ids rewritten; events go to
  every client.
- **Peers.** When the node reports frozen, the broker pauses the stock peers (`docker
  pause`) and freezes the other Freeze nodes through their agents; when it runs again, it
  undoes both. `peers.auto {on}` switches this, `peers.pause`, `peers.resume` and
  `peers.status` do it by hand. A step shorter than a grace time (250 ms) does not pause.
- **Owner.** Nobody owns a session to begin with: every client may act, as without a
  broker. A client may claim it (`session.claim`; `force` takes it from another). From
  then on the others are observers: reads and events work, anything that changes the node
  is refused with `not_owner`. `session.release` gives it up, `session.who` lists clients,
  and a claim ends when its client disconnects.
- **Recorder.** The broker records the session whether or not a UI is attached.

The agent port stays published on localhost for tools that want the agent itself;
`attach` and the tools use the broker's port when a broker is running. `--direct` skips it.
Ownership is a courtesy between clients of the broker, not a security boundary: the
token still decides who may connect at all.

### 20.4 Probes in the ledger and the script evaluator

`amaru-ledger` is also built for targets the agent crate cannot be built for. It depends
on `amaru-freeze` only under the target condition it already has for such dependencies,
and its call sites go through a crate-local `probe!` that is empty on the other targets.
Probes are on: each phase-one rule (`ledger.rule.*`, with the error when it fails), the
block-level rules (`ledger.block_rule.*`), the transaction and block verdicts
(`ledger.verdict.*`), script evaluation (`ledger.script.eval`: purpose, index, Plutus
version, budget given and spent, verdict), the epoch transition and the snapshot.

### 20.5 Editing and skipping effects

Until now only `send` could be changed. Added, at the same gate:

| Effect | Edit | Drop |
|---|---|---|
| wait | the duration | returns at once |
| call | the timeout | – |
| set timeout | the delay | the timeout is not armed |
| schedule | – | the message is never delivered |
| external | its fields, if the effect's type reads them back | – |

An edit that the type refuses keeps the original and logs why, as for messages. Effects
that create or stop parts of the node stay read-only.

### 20.6 Writing to the stores

- **Chain store:** `chain/headers`, `chain/blocks` and `chain/best` accept `state.put` and
  `state.delete` through the node's own store handle. A block or header is given as hex
  CBOR; the key must match its hash unless `force` is set, so a mismatched pair is a
  deliberate act.
- **Ledger:** the ledger store belongs to the ledger thread. Writes are queued and applied
  by that thread between blocks through the store's own transaction, for `ledger/utxo`,
  `ledger/pots`, `ledger/params` and `ledger/stake`. They change the ledger as stored;
  what the node holds in memory for the last blocks is not changed, and the reply says so.
- Every store write through the agent is recorded as a `store_write` item and counts as
  tampering. Writes are refused unless the node is frozen.

### 20.7 Every function

`#[amaru_freeze::trace_fn]` (an attribute macro in `amaru-freeze-macros`) puts a guard at
the top of a function body. With tracing off the guard is one atomic load. With the
function enabled, entry is reported as a `call` item (name, arguments that implement
`Debug`, depth, caller) and the guard's drop reports the end (time taken, whether it
unwound).

- Enabled per function, per module or per crate by path prefix: `trace.enable {prefix}`,
  `trace.disable`, `trace.list`; env `DWARF_FREEZE_TRACE`.
- The call stack is kept per thread for ordinary functions. An `async fn` is reported
  when it starts and ends but is not put on the stack, because it moves between threads.
- A `call` item is reported, not held: a function may run on a thread that must not
  block. "Run until" on a call freezes the node at its next step.
- The attribute is not in the stored patch. `build` adds it to every function of the
  crates the manifest lists under `trace` (`instrument.py`), so the patch stays small
  enough to review. `const fn`, functions without a body, tests and functions inside
  macros are left alone.

### 20.8 Applier

`dwarf/freeze/applier.py` and `targets/amaru/spec.yaml`; command `port`. The spec puts
every file the stored patch touches into one group, names the capabilities each group
gives, and marks the groups a build cannot do without (`gate`, `start`).

1. Copy the other revision and add the agent crates.
2. Group by group, merge each hooked file three ways: the base revision's file, the same
   file with hooks, the other revision's file. A group is applied whole or not at all.
3. A mandatory group that does not merge stops the port. An optional one is left as
   upstream has it and its capabilities are reported missing.
4. Compile-check in a container with the tree's toolchain (this also brings the lock file
   up to date). If the errors are in an optional group's files, that group is taken out
   and the check repeated; errors in a mandatory group, or outside the hooks, stop the port.
5. Store the result as a patch series with a manifest (`ported_from`, `missing`,
   `left_out`), in the user's data folder or, with `--shipped`, among the shipped recipes.

When both sides changed the same place, a merge cannot decide. `port --by-hand DIR`
leaves the tree in `DIR` with those places marked; after a person resolves them,
`port --resolved DIR` does steps 4 and 5.

Porting `v10.11.20260925` to itself reproduces the stored series (a test). The releases
after it reworked the stage runtime, so the gate does not merge by itself there: see the
guide for what was done for `v10.11.20261105`.

### 20.9 Rewind

`devnet up --frozen` saves a snapshot named `start` before the node handles its first
input. `rewind --to N` restores `start`, starts the node frozen, puts back every change
the session made before item N (the same mechanism as "do again", section 14), and runs
until the item that corresponds to N, where it freezes. Correspondence is by content
(kind, type, the block or header it carries) and by how many like items came before, not
by number, since a second run numbers its items differently. If the node does not reach
it, rewind says how far it got. In-memory state is rebuilt by the node, not restored, as
with any snapshot.

### 20.10 Source lines

The normal Freeze build has no debug information (the node's own build profile turns it
off). `build --debug-info` keeps it (the node's own crates in full, dependencies as line
numbers) and also builds a tools image, `<image>-tools`, holding lldb, the source tree
and `lldb_driver.py`. `source` runs that image beside the node's container, sharing its
processes and volumes, and talks to the driver over its standard input and output:
`break FILE:LINE`, `step`, `next`, `out`, `continue`, `interrupt`, `where`, `vars`,
`threads`, `detach`. The UI has a **Source** tab showing the file around the current
line, the stack and the locals.

A process stopped by a debugger stops its agent too. So source stepping is a mode: while
stopped, the UI shows "stopped in the debugger" and only the Source tab acts; `continue`
or `detach` returns to normal. Only the tools container gets the right to trace; the
node's own container is not changed.

### 20.11 Not in this round

- Re-sealing beyond headers (section 18 is separate work).
- Windows without WSL and the macOS devnet: the client is plain Python and is expected to
  work against a remote node; nothing here was run on Windows. The devnet needs a Linux
  x86-64 host because the images are.

## 21. Third round: what is left after section 20

Status of every item: built, except as section 21.18 says. Order is build order; an item names
what it needs. "Run" says how it is shown working end to end.

| # | Item | Needs | Can be run end to end |
|---|---|---|---|
| 1 | Event filter | – | yes |
| 2 | Capabilities as built | – | yes |
| 3 | Agent crate per recipe | – | yes |
| 4 | Old ledger probes removed | 3 | yes |
| 5 | Schedules editable | – | yes |
| 6 | Freeze nodes frozen together | – | yes |
| 7 | Rewind and snapshots for any Freeze node | – | yes |
| 8 | Rust values in source stepping | – | yes |
| 9 | Tracing: macros and async depth | – | yes |
| 10 | Holding inside a traced function | 9 | yes, for the threads named in 21.10 |
| 11 | Nodes tab that acts on several nodes | 1 | yes |
| 12 | Writing stake and in-memory ledger state | – | at risk, see 21.12 |
| 13 | Later release in the devnet | 3 | only by trying, see 21.13 |
| 14 | Transactions on the devnet, for the rule probes | – | only by trying, see 21.14 |
| 15 | Applier by anchor | 3 | partly, see 21.15 |
| 16 | Client on Windows | – | no machine to run it on |

### 21.1 Event filter

**Problem.** The agent sends every event to every client. A node catching up on a public
network produces about 45,000 a second; a Python client reads fewer, falls behind, and its
answers arrive seconds late.

**Protocol.** `events.subscribe {when?, kinds?, sample?}` → `{}`. Per connection.
- `kinds`: item kinds to receive; default all.
- `when`: a predicate (section 7 of the protocol); only matching items are sent.
- `sample: n`: of the items that pass, send one in `n`; `stats` events make up the rest.
- Always sent whatever the filter: held items, `released`, `deferred`, `frozen`, `thawed`,
  `condition`, `log`, `error`, and `item.done` for items that were sent.
- New event `stats {seq, items, by_kind, dropped}` once a second while anything is
  filtered out, so a client can still show progress and rates.
- `events.subscribe {}` restores everything. Feature string `"events:filter"`.

**Agent.** The filter is applied in the per-connection writer, before serialising, so an
unsent item costs a predicate evaluation and nothing else. The item is still kept in the
agent's recent-items buffer, so `item.get` works for it.

**Broker.** Subscribes to everything itself only if it records everything; otherwise to the
union of its clients' filters, and forwards what arrives to all of them. A client may
therefore be sent more than it asked for when another asks for more; evaluating every
client's filter in the broker would cost what the filter is there to save.
`--record all|held|none` on `devnet up` and `run` (default `all` for the devnet, `held`
for public networks: held items, verdicts and what followed from them).

**Client.** `watch`, tools and the UI subscribe to what they show. The UI's default on a
public network is held items plus a one-in-`n` sample chosen so that fewer than 2,000
events a second arrive; the status bar says `showing 1 in n`. History's filter box narrows
the subscription as well as the table.

**Run.** Preview during catch-up: request latency through the broker under 200 ms with
the default filter, against many seconds without; held items never missed (a rule that
holds every tenth header sees every one).

### 21.2 Capabilities as built

`build` writes the recipe's capability list into the image as a label and passes it to
the node as `DWARF_FREEZE_CAPABILITIES`. The node's `hello.node.features` is the
intersection of what the agent crate can do and that list. A port that left a group out
therefore yields a node that does not claim it, and the UI hides what is not there
(no probes table without `probes`, and so on). **Run:** port with the ledger group left
out; `hello` lacks `probes:ledger`; `probes --enable 'ledger.*'` says there are none.

### 21.3 Agent crate per recipe

A recipe's manifest may name `crate_dir`, a folder beside its patch holding its own copy
of the agent crates; without it the shared ones are used. `port` writes the crates as
they are in the resolved tree when they differ from the shared ones. The
`v10.11.20261105` recipe gets its own copy, with the probe list that release has.
**Run:** the probe-list test in that tree; `probes` on a node of that release lists no
probe that cannot fire.

### 21.4 Old ledger probes removed

`ledger.epoch_boundary` and `ledger.snapshot` (after-the-fact comparisons in the block
validator) go; `ledger.epoch_transition` and `ledger.snapshot.taken` replace them. The
guide and the protocol document lose the two names. **Run:** an epoch boundary on the
devnet reports the new pair only.

### 21.5 Schedules editable

A held `schedule` accepts a replacement `{at_ms?, message?}`: `message` is rebuilt as any
message is; `at_ms` is an offset in milliseconds from now on the stage clock. The stage made
the schedule's id itself before the effect was interpreted, so the id is kept and only the
time it fires changes; a later `cancel` by the stage therefore still finds it. **Run:** the gate test; on the devnet the
keep-alive schedule moved from its interval to 50 ms and seen firing then.

### 21.6 Freeze nodes frozen together

`peers.auto` gains a mode: `{on: true, freeze_nodes: true}`. The main node's broker then
also freezes the devnet's other Freeze nodes through their agents when the main node
freezes, and thaws them when it runs. The other brokers do nothing on their own, so there
is one source of truth. A node that was frozen by hand before stays frozen on thaw
(the broker remembers which it froze). `devnet up --freeze-together`. **Run:** two-node
devnet: freeze the main node, both report frozen within the grace time; thaw, both run.

### 21.7 Rewind and snapshots for any Freeze node

`devnet snapshot save|restore --node SERVICE` and `rewind --node SERVICE`. The snapshot's
metadata already names its node; restore refuses a snapshot of another node. Each node's
broker saves its own `start` (`start-<service>`) when the devnet starts frozen.
**Run:** rewind the second relay of `relay-pair` to an item of its own recording.

### 21.8 Rust values in source stepping

The tools image installs lldb from the LLVM release that matches the node's toolchain
(not Debian's 14) and loads the Rust formatters shipped with that toolchain
(`lldb_lookup.py`, `lldb_commands`). The driver's `vars` then gives strings, vectors,
options, enums and structs one level deep as text; `vars NAME` opens one further.
**Run:** at a breakpoint in the stage loop, the stage name shows as its text and the
message as its variant with fields.

### 21.9 Tracing: macros and async depth

- **Macros.** The tool marks functions inside the bodies of `macro_rules!` definitions
  and inside the arguments of the workspace's own item macros (a list in the spec),
  skipping any macro it does not know. Each addition is compile-checked as a whole; a
  crate that no longer compiles falls back to the current rule for that file.
- **Async depth.** The guard of an `async fn` keeps its place in a task-local stack
  (the same mechanism the stage context already uses), so calls made inside it report
  their depth and caller across awaits. Outside a stage task it falls back to today's
  behaviour.
**Run:** functions seen on the devnet rise above 2,084 traceable; an `async` stage
handler's callees report it as caller.

### 21.10 Holding inside a traced function

`trace.hold {prefix}` marks functions whose calls are **held at entry** like any item:
the call item is `held`, shows arguments, and waits for forward. Drop and replace are not
offered (there is no sound way to skip a function or swap its arguments by type).

- On the ledger thread and other plain threads the wait blocks that thread.
- In a stage task the wait is the same async wait the stage gate uses.
- In an `async fn` outside a stage, and in sync code on a runtime worker thread outside a
  stage, holding is refused for that call and it is reported as usual, with
  `not_held: "would block the runtime"`. The item says which case it was.
- Holding freezes the node, as any hold does (12.2), so timeouts cannot fire meanwhile.
**Run:** hold `amaru_ledger::rules::block::execute` on the devnet; the node stops inside
block validation with the block's arguments shown; forward lets that one block through.

### 21.11 Nodes tab that acts on several nodes

The Nodes tab gains a held list for all nodes (node, item) with forward, drop and step on
the selected row, sent to that node's connection. Editing an item still opens it on its
own node (Enter moves the window with the item selected). The connections to the other
nodes use the event filter (held items only), which is what makes this affordable.
**Run:** two-node devnet with intercept on in both: items of both listed, one of each
forwarded from the same window.

### 21.12 Writing stake and in-memory ledger state

- `ledger/volatile` (new, read first): the blocks the ledger holds in memory past the
  stored tip, each with its point and a summary of what it changed. Read-only to begin with.
- Writes to it, and to `ledger/stake`, are done by the ledger thread through the same
  hook as stored writes. `ledger/stake` is not a column; a write there is turned into the
  account and delegation entries it is derived from.
- **Risk.** The in-memory state has invariants (it must replay to the same result on
  rollback) that an outside edit can break in ways that show only later. So: every such
  write is refused unless the value carries `"unsafe": true`, the reply and the session
  record say that the node's later behaviour is no longer evidence of anything but the
  edit, and the UI marks the session.
**Run:** read side on the devnet. Write side: a treasury change in the volatile state
followed by a rollback past it, checking the node neither panics nor diverges from a
second node; if it does, the write side is withdrawn and the item stays read-only.

### 21.13 Later release in the devnet

A Freeze profile may set `node_command` (the words that start the node) and
`bootstrap_command`; the override passes them to DWARF's wrapper through the variables it
already reads, or, where the wrapper has none, replaces the service's command with a
small script of Freeze's own that does what the wrapper does with the new words. DWARF's
files are not edited. Profile `relay-20261105`. **Run:** by trying. If the devnet's
bootstrap data or configuration does not fit that release, the profile is not shipped and
the guide keeps its note.

### 21.14 Transactions on the devnet

`devnet tx` sends a transaction with the devnet's own funded keys (those DWARF's
configurator generates): a plain payment, and one that runs a script of each Plutus
version from a small set shipped with Freeze. It uses the cardano-cli already in the
producers' containers; Freeze brings no keys and no wallet code. **Run:** the per-rule
probes, the transaction verdict and `ledger.script.eval` report on the devnet. If the
devnet's genesis leaves no funds those keys can spend, this is not possible and the
probes stay covered by the node's tests only.

### 21.15 Applier by anchor

The spec gains, per hook, an anchor: the enclosing function and a short pattern inside
it, plus the lines to insert before or after. `port` tries the three-way merge first and,
for a file that conflicts, places that file's hooks by anchor in upstream's version.
A hook whose anchor is not found, or found more than once, is a conflict as today.
This removes conflicts caused by code moving or changing near a hook. It cannot help
where upstream rewrote what the hook hooks: of the 18 files of `v10.11.20261105`,
this would have placed the probes (about 12 files) and not the gate, the block
validator or the node wiring. **Run:** porting to `v10.11.20261105` again leaves only
those files for a person.

### 21.16 Client on Windows

What is POSIX-only today: the broker (signal handlers, `os.kill(pid, 0)`, new sessions),
file modes on the token, `docker` invoked with POSIX paths, the launcher script. The
client, the UI and `attach --host` to a node elsewhere use none of these once the broker
is skipped (`--direct`). Design: `attach`, `watch`, tools and the UI are supported on
Windows against a remote node; `build`, `run` and `devnet` are refused there with a
message naming WSL. **Not run:** there is no Windows machine in this work.

### 21.17 Not designed here

- **Paused peers' clocks.** Stock peers keep real time while paused. Stopping it means
  running them under a faked clock inside DWARF's containers, which is DWARF's side.
- **macOS devnet.** The devnet's images are x86-64 Linux; this needs ARM images of DWARF's
  nodes.
- **Edits that the node refuses before the rule under test.** Section 18 covers one case.
  The rest is separate work with its own document.

### 21.18 How section 21 came out

| # | Item | Result |
|---|---|---|
| 1 | Event filter | built as designed |
| 2 | Capabilities as built | built; probes are grouped by the crate they sit in, not by name |
| 3 | Agent crate per recipe | built (`crate_dir`); the `v10.11.20261105` recipe has its own |
| 4 | Old ledger probes removed | built |
| 5 | Schedules editable | built; the id is kept (21.5) |
| 6 | Freeze nodes frozen together | built (`--freeze-together`, `peers.auto {freeze_nodes}`) |
| 7 | Rewind and snapshots for any node | built |
| 8 | Rust values in source stepping | built with Debian's lldb 19 and the toolchain's formatters |
| 9 | Tracing in macros, async depth | built; 25 more functions (one macro); async depth without task-locals: the frame travels with the future |
| 10 | Holding inside a traced function | built for the ledger thread and `async` functions in stages; refused elsewhere, and said so |
| 11 | Nodes tab acting on several nodes | built: a held list across nodes with forward, step, drop |
| 12 | Stake and in-memory ledger state | `ledger/volatile` is read-only and `ledger/accounts` is new and writable with `"unsafe": true`. Dropping in-memory blocks was **not** built: the ledger only rolls back as half of a fork switch and does not check a block's parent, so a bare rollback would leave the stage and the chain store ahead of it and the next block applied on the wrong parent. `ledger/stake` stays derived and read-only. |
| 13 | Later release in the devnet | works: the image says how its node is started and the devnet puts a two-line launcher in the node's place |
| 14 | Transactions on the devnet | works: `devnet tx pay` and `devnet tx script`, with the configurator's funded keys |
| 15 | Applier by anchor | built: hooks are placed by the lines around them when a merge gives up. `v10.11.20261105`: 8 files left for a person instead of 18 |
| 16 | Client on Windows | local-only commands are refused there with a pointer to WSL; not run on Windows |
