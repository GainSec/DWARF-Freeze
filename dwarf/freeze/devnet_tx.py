"""Send transactions on the closed devnet, so the ledger's rules and scripts have something to do.

Everything is done with what the devnet already has: the `cardano-cli` in a block producer's
container, that producer's node socket, and the funded keys DWARF's configurator generated for
this devnet (they stay in the configurator's stopped container). Freeze brings no keys and no
wallet code, and none of this works anywhere but on that devnet.
"""

from __future__ import annotations

import json
import shlex
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

from . import devnet
from .devnet import DevnetError

WORK = "/tmp/freeze-tx"
KEYS = "/tmp/testnet/utxos/keys"
SCRIPT = Path(__file__).resolve().parent / "scripts" / "always-succeeds-v3.plutus"


class Wallet:
    """One funded key of the devnet, used through a producer's container."""

    def __init__(self, data_root: Path, via: str = "p1", key: int = 1) -> None:
        self.data_root, self.via, self.key = data_root, via, key
        info = devnet.current(data_root, direct=True)
        if info is None:
            raise DevnetError("the devnet is not running; start it with `freeze-debugger devnet up`")
        network = str(info.get("network") or devnet.active(data_root).network)
        if not network.startswith("testnet_"):
            raise DevnetError(f"transactions are only sent on the closed devnet, not on {network}")
        self.magic = network.split("_", 1)[1]
        self.container = self._container(via)
        self._prepared = False

    def _container(self, service: str) -> str:
        result = devnet._compose(self.data_root, "ps", "-aq", service, capture=True)
        found = result.stdout.strip().splitlines()
        if not found:
            raise DevnetError(f"the devnet has no `{service}` container")
        return found[0]

    def run(self, script: str, check: bool = True) -> str:
        """Run a shell line in the producer's container and return what it printed."""
        result = subprocess.run(["docker", "exec", self.container, "bash", "-ec", script], capture_output=True, text=True)
        if check and result.returncode != 0:
            # cardano-cli says what is wrong first and where in its own code afterwards.
            said = (result.stderr or result.stdout).strip()
            raise DevnetError(said.split("\nCallStack")[0].strip()[:900] or "the command failed")
        return result.stdout

    def cli(self, *words: str, check: bool = True) -> str:
        return self.run("cardano-cli latest " + " ".join(shlex.quote(word) for word in words), check)

    def prepare(self) -> None:
        """Bring the devnet's funded key and the test script into the producer's container."""
        if self._prepared:
            return
        configurator = self._container("configurator")
        with tempfile.TemporaryDirectory(prefix="freeze-tx-") as scratch:
            copied = subprocess.run(["docker", "cp", f"{configurator}:{KEYS}/.", scratch], capture_output=True, text=True)
            if copied.returncode != 0 or not (Path(scratch) / f"genesis.{self.key}.skey").is_file():
                raise DevnetError("this devnet's configurator kept no funded keys Freeze can use "
                                  f"({copied.stderr.strip()[-200:] or f'no genesis.{self.key}.skey'})")
            (Path(scratch) / SCRIPT.name).write_text(SCRIPT.read_text(encoding="utf-8"), encoding="utf-8")
            self.run(f"rm -rf {WORK} && mkdir -p {WORK}")
            if subprocess.run(["docker", "cp", f"{scratch}/.", f"{self.container}:{WORK}"], capture_output=True).returncode != 0:
                raise DevnetError("could not copy the keys into the producer's container")
        self.skey = f"{WORK}/genesis.{self.key}.skey"
        self.address = self.cli("address", "build", "--payment-verification-key-file", f"{WORK}/genesis.{self.key}.vkey",
                                "--testnet-magic", self.magic).strip()
        self._prepared = True

    def node(self) -> list[str]:
        return ["--socket-path", "/state/node.socket", "--testnet-magic", self.magic]

    def utxos(self, address: str) -> dict[str, Any]:
        return json.loads(self.cli("query", "utxo", "--address", address, *self.node(), "--output-json") or "{}")

    def plain(self, least: int = 0) -> list[tuple[str, int]]:
        """This key's outputs that hold only lovelace, largest first."""
        found = [(name, entry["value"]["lovelace"]) for name, entry in self.utxos(self.address).items()
                 if set(entry["value"]) == {"lovelace"} and not entry.get("referenceScript") and entry["value"]["lovelace"] >= least]
        return sorted(found, key=lambda pair: -pair[1])

    def send(self, name: str, build: list[str]) -> str:
        """Build (the node works out fee and script budgets), sign and submit. Returns the transaction id."""
        raw, signed = f"{WORK}/{name}.raw", f"{WORK}/{name}.signed"
        self.cli("transaction", "build", *build, "--change-address", self.address, "--out-file", raw, *self.node())
        self.cli("transaction", "sign", "--tx-body-file", raw, "--signing-key-file", self.skey, "--testnet-magic", self.magic,
                 "--out-file", signed)
        self.cli("transaction", "submit", "--tx-file", signed, *self.node())
        answer = self.cli("transaction", "txid", "--tx-file", signed).strip()
        return json.loads(answer)["txhash"] if answer.startswith("{") else answer

    def wait_for(self, address: str, transaction: str, timeout: float = 120.0) -> str:
        """The first output of ``transaction`` at ``address``, once a block holds it."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            for name in self.utxos(address):
                if name.startswith(transaction + "#"):
                    return name
            time.sleep(3)
        raise DevnetError(f"transaction {transaction} was not in a block within {timeout:.0f}s")


def pay(wallet: Wallet, lovelace: int = 2_000_000) -> str:
    """A plain payment from the funded key to itself."""
    wallet.prepare()
    funds = wallet.plain(lovelace + 2_000_000)
    if not funds:
        raise DevnetError("the devnet's funded key has nothing left to spend")
    sent = wallet.send("pay", ["--tx-in", funds[0][0], "--tx-out", f"{wallet.address}+{lovelace}"])
    # Until a block holds it, the node still lists what it spent; the next transaction must not pick that.
    wallet.wait_for(wallet.address, sent)
    return sent


def script(wallet: Wallet, lovelace: int = 5_000_000, log: Any = print) -> tuple[str, str]:
    """Lock an output at a script that always succeeds, then spend it, which makes every node
    evaluate the script. Returns the two transaction ids."""
    wallet.prepare()
    plutus = f"{WORK}/{SCRIPT.name}"
    address = wallet.cli("address", "build", "--payment-script-file", plutus, "--testnet-magic", wallet.magic).strip()
    funds = wallet.plain(lovelace + 10_000_000)
    if not funds:
        raise DevnetError("the devnet's funded key has nothing left to spend")
    locked = wallet.send("lock", ["--tx-in", funds[0][0], "--tx-out", f"{address}+{lovelace}", "--tx-out-inline-datum-value", "42",
                                  "--tx-out", f"{wallet.address}+6000000"])
    log(f"locked {lovelace:,} lovelace at the script: {locked}; waiting for a block…")
    at_script = wallet.wait_for(address, locked)
    collateral = next((name for name, amount in wallet.plain() if name.startswith(locked + "#") and amount == 6_000_000), None)
    if collateral is None:
        raise DevnetError("the output set aside as collateral is not there")
    spent = wallet.send("spend", ["--tx-in", at_script, "--tx-in-script-file", plutus, "--tx-in-inline-datum-present",
                                  "--tx-in-redeemer-value", "42", "--tx-in-collateral", collateral])
    wallet.wait_for(wallet.address, spent)
    return locked, spent
