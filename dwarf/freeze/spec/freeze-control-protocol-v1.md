# Freeze Control Protocol, version 1 (`fcp/1`)

Status: **draft**. It becomes v1 when the Amaru agent passes the end-to-end test.

This is the contract between a *debugger* (the Freeze broker, the terminal UI, a tool, a
script) and an *agent* running inside a node process. Any node that implements the agent
side can be driven by the same debugger. Reference implementations: agent
`amaru-freeze` (Rust), client `dwarf/freeze/client.py` (Python).

## 1. Transport

- TCP. The agent listens on the address in the environment variable
  `DWARF_FREEZE_LISTEN` (for example `0.0.0.0:7373`). With the variable unset the agent
  does not exist and the node behaves as an unmodified node.
- Messages are UTF-8 JSON objects, one per line, terminated by `\n`. No message contains
  a raw newline.
- `DWARF_FREEZE_TOKEN` is required and is the shared secret. If it is missing or empty,
  the agent fails closed before binding the control socket. `DWARF_FREEZE_START=frozen`
  makes the node hold its very first step.
- The agent must keep answering while the node is frozen.

## 2. Messages

Request, from the debugger:

```json
{"id": 7, "method": "item.forward", "params": {"seq": 48212}}
```

Response, from the agent, exactly one per request:

```json
{"id": 7, "result": { ... }}
{"id": 7, "error": {"code": "failed", "message": "item 48212 is not held"}}
```

Event, from the agent, at any time after `hello` succeeded:

```json
{"event": "frozen", "data": {"seq": 48212, "reasons": ["hold"]}}
```

`id` is chosen by the debugger and echoed. Error codes: `bad_json`, `hello_first`,
`bad_token`, `failed`. After `bad_token` the agent closes the connection.

The first request on a connection must be `hello`. Events are delivered to every
connection that completed `hello`, starting from the moment it did.

## 3. Items

Everything a node does is reported as an **item**: one step, with a sequence number.

| Field | Meaning |
|---|---|
| `seq` | Positive integer, unique and increasing for the life of the node process. |
| `t_ms` | Wall-clock time the item was observed, milliseconds since the Unix epoch. |
| `kind` | See below. |
| `from`, `to` | Where the item comes from and goes to: `peer:<address>`, `stage:<name>`, `node`, `store:<namespace>`, or empty. |
| `type` | The node's own type name for the payload. May be empty. Mini-protocol messages are `ouroboros/<n>`; the serving side of a protocol has 32768 added to `n` (chain sync is 2 for the side that asks, 32770 for the side that serves). |
| `label` | Short human name: `Block`, `send`, `RollForward`, `clock`. |
| `cold_spots` | Tags of the named interesting places this item is at. May be empty. |
| `payload` | The item's content, in the payload encoding (section 4). |
| `raw` | Present when the item is bytes: lowercase hex. |
| `parent` | `seq` of the item that caused this one, when known. |
| `cause` | `seq` of the item at the root of the chain of steps this one belongs to, when the node tracks it: usually the message from a peer that set the chain off. Absent on the root itself. A node that fills this in lists `"cause"` in `hello.node.features`. |
| `held` | `true` if the node is waiting for a decision on this item. |
| `editable` | `true` if `item.replace` is allowed. |
| `droppable` | `true` if `item.drop` is allowed. |

Kinds. A node reports the kinds it supports and its own name for each in `hello`.

| Kind | What it is |
|---|---|
| `internal_input` | A message about to be handled by a component of the node. |
| `internal_action` | An action a component is about to take: send a message, read a clock, read or write a store, validate something, wait. |
| `internal_result` | The answer to an `internal_action`, about to be handed back. |
| `internal_state` | The state of a component after it handled an input. Never held. |
| `wire_in`, `wire_out` | Raw bytes received from or about to be sent to a peer. |
| `proto_in`, `proto_out` | A decoded network protocol message received or about to be encoded. |
| `store_write` | A write to persistent state about to happen. |
| `timer` | A timer about to fire. |
| `probe` | A named place in the code was reached. |

## 4. Payload encoding

Payloads are JSON with these wrappers for what JSON cannot express. A debugger sends
edited payloads back in the same form.

| Value | Encoding |
|---|---|
| null, booleans, text, arrays | as JSON |
| integer within ±(2^53 − 1) | JSON number |
| any other integer | `{"$int": "<decimal>"}` |
| finite float | JSON number |
| NaN, ±infinity | `{"$float": "nan" \| "inf" \| "-inf"}` |
| byte string | `{"$bytes": "<lowercase hex>"}` |
| CBOR tag | `{"$tag": <n>, "value": <v>}` |
| map whose keys are all text, unique, and do not start with `$` | JSON object |
| any other map | `{"$map": [[<key>, <value>], ...]}`, order preserved |
| content the node cannot serialise | `{"$unserializable": "<reason>"}`; the item is not editable |

The agent does not interpret payloads. Decoding Cardano structures out of them is the
debugger's job.

## 5. Frozen, held, and how a node proceeds

- The node is either **running** or **frozen**.
- When running, each step is reported and proceeds, unless it must be held.
- A step is **held** when the node is frozen, or it matches the hold filter, or it matches
  a condition whose action is `freeze`. Holding a step freezes the node.
- While frozen, every step any part of the node reaches is held. Held steps are released
  only by a debugger.
- Releasing one held item (`item.forward`, `item.drop`, `item.replace`, `step`) leaves
  the node frozen: that part of the node runs until its next step, which is held.
- `thaw` releases all held items that do not match the hold filter and lets the node
  run. If any held item matches the hold filter it stays held and the node stays frozen.
- Time: while frozen, the node's timers and its internal clock do not advance, as far as
  the node supports this (reported in `hello`). `status.frozen_ms` is the total.

## 6. Requests

`when` is a predicate (section 7).

### Session

| Method | Params | Result |
|---|---|---|
| `hello` | `token?` | `{protocol: "fcp/1", node: {...}, status, namespaces: [..], destinations: [..]}`. `node` describes the implementation: at least `implementation`, `version`, and `kinds` (kind → the node's name for it). |
| `status` | | `{frozen, seq, held: [seq..], hold: when \| null, conditions: n, frozen_ms, resigning}`. `resigning` is `{enabled, available, error?}`; implementations without the controlled-devnet feature may omit it. |

### Flow

| Method | Params | Result |
|---|---|---|
| `freeze` | | status |
| `thaw` | `keep?` (a predicate) | status. Items matching `keep` stay held, as do those matching the hold filter; a debugger that acts on certain items passes them here so none slips through at the moment of release. |
| `step` | `seq?` | `{seq}`: forwards the given held item, or the oldest, and stays frozen |
| `run_until` | `when` | `{condition}`: adds a one-shot freeze condition, then thaws |
| `set_hold` | `when` (or null to clear) | status |

### Items

| Method | Params | Result |
|---|---|---|
| `item.get` | `seq` | the item; fails if it has aged out of the agent's memory |
| `item.forward` | `seq`, `then?: "run"` | status. With `then: "run"` the node is thawed after. |
| `item.drop` | `seq` | status. Only for `droppable` items. |
| `item.defer` | `seq`, and `ms` and/or `until: {when, count?}`, `then?: "run"` | status. Sets a held item aside: it keeps waiting but no longer keeps the node frozen. It goes on (forwarded unchanged) after `ms` milliseconds of real time, or once `count` (default 1) items matching `when` have been seen, whichever is first. The part of the node waiting on it handles nothing else meanwhile, so items queued behind it in the same part cannot overtake it. `status.deferred` lists such items; the `released` event carries `deferred: true` and, if applicable, `overtaken_by`. |
| `item.resume` | `seq` | `{resumed: bool}`: lets a deferred item go on now |
| `item.replace` | `seq`, and `payload` or `raw` (hex) | status. Only for `editable` items. If the replacement cannot be turned into what the node needs, the original proceeds and a `log` event of level `error` says so. |
| `item.inject` | `to`, `payload` | `{}`: delivers a new input to destination `to`. A node listing `"inject:typed"` in its features accepts this before the destination has handled anything. |
| `destinations` | | list of destinations `item.inject` accepts |

### Conditions

| Method | Params | Result |
|---|---|---|
| `cond.add` | `when`, `action?: "freeze" \| "log"` (default freeze), `once?: bool` | `{id}` |
| `cond.remove` | `id` | `{removed: bool}` |
| `cond.list` | | `[{id, predicate, action, once, hits}]` |

### State

State is a tree of namespaces. `path` is `<namespace>` or `<namespace>/<key>`.

| Method | Params | Result |
|---|---|---|
| `state.namespaces` | | list of namespaces |
| `state.list` | `path`, `after?`, `limit?` (default 100) | `[{key, value}]` in key order, starting after `after` |
| `state.get` | `path` | the value, or null |
| `state.put` | `path`, `value` | `{}` when written at once. A namespace that writes later answers `{queued: true, applied, scope}` and reports the write as a `store_write` item when it is done, or a `log` event if it fails. |
| `state.delete` | `path` | as `state.put` |

A namespace may be read-only, and a node may refuse writes unless it is frozen. A node whose
stores can be written lists `"state:<namespace>:write"` in its features. A `store_write`
item has the namespace as `label` and `{key, op: "put" | "delete"}` as payload.

### Controlled-devnet edited-header re-signing

These methods exist only to control an implementation's fixed, locally mounted devnet
identity. They do not accept key paths or key bytes.

| Method | Params | Result |
|---|---|---|
| `resign.get` | | `{enabled, available, error?}` |
| `resign.set` | `enabled: bool` | `{enabled, available, error?}`. Enabling fails when the implementation did not establish the controlled-devnet key boundary. |

When enabled, a supported edited header is fail-closed: the implementation either emits
a fully consistent re-signed protocol flow or drops the edit and reports a non-secret
error. The agent emits an item with `kind=resign_error`, linked to the edited
item with `parent`, when that atomic operation fails. When disabled, replacement retains
the historical byte-exact stale-signature behaviour.

`state.list` pages with `after`: pass the last key of one page to get the next. Keys of a
namespace need not sort as text; the order is the namespace's own (for `chain/*`, chain order).

A namespace may refuse writes or deletes. Recommended namespaces, so that one debugger
fits all nodes: `stages` (internal component states), `chain/headers`, `chain/blocks`,
`chain/nonces`, `chain/best`, `ledger/utxo`, `ledger/accounts`, `ledger/pools`,
`ledger/dreps`, `ledger/proposals`, `ledger/votes`, `ledger/pots`, `mempool/txs`, `peers`.

The Amaru agent has `stages` (read and write) and, read-only: `chain/best`, `chain/headers`,
`chain/blocks`, `chain/nonces`, `ledger/tip`, `ledger/pots`, `ledger/params`, `ledger/utxo`,
`ledger/stake`. `chain/*` keys are `<slot, 20 digits>-<header hash>`; a `state.get` also
accepts the bare hash. It lists `"state:chain"` and `"state:ledger"` in `hello.node.features`.

### Probes

A probe is a named place inside one of the node's functions.

| Method | Params | Result |
|---|---|---|
| `probe.list` | | `[{name, where, enabled, hits}]` in name order. `where` says where in the node's source the probe is. `hits` counts every time the place was reached, whether or not the probe is enabled. |
| `probe.enable` | `name` | `{changed: n}`. `name` is one probe, a prefix ending in `*`, or `all`. An enabled probe reports an item of kind `probe` each time it is reached: `label` is the probe's name, `from` is `probe:<name>`, `type` is its `where`, `payload` is a small object of fields. Probe items are never held; a condition can match one and freeze the node at its next step. |
| `probe.disable` | `name` | `{changed: n}` |

A node with probes lists `"probes"` in `hello.node.features`. `DWARF_FREEZE_PROBES` (`all`, or
names and prefixes separated by commas) enables probes from the node's first step.

### What a connection is sent

| Method | Params | Result |
|---|---|---|
| `events.subscribe` | `kinds?`, `when?`, `sample?` | `{}`. Chooses which items this connection is sent: of these kinds, matching the predicate `when`, one in `sample` (the 1st, the n+1th, …). With no fields, everything again. A bad value fails and leaves the earlier choice in place. |

Whatever the choice, a connection is always sent: held items (they do not count towards the
sample), `released`, `deferred`, `frozen`, `thawed`, `condition`, `log` and `error`. `item.done`
is sent for an item that was sent to this connection (the last 8,192 of them are remembered).
An item that was not sent can still be read with `item.get`. While something is being left
out, the connection is sent `stats {seq, items, by_kind, dropped}` about once a second: what
the node produced in that time, how much of it this connection was not sent, and the highest
item number so far. A node with this lists `"events:filter"`.

### Capabilities

A build may have fewer capabilities than the agent can offer (a hook that could not be
brought to the node's revision). `DWARF_FREEZE_CAPABILITIES` names what the build has; the
node then lists a feature only if it, or a name it refines (`state` for `state:chain:write`),
is named, and gives the list as `hello.node.capabilities`. Probes are grouped by where they
sit (`probes:consensus`, `probes:header`, `probes:ledger`, and those in the node and network
code, present with any of them); a probe of a group the build lacks is not listed and cannot
be enabled.

### Function tracing

| Method | Params | Result |
|---|---|---|
| `trace.list` | `prefix?`, `offset?`, `limit?` (default 2000, at most 5000) | `{enabled: [prefix..], sites: [{name, hits, enabled}], total}`. A function is a site once it has been called; `hits` counts calls made while it was enabled. |
| `trace.enable` | `prefix` | `{changed: n}`: how many known functions changed state |
| `trace.disable` | `prefix?` (default `all`) | `{changed: n}`; removes every enabled prefix the given one selects |

A prefix selects whole `::` segments of a function's full path; `text*` is a plain prefix;
`all`, `*` or nothing is everything; `-` is read as `_`. An enabled function reports an item
of kind `call` each time it is called: `label` is its name, `type` its full path, `from` is
`call:<path>`, and `payload` is `{args: {name: text}, depth, caller, async}`. When the call
ends, `item.done` carries `{ms, unwound}`. Call items are not held unless asked for (below). A node with tracing
lists `"trace"` in its features; `DWARF_FREEZE_TRACE` (prefixes separated by commas) enables
tracing from the node's first step.

| Method | Params | Result |
|---|---|---|
| `trace.hold` | `prefix` | `{changed: n}`. The node is held each time one of these functions is entered: the `call` item arrives `held`, not editable and not droppable, and the function goes on when it is forwarded. A held function is also enabled. |
| `trace.unhold` | `prefix?` (default `all`) | `{changed: n}` |

`trace.list` gives `held` for each site and `holding`, the prefixes. The node can wait in a
plain function on a thread of its own (the ledger's) and in an `async` function inside a
stage. Anywhere else waiting would stop the runtime: the call is reported as usual with
`not_held: "would block the runtime"` in its payload. A node with this lists `"trace:hold"`.
An `async` function is part of the call depth of what it calls, across awaits, except one
that returns `impl Trait` and a plain function that returns a future.

### Effects

An `internal_action` item says with `editable` and `droppable` what can be done to it. A node
listing `"effects:edit"` supports, besides sends: `wait` (replace `{ms}`; drop returns at
once), `call` (replace `{timeout_ms}`), `set timeout` (replace `{delay_ms}`; drop leaves it
unarmed), `schedule` (drop: never delivered; replace `{at_ms?, message?}`: `at_ms` is milliseconds from now on the stage clock, and the stage keeps the id it holds, so a later cancel still cancels it) and external effects whose payload reads back
unchanged (replace the payload). A dropped item's `item.done` carries `{dropped: true}`.

### Reserved for later revisions of this draft

`clock.get`, `clock.skew`, `timer.list`, `timer.fire`, `snapshot.save`. A node that does not
implement a method answers with error `failed`.

## 7. Predicates

A predicate is a JSON object with an `op`.

| `op` | Other fields | True when |
|---|---|---|
| `true` | | always |
| `and`, `or` | `args: [predicate..]` | all / any are true |
| `not` | `arg: predicate` | `arg` is false |
| `kind` | `in: [kind..]` | the item's kind is listed |
| `cold_spot` | `eq: tag` | the item carries the tag |
| `seq` | `ge: n` | `seq >= n` |
| `to`, `from`, `type`, `label` | one of `eq`, `prefix`, `suffix`, `contains` | the text attribute matches |
| `field` | `path`, and one of `eq`, `ne`, `lt`, `gt`, `contains`, `len_gt`, `exists: true` | the value at `path` passes the test |

`path` is dot-separated keys with `[n]` indexes, resolved inside `payload`; a leading `/`
resolves from the item itself (`/label`). `lt`/`gt` compare numbers, including
`{"$int": ...}`. `contains` tests text, the hex of a byte string, the keys of an object,
or the text elements of an array. `len_gt` is the length of text, an array, a byte string
(in bytes) or a map. A missing value fails every test except `ne`.

## 8. Events

| Event | Data | When |
|---|---|---|
| `item` | the item | every item, held or not |
| `item.done` | `{seq, outcome}` | a step finished; `outcome` is node-specific |
| `deferred` | `{seq}` | a held item was set aside with `item.defer` |
| `released` | `{seq, verdict: "forward" \| "drop" \| "replace", payload?, raw?}` | a held item was decided |
| `frozen` | `{seq?, reasons: [..]}` | the node became frozen; reasons are `"requested"`, `"frozen"`, `"hold"`, or `{condition: id}` |
| `thawed` | `{}` | the node is running again |
| `condition` | `{id, seq}` | a `log` condition matched |
| `log` | `{level, message}` | the agent has something to say |
| `error` | `{code, ...}` | `events_lost` with `missed: n` when a slow connection missed events |

`resign_error` is an item kind carried by the existing `item` event, so existing clients retain
forward compatibility.

## 9. Broker extensions

The Freeze broker speaks this protocol to debuggers and forwards requests to the agent. A
client cannot tell it from an agent except that `hello` also carries
`broker: {client, owner, you_own, session, peers, node}`. `hello` may carry `client`, a name
for this client. While the agent is not reachable (or the broker is preparing the node),
`hello` fails with `not_ready`. The broker serves these itself:

| Method | Params | Result |
|---|---|---|
| `session.who` | | `{owner, clients: [{client, name, owner, seconds}]}` |
| `session.claim` | `force?` | as `session.who`. From then on, requests from other clients that would change the node fail with `not_owner`; reads still work. Fails with `not_owner` if another client holds it and `force` is not set. |
| `session.release` | | as `session.who` |
| `session.events` | `limit?` (default 5000) | `{events: [{event, data}]}`: the most recent events, oldest first, for a client that connected late |
| `peers.status` | | `{available, auto, paused, seconds, error}` |
| `peers.auto` | `on`, `freeze_nodes?` | as `peers.status`. With `freeze_nodes`, the devnet's other Freeze nodes are frozen and thawed with this one (one that was frozen before is left frozen). With `auto`, the other nodes are paused once the node has been frozen for 250 ms and resumed once it has run for 250 ms. |
| `peers.pause`, `peers.resume` | | as `peers.status` |

A claim ends when its client disconnects. Events of the broker's own: `session` (as
`session.who`) and `peers` (as `peers.status`). The broker records every event to
`events.jsonl` in `broker.session`, each line `{at, event, data}`.
