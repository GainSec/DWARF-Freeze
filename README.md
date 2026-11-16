# dwarf-freeze

DWARF Freeze: an interactive debugger for Cardano nodes, used like an intercepting proxy.
Start a node built with the Freeze agent, and everything it receives, sends and does becomes
an item you can hold, read, edit, forward or drop.

Freeze is an add-on to [DWARF](https://github.com/pragma-org/dwarf) and **needs a DWARF
checkout**. This repository holds only the Freeze files, at the paths they have inside DWARF;
installing means copying them into a DWARF checkout. DWARF supplies the devnet layouts, the
scenarios and the Amaru source the node is built from.

Learn more information, see screenshots and more on this repo's gitpages [HERE](https://gainsec.github.io/DWARF-Freeze/)

## Requirements

- Linux x86-64 for the devnet (the main target). macOS runs the demo, the UI and single nodes. On Windows, use WSL 2 with Docker available inside it; this is untested
- Git, rsync, Docker with the compose and buildx plugins, and a user allowed to run `docker`
- Python 3.11 or newer. If the system Python is older, use another one for step 3 below (for example `~/.pyenv/versions/3.12.11/bin/python`); the system Python is not touched
- Access to `github.com` and `ghcr.io`
- About 10 GB of free disk and 8 GB of free memory for the build and the devnet
- Local ports 7373 and 7423 free on `127.0.0.1`

## Deploy

Every step is a command you can paste. The directory `~/freeze` is an example; any directory works.

```bash
# 1. DWARF and Freeze, side by side
mkdir -p ~/freeze && cd ~/freeze
git clone https://github.com/pragma-org/dwarf
git clone https://github.com/gainsec/DWARF-freeze.git dwarf-freeze

# 2. Copy Freeze into the DWARF checkout
rsync -a --exclude .git --exclude /README.md --exclude /.gitignore dwarf-freeze/ dwarf/

# 3. Python packages, in their own environment (python3 must be 3.11 or newer)
cd ~/freeze/dwarf
python3 -m venv .venv-freeze
.venv-freeze/bin/pip install -r dwarf/freeze/requirements.txt

# 4. A short name for the command, for this shell
alias freeze-debugger="$HOME/freeze/dwarf/.venv-freeze/bin/python $HOME/freeze/dwarf/dwarf/freeze-debugger"

# 5. Check the install: the tests need no node and no Docker (about 2 minutes)
.venv-freeze/bin/pip install pytest
.venv-freeze/bin/python -m pytest -q tests/test_freeze_*.py

# 6. Build the node with the Freeze agent compiled in (compiles Amaru; 7 minutes on a 16-core machine, longer on a smaller one)
freeze-debugger build --static --source dwarf/testplease/amaru/v10.11.20260925

# 7. Start the closed local devnet
freeze-debugger devnet up
```

All commands are run from the DWARF checkout (`~/freeze/dwarf`). Nothing else of DWARF has
to be installed or running. Steps 1 to 6 are done once. Step 7 is done each time you want
to work, because a devnet does not last:

- Step 7 returns at once, but the Amaru relays first wait for the block producers to make
  three epochs of chain. The Freeze node answers 12 to 15 minutes later.
- The Amaru relays stop following the chain about 20 minutes after that. Do your work in
  that window, then start a fresh devnet (`devnet down`, `devnet up`).
- While the Freeze node is frozen, the other nodes are paused with it. A freeze of several
  minutes can leave the chain stopped for good; a fresh devnet is the fix.

`devnet up --frozen` holds the node at its very first step and saves its stored chain as
the snapshot that `rewind` goes back to. Attach within a minute or two of the node
becoming ready, because the other nodes are paused from that moment.

## Check status

```bash
freeze-debugger devnet status          # every container, and whether the Freeze node answers
freeze-debugger devnet nodes           # each Freeze node: port, frozen or running, step, held items
freeze-debugger devnet broker status   # the broker in front of each node, and who is connected
freeze-debugger sessions               # what has been recorded
docker images 'dwarf/freeze-amaru'     # the node images that were built
```

While the node bootstraps, `devnet status` ends with
`Freeze agent in amaru-relay-1: not ready yet` and the line the node last logged. When it
is ready, `devnet nodes` shows the node as `frozen` or `running` with a step number.

## Use it

```bash
# Open the UI. It waits until the node is ready.
freeze-debugger devnet attach
```

In the UI:

1. Press `z` to freeze the node (after `devnet up --frozen` it already is). Press `space` to let one item through at a time, or `g` to let the node run again.
2. Press `i` to turn Intercept on. The node now stops at every item ticked in the "holding" row.
3. Move to a held item. Press `e` to edit the selected field, `d` to drop the item, `r` to undo your edits.
4. Press `f` to forward the item (with your edits) and run to the next held one.
5. Press `u` and type a condition, for example `kind=proto_in and type=ouroboros/2 and [0]=2` (a block header from a peer), to run until it matches.
6. Press `2` for History, `3` for State (stages, chain and ledger stores), `8` for the other nodes. `1` goes back to Intercept.
7. Press `q` to leave. The node keeps running or stays frozen as you left it; `devnet attach` opens it again.

### A worked session: record, edit, rewind

Every command below was run on a fresh devnet started with `devnet up --frozen`. Use
absolute paths for your own files.

Everything is recorded from the moment the node starts; there is nothing to switch on.
`freeze-debugger sessions` lists the recordings, newest first.

**Edit a value in the UI.** `freeze-debugger devnet attach`, then:

1. Press `u`, type `kind=proto_in and type=ouroboros/2 and [0]=2`, press Enter. The node runs until a peer sends it a block header (RollForward) and holds it.
2. Press Tab to move into the item, then the down arrow until the cursor is on `block number`.
3. Press `e`, replace the number (243 became 5243), press Enter. The field and the item are marked `CHANGED`.
4. Press `f`. The top line says `item 472 forwarded with your changes` and counts `1 tamper`.
5. Press `q`.

**Look at what happened, and keep it.**

```bash
freeze-debugger sessions                                         # ... 1,107 items  1 tampers
freeze-debugger search 'kind=proto_in and label=RollForward'      # every header the node received
freeze-debugger mark 472 --bookmark --comment "block number 243 to 5243"
freeze-debugger mark 491 --finding "Header far ahead is parked, not rejected" --severity info
freeze-debugger findings -o ~/freeze/work/findings.md            # the findings as a report
freeze-debugger coverage                                         # what the session reached
freeze-debugger export -o ~/freeze/work/run1.tar.gz              # the whole run as one file
freeze-debugger import ~/freeze/work/run1.tar.gz                 # says what the run did, in sentences
```

**Rewind.** Put the node's chain back to its start and run it to the same item, unedited:

```bash
freeze-debugger rewind --to 472 --no-attach    # 13 seconds; the node is frozen with that header held
```

Leave out `--no-attach` to land in the UI. Rewind needs the devnet to have been started
with `--frozen`.

**Edit without the UI: a template.** Save this as `raise-block-number.yaml`:

```yaml
name: inbound header with a raised block number
rules:
  - name: raised block number
    when: kind=proto_in and type=ouroboros/2 and [0]=2
    field: header_body.block_number
    values: ["add:5000"]
    mode: each
```

```bash
freeze-debugger template ~/freeze/work/raise-block-number.yaml --network devnet --wait 90 \
    -o ~/freeze/work/result.json &
freeze-debugger devnet nodes --thaw            # a template waits for traffic; it says so if the node is frozen
wait
# raised block number #1  item 948  add:5000  243 → 5243  set aside by the node, undecided
```

**Edit without the UI: a Python script.** `on_item(item)` returns `"drop"`, `"hold"`, a
replacement, or nothing to forward. The script is not sandboxed.

```bash
freeze-debugger script ~/freeze/work/drop_two.py --network devnet \
    --only 'kind=proto_in and type=ouroboros/2'
# actions: forward=793 drop=2 ...
```

A script runs until it returns `"hold"` or you press Ctrl-C. Stop it with Ctrl-C, not by
killing it: a killed script leaves its hold in place and the node freezes on the next item.

**Send a recorded item again**, with a field changed:

```bash
freeze-debugger repeat 472 --session SESSION_ID \
    --set header_body.block_number=999 --network devnet --wait 60
```

**Other commands.**

```bash
freeze-debugger watch --network devnet         # one line per step; Ctrl-C to stop
freeze-debugger devnet nodes --freeze          # stop every Freeze node (--thaw lets them run)
freeze-debugger devnet tx pay                  # send a payment, so the ledger rules run
freeze-debugger devnet timeline --last 20      # what the Freeze nodes did, by time
freeze-debugger devnet snapshot list           # saved chains that rewind can start from
```

To use a devnet on another machine, run the UI there over SSH:

```bash
ssh -t HOST 'cd ~/freeze/dwarf && .venv-freeze/bin/python dwarf/freeze-debugger devnet attach'
```

`devnet profiles` lists the other devnet layouts; `devnet up --profile NAME` starts one.
The full reference is [`dwarf/freeze/docs/GUIDE.md`](dwarf/freeze/docs/GUIDE.md).

## Stop, start again, update

```bash
freeze-debugger devnet down            # stop the devnet and delete its chain data
freeze-debugger devnet up --frozen     # start a fresh one (bootstraps again, about 15 minutes)

# Update Freeze
cd ~/freeze/dwarf-freeze && git pull
cd ~/freeze && rsync -a --exclude .git --exclude /README.md --exclude /.gitignore dwarf-freeze/ dwarf/
cd ~/freeze/dwarf && freeze-debugger devnet down
freeze-debugger build --static --source dwarf/testplease/amaru/v10.11.20260925
freeze-debugger devnet up --frozen
```

## Remove

```bash
cd ~/freeze/dwarf
freeze-debugger devnet down                                   # devnet containers, network and volumes
freeze-debugger stop --network preprod --remove-data          # only if you used `run`; once per network
docker rmi $(docker images -q 'dwarf/freeze-amaru')           # the node images
docker rmi $(docker images -q 'dwarf/freeze-toolchain')       # only present if you used `port` or `--debug-info`
# build records, tokens, recorded sessions, saved chains (those are owned by root, hence Docker)
docker run --rm -v ~/.local/share/dwarf:/d python:3.12-slim rm -rf /d/freeze
rm -rf ~/freeze                                               # both checkouts and the Python environment
```

Check that nothing is left:

```bash
docker ps -a --filter name=dwarf-control --filter name=dwarf-freeze
docker images | grep dwarf/freeze
ls ~/.local/share/dwarf/freeze ~/freeze
```

Each command should print nothing, or "No such file or directory". The build cache is left
in Docker; `docker builder prune` removes it, together with **all** other unused build cache.
The devnet's other images (`ghcr.io/intersectmbo/cardano-node` and the like) are DWARF's and
are left in place.

## Public networks

Freeze can also run a node on preprod, preview or mainnet. Only preprod has been tested.
This uses the ordinary build, not the `--static` one the devnet needs:

```bash
freeze-debugger build --source dwarf/testplease/amaru/v10.11.20260925

# Start the node, frozen, and open the UI on it
freeze-debugger run --network preprod

# Reopen the UI later, and stop the node
freeze-debugger attach --network preprod
freeze-debugger stop --network preprod
```

On a public network, use Freeze to observe and to tamper with what your own node receives.
Do not use it to send altered traffic to other people's nodes; use the devnet for that.

## Attach to a node that is already running

The UI can open on any running node built with the Freeze agent and started with
`DWARF_FREEZE_LISTEN` set, whoever started it:

```bash
freeze-debugger attach --host 127.0.0.1 --port 7373 --token-file FILE
```

The agent is remote control of a node and is published on localhost only. For a node on
another machine, use an SSH tunnel (`ssh -L 7373:127.0.0.1:7373 HOST`) instead of exposing
the port.

For non-interactive interception, `freeze-debugger script SCRIPT` runs a trusted local
Python file against held items. **The script is not sandboxed.** See
[`dwarf/freeze/README.md`](dwarf/freeze/README.md#intercept-with-a-python-script) for the
callback decisions, filtering, recording, and safety behavior.

## What is here

| Path | What it is |
|---|---|
| `dwarf/freeze/` | the subsystem: debugger, terminal UI, devnet profiles, the agent crate and the Amaru patch |
| `dwarf/freeze-debugger` | the entry point |
| `dwarf/docs/freeze-debugger-design.md` | the design |
| `tests/test_freeze_*.py` | the tests |

Beyond holding and editing traffic, Freeze has: a broker per node (records the session, pauses
the devnet's other nodes while the node is frozen), several Freeze nodes in one window, probes
in the ledger rules, call tracing for about 2,000 functions (`trace`), writing to the chain
and ledger stores, rewind to an earlier item (`rewind`), source-line stepping
(`build --debug-info`, `source`), and `port` for other Amaru revisions. Recipes are shipped
for Amaru `v10.11.20260925` and `v10.11.20261105`; the devnet runs the first. All of it is in
section 18 of the guide.

Read next: [`dwarf/freeze/README.md`](dwarf/freeze/README.md), then the full guide in
[`dwarf/freeze/docs/GUIDE.md`](dwarf/freeze/docs/GUIDE.md).

The standalone, illustrated documentation is
[`docs/freeze-complete.html`](docs/freeze-complete.html). Its canonical source is this
repository: <https://github.com/gainsec/DWARF-freeze>.

## Tests

From the DWARF checkout, after the install:

```bash
.venv-freeze/bin/pip install pytest
.venv-freeze/bin/python -m pytest -q -p no:asyncio tests/test_freeze_*.py
```

## Without DWARF

The demo, the UI, attaching to a Freeze node that is already running, and browsing recorded
sessions work from this repository alone. The devnet, the node build and the scenarios tab
do not.

## Contributing and security

See [CONTRIBUTING.md](CONTRIBUTING.md) before proposing a change. Report security issues
using the private process in [SECURITY.md](SECURITY.md); do not publish unpatched
vulnerabilities in a public issue.

## Licensing

No license has been specified for this repository. Public visibility does not grant
permission to use, copy, modify, or redistribute the code or documentation. Licensing is
an explicit publication limitation until the project adopts terms.
