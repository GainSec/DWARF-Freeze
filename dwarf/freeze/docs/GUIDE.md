# DWARF Freeze — Guide

An interactive debugger for Cardano nodes, used like an intercepting proxy. You start an Amaru node that was built with the Freeze agent inside it. Everything the node receives, sends and does becomes an **item** you can hold, read, edit, forward or drop, and you can stop the node at any step.

Freeze is its own subsystem inside the DWARF repo. It does not use or change measurement taps, measurement patches or release builds, and a Freeze run is never measurement evidence.

---

## 1. Capabilities

| Capability | Support |
|---|---|
| Agent inside Amaru | hold, edit, drop, step, run-until, inject, stage state, and token-protected control socket |
| Runtime and network hooks | stage inputs, effects, effect results, protocol messages, raw network chunks, and freeze-aware node time |
| Python debugger | session recording, conditions, glossary, byte-exact CBOR editing, and Cardano field names |
| Payload testing | reusable value lists, rules, captures, delays, templates, and result recording |
| DWARF scenarios | supported scenarios convert to Freeze templates; unsupported primitives report a reason |
| Run lifecycle | export, import, offline browsing, replay with `ingest`, and optional devnet snapshots |
| Terminal UI | Intercept, History, State, Freeze points, Overview, Payloads, Scenarios, Nodes, and Source |
| Node launch | native single-node images and static Linux x86-64 devnet images |
| Several nodes, scripted peer | more than one Freeze node per devnet, each with its own agent; a Freeze node upstream of the node under test sends scripted messages on the wire (section 17) |
| Stores | chain and ledger stores readable in the State tab and from scripts (section 17); headers, blocks, the best-chain pointers, unspent outputs, pots and protocol parameters can be written while frozen (section 18) |
| Probes | 69 named places inside the node's functions, 32 of them in the ledger rules and script evaluation: hit counts always, items when turned on (sections 17 and 18) |
| Every function | about 2,100 functions of seven crates can report each call with its arguments, switched on by path prefix (section 18), and the node can be held on entry to one (section 19) |
| Busy nodes | a client chooses what it is sent; the UI samples by itself when the node produces more than it can read (section 19) |
| Devnet transactions | payments and script spends from the devnet's own funded key (section 19) |
| Broker | one per Freeze node: records the session with or without a UI, pauses the devnet's other nodes while the node is frozen, lets one client claim the node (section 18) |
| Effects | waits, call timeouts, timeouts, schedules and external effects can be edited or skipped, not only sends (section 18) |
| Rewind | take the devnet's node back to an earlier item of a recorded session (section 18) |
| Source lines | step through the node's source with a debugger beside it, from an image built with `--debug-info` (section 18) |
| Other Amaru revisions | recipes for `v10.11.20260925` and `v10.11.20261105`, both running in the devnet; `port` brings the hooks to another revision (sections 18 and 19) |
| Saved filters | conditions kept under a name, used as `@name` (section 17) |
| Devnet header/body repair | controlled devnet only; default on for shipped profiles, with per-edit dependency reports and explicit stale negative controls |

The re-signing architecture and verification contract are documented in
`RESIGNING-DISCOVERY.md`.

**Not built:** general re-sealing outside the controlled-devnet typed ChainSync and
request-bound BlockFetch flows; transaction-witness regeneration; slot/issuer/opcert/VRF
forgery; dropping the ledger's in-memory blocks; Windows without WSL
(the client side is expected to work there and was not run).

---

## 2. Requirements

- Python 3.11 or newer.
- Docker with Compose v2 (Docker Engine on Linux, Docker Desktop on macOS).
- `git` on the PATH (the builder uses `git apply`).
- Disk: about 10 GB for the first build's caches; a preprod node needs about 4 GB more.
- A checkout of the DWARF repo that contains `dwarf/freeze/`.

Platform priority: Linux, then macOS, then WSL, then Windows. The local devnet needs a Linux x86-64 host.

---

## 3. Install

Run from the directory that contains the `dwarf` checkout. Freeze has its own small dependency list and does not need DWARF's framework requirements.

```bash
python3 -m venv .venv-freeze
.venv-freeze/bin/pip install -r dwarf/dwarf/freeze/requirements.txt
.venv-freeze/bin/python dwarf/dwarf/freeze-debugger --help
```

`requirements.txt` is three packages: `textual` (the UI), `cbor2`, `PyYAML`.

In the rest of this guide, `freeze-debugger` means `.venv-freeze/bin/python dwarf/dwarf/freeze-debugger`.

---

## 4. Three ways to use it

### 4.1 No node at all: the demo

```bash
freeze-debugger demo
```

Replays a recording of a real Amaru node syncing preprod through the real UI. It starts frozen: `space` steps, `g` lets it run, `i` turns Intercept on.

### 4.2 One node on a public test network (observe; tamper only with what your own node receives)

```bash
freeze-debugger build --source dwarf/dwarf/testplease/amaru/v10.11.20260925
freeze-debugger run --network preprod       # downloads snapshots if needed, starts frozen, opens the UI
freeze-debugger stop --network preprod
```

Do not use Freeze to send altered traffic to other people's nodes.

### 4.3 A closed local devnet (tamper freely) — Linux x86-64

Three block-producing cardano-nodes feed two Amaru relays on a private network (`testnet_42`). The first Amaru relay runs the Freeze build. Nothing leaves the machine.

```bash
freeze-debugger build --static --source dwarf/dwarf/testplease/amaru/v10.11.20260925
freeze-debugger devnet up
freeze-debugger devnet attach      # waits until the Freeze node is ready, then opens the UI
freeze-debugger devnet status
freeze-debugger devnet down
```

The Amaru relays cannot start until the producers have forged three epochs, so the first start takes about 15 minutes.

The shipped profiles enable edited-header re-signing by default. `devnet up --no-resign`
keeps the historical stale KES signature for negative-control testing; `--resign` is the
explicit on form. Only exact identities under the fixed, read-only generated
`/freeze-producers/{p1,p2,p3}` mount are considered,
and identity or key-material mismatches drop the edited message instead of emitting a
partially updated header. In the UI press `s` or use **Header/body repair** to switch the
runtime state. Generic public-testnet `run` nodes report the feature unavailable. After an edit,
the inspector lists damaged dependencies, repairs the typed gate will apply, remaining stale
material, or the exact refusal. Press uppercase `N` on that edited item only to authorize a
deliberately invalid negative control; the authorization is consumed when the item is released.

An inbound BlockFetch block can also be edited coherently with a re-signed header. Make the
body edit once and copy the **Edited block commitment** hash and size from the inspector. Rewind
or replay from before the matching ChainSync roll-forward, put both values in that header, then
repeat the identical body edit. Freeze verifies the edited body against the pre-announced signed
header and substitutes that header as one atomic operation. This reseals the block envelope only:
transaction witnesses are not regenerated, and slot, issuer, VRF, and operational-certificate
edits remain unsupported. Every mapped response must match both the signed body hash and signed
body size, even when its body was not edited. If replay created several mappings for the same
original block, Freeze also binds the response to the edited point requested by consensus and the
producer range actually requested on the wire. The dispatched request snapshots every announced
edited point on that branch, including intermediate blocks. A mapping is consumed atomically; a
repeated response cannot reuse it within the same peer request or fall back to the producer header.
Each concurrently contacted peer has an independent request identity, so a valid straggler may use
the same retained signed-header mapping; completing one peer's batch does not revoke another's.
The mapping also remains available for a later refetch. An all-original range has no expected edited
points, so unrelated edited mappings are left untouched. If an edited range expects an intermediate
mapping that is missing, belongs to a finished request, or was already used by that request, Freeze
rejects the response. A path without request identity rejects multiple headers with the same body
commitment as ambiguous; it never chooses by header-hash order. A new edited request whose branch
snapshot cannot be reconstructed because a mapping is missing is rejected as a whole. No exact,
unambiguous match is forwarded.

The same repair chain applies when a controlled scripted-peer Freeze node sends ChainSync and
serves BlockFetch. Outbound ChainSync ancestry is qualified by direction and mux connection.
Responder BlockFetch leases are created only by that peer's exact RequestRange and consumed by the
matching outbound block; two peers, duplicate commitments, finished requests and reconnects never
share a fallback map. BatchDone, NoBlocks, protocol errors and disconnects retire only the exact
request. Protected slot, issuer, operational-certificate and VRF inputs still refuse before key
loading.

**Using a devnet on another machine:**

```bash
ssh devnet-host 'cat <work-dir>/data/freeze/devnet/token' > /tmp/freeze.token
ssh -N -L 7373:127.0.0.1:7373 devnet-host &
freeze-debugger attach --port 7373 --token-file /tmp/freeze.token
```

---

## 5. Command reference

### `build` — make a node image with the Freeze agent compiled in

| Option | Meaning |
|---|---|
| `--source DIR` | required. An Amaru source tree. It is copied; the original is never modified. |
| `--recipe NAME` | which hook recipe to use. Default: the first recipe whose patches apply to the source. Recipes live in `dwarf/freeze/targets/amaru/patches/<name>/`. Shipped recipes cover `v10.11.20260925` and `v10.11.20261105`. |
| `--tag TAG` | image tag. Default: `dwarf/freeze-amaru:<source-name>-<recipe-digest>` (plus `-static`). |
| `--platform P` | container platform, e.g. `linux/amd64`. Default: this machine's. Ignored with `--static`. |
| `--static` | build a fully static x86-64 binary in an amd64 image. Required for the devnet. Cross-compiles, so it is not slowed by emulation. |
| `--debug-info` | keep what source-line stepping needs in the node and also build `<image>-tools` (lldb and the source) for `freeze-debugger source`. The tag gets `-dbg`. A full rebuild the first time. |

If no recipe applies, the build stops and says the hooks need porting to that revision: `freeze-debugger port` (section 18).

### `port`, `trace`, `source`, `rewind`

| Command | Options | Meaning |
|---|---|---|
| `port` | `--source TREE` (required), `--base TREE`, `--recipe NAME`, `--name NAME`, `--shipped`, `--keep DIR`, `--by-hand DIR`, `--resolved DIR` | bring the hooks to another revision of the node and store the result as a recipe |
| `trace` | connection options; `--enable PREFIX`, `--disable PREFIX`, `--hold PREFIX`, `--unhold PREFIX`, `--prefix P`, `--hit`, `--top N` | which functions were called, and which report each call |
| `source` | `--network NAME` (default: the devnet's node), `--node SERVICE`, `--do COMMAND` (repeatable), `--wait S` | step through the node's source lines |
| `rewind` | `--to ITEM` (required), `--node SERVICE`, `--session NAME`, `--snapshot NAME` (default: the node's start snapshot), `--by-count`, `--wait S`, `--no-attach` | take the devnet's Freeze node back to an earlier item of a recording |

All four are described in section 18.

### `run` — start one Freeze node in a container and open the UI

| Option | Meaning |
|---|---|
| `--network N` | required. `mainnet`, `preprod`, `preview` or `testnet_<magic>`. |
| `--image I` | default: the most recently built non-static Freeze image. |
| `--port P` | local port for the agent. Default 7373. Published on `127.0.0.1` only. |
| `--peer HOST:PORT` | upstream peer; repeatable. Default: the network's own bootstrap peers. |
| `--thawed` | let the node run from the start. Default: start frozen at the first step. |
| `--no-attach` | start the node but do not open the UI. |
| `--node-arg ARG` | extra argument passed to `amaru run`; repeatable. |

The node's data lives in Docker volume `dwarf-freeze-<network>-data`. A random token is written to `<data root>/freeze/run/dwarf-freeze-<network>/token`.

### `stop` — stop a node started by `run`

| Option | Meaning |
|---|---|
| `--network N` | required. |
| `--remove-data` | also delete the node's chain and ledger volume. |

### `attach` — open the UI on a running Freeze node

| Option | Meaning |
|---|---|
| `--network N` | find the port and token of the node started by `run` for this network; use `devnet` for the local devnet. |
| `--host H` | default `127.0.0.1`. |
| `--port P` | default 7373. |
| `--token T` | the shared secret. Default: `$DWARF_FREEZE_TOKEN`. |
| `--token-file F` | read the secret from a file. |
| `--timeout S` | seconds to wait for each answer. Default 15. |
| `--no-record` | do not write the session to disk. |

### `watch` — print one line per step, no UI

Same connection options as `attach`, plus:

| Option | Meaning |
|---|---|
| `--only COND` | a condition; print only matching steps. |

### `demo` — the UI on a recording

| Option | Meaning |
|---|---|
| `--capture FILE` | a recorded event stream (`.jsonl` or `.jsonl.gz`). Default: the bundled preprod recording. |
| `--delay S` | seconds between replayed steps. Default 0.002. |

### `devnet` — the closed local devnet

| Subcommand | Options | Meaning |
|---|---|---|
| `devnet profiles` | | list the devnets you can start (shipped profiles and yours) and, for each DWARF layout a Freeze node fits into, which services it can replace |
| `devnet up` | `--profile NAME\|FILE` (default `relay`), `--compose FILE` and `--target SERVICE` (use a compose file directly, or pick a different node of a profile), `--image I` (default: the most recent static image), `--port P` (default 7373), `--also SERVICE` (another service that also runs the Freeze build, on the next agent port; repeatable), `--frozen` (hold every Freeze node at its first step; the main node's stored chain is then saved as snapshot `start`, which `rewind` goes back to), `--no-broker` (no broker in front of the nodes), `--freeze-together` (the other Freeze nodes freeze and run with the main one), `--record all|held|none` (what the brokers keep on disk), `--no-auto-pause` (do not pause the other nodes while the main Freeze node is frozen), `--resign` / `--no-resign` (controlled-devnet edited-header signing; shipped-profile default is on), `--peer-cooldown S` (default: the profile's, normally 5; seconds the Freeze node keeps a misbehaving peer out — Amaru's own default is 600, which would leave the node without traffic for ten minutes after each successful tamper) | start the devnet. Returns once the Freeze relay's container is running; bootstrap continues in the background. If the image changed since the last `up`, the Freeze node is restarted with it. |
| `devnet status` | — | list containers and say whether each Freeze agent answers. |
| `devnet nodes` | `--freeze`, `--thaw` | each Freeze node with its port, state, step and held items; freeze or thaw all of them at once |
| `devnet attach` | `--wait S` (default 3600), `--node SERVICE`, `--direct` | wait until the node answers, then open the UI. With several Freeze nodes, `--node` picks one and the Nodes tab moves between them. `--direct` talks to the agent instead of its broker. |
| `devnet broker` | `status` (default), `start`, `stop`; `--auto-pause on\|off` | the brokers: port, whether the other nodes are paused with the node, how many clients, who claimed it |
| `devnet tx` | `pay` or `script`; `--lovelace N`, `--count K`, `--via SERVICE`, `--key N` | send a transaction with the devnet's own funded key (section 19) |
| `devnet timeline` | `--last N` (default 60), `--when CONDITION` | what every Freeze node did, merged by time, from the brokers' recordings |
| `devnet pause-peers` | — | pause every container except the Freeze nodes. With a broker running this also turns its automatic pausing off, so it does not undo what you asked for. |
| `devnet resume-peers` | — | resume them. |
| `devnet down` | `--keep-data` | stop and remove the devnet. Without `--keep-data` its volumes are deleted. |

### `payloads` — manage payload lists

| Subcommand | Meaning |
|---|---|
| `payloads list` | every list, shipped and yours, with its size |
| `payloads show NAME` | print a list's values |
| `payloads import FILE` | import list(s) from `.txt` (one value per line), `.csv` (first column), `.json` or `.yaml` |
| `payloads export NAME FILE` | write a list to `.yaml` or `.json` |
| `payloads delete NAME` | delete one of your lists (a shipped list of the same name reappears) |

### `template` — run a template of rules against a node, without the UI

| Option | Meaning |
|---|---|
| `TEMPLATE` | a `.yaml` or `.json` template (format in section 14) |
| connection options | as for `attach` (`--network devnet`, or `--port` / `--token-file`) |
| `--wait S` | seconds to wait for the rules to finish. Default 600. |
| `-o FILE` | write the results as JSON |

### `scenarios` — DWARF's scenarios

| Subcommand | Meaning |
|---|---|
| `scenarios list [--filter TEXT] [--all]` | scenarios Freeze can carry out; `--all` also lists the rest with the reason |
| `scenarios show ID` | what the scenario does and the rules Freeze derives from it |
| `scenarios template ID [-o FILE]` | write the scenario as a Freeze template file |
| `scenarios run ID` + connection options, `--wait S`, `-o FILE` | carry it out on a node and judge the reaction. Exit code 1 if a reaction was not as expected or a rule did not finish |
| `scenarios lists` | save DWARF's own test values (handshake, block-fetch, tx-submission and invalid-CBOR cases, protocol seeds) as payload lists |

### `sessions`, `export`, `import`, `open`, `ingest` — whole runs

| Command | Options | Meaning |
|---|---|---|
| `sessions` | — | list recorded sessions with their item and tamper counts |
| `export` | `--session NAME` (default: the most recent), `--until N` (keep only up to item N), `--note TEXT`, `-o FILE` | write a recorded session as one `.tar.gz` |
| `import RUN` | — | unpack an exported run into the data root and print what it did, one sentence per intervention |
| `open RUN` | — | browse an imported run in the UI, with no node |
| `ingest RUN` | connection options; `--no-restart` (use the node as it is), `--no-attach`, `--wait S` (default 900), `--stale-negative-control` | restart the node frozen and repeat the run's interventions, then open the UI with the node frozen at that point; protected or unsupported edits are refused by default |

### Working on a recorded session: `search`, `coverage`, `compare`, `mark`, `findings`

All take `--session NAME` (default: the most recent; see `sessions`).

| Command | Arguments | What it does |
|---|---|---|
| `search QUERY` | `--limit N` | items matching a condition (`kind=proto_in and slot>100`) or a saved filter (`@name`), or containing the text if it is not a condition |
| `filters` | `save NAME CONDITION --about TEXT`, `delete NAME` | list, save or forget named conditions (section 17) |
| `probes` | connection options; `--enable NAME`, `--disable NAME`, `--hit`, `--missed` | on a running node: every probe with how often it was reached, and turning their reporting on or off (section 17) |
| `coverage` | `--against SESSION`, `--top N` | what the run reached: parts of the node, effects, protocol messages, errors, rejections, and queues (sent to a part but not yet taken by it). With `--against`, what only one of the two runs reached |
| `compare A.json B.json` | `--strict` | two result files from `template -o` / `scenarios run -o`, attempt by attempt; `≠` marks where the node answered differently. This is also how to compare Amaru with another implementation once it has an agent |
| `compare --items A B` | `--other SESSION` | two items field by field, and what the node did with each |
| `mark SEQ` | `--bookmark`, `--comment TEXT`, `--finding TITLE --severity S --detail TEXT` | marks are kept in `notes.json` beside the session |
| `findings` | `-o report.md`, `--title T` | list the marks, or write the findings as a Markdown report with the evidence as JSON beside it (item, what was changed, what the node said, the chain of steps) |

### `repeat` — send an item from a recorded session again

`repeat SEQ [--session S] [--set FIELD=VALUE]… [--times N] [--wait S] [--stale-negative-control]`
plus connection options. An input to a part of the node that still exists is delivered again
directly. A message from a peer is sent in place of the next message of the same kind (it cannot
be put on the wire from here). Values may be transforms (`slot=add:1`). Network edits use the same
dependency classifier as the inspector and fail closed; the stale option is an explicit
negative-control authorization, not a signing mode.

### `devnet snapshot`, `devnet stats`

| Command | What it does |
|---|---|
| `devnet snapshot save NAME [--note T]` | stop the Freeze node, copy its chain and ledger stores aside, start it again |
| `devnet snapshot list` / `delete NAME` | |
| `devnet snapshot restore NAME [--running]` | put a saved chain back and restart the node, frozen at its first step unless `--running` |
| `ingest RUN --network devnet --snapshot NAME` | restore first, so the run is repeated from the same stored state |
| `devnet stats [--every S] [--duration S]` | sample the Freeze node's memory, CPU and thread count, and say whether memory grew |

Only the Freeze node is rewound; the other nodes keep their chain, so the node catches up from the saved point.

### Environment variables

| Variable | Read by | Meaning |
|---|---|---|
| `DWARF_DATA_ROOT` | `freeze-debugger` | where DWARF keeps runtime data. Freeze uses `<root>/freeze/`. Default: `${XDG_DATA_HOME:-~/.local/share}/dwarf`. |
| `DWARF_FREEZE_TOKEN` | `freeze-debugger attach` / `watch` | the shared secret, if not given another way. |
| `DWARF_FREEZE_LISTEN` | the node | address the agent listens on, e.g. `0.0.0.0:7373`. **Unset means no agent: the node behaves as an unmodified node.** |
| `DWARF_FREEZE_TOKEN` | the node | required secret debuggers must present; missing or empty disables the control socket. |
| `DWARF_FREEZE_START` | the node | `frozen` holds the node at its very first step. |

The launcher and the devnet set the three node-side variables for you.

---

## 6. The terminal UI

### Tabs

| Tab | Key | What it is |
|---|---|---|
| **Intercept** | `1` | Left: the held items. Middle: the selected item as a tree of plain-named fields. Right: Inspector — where it came from and goes to, what it is in plain words, the selected field, and your edits. |
| **History** | `2` | Every item as a table, newest at the bottom, with a filter box. Selecting a row shows "what went in" (with your edits marked) and "what the node did" (everything that followed from it). |
| **State** | `3` | What the node holds: each stage's internal state, and the chain and ledger stores (loaded when opened, 200 entries at a time). Select a value, `e` to edit, "Apply edits" to write it. A stage's state takes effect when that stage next handles a message; a store is written while the node is frozen (section 18). |
| **Freeze points** | `4` | Conditions that freeze the node (or only log) when an item matches. Type one and press Enter; prefix with `log ` to only log, `once ` to fire once; `x` deletes the selected one. Below them, the node's probes: how often each was reached, Enter turns its reporting on or off, `a` all on, `n` all off. |
| **Payloads** | `6` | Left: payload lists and the values of the selected one. Right: the rules that are running, and one result row per attempt (value, what the field was and became, what the node said). Keys here: `n` new list, `a` add value, `e` edit value, `x` delete, `I` import a list file, `E` export the list, `T` load a template and start its rules, `S` stop all rules, `C` clear results, `R` save results. |
| **Scenarios** | `7` | DWARF's scenarios. The table shows the ones Freeze can carry out (`A` toggles the rest, with the reason). The right side shows what DWARF does and checks, and the rules Freeze would run. `L` loads the selected scenario as rules and starts them; `W` writes it as a template file. |
| **Nodes** | `8` | Every Freeze node of the devnet with its state, and below it what all of them did, merged by time. Enter moves this window to the selected node; `Z` freezes all of them, `G` runs all. |
| **Source** | `9` | Source-line stepping (section 18): `A` attaches a debugger to the node and detaches it, `b` break, `c` continue, `s` step into, `n` next line, `o` out, `x` stop now. Shows the file around the current line, the call stack and the locals. |
| **Overview** | `5` | The node at a glance. **Peers:** each one with how many messages were heard from it and said to it, the last message heard, and whether the node marked it hostile or hung up. **Inside the node:** the path a block takes (track peers → select chain → fetch blocks → validate block → adopt chain), then the other parts, each with how much it has handled, how many actions it took and the last thing it did. A snowflake marks where the node is stopped. Counts are for this session. |

### History keys

`X` export the run · `b` bookmark · `m` note · `F` finding (`severity: title`) · `c` compare (press on one item, then on another) · `R` send the item again (`as it is`, or `field=value; field=value`). Marked items show `!` (finding), `*` (bookmark), `#` (note) before their number.

### Keys

| Key | Does |
|---|---|
| `i` | Intercept on/off. On: hold every item of the kinds ticked in the "holding" row. Off: clear that and let the node run. |
| `f` | Forward the selected item (with your edits) and let the node run until the next held item. |
| `space` | Step: forward the selected item and stay frozen. |
| `d` | Drop the selected item. |
| `e` | Edit the selected field. |
| `r` | Undo your edits to the selected item. |
| `u` | Run until a condition matches, then freeze. |
| `p` | In Intercept: run a payload list against the selected field. Freeze asks which list, then applies one value per matching item automatically. |
| `X` | In History: export the run up to the selected row (or everything, if the last row is selected). |
| `n` | In Intercept and History: deliver a new message to a part of the node. The dialog is filled in from the selected item if it was an input. |
| `O` | With a broker: claim the node, so only this window can change it; again to release. The status bar says who has it. |
| `z` | Freeze now: the node stops at its next step. |
| `g` | Run: release everything and let the node go. |
| `q` | Quit the UI. The node keeps running. |

### The "holding" row

| Box | Holds | Default |
|---|---|---|
| from peers | decoded protocol messages arriving | on |
| to peers | decoded protocol messages leaving | on |
| raw bytes | every chunk read from or written to a socket | off |
| between parts | every message one stage hands another | off |
| actions | every effect and its result | off |
| store writes | reserved; the node does not report these yet | off |
| timers | reserved | off |

### Reading it

- **Colours:** blue = field names, amber = kinds of thing (Block, RollForward), mint = parts of the node, violet = actions the node takes, red = changed by you.
- **Names** are plain: `vrf_proof` shows as "vrf proof", `prev_hash` as "previous block". The raw name is in the Inspector.
- **Hover** the mouse over a name, or move the cursor onto it, for what it is and why it matters for security.
- **Frozen means the whole node.** While anything is held, every part of the node stops at its next step, and the node's clock and timers stop too.
- **Terminal width.** At 140 columns or more the buttons are one row and the held list, the item and the Inspector sit side by side. Below that the buttons and the "holding" row wrap onto more rows, and the held list goes above the item and the Inspector, so nothing runs off the right edge. It rearranges as the window is resized. 80 by 24 is the smallest size that is usable; tables wider than their pane scroll sideways.
- **The Source tab is empty until a debugger is attached** (`A`), and that needs an image built with `build --debug-info`. The tab says so until then.

### Editing

Select a single value and press `e`. Numbers, text, yes/no and bytes (as hex) keep their kind. The change is local and shown in red until you Forward or Step. Network messages are edited **byte-exactly**: only the bytes of that field change, plus enclosing lengths if the size changed.

---

## 7. Conditions

Used by "Run until…", Freeze points, the History filter and `watch --only`.

```
kind=input and to~validate_block      label=RollBackward         slot>=138760000
type=ouroboros/2 and [0]=2            txs#>10                    not label=clock
```

| Form | Meaning |
|---|---|
| `kind=a,b` | one of these kinds. Short names: `input`, `action`, `result`, `state`, `wire`, `proto`, `store`, `timer`, `probe`. |
| `to=…` `from=…` `type=…` `label=…` | text tests. `=` exact, `~` contains, `^` starts with, `$` ends with. |
| `label=RollForward` | a network message by the name the UI shows. Works for every mini-protocol message name (`RollBackward`, `Block`, `KeepAlive`, …), on either side of the protocol. Only with `=`: a network item's own label is its protocol (`chain sync`), so `label~Roll` finds the node's effects with that text, not network messages. |
| `spot=tag` | carries this cold-spot tag. |
| `seq>=N` | item number at least N. |
| `path=value`, `!=`, `<`, `>`, `>=`, `<=` | compare a payload field. Paths use dots and `[n]`, e.g. `txs[0].fee`. |
| `path~text` | the field contains this (text, or hex of bytes). |
| `path#>N` | the field is longer than N. |
| `path?` | the field exists. |
| `everything` | always true. |

Join with `and` / `or` (`and` binds tighter); prefix a term with `not`.

**Protocol messages:** `type` is `ouroboros/<n>` — 0 handshake, 2 chain sync, 3 block fetch, 4 tx submission, 8 keep alive, 10 peer sharing. `[0]` is the message number, so `type=ouroboros/2 and [0]=2` is a RollForward and `type=ouroboros/3 and [0]=4` is a Block.

---

## 8. What you can do

**Watch a node.** `watch --network preprod --only "kind=proto"` prints each protocol message.

**Stop on something specific.** `u`, then for example `type=ouroboros/2 and [0]=3` to freeze on the next rollback instruction from a peer.

**Step.** `z` to freeze, then `space` repeatedly. Tick "actions" to see every store read, validation call and clock read as its own step.

**Tamper with a block header (verified on the Linux devnet).** `u` → `type=ouroboros/2 and [0]=2`; open header → header body; select `slot`; `e`; change it; `f`. The node runs "check header", gets back an invalid VRF proof, records the header as rejected, marks the peer hostile and hangs up. All of that appears in History under the item.

**Drop a message.** Select it, `d`. A dropped protocol message never reaches the node; the peer sees silence.

**Change what a part of the node remembers.** State tab → pick a stage → edit a value → Apply.

**Send the node a message nobody sent** (client only for now):

```python
client.inject("stage:validate_block-6", payload)   # available once that stage has handled one message
```

**Script it.** Anything the UI does is a call on the same socket:

```python
import sys; sys.path.insert(0, "dwarf/dwarf")
from freeze import cardano, cbor_tree, conditions
from freeze.client import FreezeClient

c = FreezeClient("127.0.0.1", 7373, token=open("/tmp/freeze.token").read().strip()); c.connect()
c.run_until(conditions.compile_condition("kind=proto_in and type=ouroboros/2 and [0]=2"))
item = c.wait_held(lambda it: it["kind"] == "proto_in")
raw = bytes.fromhex(item["raw"])
root = cbor_tree.parse(raw); cbor_tree.open_tagged(root); payload = cbor_tree.to_json(root)
names = cardano.names_for(item["type"], payload)
slot_path = next(p for p, n in names.items() if n == "slot" and "$embed" in p)
c.replace(item["seq"], raw=cbor_tree.edit(raw, slot_path, 999))
c.thaw()
for event in c.events(timeout=10):
    print(event["event"], event["data"].get("label"))
```

`freeze/client.py` has one method per protocol request: `freeze`, `thaw`, `step`, `run_until`, `set_hold`, `item`, `forward`, `drop`, `replace`, `inject`, `add_condition`, `conditions`, `state_list`, `state_get`, `state_put`, `wait_held`, `events`.

---

## 14. Payload lists, rules and templates

**A payload list** is a named list of test values. Shipped lists: `integer boundaries`, `nearby numbers`, `byte string damage`, `32-byte hashes`. `scenarios lists` adds DWARF's own.

**A value** is a literal or a transform of what the field already holds:

| Form | Meaning |
|---|---|
| `123`, `-1`, `true`, `null`, `"text"` | a literal |
| `0xdeadbeef` | bytes |
| `add:N`, `sub:N`, `mul:N` | arithmetic on the original number |
| `empty` | empty bytes or text |
| `trunc:N` | keep the first N bytes or characters |
| `append:HEX`, `prepend:HEX` | add bytes at the end or start |
| `zero`, `ones` | same length, every byte 00 or ff |
| `flip:K` | flip bit K (0 is the lowest bit of the first byte) |
| `xorlast:HH` | XOR the last byte with HH |
| `repeat:N` | the original repeated N times |
| `len:N` | N zero bytes |

**Running a list against one field** (the Intruder-style flow): in Intercept, hold an item, select a field, press `p`, choose a list. Freeze works out a condition for "items like this one", then for each value waits for the next such item, puts the value in, forwards it and records the node's reaction. When the list is used up the rule stops and traffic flows untouched. Results are in the Payloads tab.

**A template** applies several rules at once. Example:

```yaml
name: header and chain selection
description: Edges of the header's numbers, and a starved chain selection.
lists:
  tiny: [0, 1]
rules:
  - name: header slot
    when: kind=proto_in and type=ouroboros/2 and [0]=2
    field: header_body.slot           # a field name, a chain of names, or a path like txs[0].fee
    values: integer boundaries        # a list name, or an inline list
    mode: each                        # each: one value per matching item, then stop. cycle: keep going
    expect: [rejected, error, hung up, hostile]
  - name: starve chain selection
    when: kind=input and to~select_chain
    action: drop
    count: 3
  - name: old header again
    when: kind=proto_in and type=ouroboros/2 and [0]=2
    action: replay                    # let `after` go by, then send the first of them again
    after: 3
  - name: damage a block
    when: kind=proto_in and type=ouroboros/3 and [0]=4
    field: "@message"                 # the whole message as bytes
    values: ["xorlast:01"]
```

More actions:

```yaml
  - name: keep the slot
    when: kind=proto_in and type=ouroboros/2 and [0]=2
    action: capture                   # read a field, let the message through
    field: header_body.slot
    as: slot
  - name: use it elsewhere
    when: kind=proto_in and type=ouroboros/2 and [0]=2
    field: header_body.block_number
    values: ["$slot"]                 # the value kept above
  - name: late header
    when: kind=proto_in and type=ouroboros/2 and [0]=2
    action: delay                     # set the message aside while the node runs on
    delay_ms: 1500
  - name: let the block go first
    when: kind=proto_in and type=ouroboros/2 and [0]=2
    action: overtake                  # wait until a message matching `until` has gone by
    until: kind=proto_in and type=ouroboros/3
```

A message that is set aside stalls the part of the node waiting on it, so messages queued behind it in that same part (the same protocol on the same connection) cannot overtake it; messages for other parts can. This requires an agent that advertises deferred-item support.

Rule fields: `name`, `when` (a condition), `action` (`replace`, `drop`, `forward`, `replay`),
`field`, `values`, `mode`, `count` (how many items to act on), `after` (let this many through
first), `expect` (phrases; the attempt is "as expected" if the node's reaction mentions any), and
`stale_negative_control` (default `false`; set `true` only when the rule deliberately emits
stale/invalid network validation material). Network rules otherwise use the same fail-closed
dependency classifier as interactive edits.

Load a template with `T` in the Payloads tab, or run it without the UI: `freeze-debugger template FILE --network devnet`.

**What "the node said" means.** After each attempt Freeze reads the items that followed: results carrying an error (named by what is inside, e.g. `Assert › KesSignature › InvalidKesSignature`), rejections, a peer marked hostile, a connection closed, or the node no longer answering. The node itself says which steps belong to which message: every item carries a `cause`, the number of the message from a peer (or other first step) its chain started from, carried across every hand-off between parts of the node. So a reaction lands on the attempt that caused it however closely attempts follow each other, and an attempt with no error reads `no error in N steps, reached …` rather than a guess. History's "what the node did" pane shows the same chain. Against an implementation that does not report `cause`, Freeze falls back to taking the items that came next, up to the next attempt.

Two reactions are not rejections and are worded apart from them. `already had it (duplicate, no penalty)`: another peer delivered the same header first, which is routine with more than one upstream peer. `set aside by the node, undecided (…)`: Amaru parks a header that claims a height more than 1000 blocks ahead of its ledger (or a slot up to 2 seconds ahead of its clock) and looks again every 200 ms. It does this before checking the header against its parent, so a header with an inflated block number is neither validated nor rejected, the node stops asking that peer for more, and the peer is not marked hostile until the ledger reaches that height, if ever. Other peers are unaffected.

## 15. Runs: export, import, do again

Every `attach` or `run` records the session. To move a run somewhere else or repeat it:

```bash
freeze-debugger sessions
freeze-debugger export --until 5000 -o my-run.tar.gz     # or from the UI: History, select a row, X
freeze-debugger import my-run.tar.gz                     # prints what was done, in sentences
freeze-debugger open my-run                              # browse it in the UI, no node
freeze-debugger ingest my-run --network devnet           # restart the node, do it again, open the UI
```

An exported run holds the full event stream up to the chosen point, and its **interventions**: every drop and edit. `ingest` restarts the node frozen and repeats them, then leaves the node frozen at that point.

How faithfully: a live node never produces exactly the same items in the same order twice. So an intervention is tied to what the item was (its kind, where it was going, which message) and to how many like it went by first, not to an item number. Interventions on the same kind of item keep their order.

Limits: the restart keeps the node's stored chain, so the node does not return to the state the original run started from. Messages you injected and State-tab edits are not replayed. An edit is replayed as the same value, not the same change: if you set a header's signature to particular bytes, those bytes go into the next matching header, whatever its own signature was.

## 16. DWARF scenarios in Freeze

Freeze reads DWARF's 360 scenario files and its test-value tables as plain data; it does not import or run DWARF's code.

| Status | Count | Meaning |
|---|---|---|
| ready | 54 | the scenario's tampering is carried out by Freeze rules |
| partly | 22 | carried out with a stated difference (for example, DWARF drops traffic for a time, Freeze drops a number of chunks) |
| no | 284 | not possible from inside one node; the reason is shown |

What converts: DWARF's man-in-the-middle proxy modes (replay an earlier message, damage a header signature, flip a byte of a message), its fake-peer test cases (handshake, block fetch, tx submission, invalid CBOR, sent in place of a real peer's next message), and port drops.

What does not, and why: decoder-only fuzzing (no running node), measurement and evidence tooling (nothing is tampered with), timed delay or loss, anything needing a new producer/leader identity or transaction witness, corruption of stored state, forced clock or epoch, comparisons between two nodes, load generators, and whole-topology arrangements.

Things to know:
- DWARF's proxy scenarios target cardano-node. Running them on Amaru through Freeze is new coverage, not a repeat of an existing result.
- DWARF's assertions are not ported one by one. "The node must reject this" becomes an expectation on the rule; assertions about the whole topology cannot be judged from inside one node.
- A fake-peer scenario opens its own connection in DWARF. Freeze instead replaces a real peer's next message of that protocol, so the connection state around it differs.

```bash
freeze-debugger scenarios list --filter chainsync
freeze-debugger scenarios show runtime-substrate-praos-header-assertion-example-smoke
freeze-debugger scenarios run  runtime-substrate-praos-header-assertion-example-smoke --network devnet
```

## 17. Several nodes, a scripted peer, stores, probes, saved filters

### Several Freeze nodes in one devnet

A devnet can have more than one Freeze node. Each gets its own agent port (the main node on
`--port`, the others on the ports after it) and all share one token.

```bash
freeze-debugger devnet up --profile relay-pair          # both Amaru relays are Freeze nodes
freeze-debugger devnet up --also amaru-relay-2          # or add a second node to any profile
freeze-debugger devnet nodes                            # each node: port, frozen or running, step, held
freeze-debugger devnet nodes --freeze                   # freeze all of them at once (--thaw lets them run)
freeze-debugger devnet attach --node amaru-relay-2      # a UI on one of them; the header names the node
```

Every command that connects takes `--network devnet --node SERVICE` to pick the node, for
example `template FILE --network devnet --node amaru-relay-2`. One UI is on one node; open
one per node (two terminals, or two tmux panes). Snapshots, `ingest` and `devnet stats` act
on the main node. In a profile file the extra nodes are `also: [service, ...]`, and
`node_environment: {service: {VARIABLE: value}}` sets variables for one of them.

### A scripted peer

Profile `scripted-peer` makes `amaru-relay-2` the node under test and has it follow
`amaru-relay-1` instead of a block producer. `amaru-relay-1` is also a Freeze node, so rules
on what it **sends** put altered messages on the wire to the node under test. The peer is
scripted with the same rules, templates and Python hooks as anything else, pointed at it:

```bash
freeze-debugger devnet up --profile scripted-peer
freeze-debugger template dwarf/freeze/examples/peer-inflated-height.yaml --network devnet --node amaru-relay-1
freeze-debugger devnet attach                            # watch the node under test take it
```

- A node **serving** a protocol reports it with 32768 added to the protocol number. Chain sync
  served to a peer is `type=ouroboros/32770`, and `kind=proto_out and type=ouroboros/32770 and [0]=2`
  is a RollForward leaving the peer. The names and field paths are the same as for `ouroboros/2`.
- The node under test also keeps a block producer as a second upstream peer, so it goes on
  following the chain whatever the scripted peer does.
- Verified on the Linux devnet: the example sent a header with its block number raised by 5000,
  and the node under test set it aside and rechecked every 200 ms for the 10 minutes it was watched.
- This is for the closed devnet only.

### Chain and ledger stores in the State tab

With a node built from this version, State lists the node's stores next to its parts. A store
is read when its branch is opened, 200 entries at a time (`more…` loads the next 200). All of
them are read-only.

| Namespace | What it holds |
|---|---|
| `chain/best` | the tip and the anchor of the best chain (slot, hash, height) |
| `chain/headers` | the best chain from the anchor to the tip, oldest first; an entry opens to the full header and whether it was validated |
| `chain/blocks` | the same chain; whether each block is stored and its size; an entry opens to the block's bytes |
| `chain/nonces` | the nonces stored for each header |
| `ledger/tip`, `ledger/pots`, `ledger/params` | the stored ledger's tip, its pots (treasury, reserves, fees, donations) and protocol parameters |
| `ledger/utxo` | unspent outputs by `<transaction id>#<index>` |
| `ledger/stake` | lovelace in unspent outputs per stake credential (not the per-pool distribution, and without rewards) |

- The ledger namespaces show the ledger **as stored on disk**, which trails the chain tip by
  the rollback window: on the devnet the stored ledger was at slot 1420 while the chain tip was at 1508.
- `ledger/stake` reads the whole UTxO set on every request; on a large network it is slow.
- From a script: `client.state_list("chain/headers", after=KEY, limit=200)` and `client.state_get("chain/headers/KEY")`.

### Probes: named places inside the node's functions

The node has 43 probes at function level: the four header checks (KES, VRF, operational
certificate, leader stake), chain selection, what `track_peers` decides about a header, block
fetch, block validity, chain-store writes, the mempool, peer selection, handshakes, and
protocol and mux errors.

- **Every probe counts how often it was reached, always.** That is the coverage answer:
  `freeze-debugger probes --network devnet` lists each with its count and ends with
  `N of 43 probes reached`; `--missed` shows the ones never reached, `--hit` the others.
- **A probe reports an item only when it is turned on**, because some are reached thousands of
  times: `probes --enable 'consensus.validate_header*' --enable ledger.block.valid`,
  `--disable all`. Names take a trailing `*`. In the UI: Freeze points tab, PROBES table,
  Enter on a row, `a` all on, `n` all off. To have them on from the node's first step, set
  `DWARF_FREEZE_PROBES` (`all`, or names and prefixes separated by commas) in the profile's `environment`.
- A probe item has kind `probe`, its name as label, and a few small fields (slot, height, hash,
  peer, error). Conditions work on it (`kind=probe and label~validate_header.kes`), so a probe
  can freeze the node: add that as a Freeze point. Probes inside a part of the node carry `cause`
  and appear in the chain of the message that led to them; the four header checks and the ledger
  probes run on other threads and do not.
- `coverage` on a recorded session counts the probes that reported while it was recording.

### Saved filters

A condition can be saved under a name and used as `@name` wherever a condition is typed for
`search` and in History's filter box. They are kept in `filters.json` in the Freeze data folder
and shared by all sessions.

```bash
freeze-debugger filters save headers 'kind=proto_in and type=ouroboros/2 and [0]=2' --about "headers from peers"
freeze-debugger filters                  # list
freeze-debugger search @headers
freeze-debugger filters delete headers
```

In History's filter box: `@headers` uses it, `@headers = kind=proto_in and …` saves and uses it,
`@-headers` forgets it, `@` alone lists the names.

## 18. Broker, nodes view, injection, effects, store writes, ledger probes, tracing, rewind, source lines, other revisions

### The broker

`devnet up` and `run` start one broker per Freeze node. Commands and the UI connect to the
broker (the agent's port plus 50; `devnet broker` shows it) and need nothing changed to do so.
`--direct` talks to the agent itself.

- **Recording.** The broker writes the session to `sessions/<time>-<node>/` whether or not a
  UI is open. A UI that attaches later starts with History filled from it, and does not write
  a second copy.
- **Peers.** While the devnet's main Freeze node is frozen, the broker pauses the other
  containers; when it runs, it resumes them. A freeze or a run shorter than a quarter of a
  second changes nothing, so stepping keeps them paused throughout. The status bar shows
  `peers paused`. `devnet broker --auto-pause off` turns it off; `devnet pause-peers` and
  `resume-peers` do it by hand. Only the main node's broker does this.
- **Claiming.** Nobody owns a node to begin with. `O` in the UI claims it: the other windows
  and commands can still watch, and anything of theirs that would change the node is refused
  until you press `O` again or close the window. Another window can take it over with `O`.
  This is a courtesy between clients, not access control: the token decides who may connect.

### Several nodes in one window

With more than one Freeze node, the **Nodes** tab (`8`) lists them and shows what all of them
did, merged by time. Enter on a node moves the window to it; `Z` and `G` freeze and run all
of them. `freeze-debugger devnet timeline` prints the merged list from the recordings.

### Injecting a message from the UI

`n` in Intercept or History opens a dialog: where to (a part of the node; the `stage:` prefix
may be left off) and the message as JSON. If the selected item is an input, both are filled
in from it. A node built from this version takes a message for a part before that part has
handled anything; parts created while the node runs still need to have seen one message.

### Editing and skipping effects

Hold actions (the `actions` box in Intercept) and the item says whether it can be edited or
dropped.

| Effect | Edit | Drop |
|---|---|---|
| send, reply | the message | not sent |
| wait | `ms` | returns at once |
| call | `timeout_ms` | – |
| set timeout | `delay_ms` | not armed (one already armed for that slot stays) |
| schedule | – | never delivered; the part still gets its schedule id |
| external (store reads, network sends, ledger calls…) | its fields, where the effect reads them back unchanged | – |

An edit the type refuses keeps the original and is logged as an error.

### Writing to the stores

Only while the node is frozen. In the State tab: open the entry, edit, **Apply edits**.

| Namespace | Write | Delete |
|---|---|---|
| `chain/headers/<key>` | `{"header": …, "valid": true \| false \| null}` | yes |
| `chain/blocks/<key>` | `{"block": {"$bytes": "<hex>"}}` | yes (the header stays) |
| `chain/best/tip`, `chain/best/anchor` | `{"slot", "hash", "height"}` | no |
| `ledger/utxo/<txid>#<index>` | the output | yes |
| `ledger/pots` | `{"treasury", "reserves", "fees", "donations"}` | no |
| `ledger/params` | the protocol parameters | no |

- A header or block must hash to its key, and a new tip must name a stored header, unless the
  value carries `"force": true`. **A forced mismatch makes the node stop with an integrity
  error the next time it reads that entry.** Use it when that is the test.
- Chain writes happen at once. Ledger writes are queued and done by the ledger thread before
  its next request; the answer says `queued`, and a `store_write` item appears when it is done.
- Ledger writes change the ledger as stored. What the node holds in memory for the last
  blocks is not changed.
- Give bytes as `{"$bytes": "<hex>"}` when the exact encoding matters for a hash.

### Probes in the ledger

32 of the probes are in the ledger: one per phase-one rule (`ledger.rule.fees`,
`ledger.rule.collateral`, … with the error when the rule fails), the block rules
(`ledger.block_rule.*`), the verdict on each transaction and block (`ledger.verdict.*`),
each script evaluation (`ledger.script.eval`: purpose, index, Plutus version, budget given and
spent, verdict), the epoch transition and the snapshot. Turn them on like any probe:
`freeze-debugger probes --enable 'ledger.*'`.

### Every function

About 2,000 functions (consensus, ledger, protocols, header validation, stores, mempool,
network) can report each call. Nothing is reported until you ask:

```bash
freeze-debugger trace --network devnet --enable amaru_consensus::stages::select_chain
freeze-debugger trace --network devnet --enable amaru-ledger        # a whole crate
freeze-debugger trace --network devnet --hit                         # functions seen so far, most called first
freeze-debugger trace --network devnet --disable all
```

- A prefix selects whole path segments: `amaru_ledger::rules` covers
  `amaru_ledger::rules::block::execute`, not `amaru_ledger::rules_x`. `text*` is a plain
  prefix; `all` is everything.
- Each call is an item of kind `call`: the function's full path, its arguments as text (those
  that can be printed), how deep it is in traced calls on its thread and which traced function
  called it. When it returns, the item gets the time it took and whether it ended by a panic.
- Calls are reported, not held. `kind=call and type~validate_header` as a freeze point
  freezes the node at its next step after such a call.
- A function is listed by `trace` once it has been called. `async` functions are reported at
  start and end but are not part of the call depth.
- Tracing everything is thousands of items a second. Use a narrow prefix.
- `DWARF_FREEZE_TRACE=prefix,prefix` switches tracing on from the node's start.

### Rewind

`devnet up --frozen` saves the main node's stored chain as snapshot `start` before its first
step (the broker does this; the UI is let in afterwards).

```bash
freeze-debugger rewind --to 1306                 # item 1306 of the node's latest recording
freeze-debugger rewind --to 1306 --session SESSION_ID --no-attach
```

The node is restarted from `start`, frozen; every change the session made before that item is
made again (as `ingest` does); then the node runs until the item that corresponds to it and
freezes with that item held. Items are numbered differently in a second run, so the item is
recognised by what it is and what it carries (for a chain-sync roll forward: its header).
`--by-count` stops at the item in the same position among its like instead. If the node never
produces it, rewind says how many like it went by. As with any snapshot, what the node held
only in memory is rebuilt by the node, not restored.

### Source lines

```bash
freeze-debugger build --static --debug-info --source dwarf/testplease/amaru/v10.11.20260925
freeze-debugger devnet up --image <the image it printed>
freeze-debugger source                                   # asks for commands
freeze-debugger source --do 'break select_chain/mod.rs:120' --do continue --do vars
```

`--debug-info` keeps debug information in the node (its own code in full, dependencies as
line numbers) and builds a second image, `<image>-tools`, with lldb and the source. `source`
runs it beside the node. Commands: `break FILE:LINE` or `break FUNCTION`, `breaks`,
`unbreak [N]`, `continue`, `step`, `next`, `out`, `interrupt`, `where`, `vars [FRAME]`,
`threads`, `list [N]`, `detach`. In the UI it is the **Source** tab (`9`).

- Attaching stops the node, **and its Freeze agent with it**: until you `continue` or
  `detach`, no other Freeze command gets an answer. Freeze the node through Freeze first if
  you want its peers paused meanwhile.
- The node's code is compiled with optimisation, so some locals show as optimised away and
  a step can move by more than one line.
- Without `--debug-info` there is no tools image, and `source` says so.
- lldb 14 has no Rust support of its own: structures show as `{…}`, plain values show.
- A line that the compiler folded into other code may never be "reached". If a breakpoint
  is not hit, put it on a neighbouring statement.
- A debug-info image is about 1 GB (the normal one 190 MB) and its tools image about 2 GB.

### Other Amaru revisions

```bash
freeze-debugger port --source /path/to/amaru-at-another-revision
freeze-debugger build --static --source /path/to/amaru-at-another-revision
```

`port` merges the stored hooks into the other tree, file by file, three ways (the revision
the hooks were written for is needed; DWARF's copy is used unless `--base` names another).
The hooks are in groups (`targets/amaru/spec.yaml`): the two a build cannot do without
(`gate`, `start`) and the rest. An optional group that does not merge or does not compile is
left out and the capabilities it gave are listed as missing. The result is stored as a recipe
in your data folder (`--shipped` puts it with the shipped ones), and `build` then uses it.

Where upstream and the hooks changed the same lines, nothing can merge them by rule:

```bash
freeze-debugger port --source TREE --by-hand /tmp/ported     # leaves the tree with the conflicts marked
# resolve them, then
freeze-debugger port --source TREE --resolved /tmp/ported    # compiles it and stores the recipe
```

### What was run, and where

On the Linux devnet (profile `relay-pair`, Amaru `v10.11.20260925`, 14 November 2026):

| What | Result |
|---|---|
| Broker | started by `devnet up`; recorded both nodes; a UI opened later had 3,476 earlier items in History |
| Peers | paused 250 ms after a freeze, resumed after a run, shown as `peers paused`; `start` snapshot saved by itself |
| Claiming | with one window holding the node, another client's `thaw` was refused (`not_owner`) |
| Nodes tab | both nodes listed, merged list from both, `Z`/`G` froze and ran both, Enter moved the window |
| Inject dialog | a message delivered to `peer-selection/ledger-check` from the UI |
| Effects | a call's timeout changed 1000 → 4321 ms, a timeout's delay 60 s → 50 ms, a scheduled keep-alive dropped |
| Store writes | refused while running; treasury set through `ledger/pots` and read back; a header written back; a header under the wrong key refused; `ledger/tip` refused |
| Ledger probes | block rules, block verdict, block valid, epoch transition reported with their fields |
| Tracing | 718 calls of two prefixes in 45 s with arguments, depth and caller; 956 with everything on for 30 s |
| Rewind | node restored from `start` and stopped on the header that item 1306 of the recording carried |
| Source lines | attach, breakpoint at `amaru-pure-stage/src/tokio.rs:422` hit, locals, `next` to line 424, detach; the node ran on |
| Saved filters | saved with `filters save`, used as `@headers` in `search` |

Not exercised live: the devnet's blocks carry no transactions, so the per-rule
(`ledger.rule.*`), transaction-verdict and script probes were reached only in the node's own
tests; the node made no `wait` effect, so editing one was run only in the gate test; chain
store deletes and `chain/blocks` writes were run only in the store tests.

A clean install (DWARF cloned from GitHub, this repository copied over it, a new environment)
passed all tests and built the image on Linux. On macOS the tests pass; the devnet needs Linux.

**On a public network** (`run --network preview`): the node started frozen behind its broker
and synced when let run; so did a `v10.11.20261105` node, started with that release's `amaru node run`. While either was catching up at full speed, requests took many seconds (section 11). The chain and ledger namespaces appear once the node has opened its
stores, not while it is held at its first step.

### Amaru `v10.11.20261105`

The hooks were brought to this release with `port --by-hand` and `--resolved`. Upstream had
reworked the stage runtime, so 18 files had places changed by both sides; they were resolved
by hand, and the result is the shipped recipe `v10.11.20261105`. In that tree the agent's
tests, the gate test, and the node, consensus, protocols and ledger-rule tests pass, and
`build` produces an image from it.

Differences from the `v10.11.20260925` hooks:

- `ledger.rule.collateral_return` is gone (upstream checks it inside the outputs rule).
  `ledger.rule.collateral` fires only for transactions with redeemers.
- `treasury_value`, `certificates`, `proposals` and `voting_procedures` are reported for
  valid transactions only.
- Upstream's new `try send` effect is shown, editable and droppable like a send.
- The node is started with `amaru node run`. Freeze's own launcher (`run`) knows this from
  the image. **DWARF's devnet layouts start the node the old way, so a `v10.11.20261105`
  Freeze node does not start in the devnet until those layouts are for that release.**

## 19. Busy nodes, holding inside functions, any node, transactions, later releases

### A node that produces more than can be read

A node catching up on a public network produces tens of thousands of items a second. The UI
asks the node for less when that happens, aiming at about 400 items a second: the status bar
shows `showing 1 in N`, and History holds that sample. On a public network it starts with
1 in 50 and widens within seconds if the node is quiet. **Held items always arrive**, as do verdicts and freezes, so
intercepting works as before. When the node calms down the UI asks for everything again. A
History filter is passed to the node too, so a filter shows every match, not a sample of them.

```bash
freeze-debugger watch --network preview --sample 50            # one step in 50, and every held one
freeze-debugger watch --network preview --only 'kind=proto_in' # the node leaves the rest out
```

`run` now records what the clients are sent (`--record held`); `--record all` keeps
everything, as the devnet does, and `--record none` nothing.

### Holding the node inside a function

```bash
freeze-debugger trace --network devnet --hold amaru_ledger::rules::block::execute
freeze-debugger trace --network devnet --unhold all
```

The node stops each time such a function is entered. The call shows in Intercept like any held
item, with its arguments; forward lets that one call go on. It cannot be dropped or edited.

- This works in functions the ledger thread runs, and in `async` functions inside a part of
  the node. Elsewhere waiting would stop the node's runtime, so the call is only reported,
  with `not_held: would block the runtime`.
- `async` functions now count in the call depth of what they call.
- 25 functions inside a macro are traced as well (the stores' read functions).

### More than one node

- **Frozen together.** `devnet up --freeze-together`: whenever the main Freeze node freezes
  or runs, the other Freeze nodes do too. One you froze yourself stays frozen.
- **Held on any node.** The Nodes tab lists what every node is holding. On a row: `f` forward
  and run, `space` forward and stay frozen, `d` drop, Enter opens it on its node to edit.
- **Snapshots and rewind for any node.** `devnet snapshot save NAME --node SERVICE`,
  `rewind --to N --node SERVICE`. Each Freeze node's chain is saved at the start of a frozen
  devnet (`start` for the main node, `start-<service>` for the others). A snapshot only goes
  back to the node it is of.

### Schedules

A held schedule can be moved and its message changed: edit `at` and type how many
milliseconds from now it should fire (on the node's clock, which stands still while frozen).
The part of the node that made it keeps its handle, so its own cancel still works.

### What the ledger holds in memory

- `ledger/volatile` (read-only): the blocks the ledger has applied but not yet stored, oldest
  first; each opens to what it produced, consumed and changed. It cannot be written: the
  ledger only drops such blocks as half of a switch to a fork, and dropping them alone would
  leave the rest of the node ahead of it.
- `ledger/accounts`: each stake account's deposit, rewards and delegations. Writing one needs
  the node frozen and a confirmation: the State tab asks you to type `unsafe`, and scripts
  put `"unsafe": true` in the value. After such a write the node's behaviour shows nothing
  but the edit, and the session says so.
- `ledger/stake` stays read-only; it is summed from unspent outputs. Change those, or accounts.

### Transactions on the devnet

```bash
freeze-debugger devnet tx pay                # a payment from the devnet's funded key to itself
freeze-debugger devnet tx script             # lock an output at a script that always succeeds, then spend it
freeze-debugger devnet tx script --count 5
```

These use the `cardano-cli` and node of a block producer (`--via p1`) and a funded key
DWARF's configurator made for this devnet. With them the per-rule probes, the transaction
verdict and `ledger.script.eval` have something to report. Closed devnet only.

### Source stepping shows Rust values

The tools image now has lldb 19 and the formatters of the toolchain that built the node:
strings, vectors, options and enums show as what they hold. `vars NAME` opens one variable
further.

### Other releases of the node

- **In the devnet.** An image records how its release starts the node. For
  `v10.11.20261105` (`amaru node run`) the devnet puts a two-line launcher in the node's place
  that passes DWARF's `amaru run …` on under the new words; nothing of DWARF's is changed.
  `freeze-debugger build --static --source <that tree>`, then `devnet up --image <it>`.
- **Its own agent.** A recipe can carry its own copy of the agent crates
  (`v10.11.20261105` does), and a node claims only the capabilities its recipe has.
- **Porting.** Where a merge gives up because lines next to a hook changed, `port` now places
  the hook by the lines around it, inside the same function. For `v10.11.20261105` that
  leaves 8 files for a person instead of 18.

### What was run, and where

On the Linux devnet (profile `relay-pair`, `v10.11.20260925` unless said), 14 November 2026:

| What | Result |
|---|---|
| Event filter | with 1 in 25 asked for, 15 of 370 items a second arrived and `stats` said so; with `kinds=[proto_in]` only those arrived; held items always did |
| Busy node (preview, catching up at 9,000 to 14,000 items a second) | answers through the broker in under 15 ms; the UI sampled 1 in 50, then 1 in 25, by itself and stayed usable; 115 MB recorded in a minute and a half |
| Capabilities | the node listed the 22 its recipe has, and the probe groups |
| Holding inside a function | `amaru_ledger::rules::block::execute` held the node with its arguments shown; drop refused; forward let that block through. A plain function inside a stage was reported with `not_held: would block the runtime` |
| Async call depth | a stage's `handle_tip_from_upstream` reported the `async` stage function as its caller |
| Macro functions | the stores' read functions appear in `trace` (128 functions of that crate seen) |
| Frozen together | freezing and running the main node froze and ran the other; one frozen by hand stayed frozen; started frozen, both ran on the first thaw |
| Held on any node | from the main node's window, the three items the other node held were listed and one forwarded |
| Snapshots and rewind per node | both nodes got their own start snapshot; the second node was rewound to item 787 of its recording |
| Schedule | a held schedule moved to 300 ms from now fired then |
| `ledger/volatile` | the ledger's three in-memory blocks listed and one opened; delete refused with the reason |
| `ledger/accounts` | refused without `unsafe`; with it, rewards changed and read back; the session recorded the warning |
| Transactions | `devnet tx pay` and `devnet tx script` accepted; every `ledger.rule.*` probe, the transaction verdict and `ledger.script.eval` (Plutus V3, budget 500 / 64,100 given and spent) reported, on both releases |
| Source stepping | lldb 19.1.7 with the toolchain's formatters: structures open to their fields |
| `v10.11.20261105` in the devnet | built from its recipe (which carries its own agent crate, 68 probes), started through the launcher, followed the chain, reported probes and calls, held and forwarded a header, reported the rule and script probes on real transactions, and was held inside `block::execute` |

Not exercised live: holding an `async` function inside a stage (the node's own tests cover it);
a node whose recipe lacks a probe group (tests). In the `v10.11.20261105` tree one of the
node's own test suites (simulated networks) times out now and then under load. It does the
same on the tree from before this round's changes, where it also passed in full when the
machine was quiet; it was not compared against the release without any hooks.

**Seen on one devnet start, and not Freeze's doing:** a relay whose bootstrap data ended four
slots before an epoch boundary rejected the first header of the next epoch (`Invalid VRF
proof`) and never followed the chain; restoring its start snapshot reproduced it, and the
other relay, bootstrapped at the same time on the same image, was fine. A fresh `devnet up`
did not show it.

### Windows

`attach --host … --port … --token-file …`, `watch` and the tools work against a node running
elsewhere. `build`, `run`, `devnet`, `rewind` and `source` say that they need Linux, macOS
or WSL 2. None of this was run on Windows.

## 9. How it works

```
 terminal UI ─┐
 scripts ─────┼── Freeze Control Protocol ──►  agent inside the node process
 watch ───────┘    JSON lines over TCP,          ├─ runtime gate   stage inputs, effects, results, state, time
                   localhost, token              ├─ protocol gate  decoded messages in and out
                                                 └─ wire gate      raw bytes per connection
```

**The agent** is a small Rust crate (`amaru-freeze`) added to the Amaru workspace. It numbers every item, publishes it to connected debuggers, and decides whether the step may proceed. It runs its socket on its own thread so it keeps answering while the node is frozen. With `DWARF_FREEZE_LISTEN` unset it is not created and the hooks do nothing.

**The hooks.** Amaru is a graph of message-passing stages run by one runtime. Two functions in that runtime deliver every stage input and execute every effect, so a handful of call sites cover almost everything:

| Hook | Where in Amaru | Item kinds | Edit | Drop |
|---|---|---|---|---|
| stage input | `amaru-pure-stage` runtime, before a stage handles a message | `internal_input` | yes | yes |
| effect | runtime, before each effect runs | `internal_action` | sends only | sends only |
| effect result | runtime, after an external effect | `internal_result` | yes | no |
| stage state | runtime, after each transition | `internal_state` | via State tab | – |
| protocol message | `amaru-protocols` mini-protocol code | `proto_in`, `proto_out` | yes, byte-exact | yes |
| raw bytes | a wrapper around the node's connection provider | `wire_in`, `wire_out` | yes | yes |

**Holding.** A step is held when the node is frozen, or it matches the hold filter, or it matches a freeze condition. Holding a step freezes the node. Releasing one held item leaves the node frozen, so the part of the node that was waiting runs to its next step and is held again: that is stepping. "Run" releases everything that does not match the hold filter.

**Time.** The runtime's clock and every timer go through the agent. Time spent frozen is subtracted, so a stage waiting 300 ms still perceives 300 ms however long you looked at it. Peers are not paused automatically; on the devnet, `devnet pause-peers` does it.

**Editing typed messages.** Every Amaru message can be turned into generic data and back into its real type, so the agent needs no per-type code. An edit that does not fit the type is refused and the original proceeds, with an error in the log.

**Editing network messages.** The client parses the exact bytes into a tree that remembers how each item was encoded, changes one leaf, and writes the rest back unchanged. Bytes wrapped in CBOR tag 24 (headers, blocks) are opened and edited in place.

**Plain names.** `glossary.yaml` (Amaru's stages and effects) and `glossary-cardano.yaml` (network messages and fields, shared with any node implementation) map each name to a label, a colour class, what it is and why it matters. About 190 entries; unknown names fall back to the raw name with underscores removed.

**The builder.** `build` copies the source, adds the agent crate, applies the selected revision's patch with `git apply`, and runs a container build. Each build writes a record (source digest, recipe digest, image id) under `<data root>/freeze/builds/`.

**Profiles.** A profile says which devnet to run and which node in it becomes the Freeze node. Shipped: `relay` (default: three producers, two Amaru relays, Freeze is relay 1 behind producer p1), `relay-2` (Freeze is relay 2 behind p2), `kes-security` and `miniprotocol-security` (DWARF's two security layouts, Freeze as their first Amaru relay), `relay-pair` (both Amaru relays are Freeze nodes) and `scripted-peer` (relay 2 is the node under test and follows relay 1, which is also a Freeze node; section 17). Your own go in `<data root>/profiles/NAME.yaml`:

```yaml
name: mine
about: what this is for
compose: antithesis/cardano_amaru_relay_bootstrap_control/docker-compose.yaml   # or a list; absolute, or relative to the DWARF checkout or this file
target: amaru-relay-2          # the service that becomes the Freeze node
network: testnet_42
peer_cooldown: 30
environment: {AMARU_LOG: trace}   # extra variables for the Freeze node
```

Any service started through DWARF's `amaru-relay-bootstrap` wrapper can be the target, because that wrapper is what lets the node binary be swapped without touching the compose file. Layouts whose Amaru nodes start another way (the baked and adversarial ones, DWARF's `profile-a…g` remote profiles) are not supported yet. One Freeze devnet runs at a time; `devnet down` before starting another profile.

**The devnet.** With the default profile, `devnet up` runs DWARF's baseline compose file `antithesis/cardano_amaru_relay_bootstrap_control/docker-compose.yaml` exactly as it is, plus a generated override that copies the Freeze binary into a shared volume, points the first Amaru relay at it, sets the agent's environment and publishes port 7373 on localhost. The relay's own wrapper does the bootstrap with the Freeze binary.

**The protocol** is documented for other node implementations in `dwarf/freeze/spec/freeze-control-protocol-v1.md` (still marked draft).

---

## 10. What Freeze creates, and how to remove it

### On any machine where you used it

| What | Where | Remove with |
|---|---|---|
| Running single node | container `dwarf-freeze-<network>` | `freeze-debugger stop --network <network>` |
| That node's chain data | volume `dwarf-freeze-<network>-data` | add `--remove-data` to `stop` |
| Running devnet | compose project `dwarf-freeze-devnet`, containers `dwarf-control-*` | `freeze-debugger devnet down` (deletes its volumes too) |
| Images | `dwarf/freeze-amaru:*` | `docker rmi $(docker images -q 'dwarf/freeze-amaru')` |
| Build caches | BuildKit cache mounts `dwarf-freeze-cargo-registry`, `dwarf-freeze-amaru-target`, `dwarf-freeze-amaru-target-static` | `docker builder prune` (this clears **all** unused build cache, not only Freeze's) |
| Build records, tokens, recorded sessions, devnet override | `<data root>/freeze/` — by default `~/.local/share/dwarf/freeze/` | `docker run --rm -v ~/.local/share/dwarf:/d python:3.12-slim rm -rf /d/freeze` (saved chains are copied out of a container and owned by root, so a plain `rm -rf` is refused) |
| Python environment | `.venv-freeze/` | `rm -rf .venv-freeze` |

Freeze does not alter DWARF's measurement state, runs, bundles, or configuration.

---

## 11. Known limits

- **Repair scope.** Automatic cryptographic repair covers eligible inbound and outbound ChainSync
  headers and request-bound initiator and responder BlockFetch bodies in the controlled devnet. A body edit needs
  an earlier matching signed-header commitment, usually prepared through rewind/replay. Transaction
  witnesses cannot be regenerated because the Freeze node has no mounted payment keys or ownership
  map. Slot/VRF/leader-election changes are refused because eligibility cannot be proven; issuer and
  operational-certificate edits are refused because the authorized cold identity key is absent.
  Chain-store block writes cannot reuse this repair chain: that API has no mux/request/source
  identity and its store interface has no atomic header-plus-body write, so `repair:true` refuses
  before mutation and a normal write requires the exact header to exist already. Generic public-network `run`
  nodes cannot sign. Unsupported network edits are refused by default; only the per-item UI action
  or the `repeat`/`ingest`/`rewind --stale-negative-control` option emits deliberately stale material.
- **Effects that create or stop parts of the node cannot be edited or dropped.** Sends, waits, call timeouts, timeouts, schedules and external effects can (section 18).
- **Paused peers still age.** The broker pauses the devnet's other nodes while the main Freeze node is frozen, but their clocks go on: after a long freeze they see a jump, may drop connections, and slots are skipped. Repeated long pauses can leave the chain stalled; start a fresh devnet then.
- **Stores.** The ledger namespaces show and change the ledger as stored on disk, behind the chain tip by the rollback window; `ledger/tip`, `ledger/stake` and `chain/nonces` are read-only.
- **One UI drives one node.** The Nodes tab lists the others and moves the window between them.
- **A traced function can be held only where the node can wait**: on the ledger's thread and in `async` functions inside a part of the node (section 19). A wide trace prefix produces a great many items.
- **Source stepping stops the whole node, its agent included**, and needs an image built with `--debug-info`.
- **Recipes.** The shipped hooks are for Amaru `v10.11.20260925` and `v10.11.20261105`. `port` brings them to another revision; where upstream reworked the same code, a person has to resolve the merge (section 18).
- **Throughput.** Every step is serialised and reported, so a Freeze node is slower than stock. On preprod it handled about 5,000 items a second with a client attached. A node catching up on a public network at full speed produces about 45,000 events a second; the UI then shows a sample (section 19).
- **The agent is remote control of a node.** It listens only when asked, is published on localhost, and requires the token — but never expose that port.
- **macOS devnet** fails under emulation (section 1).
- **The devnet's Amaru relays stall after about 20 minutes** of following the chain (section 1). Tamper tests have to happen in that window, or after a fresh `devnet up`.

---

## 12. Tests (the three new test files are `test_freeze_rules.py`, `test_freeze_runs.py`, `test_freeze_scenarios.py`)

```bash
# Python (from the directory holding the dwarf checkout)
.venv-freeze/bin/python -m pytest -q -p no:asyncio dwarf/tests/test_freeze_client.py dwarf/tests/test_freeze_tui.py \
    dwarf/tests/test_freeze_cbor_tree.py dwarf/tests/test_freeze_devnet.py dwarf/tests/test_freeze_build.py

# Rust (inside an instrumented Amaru tree, with its nightly toolchain)
cargo test -p amaru-freeze
cargo test -p amaru-pure-stage --test freeze
```

---

## 13. Where to read more

| File | What |
|---|---|
| `dwarf/freeze/README.md` | short version of this guide |
| `dwarf/docs/freeze-debugger-design.md` | the full design, including what is not built yet |
| `dwarf/freeze/spec/freeze-control-protocol-v1.md` | the protocol, for the cardano-node work |
| `dwarf/freeze/docs/tui-mockups.html` | the original mockups |
| `dwarf/freeze/glossary.yaml`, `glossary-cardano.yaml` | every plain name and explanation; edit these to improve the wording |
