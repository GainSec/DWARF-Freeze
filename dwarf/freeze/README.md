# DWARF Freeze

An interactive debugger for Cardano nodes, used like an intercepting proxy. Start a node
built with the Freeze agent, and everything it receives, sends and does becomes an item
you can hold, read, edit, forward or drop.

Freeze is its own subsystem. It shares no code path with measurement taps, measurement
patches or release builds, and a Freeze run is never measurement evidence.

- Design: [`../docs/freeze-debugger-design.md`](../docs/freeze-debugger-design.md)
- Protocol (the contract for other node implementations): [`spec/freeze-control-protocol-v1.md`](spec/freeze-control-protocol-v1.md)
- UI mockups: [`docs/tui-mockups.html`](docs/tui-mockups.html)

## Deploy, check, remove

The step-by-step version, with what each step needs, is in the `README.md` of the
`dwarf-freeze` repository. In short, from the DWARF checkout that Freeze was copied into:

```bash
python3 -m venv .venv-freeze                                    # Python 3.11 or newer
.venv-freeze/bin/pip install -r dwarf/freeze/requirements.txt
alias freeze-debugger="$PWD/.venv-freeze/bin/python $PWD/dwarf/freeze-debugger"

freeze-debugger build --static --source dwarf/testplease/amaru/v10.11.20260925   # once
freeze-debugger devnet up              # each time; the node answers about 15 minutes later
freeze-debugger devnet status          # containers, and whether the Freeze node answers
freeze-debugger devnet nodes           # frozen or running, step, held items
freeze-debugger devnet attach          # the UI
freeze-debugger devnet down            # stop it and delete its chain data
```

To remove everything: `devnet down`, `docker rmi $(docker images -q 'dwarf/freeze-amaru')`,
`docker run --rm -v ~/.local/share/dwarf:/d python:3.12-slim rm -rf /d/freeze` (saved chains
are owned by root), `rm -rf .venv-freeze`, then delete the Freeze files or the checkout. Section 10 of the guide lists each thing Freeze creates.

A devnet does not last: its Amaru relays stop following the chain about 20 minutes after
they start, and a freeze of several minutes can stop the chain. Start a fresh one for each
piece of work.

## Try it without a node

```bash
python3 -m venv .venv-freeze && . .venv-freeze/bin/activate
pip install -r dwarf/freeze/requirements.txt
python3 dwarf/freeze-debugger demo
```

`demo` replays a recording of a real Amaru node syncing preprod. It starts frozen: press
`space` to step, `g` to let it run, `i` to turn Intercept on.

## Debug a real node

Needs Docker. The first build compiles Amaru and takes a while.

```bash
python3 dwarf/freeze-debugger build --source dwarf/testplease/amaru/v10.11.20260925
python3 dwarf/freeze-debugger run --network preprod      # starts frozen and opens the UI
python3 dwarf/freeze-debugger stop --network preprod
```

`run` keeps the node's data in a Docker volume, publishes the agent on `127.0.0.1` only,
and protects it with a token stored under the DWARF data root (`.../dwarf/freeze/run/`).
`attach --network preprod` reopens the UI; `watch --network preprod` prints one line per step.

On a public network, use Freeze to observe and to tamper with what your own node receives.
Do not use it to send altered traffic to other people's nodes.

## Intercept with a Python script

> **WARNING: script code is not sandboxed.** It runs in `freeze-debugger` with your full
> Python permissions. Read and trust a script before running it.

An explicitly named local `.py` file can decide each held item without opening the UI:

```bash
python3 dwarf/freeze-debugger script dwarf/freeze/examples/drop_large_fee.py \
    --network devnet --only 'kind=input'
```

The file must define `on_item(item)`. The callback receives the protocol item's dictionary
and returns one of these decisions:

| Return value | Decision |
|---|---|
| `None` or `"forward"` | forward the item unchanged |
| `"drop"` | drop it; rejected unless `item["droppable"]` is true |
| `{"action": "replace", "payload": value}` | replace its payload; rejected unless editable |
| `{"action": "replace_raw", "raw": "00ff"}` | replace its raw bytes from validated hex; rejected unless editable |
| `"hold"` | exit successfully and leave this item held |

All other return values are errors. Decisions are fully validated before any request that
could change the item. An exception, invalid decision, or Ctrl-C leaves the current item
held; Ctrl-C exits with status 130. `--only CONDITION` calls the callback only for matching
items and forwards other held items unchanged. `--no-record` disables the normal on-disk
session; otherwise every protocol event is recorded and the session directory is printed.

The runner saves the node's existing hold predicate and frozen/running state, installs a
temporary hold-all predicate, and processes items that were already held before waiting for
new ones. On exit it restores the old predicate when possible. It restores an originally
running node only when doing so cannot release an item that must remain held; otherwise it
deliberately leaves the node frozen and reports that safety choice.

The agent defines whether an item can be held. Non-holdable observations such as
`internal_state` are still recorded in the session, but no script can intercept or change
them.

## A closed local devnet (tamper freely)

On a public network you can only safely tamper with what your own node receives. For
dropping, editing and injecting without that limit, run everything locally:

```bash
python3 dwarf/freeze-debugger build --static --source dwarf/testplease/amaru/v10.11.20260925
python3 dwarf/freeze-debugger devnet up        # 3 block producers, 2 Amaru relays, relay 1 is Freeze
python3 dwarf/freeze-debugger devnet profiles  # other devnets and which node Freeze can be: devnet up --profile NAME
python3 dwarf/freeze-debugger devnet attach    # waits until the node is ready, then opens the UI
python3 dwarf/freeze-debugger devnet status
python3 dwarf/freeze-debugger devnet down
```

The devnet is DWARF's own baseline (`antithesis/cardano_amaru_relay_bootstrap_control`),
unchanged; Freeze adds a compose override that hands its binary to the first Amaru relay
and publishes that relay's agent on `127.0.0.1`. The Amaru relays cannot start until the
producers have made three epochs of chain, so the first start takes 15 minutes or more.

`--static` builds a fully static x86-64 binary, which the devnet's containers need. It
cross-compiles, so it is not slowed by emulation on an arm64 machine; the devnet's other
containers are amd64 and do run emulated there. Linux amd64 is the first-class host.

In the shipped controlled-devnet profiles, edits to inbound or outbound Conway roll-forward headers
are re-signed by default with the exact matching identity from the three generated producer
configurations mounted read-only at fixed internal paths.
Freeze also keeps the matching ChainSync tip and BlockFetch block header consistent.
Use `devnet up --no-resign` to disable controlled header/body repair, or the
`s` key / **Header/body repair** button to toggle it at runtime. The inspector reports damaged,
repaired and still-stale dependencies for each edit. Uppercase `N` authorizes deliberately stale
validation material for that edited item only. This is unavailable to the
generic `run` command and never accepts arbitrary key paths.

`devnet pause-peers` / `resume-peers` pause and resume every other node, for long freezes.

To use a devnet running on another machine, forward its agent port and attach locally:

```bash
ssh -N -L 7373:127.0.0.1:7373 <host> &
python3 dwarf/freeze-debugger attach --port 7373 --token-file <copy of the host's token file>
```

The token file is `<DWARF data root>/freeze/devnet/token` on the host that runs the devnet.

## Payload lists, templates, runs and DWARF scenarios

```bash
python3 dwarf/freeze-debugger payloads list                      # shipped lists and yours
python3 dwarf/freeze-debugger payloads import my-values.txt
python3 dwarf/freeze-debugger template my-test.yaml --network devnet   # run a template of rules, no UI
python3 dwarf/freeze-debugger scenarios list                     # DWARF scenarios Freeze can carry out
python3 dwarf/freeze-debugger scenarios run <id> --network devnet
python3 dwarf/freeze-debugger export --until 5000 -o run.tar.gz  # a recorded session, up to an item
python3 dwarf/freeze-debugger import run.tar.gz                  # shows what it did, in sentences
python3 dwarf/freeze-debugger ingest run --network devnet        # restart the node and do it again
```

In the UI: `6` Payloads (edit lists; rules and results), `7` Scenarios, `p` in Intercept runs a
list against the selected field, `X` in History exports the run up to the selected row.
See `docs/GUIDE.md` for the template format and the value syntax.

## Keys

| Key | Does |
|---|---|
| `i` | Intercept on/off: hold everything ticked in the "holding" row |
| `f` | Forward the selected item (with your edits) and let the node run to the next held item |
| `space` | Step: forward the selected item and stay frozen |
| `d` | Drop the selected item |
| `e` | Edit the selected field |
| `r` | Undo edits to the selected item |
| `u` | Run until a condition matches |
| `z` / `g` | Freeze now / let the node run |
| `1`–`7` | Intercept, History, State, Freeze points, Overview, Payloads, Scenarios |
| `p` | Run a payload list against the selected field |
| `s` | Toggle controlled header/body repair when attached to a controlled devnet |
| `N` | Authorize stale/invalid validation material for the selected edited item only |

Hover over, or move the cursor onto, any name to see what it is and why it matters.

## Conditions

Used for "Run until…", Freeze points and the History filter.

```
kind=input and to~validate_block     label=RollBackward        slot>=138760000
type=ouroboros/3                     txs#>10                   not label=clock
```

Text tests: `=` exact, `~` contains, `^` starts with, `$` ends with. `label=RollForward` also finds
network messages by the name the UI shows. Payload fields:
`= != < > >= <=`, `~` contains, `#>` longer than, `?` exists. Join with `and` / `or`.

## What is hooked in Amaru

| Layer | Items | Edit | Drop |
|---|---|---|---|
| Stage inputs | every message a stage is about to handle | yes | yes |
| Effects | every action a stage takes (sends, store and ledger calls, clock, timers) | sends only | sends only |
| Effect results | what each external effect returned | yes | no |
| Stage state | each stage's state after every input; readable and replaceable under `stages/` | yes | – |
| Protocol messages | every mini-protocol message in and out, decoded, byte-exact edits | yes | yes |
| Raw bytes | every chunk read from or written to a peer | yes | yes |
| Time | the node's clock and timers stop while frozen | – | – |

Also built (guide, sections 17 and 18): several Freeze nodes in one devnet with a Nodes tab,
a scripted peer, the chain and ledger stores under State (writable while frozen), 71 probes
including the ledger rules and script evaluation, call tracing for about 2,000 functions,
saved filters, a broker per node (records the session, pauses the other nodes while the node
is frozen, lets one client claim it), editing and skipping effects, injection from the UI,
rewind to an earlier item, source-line stepping (`build --debug-info`, `source`), and `port`
for other Amaru revisions.

Section 19 of the guide adds: a client chooses what it is sent (the UI samples a busy node by
itself), holding the node on entry to a function, Freeze nodes frozen together, snapshots and
rewind for any node, editable schedules, the ledger's in-memory blocks and stake accounts,
transactions on the devnet, and Amaru `v10.11.20261105` running in the devnet.

Not built: re-sealing beyond controlled-devnet typed ChainSync and request-bound BlockFetch;
transaction-witness regeneration; slot/issuer/opcert/VRF forgery; dropping the ledger's in-memory
blocks; Windows without WSL.

## Layout

```
freeze-debugger            entry point (dwarf/freeze-debugger)
freeze/client.py           protocol client
freeze/scripting.py        validated Python callback runner
freeze/examples/           example interception scripts
freeze/tui.py              terminal UI
freeze/session.py          what happened in a session
freeze/conditions.py       the condition language
freeze/cbor_tree.py        byte-exact CBOR editor
freeze/cardano.py          names for Cardano network messages and their fields
freeze/glossary*.yaml      plain names and explanations
freeze/payloads.py         payload lists and value transforms
freeze/rules.py            rules, templates and the engine that applies them
freeze/runs.py             export, import and replay of whole runs
freeze/scenarios.py        DWARF's scenarios as Freeze templates
freeze/build.py            image builder        freeze/launch.py   single-node launcher
freeze/devnet.py           closed local devnet with one Freeze node
freeze/devnet_tx.py        transactions on the devnet, with its own keys and cardano-cli
freeze/broker.py           one per node: records, pauses peers, lets a client claim the node
freeze/applier.py          `port`: brings the hooks to another revision of the node
freeze/instrument.py       adds the tracing attribute to every function at build time
freeze/source.py           source-line stepping; freeze/lldb_driver.py runs beside the node
freeze/targets/amaru/      the agent crate and per-revision hook patches
freeze/testing/            a stand-in agent for tests and the demo
```

## Tests

```bash
pytest -q tests/test_freeze_*.py
```

The agent and the runtime hooks have their own Rust tests, run inside the instrumented
source tree: `cargo test -p amaru-freeze` and `cargo test -p amaru-pure-stage --test freeze`.
