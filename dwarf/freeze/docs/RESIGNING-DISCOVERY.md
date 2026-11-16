# Edited-header re-signing for controlled devnets

This document defines the cryptographic, protocol, and verification contract for re-signing an
edited inbound Cardano header in a Freeze-managed development network. It is intentionally not a
general-purpose signing facility.

The supported implementations are pinned to Amaru releases `v10.11.20260925` (vendored baseline
commit `7a53cac`) and `v10.11.20261105` (upstream release commit `c773f848`), using toolchain
`nightly-2026-09-04`. Each release has an exported integration recipe under
`dwarf/freeze/targets/amaru/patches/<release>/manifest.json`; the older recipe is also mirrored at
the target root for backward compatibility.

## 1. Scope and threat boundary

Re-signing is available only for the closed DWARF/Freeze devnet created by `freeze-debugger
devnet up`. It may use only the producer material generated for that devnet and mounted read-only
at the fixed paths:

```text
/freeze-producers/p1/{keys,configs}/...
/freeze-producers/p2/{keys,configs}/...
/freeze-producers/p3/{keys,configs}/...
```

The implementation does not accept arbitrary key paths. A normal `freeze-debugger run`, including
a public-testnet node, reports the capability as unavailable. No secret key bytes are returned over
the Freeze Control Protocol or written to logs, debugger history, test output, or diagnostics.

The feature changes only the KES signature of an otherwise eligible producer header. It can also
reseal an edited inbound BlockFetch body beneath a previously announced signed header. It does not
create a new producer identity, operational certificate, VRF proof, leader election result, or
transaction witness.

## 2. Producer material and identity

Each generated producer configuration contains:

```text
keys/opcert.cert
keys/kes.skey
keys/vrf.skey
configs/shelley-genesis.json
```

The accepted text-envelope formats are:

| File | Envelope type | Decoded form |
|---|---|---|
| `kes.skey` | `KesSigningKey_ed25519_kes_2^6` | CBOR byte string containing 608 raw Sum6 KES bytes |
| `vrf.skey` | `VrfSigningKey_PraosVRF` | CBOR byte string containing a 32-byte seed and 32-byte public key |
| `opcert.cert` | `NodeOperationalCertificate` | operational certificate and cold issuer verification key |
| verification keys | matching verification-key type | CBOR byte string containing 32 public bytes |

Before signing, the implementation must establish all of the following:

1. Blake2b-224 of the header issuer verification key equals the mounted pool ID, and that pool is
   registered in the mounted Shelley genesis.
2. The mounted VRF seed derives the exact VRF verification key declared by the header.
3. The mounted KES key derives the exact hot KES verification key in the header's operational
   certificate.
4. The mounted operational certificate exactly matches the header's hot key, sequence number,
   KES start period, cold signature, and cold issuer key.
5. The operational-certificate cold signature verifies under the header issuer key.

Producer selection is by exact VRF public-key equality. The implementation scans only the three
fixed producer directories and fails closed if no unique identity matches.

The following fields are immutable because they identify the producer or are inputs to consensus:

- `slot`;
- `issuer_verification_key`;
- `vrf_verification_key`;
- VRF output and proof;
- every operational-certificate field.

The VRF secret is used only to confirm the mounted identity. Freeze does not recompute a VRF proof:
doing so would require the active epoch nonce, and the new output would also have to satisfy leader
selection for the pool's stake.

## 3. Cryptographic contract

### 3.1 Header encoding and hashes

Source: `crates/amaru-kernel/src/cardano/header.rs` in the pinned Amaru source.

A Conway-compatible header is encoded as:

```text
[ header_body, kes_signature ]
```

The ten-element header body is:

```text
[
  block_number,
  slot,
  prev_hash,
  issuer_verification_key,
  vrf_verification_key,
  [vrf_output, vrf_proof],
  block_body_size,
  block_body_hash,
  [kes_hot_vkey, opcert_sequence, opcert_kes_start, opcert_cold_signature],
  [protocol_major, protocol_minor]
]
```

The KES signature is 448 bytes. The header hash is Blake2b-256 of the exact encoded complete
header, including that signature. Re-signing therefore changes the header hash even when the
edited field is not itself a chain-point field.

The signing preimage is exactly Amaru's canonical `to_cbor(header.body())`. It is not the full
header and not a digest of the header body.

### 3.2 VRF input and leader values

Sources:

- `crates/amaru-ouroboros/src/vrf/mod.rs`;
- `crates/amaru-ouroboros/src/praos/header.rs`.

The Praos VRF input is:

```text
Blake2b-256(slot_as_u64_big_endian || epoch_nonce_32_bytes)
```

The proof is draft-03 ECVRF with a 32-byte seed, 32-byte public key, 80-byte proof, and 64-byte
verified output. Leader selection hashes:

```text
Blake2b-256(0x4c || vrf_output_64_bytes)
```

Nonce evolution uses the separate tag `0x4e`. Since slot and the complete VRF certificate are
immutable, re-signing does not alter the VRF input, proof, output, leader value, or epoch nonce.

### 3.3 Operational certificate

Source: `crates/amaru-ouroboros/src/praos/header.rs`.

The cold Ed25519 signature preimage is:

```text
kes_hot_vkey_32
|| opcert_sequence_u64_big_endian
|| opcert_kes_start_period_u64_big_endian
```

Amaru verifies this with the header issuer verification key. It also checks the sequence number
against the latest sequence stored for that pool. Freeze verifies and preserves the complete
certificate; it never creates or modifies one.

### 3.4 KES period, evolution, and signature

Sources:

- `crates/amaru-ouroboros/src/praos/header.rs`;
- `crates/amaru-ouroboros/src/kes/mod.rs`;
- `crates/vendor/amaru-kes-summed-ed25519/src/{traits.rs,kes.rs}`.

Amaru computes:

```text
absolute_kes_period = floor(slot / slots_per_kes_period)
relative_kes_period = absolute_kes_period - opcert_kes_start_period
```

Signing is permitted only when:

```text
opcert_kes_start_period <= absolute_kes_period
relative_kes_period < max_kes_evolution
relative_kes_period fits in u32
```

The controlled topology uses `slotsPerKESPeriod = 129600` and `maxKESEvolutions = 62`. Although
Sum6 supports 64 cryptographic periods, the configured Cardano limit is authoritative.

The raw signing key is 608 bytes. Starting from a fresh copy, append the in-memory period used by
the Amaru wrapper, call `update()` exactly `relative_kes_period` times, and sign the canonical CBOR
header-body bytes. Never reuse an evolved key across requests: edited headers may arrive out of
slot order and KES evolution is one-way. Temporary secret buffers are zeroized after use.

The narrow Amaru API addition is `amaru_ouroboros::kes::SecretKey::sign(&[u8]) -> Signature`,
delegating to the vendored KES implementation. The produced signature is verified against the
declared hot key and relative period before the header may be emitted.

### 3.5 Block body hash and size

Sources:

- `crates/amaru-kernel/src/cardano/{block.rs,raw_block.rs,network_block.rs}`;
- `crates/amaru-ledger/src/rules/block/{mod.rs,body_size.rs}`.

The body commitment is computed from the exact CBOR encodings of the four block-body components:

```text
transaction_bodies_hash = Blake2b-256(exact transaction-bodies CBOR)
transaction_witnesses_hash = Blake2b-256(exact transaction-witnesses CBOR)
auxiliary_data_hash = Blake2b-256(exact auxiliary-data CBOR)
invalid_transactions_hash = Blake2b-256(exact invalid-transactions CBOR)

block_body_hash = Blake2b-256(
    transaction_bodies_hash
    || transaction_witnesses_hash
    || auxiliary_data_hash
    || invalid_transactions_hash
)

block_body_size = sum(the four exact component CBOR lengths)
```

The terminal inspector shows both values after an inbound `MsgBlock` edit. Because Cardano
announces and validates the header before fetching the block, the edited commitment cannot be
inserted retroactively. A body mutation therefore uses a two-pass/replay workflow: first obtain the
edited body's commitment, then edit the matching ChainSync header to announce that hash and size,
and finally replay the same body edit when BlockFetch returns the original block.

For every mapped block Freeze requires both the signed header hash and size to equal the returned
body, whether or not the body bytes were edited. It also requires an edited BlockFetch message to
retain the original producer header until the coordinator atomically substitutes the exact
pre-announced signed header. A body edit without that mapping, one that changes its embedded
header, or a size-only header edit that does not match the body is dropped.

## 4. Validation and protocol data flow

### 4.1 ChainSync gate

Complete mini-protocol messages normally pass through:

```text
network bytes
  -> amaru-protocols/src/protocol/miniprotocol.rs
  -> freeze::proto_in / freeze::proto_out
  -> FreezeProtoEffect
  -> amaru-protocols/src/freeze.rs::gate
  -> amaru_freeze::wire::gate_bytes
  -> decoded protocol state machine
```

An inbound ChainSync roll-forward contains an era-tagged header and a network tip. The Freeze gate
retains both the original and debugger-edited message. For a supported edit it validates protected
fields, selects the matching mounted producer, re-signs the canonical header body, verifies the new
signature, updates a matching tip, and records consistency mappings.

Unsupported protocols, eras, non-roll-forward messages, and untouched messages pass through
byte-exact and do not load signing material.

### 4.2 Amaru validation order

Before Praos header validation, `track_peers` checks decoding, era, peer state, parent hash, block
height, and slot relationships. Header validation then constructs assertions for:

1. registered leader VRF key;
2. VRF proof;
3. leader stake threshold;
4. operational certificate;
5. KES signature.

The assertions execute in parallel, so the first reported assertion error is not proof that every
earlier list item passed. Positive acceptance must instead show successful validated-header
storage and progress to a later block rule.

### 4.3 Descendant consistency

Changing a header signature changes its hash. Every descendant must therefore name the new parent
hash and be re-signed. Freeze tracks these ancestry rewrites per ChainSync stream and clears them on
rollback. They do not cross reconnects or leak into a stream replaying the original branch.

If the message's tip names the edited header itself, its slot, hash, and height are updated. A tip
that already names a later point is not rewritten.

### 4.4 BlockFetch consistency

The producer stores the original block under the original header hash and cannot serve the edited
point. After signing, Freeze records:

```text
edited NetworkPoint(slot, edited_header_hash)
  -> original NetworkPoint(slot, original_header_hash)
  -> exact signed edited header bytes
```

Amaru's BlockFetch initiator is a custom typestate driver and bypasses the generic mini-protocol
driver. The request translation stays at its typed wire boundary. Inbound `MsgBlock` responses are
re-encoded as complete messages and sent through the same Freeze gate used by other protocols:

```text
BlockFetch RequestRange
  -> freeze::blockfetch_request snapshots the selected branch's original-to-edited points
  -> retain that snapshot and the translated producer range in the typed in-flight request
  -> request original point from producer

typed MsgBlock response
  -> freeze::proto_in_blockfetch(immutable request snapshot) / UI or rule edit
  -> resign::Coordinator::blockfetch_in_for_range(original, candidate, request context)
  -> require unchanged embedded producer header
  -> require a pre-announced mapping for any body edit
  -> select by requested edited point, original point, exact body hash, and exact body size
  -> splice exact signed edited header atomically
  -> decode back into the typed BlockFetch state machine
  -> deliver block under edited identity
```

Mappings are process-global because fetch scheduling may choose any producer that stores the
original block. The consensus fetch stage selects up to three peers (`MAX_FETCH_PEERS`), the manager
fans the same range out to their independent connection stages, and valid later responses remain
ordinary stragglers after another peer has delivered first. When a typed range is dispatched, the
coordinator walks backward from its edited
`through` point through the signed parent hashes and snapshots the exact original-to-edited mapping
for the selected branch within the producer slot range. Each pipeline instance carries that
immutable snapshot and the translated producer range. This binds endpoint and intermediate
responses to the same request, preserves distinct signed headers that share one body commitment,
and remembers an expected identity after another peer has answered. Every peer pipeline receives a
distinct active request identity. A point is consumed at most once for that identity, but the global
signed-header mapping remains available to other already-issued peer requests and later refetches.
The pipeline releases only its own replay state at `NoBlocks` or `BatchDone`; it never revokes a
concurrent straggler's request. An all-original request has an empty snapshot, so a stale edited
mapping for an intermediate block is ignored and left available. An edited request whose expected
mapping is missing, belongs to a finished request, or was already used by that same request is
rejected rather than returned under the producer identity. Each mapping records both original and
signed parent hashes. If a new request encounters a missing parent mapping where those hashes
differ, its rewritten branch is incomplete and the whole request fails closed; a matching parent
hash marks the legitimate boundary between an edited suffix and unchanged earlier blocks. Only a
body-only helper call without request identity uses commitment matching; one exact match is accepted
but two or more are rejected as ambiguous, and key order is never a selector. The coordinator holds
its mapping and active-request mutexes from selection through header substitution, rewritten-hash
verification, and per-request consumption. Returned headers, request identities, body commitments,
message types, and rewritten hashes are checked exactly. A rewrite error leaves the mapping and
request slot available and the typed fetch waiting without requesting another block; neither the
original nor a partially rewritten block is forwarded.

## 5. Implementation boundary and controls

`amaru-protocols::resign` owns key-envelope parsing, identity validation, KES-period calculation,
key evolution, signing, verification, secret zeroization, ancestry, and BlockFetch mapping.
`amaru-freeze` owns only non-secret availability/enabled state and control-protocol methods.

Control-server startup is atomic with agent visibility: token validation, socket bind, runtime
creation, listener conversion, and worker-thread creation complete before the process-wide agent
is installed. The worker cannot accept requests until that exact agent is installed and the server
is activated. Any startup failure leaves hooks disabled, including when start-frozen was requested.

The feature is enabled by default for shipped `devnet up` profiles. Controls are:

- `devnet up --resign` for explicit enablement;
- `devnet up --no-resign` for the stale-signature negative-control mode;
- `resign.get` and `resign.set { enabled: bool }` over FCP;
- the **Re-sign edits** toggle in the terminal UI.

Restart, snapshot restore, and devnet ingest preserve the persisted effective setting. Enabling at
runtime still fails closed unless the fixed mounted material is present and matches the header.
Disabling clears consistency mappings and forwards edited bytes with their stale signature.

For any enabled supported edit, emission is atomic: either identity validation, descendant/tip
updates, KES signing, signature verification, and mapping all succeed, or no edited header is
emitted. Failures are reported as non-secret `resign_error` events.

## 6. Required negative cases

The implementation fails closed for:

- missing config directory or required file;
- wrong text-envelope type, malformed JSON/hex/CBOR, or trailing CBOR;
- KES payload not exactly 608 bytes;
- VRF payload not exactly 64 bytes or a seed/public mismatch;
- malformed operational certificate or genesis;
- unsupported header encoding;
- issuer or pool absent from the mounted genesis;
- cold key, VRF key, KES hot key, pool ID, or certificate mismatch;
- invalid operational-certificate cold signature;
- a change to slot, issuer, VRF key/output/proof, or any certificate field;
- a slot before the certificate's KES start period;
- a relative KES period outside `maxKESEvolutions` or `u32`;
- KES update, signing, or post-sign verification failure;
- ancestry state crossing a ChainSync reconnect;
- a BlockFetch response that does not match the mapped original point;
- an edited header body hash that differs from the returned block body;
- a signed header body size that differs from the returned body, including an unchanged body;
- multiple same-commitment mappings when the returned block cannot be bound to one requested
  edited point;
- a repeated response after that same active request used its endpoint or intermediate mapping;
- a response carrying an absent, finished, or otherwise inactive request identity;
- an all-original typed range attempting to substitute a stale intermediate edited mapping;
- an edited body without a pre-announced signed header mapping;
- an edited body whose size differs from the pre-announced signed size;
- a body edit that also changes the embedded producer header;
- a BlockFetch edit that changes the mini-protocol message type;
- malformed ChainSync, BlockFetch, header, or block encoding.

## 7. Verification

### 7.1 Automated tests

Run from the DWARF checkout and pinned Amaru source as appropriate:

```text
pytest -q tests/test_freeze_*.py

cargo +nightly-2026-09-04 test -p amaru-freeze
cargo +nightly-2026-09-04 test -p amaru-ouroboros
cargo +nightly-2026-09-04 test -p amaru-protocols
cargo +nightly-2026-09-04 test -p amaru-pure-stage --test freeze

cargo +nightly-2026-09-04 fmt --all --check
cargo +nightly-2026-09-04 clippy \
  -p amaru-freeze -p amaru-ouroboros -p amaru-protocols -p amaru-pure-stage \
  --all-targets -- -D warnings

git -C <amaru-v10.11.20260925> apply --check \
  <dwarf>/dwarf/freeze/targets/amaru/patches/v10.11.20260925/0001-freeze-hooks.patch
git -C <amaru-v10.11.20261105> apply --check \
  <dwarf>/dwarf/freeze/targets/amaru/patches/v10.11.20261105/0001-freeze-hooks.patch
```

The verified suites pass with these summarized results:

- 270 integrated Freeze Python tests;
- the full `amaru-freeze` suite for each release (53 unit tests and seven trace-function tests),
  including missing/empty-token rejection, failed-bind startup with a frozen agent, and a
  concurrent resolve/thaw regression;
- the full `amaru-ouroboros` suite for each release (14 unit tests on `v10.11.20260925`, 60 on
  `v10.11.20261105`, and two validation tests on each);
- the full `amaru-protocols` suite for each shipped release, including twenty-two focused re-signing
  integration tests per release;
- the full `amaru-pure-stage` suite for each release, including the Freeze runtime test;
- formatting, strict Clippy, and baseline patch application clean for both shipped releases.

Focused regressions cover toggle on/off, untouched byte identity, exact signing preimages,
period-zero and evolved KES signatures, protected fields, missing/malformed/mismatched material,
tip updates, descendant re-signing, reconnect isolation, cross-peer fetch, request translation,
block header substitution, exact edited-body hash and size, missing pre-announcement, protected
embedded headers, message-type changes, unchanged-body size rejection, multiple mappings for one
original point, exact commitment selection, same-commitment request binding, ambiguity rejection,
explicit-original request preservation, consumed-mapping replay rejection, and body-commitment
rejection. Multi-block cases cover all-original intermediate preservation and fail-closed replay of
a consumed intermediate mapping within one edited request. Multi-peer cases prove that independent
concurrent requests and later refetches may use the retained mapping, that finishing one request does
not revoke a straggler, and that a response for the finished request is rejected.

### 7.2 Live devnet acceptance

Use a fresh closed devnet and the generated read-only producer mounts. Do not reuse public-network
keys or expose the control port beyond localhost.

On-mode body-edit procedure:

1. Start `freeze-debugger devnet up` without overriding the default re-sign setting.
2. Confirm status reports re-signing enabled and available.
3. Forward an untouched header as a byte-preservation regression.
4. Edit a held inbound BlockFetch `MsgBlock` transaction body and record the inspector's body hash
   and size.
5. Rewind or replay from before its matching ChainSync roll-forward, put that exact hash and size
   in the header, and forward it.
6. Repeat the identical body edit when BlockFetch arrives.
7. Confirm the header passes VRF and KES, BlockFetch selects the exact matching mapping, and the
   delivered block contains both the edited body and its pre-announced signed header. An unchanged
   transaction witness may then reach and fail its later ledger or witness rule.

Fail-closed size procedure:

1. Re-sign a header whose body hash is correct but whose declared body size is wrong.
2. Leave the returned BlockFetch body unchanged.
3. Confirm Freeze reports a `resign_error` naming the body size and does not deliver either the
   original block or a partially rewritten block.

Off-mode procedure:

1. Disable re-signing explicitly with `--no-resign` or the runtime toggle.
2. Apply the same header commitment edit to a later inbound header.
3. Confirm validation stops at `Assert > KesSignature > InvalidKesSignature` and does not reach
   block-body validation.

For a transaction-body experiment, make the body edit once while inspecting the held inbound
BlockFetch `MsgBlock` and record the displayed hash and size. Rewind or replay from before the
matching ChainSync announcement, put those two values in that roll-forward header, and repeat the
identical body edit when BlockFetch arrives. Acceptance means the delivered block contains the
edited transaction and the exact signed header. Unchanged transaction witnesses are expected to
fail the appropriate later ledger or witness rule; reaching that rule is the useful result. This
procedure does not claim to repair a transaction witness.

Always finish with:

```text
freeze-debugger devnet down
```

and confirm no containers from the Freeze devnet project remain running.

## 8. Limitations

- Conway-compatible inbound roll-forward headers and typed inbound BlockFetch `MsgBlock` bodies
  only; unsupported eras and message shapes pass through unchanged.
- Slot, issuer, VRF certificate, and operational certificate are immutable. VRF proofs and leader
  values are never recomputed.
- Transaction witnesses, scripts, auxiliary-data signatures, and other transaction-level
  authentication are not recreated. A fee/output/script edit can reach those later ledger rules,
  but it is not made transaction-valid.
- Body resealing requires a prior matching signed-header announcement and therefore a two-pass or
  replay workflow. Freeze does not delay an already announced header while waiting for a later body
  edit.
- Outbound scripted-peer blocks, UI-injected new blocks or headers, and direct chain-store writes
  are not signed or resealed.
- KES evolution starts from the generated base key for each request, is forward-only, and is
  bounded by genesis `maxKESEvolutions`. Evolved keys are not persisted.
- Consistency mappings are process-local and clear on disable or restart. After restart, the peer
  must replay the original ChainSync announcement before a resealed body can be accepted. Session
  files do not persist signing mappings or secret material.
- The later asynchronous BlockFetch/ledger result is visible in node effects but may not be
  attributed to the intercepted ChainSync item by FCP cause tracking.
- The feature is unavailable to generic `run`, accepts no external key path, and is unsupported
  for public networks or third-party keys.
