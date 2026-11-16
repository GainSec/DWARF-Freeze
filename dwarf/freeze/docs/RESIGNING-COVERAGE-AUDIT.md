# Editing and dependent-value repair coverage audit

Date: 2026-11-16. This is the Phase 1 gate for extending Freeze editing. It records what the
source does, not what older documentation says it does.

## Baseline and release identity

- The public Freeze repository begins with fresh history; earlier development refs are not
  part of this audit's publication provenance.
- Supported Amaru recipes are `v10.11.20260925`, public tag commit
  `eaf8ac3fa309f2a174c045454c276194c9312b8d`, and `v10.11.20261105`, public tag commit
  `c773f8486e5b64284506824daa3974020e0c8bf6`. Both patches apply cleanly to those exact tags.
  `RESIGNING-DISCOVERY.md` calls the first one vendored commit `7a53cac`; that object is not in
  the current public Amaru repository and the shipped patch actually applies to `eaf8ac3...`.
- Both manifests pin `nightly-2026-09-04`. The root compatibility patch is byte-identical to
  the `v10.11.20260925` patch (SHA-256
  `0e9251cc39c7a52ff751848d4084c969347cd79c379d7c6b54f9c0ff8f5a42aa`). The later patch is
  `5de0d8e3a4604a090b8eef9733141d0bdd2c2c9a395f2ef43748d43fc5ef8a6d`. The later recipe
  carries its own crate overlay; only `probes.rs` differs from the base overlay.

Evidence: `git rev-parse`, public Amaru tag resolution, clean `git apply --check`, recipe
manifests, `targets/amaru/spec.yaml`, and semantic inspection of both applied source trees.

## Repair contract

An edit may be emitted only as one of these states:

1. `repaired`: every known dependent value is recomputed, and secret-dependent values are
   regenerated only from a uniquely matched controlled-devnet identity;
2. `stale-negative-control`: validation material is deliberately stale and the operator selected
   that mode explicitly;
3. `refused`: emission is stopped with the exact dependency or identity reason.

`non-cryptographic` is a ledger classification, not a fourth emission state: such an edit is
`repaired` when its deterministic serialization dependencies are rebuilt by the typed encoder.
Unknown or ambiguous dependency graphs fail closed.

## Source-level findings

The quoted narrow statement was true at the audited baseline, but is no longer the complete
implemented behavior in this candidate. Applied Amaru source routes complete mini-protocol
messages through `amaru-protocols/src/freeze.rs`. With repair enabled it invokes the coordinator
for inbound and outbound ChainSync (`proto_in/2`, `proto_out/2`), initiator BlockFetch range
translation and inbound blocks, and responder-side mux/request-bound BlockFetch requests and
outbound blocks. `Resigner::resign_header` protects
slot, issuer, VRF key/result, and the complete operational certificate, then checks the mounted
producer identity, operational-certificate signature, KES key and period before replacing the KES
signature. The coordinator keys BlockFetch consumption by typed request identity and exact point,
body hash, and body size. Its mappings survive independent requests; per-request consumption does
not.

Other protocols still bypass that coordinator. `gate_bytes` can canonicalize an edited JSON payload
but does not repair protocol commitments. `amaru-pure-stage/src/freeze.rs` reconstructs typed
values and effects. Reconstruction failure now emits a typed `item.done` rejection with
`edit_reconstruction_failed` and terminates or skips the exact effect; it never silently runs the
original effect or returns the original result. Raw replacement of typed internal input/result is
rejected before release with `typed_raw_replacement_unsupported`; the authenticated FCP error
envelope carries that stable code and the item remains held. Chain-store writes validate
address/hash/body commitment unless `force:true`; block writes
also require an exact pre-existing header and refuse repair requests before writing because this API
has no mux/request identity and the store has no atomic header-plus-body operation. Ledger writes
use typed decoding and transactions, with account writes requiring `unsafe:true`. Those explicit
flags are real negative controls. The global re-sign toggle is not a capability description for
the many surfaces it does not repair.

Mounted signing material is limited to each producer's `kes.skey`, `vrf.skey`, `opcert.cert`, and
Shelley genesis. The devnet transaction helper separately copies `genesis.<n>.skey` from
`/tmp/testnet/utxos/keys` into a producer container to call `cardano-cli`; payment keys are not
mounted in a Freeze node, no signer-ownership index is exposed to the agent, and no transaction
signing preimage is retained there. Transaction witness regeneration therefore requires new,
explicitly scoped key material and ownership metadata. It is not feasible from the material
currently mounted in the shipped Freeze node.

Slot editing and Praos proof regeneration are distinct from KES signing. A new slot changes the
VRF input. Although producer VRF secrets are mounted, Freeze does not have a proven active epoch
nonce/stake snapshot/leader eligibility binding at this boundary; producing a proof without proving
eligibility would lie. Issuer changes require another cold identity and matching operational
certificate. Operational-certificate changes require the cold signing key, which is not mounted.
KES-period fields are part of the operational certificate and cannot be edited independently;
only the KES signature for the existing certificate and slot is regenerable.

## Complete surface inventory

| Stable ID | Source and call graph | Direction / boundary | Mutable data and dependencies | Current evidence and available material | Classification |
|---|---|---|---|---|---|
| `CARDANO-TREE` | `tui.py:action_edit` -> `cbor_tree.edit` -> `cardano.edit_impact` -> `client.replace` | operator -> held raw item | Exact CBOR widths/embedded byte-string lengths are repaired. Protocol hashes, IDs, signatures and proofs are surface-specific. | Python tests prove byte-exact untouched round trips, enclosing-length changes, per-boundary classification, and refusal before emission. | `implemented` for known protocol shapes; unknown/ambiguous shapes refuse |
| `RAW-REPLACE` | FCP `item.replace(raw)` -> `Agent::resolve(ReplaceRaw/ReplaceRawStale)` -> protocol/typed gate | any editable held item | Arbitrary bytes; all semantic dependencies unknown unless the boundary classifier parses them. | Typed internal items reject raw and raw-stale verdicts before release with stable code `typed_raw_replacement_unsupported`. Protocol bytes pass a Rust emission boundary: unavailable/disabled/public repair refuses, enabled repair admits only ChainSync/BlockFetch and must succeed, and only an exact `ReplaceRawStale` verdict bypasses repair. | `implemented` fail-closed boundary plus `intentionally-stale-negative-control` only after explicit per-item authorization |
| `WIRE-IN` | `freeze_connections.rs` -> `wire::gate_bytes` -> network read | peer -> node, pre-mux/decode | Mux framing, protocol state, all message commitments/signatures. Raw chunks lack a proven complete typed message identity. | No repair coordinator and no safe typed preimage. | `cryptographically-impossible-with-current-identity` for automatic repair; explicit stale control or refuse |
| `WIRE-OUT` | node connection writer -> `wire::gate_bytes` | node -> peer, post-encode | Same as `WIRE-IN`; may contain locally produced or relayed identity with no request/source binding. | No repair coordinator. | `cryptographically-impossible-with-current-identity` for automatic repair; explicit stale control or refuse |
| `CS-IN-HEADER` | `miniprotocol` -> `proto_in` -> `Coordinator::chain_sync` -> `Resigner` | peer -> node, decoded-message boundary | Header hash, KES signature, tip point, descendant parent hashes, BlockFetch point/body mapping. Slot/issuer/VRF/opcert protected. | Implemented for Conway; exact producer/key/period checks and tests. | `implemented` |
| `CS-OUT-HEADER` | `proto_out` -> direction-qualified `Coordinator::chain_sync` -> `Resigner` | node/scripted peer -> peer | Same dependencies as inbound, scoped by `outbound:{mux}` ancestry. | Exact producer/KES identity checks; reconnect removes inbound and outbound stream state; protected fields carry stable refusal codes. | `implemented` |
| `CS-ERA` | Python ChainSync classifier -> Rust `Coordinator::chain_sync` -> `Resigner::resign_header` | both ChainSync directions | Era tag selects the header type and signing preimage; changing it invalidates the supported typed repair proof. | Python refuses changed era with `era_identity_change_forbidden`; Rust rejects an era mismatch and rejects same-era non-Conway headers as `unsupported_header_era`. No auto-repair claim survives classification or the emission gate. | `implemented` exact refusal |
| `BF-OUT-REQUEST` | typed initiator `blockfetch_request` plus `proto_out/3` | node -> peer | Requested edited point/range must translate to producer point/range and remain request-bound. | Implemented for initiator requests. | `implemented` |
| `BF-IN-BLOCK` | typed initiator -> `proto_in_blockfetch` -> coordinator | peer -> node | Embedded header, four-component body hash and size, exact request identity, one-use-per-request. | Implemented; exact point/hash/size matching, ambiguity rejection, request cleanup. | `implemented` |
| `BF-IN-REQUEST` | custom responder receives RequestRange -> `responder_blockfetch_request(mux, range)` | peer -> scripted peer | Edited range translates to producer range and creates a direction/mux-qualified one-use lease. | Implemented without a global fallback; disconnect, BatchDone, NoBlocks, error, and completion clean up the exact lease. | `implemented` |
| `BF-OUT-BLOCK` | custom responder gates StartBatch/Block/BatchDone/NoBlocks -> `blockfetch_out_for_range` | scripted peer -> peer | Exact mux/request lease, point, body commitment and pre-announced signed header. | Implemented; two-peer, multi-block, duplicate, stale/finished, restart, ambiguity and terminal-reply tests exercise the real responder boundary. | `implemented` |
| `TXSUB-IN` | `proto_in/4` | peer -> node | Transaction body hash/ID, vkey/bootstrap witnesses, script-data/auxiliary hashes, sizes and protocol state. | The typed gate refuses the entire edited transaction with `tx_witness_material_unavailable`. Payment signing keys and an exact owner map are not mounted; recomputing public hashes without the witnesses would still emit an invalid transaction. | `requires-new-key-material` with implemented exact refusal |
| `TXSUB-OUT` | `proto_out/4` | node -> peer | Same as inbound; request/reply IDs/counts must agree. | The typed gate refuses before emission; no partial deterministic-envelope repair is claimed because the witness dependency remains unsatisfied. | `requires-new-key-material` with implemented exact refusal |
| `PROTO-OTHER` | `proto_in/out` for handshake, keepalive, peer sharing, unknown | both | CBOR lengths plus protocol-specific counts/cookies/version negotiation. | Typed protocol state machine validates after emission from gate. No dedicated dependency repair. | `non-cryptographic`; typed canonical rebuild feasible, ambiguous semantic changes refuse |
| `INTERNAL-INPUT` | runtime `freeze::input` -> `rebuild` | mailbox -> stage | Serde/CBOR shape and type tag; domain invariants may include hashes/IDs not described by the runtime. | Typed reconstruction succeeds or emits `item.done` rejection and no message. | `implemented` for typed reconstruction/refusal; domain-specific signed values still require their own gate |
| `INTERNAL-ACTION` | runtime `freeze::action` -> `edit` -> effect runner | stage -> node/stage | Sends/schedules/timeouts/external effects; effect-specific IDs and domain commitments. | Malformed edits emit exact rejection and skip the original; unsupported effects return `Gated::Rejected`. | `implemented` bounded typed repair/refusal |
| `INTERNAL-RESULT` | effect response -> `freeze::result` -> `rebuild` | node -> stage | Typed response plus domain invariants. | Malformed edited result emits exact rejection and terminates rather than returning the original. | `implemented` bounded typed repair/refusal |
| `STAGE-STATE` | `state.put(stages/...)` -> queued replacement -> `rebuild` on next input | control -> in-memory stage | Entire typed state, indexes/caches/derived fields unknown outside stage implementation. | JSON-to-CBOR checked when queued; concrete rebuild may fail later and only logs. | `feasible-now` fail-closed receipt; deeper repair requires per-stage semantics |
| `INJECT-TYPED` | `item.inject` -> registered typed decoder -> mailbox | control -> stage | Type tag and concrete serde shape; no authenticated source/request identity. | Typed stages decode immediately; erased stages use a first-message witness. No cryptographic repair. | `non-cryptographic` for types without signed material; signed/ambiguous types must refuse |
| `CHAIN-HEADER-WRITE` | `state.put(chain/headers)` -> `put_header` | control -> chain store | Key slot/hash, header hash/signature, validity. | Rejects key/hash/slot mismatch unless `force:true`; does not re-sign. | coherent existing headers `implemented`; mutations `intentionally-stale-negative-control` via `force` or require devnet repair chain |
| `CHAIN-BLOCK-WRITE` | `state.put(chain/blocks)` -> `put_block` | control -> chain store | Key slot/hash, embedded header hash, body hash, component lengths. | Source falsified safe repair: no mux/request/source identity reaches this API and `ChainStore` exposes no atomic header-plus-body write. Non-force writes require the exact header already stored; `repair:true` refuses with `store_block_repair_unavailable`; `force:true` is the explicit stale control. | exact pre-write refusal `implemented`; automatic repair impossible at this boundary |
| `CHAIN-POINTER-WRITE` | `state.put(chain/best)` | control -> chain store | Tip/anchor point must name a stored header. | Checked unless `force:true`; pointer-only non-cryptographic change. | `implemented` / explicit stale control |
| `LEDGER-WRITE` | `state.put/delete(ledger/*)` -> queued ledger-thread transaction | control -> ledger RocksDB | Typed UTxO keys/outputs, pots, parameters, accounts; ledger roots/caches outside this store API. | Typed decode and transactional application. Account writes require `unsafe:true`; volatile ledger is read-only with exact reason. | `implemented` bounded writes; `intentionally-stale-negative-control` for unsafe account write |
| `EFFECT-STORE` | generic editable external effect -> store API | stage -> store | Store key/value and any application-level indexes. | Runtime only proves serde roundtrip; store implementation may validate later. | `feasible-now` typed refusal/receipt; no generic cryptographic repair |
| `REPEATER` | `repeater.edited/plan/send` -> `client.replace`/inject | recorded item -> new held item/stage | Inherits target surface; CBOR editor repairs lengths only. | No repair metadata preserved in plan. | wrapper; must propagate target surface state |
| `SCRIPT` | `scripting.run` -> callback -> `client.replace` | held item -> original boundary | Inherits target surface; validates FCP JSON syntax only. | Replacement accepted before boundary-specific outcome. | wrapper; must propagate target surface state |
| `RUN-REPLAY` | `runs.interventions` -> `Runner._act` -> replace/drop | recorded intervention -> matching held item | Inherits target surface plus identity/order matching. | Content/name matching; current coordinator covers typed inbound request identities only. | wrapper; must propagate target state and refuse ambiguity |
| `REWIND-INGEST` | snapshot restore/restart -> `Rewinder`/`Runner` | persisted node + replay | Same as replay, plus restarted coordinator maps are empty. | Restart intentionally loses in-memory signing maps; replay must reconstruct announcements before bodies. | `feasible-now`; executable restart/stale-map tests required |
| `RULES-PAYLOADS` | `rules.Engine`/payload lists -> repeated replacements | held item -> original boundary | Inherits target surface for every generated value. | Results record field changes, not repair state. | wrapper; must propagate target state |
| `SLOT` | header field edit | consensus boundary | VRF input/proof/output, leader threshold, KES period/signature, header hash/ancestry. | Epoch nonce/stake eligibility proof not bound at edit boundary. | `cryptographically-impossible-with-current-identity` |
| `ISSUER` | header issuer edit | consensus boundary | Pool ID, VRF/KES/opcert identities and cold signature. | No matching cold identity authorization for arbitrary change. | `cryptographically-impossible-with-current-identity` |
| `OPCERT` | header operational certificate edit | consensus boundary | Cold-key signature, sequence state, KES hot key/start. | Cold signing key is not mounted. | `requires-new-key-material` |
| `KES-SIGNATURE` | `Resigner::resign_header` | controlled devnet only | Canonical header-body CBOR and relative KES period. | Mounted KES material plus exact identity/period checks. | `implemented` |
| `PRAOS-PROOF` | header VRF edit | consensus boundary | Epoch nonce, slot, VRF proof/output and leader eligibility. | VRF key alone is insufficient to prove honest eligibility. | `cryptographically-impossible-with-current-identity` |
| `TX-WITNESS` | transaction witness-set edit | TxSubmission/block body | Transaction-body hash, exact required signer set, vkey/bootstrap/script witnesses. | Genesis payment keys exist elsewhere but are not mounted or ownership-indexed in the Freeze node. | `requires-new-key-material` |

## Baseline documentation, UI, code, patch, and test gaps

These are the Phase 1 gaps found before implementation. The disposition immediately below keeps
the historical audit distinct from the still-open ledger entries.

1. The guide correctly states the narrow cryptographic scope, but the TUI exposes one global
   “Re-sign edits” switch beside every held kind. It does not say whether the selected edit was
   repaired, deliberately stale, unsupported, or refused.
2. `RESIGNING-DISCOVERY.md` names an unverifiable old baseline SHA. Manifests name releases but
   carry no source-commit or patch checksum field.
3. The recipe capability strings claim only header/body re-signing, accurately, but the UI and
   generic `editable` bit do not restrict unrelated cryptographic edits.
4. Script, repeater, rule, replay, rewind, injection, and state-write APIs preserve no repair
   outcome. Their tests assert replacement delivery, not dependency validity.
5. Runtime typed edit failures and byte-payload re-encoding failures log “original kept” after
   `item.replace` returned success. That is silent partial behavior from the operator's point of
   view and does not meet the three-state contract.
6. Tests cover inbound re-sign/reseal, protected slot/identity fields, malformed keys, concurrency,
   duplicate commitments, request cleanup, and public unavailability. They do not cover outbound
   scripted-peer repair, responder request identity, restart maps across every wrapper, transaction
   refusal, or per-edit UI repair reports.
7. The scripted-peer profile explicitly disables re-signing. Its example changes outbound
   ChainSync height and therefore intentionally creates a stale signature, but neither profile nor
   inspector labels it as a negative control.
8. Generated patches contain the real protocol coordinator and tests; the overlay crates contain
   the generic agent/wire behavior. Reviewing only either half misses the surface split. Both
   release patches currently apply, but semantic equivalence is not asserted by a cross-release
   test.

Phase 2 closed gap 5 for runtime input/action/result reconstruction, implemented outbound
ChainSync and responder BlockFetch at their typed production boundaries, and closed chain-block
write ambiguity with an exact pre-write refusal after source inspection falsified safe repair.
TxSubmission edits now refuse in the Rust protocol gate with `tx_witness_material_unavailable`;
slot, issuer, opcert and VRF mutations have stable codes in both the cryptographic gate and UI.
Direct arbitrary scripting and queued stage-state semantics remain ledgered gaps. Public-network
signing and protected-consensus exclusions remain intentional.

## Machine-readable coverage ledger

```yaml
schema: dwarf-freeze-edit-coverage-v1
baseline: 5500100fb36f607e28b07b126f13ee47e05d98b7
releases:
  v10.11.20260925: eaf8ac3fa309f2a174c045454c276194c9312b8d
  v10.11.20261105: c773f8486e5b64284506824daa3974020e0c8bf6
surfaces:
  - {id: CARDANO-TREE, status: implemented, repair: [exact-cbor-widths, embedded-lengths, classify-target, refuse-unknown], tests: [PY-CARDANO-IMPACT-001], evidence: [dwarf/freeze/cbor_tree.py, dwarf/freeze/cardano.py, tests/test_freeze_cardano.py, tests/test_freeze_cbor_tree.py]}
  - {id: RAW-REPLACE, status: implemented, repair: [classify-boundary, rust-protocol-emission-gate, typed-raw-refusal-before-release, request-bound-stale-verdict], tests: [PY-RAW-MODE-001, RS-STALE-BYPASS-001, RS-PROTOCOL-BOUNDARY-001, RS-PROTOCOL-ALIAS-001, RS-PROTOCOL-TRANSITION-001, RS-TYPED-RAW-001], evidence: [dwarf/freeze/client.py, dwarf/freeze/targets/amaru/crate/amaru-freeze/src/agent.rs, dwarf/freeze/targets/amaru/crate/amaru-freeze/src/server.rs, crates/amaru-protocols/src/freeze.rs, crates/amaru-pure-stage/src/freeze.rs]}
  - {id: WIRE-IN, status: intentionally-stale-negative-control, repair: [request-bound-explicit-stale-mode], tests: [RS-WIRE-REFUSE-001, RS-STALE-BYPASS-001], evidence: [crates/amaru-node/src/freeze_connections.rs, crates/amaru-protocols/src/freeze.rs]}
  - {id: WIRE-OUT, status: intentionally-stale-negative-control, repair: [request-bound-explicit-stale-mode], tests: [RS-WIRE-REFUSE-002, RS-STALE-BYPASS-001], evidence: [crates/amaru-node/src/freeze_connections.rs, crates/amaru-protocols/src/freeze.rs]}
  - {id: CS-IN-HEADER, status: implemented, repair: [protect-consensus-inputs, match-producer, kes-resign, rewrite-tip, record-ancestry, record-body-map], tests: [RS-RESIGN-IN-001], evidence: [crates/amaru-protocols/src/resign.rs, crates/amaru-protocols/tests/resign.rs]}
  - {id: CS-OUT-HEADER, status: implemented, repair: [direction-qualified-stream-ancestry, reconnect-cleanup, match-producer, kes-resign, record-body-map], tests: [RS-RESIGN-OUT-001, RS-PROTECTED-CODES-001, RS-PROTOCOL-ALIAS-001, RS-PROTOCOL-TRANSITION-001], evidence: [crates/amaru-protocols/src/freeze.rs, crates/amaru-protocols/src/resign.rs, crates/amaru-protocols/tests/resign.rs]}
  - {id: CS-ERA, status: implemented, repair: [era-identity-invariant, reject-era-mismatch, reject-unsupported-same-era], tests: [PY-ERA-INVARIANT-001, RS-ERA-INVARIANT-001], evidence: [dwarf/freeze/cardano.py, tests/test_freeze_cardano.py, crates/amaru-protocols/src/resign.rs, crates/amaru-protocols/tests/resign.rs]}
  - {id: BF-OUT-REQUEST, status: implemented, repair: [request-id, edited-to-producer-range], tests: [RS-BF-REQUEST-001], evidence: [crates/amaru-protocols/src/resign.rs, crates/amaru-protocols/src/blockfetch/initiator.rs]}
  - {id: BF-IN-BLOCK, status: implemented, repair: [request-id, exact-point, exact-body-hash, exact-body-size, signed-header-substitution], tests: [RS-BF-IN-001, RS-BF-CONCURRENCY-001], evidence: [crates/amaru-protocols/src/resign.rs, crates/amaru-protocols/tests/resign.rs]}
  - {id: BF-IN-REQUEST, status: implemented, repair: [direction-mux-request-id, edited-to-producer-range, raii-terminal-cleanup], tests: [RS-BF-RESPONDER-001, RS-BF-LEASE-001], evidence: [crates/amaru-protocols/src/freeze.rs, crates/amaru-protocols/src/blockfetch/responder.rs, crates/amaru-protocols/src/resign.rs]}
  - {id: BF-OUT-BLOCK, status: implemented, repair: [direction-mux-request-id, exact-point, exact-body-commitment, signed-header-substitution, terminal-cleanup], tests: [RS-BF-RESPONDER-002, RS-BODY-SIZE-001, RS-PROTOCOL-ALIAS-001, RS-PROTOCOL-TRANSITION-001], evidence: [crates/amaru-protocols/src/blockfetch/responder.rs, crates/amaru-protocols/src/resign.rs, crates/amaru-protocols/tests/resign.rs]}
  - {id: TXSUB-IN, status: requires-new-key-material, repair: [canonical-tx-body-id, signer-ownership, witnesses], tests: [RS-TX-REFUSE-001], evidence: [dwarf/freeze/devnet_tx.py, crates/amaru-protocols/src/freeze.rs]}
  - {id: TXSUB-OUT, status: requires-new-key-material, repair: [request-bound-ids-counts, canonical-tx-body-id, signer-ownership, witnesses], tests: [RS-TX-REFUSE-002], evidence: [dwarf/freeze/devnet_tx.py, crates/amaru-protocols/src/freeze.rs]}
  - {id: PROTO-OTHER, status: non-cryptographic, repair: [typed-canonical-cbor, protocol-semantic-validation, refuse-unknown], tests: [PY-PROTO-OTHER-001], evidence: [dwarf/freeze/cardano.py, crates/amaru-protocols/src/freeze.rs]}
  - {id: INTERNAL-INPUT, status: implemented, repair: [typed-rebuild, explicit-item-done-refusal, no-original-fallback], tests: [RS-TYPED-RECEIPT-001], evidence: [crates/amaru-pure-stage/src/freeze.rs, crates/amaru-pure-stage/tests/freeze.rs]}
  - {id: INTERNAL-ACTION, status: implemented, repair: [bounded-effect-fields, typed-rebuild, explicit-item-done-refusal, skip-original], tests: [RS-EFFECT-RECEIPT-001], evidence: [crates/amaru-pure-stage/src/freeze.rs, crates/amaru-pure-stage/src/tokio.rs, crates/amaru-pure-stage/tests/freeze.rs]}
  - {id: INTERNAL-RESULT, status: implemented, repair: [typed-rebuild, explicit-item-done-refusal, terminate-without-original], tests: [RS-RESULT-RECEIPT-001], evidence: [crates/amaru-pure-stage/src/freeze.rs, crates/amaru-pure-stage/src/tokio.rs, crates/amaru-pure-stage/tests/freeze.rs]}
  - {id: STAGE-STATE, status: feasible-now, repair: [typed-preflight, queued-receipt], tests: [RS-STATE-RECEIPT-001], evidence: [crates/amaru-pure-stage/src/freeze.rs]}
  - {id: INJECT-TYPED, status: non-cryptographic, repair: [typed-decode, reject-signed-ambiguity], tests: [RS-INJECT-001], evidence: [crates/amaru-pure-stage/src/freeze.rs]}
  - {id: CHAIN-HEADER-WRITE, status: intentionally-stale-negative-control, repair: [hash-slot-validation, force-is-negative-control], tests: [RS-STORE-HEADER-001], evidence: [crates/amaru-node/src/freeze_stores.rs]}
  - {id: CHAIN-BLOCK-WRITE, status: cryptographically-impossible-with-current-identity, repair: [refuse-repair-without-mux-request-identity, require-exact-preexisting-header, validate-header-hash-body-hash-key-slot, atomic-error-no-success-event, force-is-negative-control], tests: [RS-STORE-BLOCK-001, RS-STORE-ATOMIC-001], evidence: [crates/amaru-node/src/freeze_stores.rs, crates/amaru-ouroboros-traits/src/stores/chain_store/write_chain_store.rs]}
  - {id: CHAIN-POINTER-WRITE, status: implemented, repair: [stored-header-reference, force-is-negative-control], tests: [RS-STORE-POINTER-001], evidence: [crates/amaru-node/src/freeze_stores.rs]}
  - {id: LEDGER-WRITE, status: implemented, repair: [typed-decode, transaction, unsafe-is-negative-control], tests: [RS-LEDGER-WRITE-001], evidence: [crates/amaru-node/src/freeze_stores.rs]}
  - {id: EFFECT-STORE, status: feasible-now, repair: [typed-rebuild, store-specific-validation, synchronous-outcome-receipt], tests: [RS-EFFECT-STORE-001], evidence: [crates/amaru-pure-stage/src/freeze.rs, crates/amaru-node/src/freeze_stores.rs]}
  - {id: REPEATER, status: implemented, repair: [propagate-target-repair-report, fail-closed-default, explicit-stale-negative-control], tests: [PY-WRAPPER-001], evidence: [dwarf/freeze/repeater.py, tests/test_freeze_workbench.py]}
  - {id: SCRIPT, status: feasible-now, repair: [propagate-target-repair-report], tests: [PY-WRAPPER-002], evidence: [dwarf/freeze/scripting.py]}
  - {id: RUN-REPLAY, status: implemented, repair: [propagate-target-repair-report, ambiguity-refusal, explicit-stale-negative-control], tests: [PY-WRAPPER-001], evidence: [dwarf/freeze/runs.py, tests/test_freeze_runs.py]}
  - {id: REWIND-INGEST, status: implemented, repair: [reconstruct-maps-in-order, restart-refusal, explicit-stale-negative-control], tests: [PY-WRAPPER-001, PY-RESTART-MAP-001], evidence: [dwarf/freeze/runs.py, dwarf/freeze/cli.py]}
  - {id: RULES-PAYLOADS, status: implemented, repair: [propagate-target-repair-report, fail-closed-default, per-rule-stale-negative-control], tests: [PY-RULE-POLICY-001], evidence: [dwarf/freeze/rules.py, dwarf/freeze/payloads.py, tests/test_freeze_rules.py]}
  - {id: SLOT, status: cryptographically-impossible-with-current-identity, repair: [refuse-vrf-eligibility-unproven], tests: [RS-SLOT-REFUSE-001], evidence: [crates/amaru-protocols/src/resign.rs]}
  - {id: ISSUER, status: cryptographically-impossible-with-current-identity, repair: [refuse-identity-change], tests: [RS-ISSUER-REFUSE-001], evidence: [crates/amaru-protocols/src/resign.rs]}
  - {id: OPCERT, status: requires-new-key-material, repair: [refuse-missing-cold-key], tests: [RS-OPCERT-REFUSE-001], evidence: [crates/amaru-protocols/src/resign.rs]}
  - {id: KES-SIGNATURE, status: implemented, repair: [identity-checks, period-checks, kes-sign, self-verify], tests: [RS-KES-001], evidence: [crates/amaru-protocols/src/resign.rs]}
  - {id: PRAOS-PROOF, status: cryptographically-impossible-with-current-identity, repair: [refuse-leader-eligibility-unproven], tests: [RS-VRF-REFUSE-001], evidence: [crates/amaru-protocols/src/resign.rs]}
  - {id: TX-WITNESS, status: requires-new-key-material, repair: [refuse-missing-owner-map-and-keys], tests: [RS-TX-WITNESS-REFUSE-001], evidence: [dwarf/freeze/devnet_tx.py]}
```

Every `feasible-now` row names its acceptance-test ID and exact repair chain above. The executable
test targets are `tests/test_freeze_cardano.py`, `tests/test_freeze_rules.py`,
`tests/test_freeze_runs.py`, `tests/test_freeze_workbench.py`, and, for `RS-*`, the release-applied
`crates/amaru-protocols/tests/resign.rs` or the package-local Freeze/store test module named by the
surface. A remaining row is complete only when that named test first proves emission/refusal wrong
and then passes against both release trees; absence of that test is itself an open acceptance gate.

## Prioritized vertical slices and falsification tests

1. Add one pure Python edit-impact/repair report used by the TUI, repeater, rules, scripts and run
   replay. RED tests must show dependency damage, applied deterministic CBOR repairs, stale values,
   and exact refusal text for slot, issuer/opcert/VRF and transaction witnesses. Falsify it with
   malformed CBOR, protocol-number aliases, whole-raw replacement, and a changed field that the
   classifier does not know: the last case must be `refused`, never “safe”.
2. Replace the global UI claim with per-edit state while retaining the control as an explicitly
   labelled negative-control/repair policy. RED TUI tests must prove public `run` never reports a
   signing repair, controlled devnet reports only the applicable header/body repairs, and stale
   forwarding is visually explicit.
3. Add outbound ChainSync re-signing. RED Rust tests: outbound header edit succeeds with exact
   mounted producer; slot/issuer/opcert/VRF edits fail; reconnect clears ancestry; two peers with
   the same original header do not share ancestry.
4. Add responder-side, mux/request-bound BlockFetch translation and resealing. RED Rust tests cover
   two peers, two blocks, duplicate commitments, stale/finished request, restart, ambiguous body,
   BatchDone and NoBlocks. No global-map fallback is allowed.
5. Make typed runtime edit/refusal outcomes observable synchronously or through an outcome event
   that wrappers wait for. Falsify with deserialize failure, effect-specific refusal, queued state
   failure and a dropped connection. “Original kept” may be a chosen outcome only if the request
   says forward-original-on-refusal.
6. Add explicit TxSubmission refusal now. A later witness-regeneration slice is excluded until the
   Freeze node receives a read-only, devnet-only payment-key mount plus a generated ownership map.
   Acceptance tests must prove exact signer-set equality, transaction-body hash, network magic,
   bounded transaction class, no bootstrap/script witness guessing, and public-network refusal.
7. Extend chain-block store writes through the same controlled block repair chain; otherwise retain
   `force:true` as an explicitly stale negative control. Test mismatch, force, exact key ownership,
   restart and atomic failure.
8. Regenerate both release patches from their exact clean tags using the applier/export path. Run
   focused and affected Rust suites, strict Clippy/rustfmt, patch apply and byte-regeneration,
   manifest/provenance/checksum, Python suite, and independent applied-source diff review.

Explicit exclusions: public-network signing; arbitrary issuer or operational-certificate creation;
slot/VRF/leader-proof changes without a proven eligibility context; transaction witness guessing;
automatic repair at raw wire boundaries without complete-message identity; and any fallback that
emits an original or partly repaired value after claiming the edit succeeded.

## RED/GREEN evidence log

- `PY-CARDANO-IMPACT-001` RED, 2026-11-15:
  `pytest -q tests/test_freeze_cardano.py -k edit_impact` failed 3/3 focused tests with the
  expected `AttributeError: module 'freeze.cardano' has no attribute 'edit_impact'`. The tests
  cover controlled header repair, slot/opcert refusal, transaction-witness partial-repair refusal,
  explicit stale control, raw-wire refusal, unknown protocol refusal, and public-network refusal.
- `PY-CARDANO-IMPACT-001` GREEN, 2026-11-15:
  the same focused command passed `3 passed, 2 deselected`. `cardano.edit_impact` parses the exact
  edited bytes, fails closed on unknown shapes/protocols, distinguishes protocol-bound repair from
  deterministic CBOR length repair, and never claims Python generated a signature.
- `PY-TUI-EDIT-STATE-001` RED, 2026-11-15:
  `pytest -q tests/test_freeze_tui.py -k 'network_messages_are_named or public_header_edit'`
  failed both focused tests for the expected missing behavior: Forward emitted the edit instead of
  retaining it with the protected/public-network refusal, and no per-edit stale-negative-control
  action or inspector state existed.
- `PY-TUI-EDIT-STATE-001` GREEN, 2026-11-15:
  the same focused command passed `2 passed, 9 deselected`. Protected or public-network raw edits
  remain held with an exact reason; `N` is the per-edit explicit stale-negative-control action; the
  inspector lists damage, planned repairs, remaining stale dependencies and outcome. The old
  global label now says “Header/body repair”, so it no longer claims coverage of every edit.
- `PY-WRAPPER-001` RED, 2026-11-15:
  `pytest -q tests/test_freeze_workbench.py -k old_message` failed because `repeater.plan` did not
  refuse a protected slot edit, and `pytest -q tests/test_freeze_runs.py -k run_replay_refuses`
  reached `item.replace` (`item 41 is not held`) instead of rejecting before emission.
- `PY-WRAPPER-001` GREEN, 2026-11-15:
  both focused commands passed. `repeat`, `ingest`, and `rewind` now classify exact network bytes
  through the same dependency report, refuse protected/unsupported edits by default before
  `item.replace`, and require the explicit `--stale-negative-control` option for an intentional
  invalid replay. A refused whole-run edit forwards the unchanged original and records that exact
  outcome rather than claiming the edit was applied.
- `PY-DIRECTION-001` RED, 2026-11-15:
  `pytest -q tests/test_freeze_cardano.py -k supported_direction` failed with the expected
  `TypeError` because the dependency classifier had no direction input and therefore could not
  distinguish the implemented inbound hooks from outbound scripted-peer traffic.
- `PY-DIRECTION-001` GREEN, 2026-11-15:
  `pytest -q tests/test_freeze_cardano.py -k edit_impact` passed `4 passed, 2 deselected`.
  At this intermediate slice, cryptographic reports required an explicit `proto_in` boundary and
  outbound traffic failed closed. `RS-RESIGN-OUT-001` and `RS-BF-RESPONDER-001/002` below supersede
  that temporary direction exclusion with typed outbound repair; absent/unknown direction still
  fails closed unless that individual operation explicitly selects stale-negative-control mode.
- `PY-RULE-POLICY-001` RED, 2026-11-15:
  `pytest -q tests/test_freeze_rules.py -k explicit_stale_negative_control` failed because a rule
  changed a protected header slot and emitted `replace` without a negative-control declaration.
- `PY-RULE-POLICY-001` GREEN, 2026-11-15:
  the focused test passed, followed by `15 passed` for the complete rule suite. Raw network rules
  now use the shared dependency classifier and default to forwarding the unchanged original with
  `not applied: edit refused: …`. `stale_negative_control: true` is serialized in templates and is
  required for deliberately invalid replacements/replays. Shipped DWARF fault scenarios carry
  that explicit marker because their declared purpose is stale/invalid protocol traffic.
- `RS-OVERLAY-PROBES-001` RED, 2026-11-15:
  the complete November `amaru-freeze` package test rejected its probe inventory because the
  release overlay omitted `ledger.rule.collateral_return`; the focused rerun then exposed that
  `ledger.rule.collateral` named the old `phase_one/fees.rs::validate` site instead of the actual
  `phase_one/mod.rs::execute` site. This was a semantic overlay failure, not a patch hash failure.
- `RS-OVERLAY-PROBES-001` GREEN, 2026-11-15:
  the release-specific `probes.rs` inventory now names both collateral probes at the actual site.
  The focused inventory test and the complete affected four-package November suite pass; the
  September suite remains green.
- `PY-RECIPE-INHERITANCE-001` RED, 2026-11-15:
  regenerating the November recipe stopped with
  `crates/amaru-ouroboros/src/kes/mod.rs is in the spec but the patch does not touch it`. Semantic
  inspection showed that the later upstream already supplies the KES signing API, so copying an
  artificial patch hunk would have been false provenance.
- `PY-RECIPE-INHERITANCE-001` GREEN, 2026-11-15:
  later manifests can explicitly name files whose hook semantics are inherited from upstream;
  `check_spec` rejects undeclared, unlisted, and falsely touched inheritance. The focused test
  passed. Regeneration from both pinned sources reproduces the complete hook patches and the
  November release overlay byte-for-byte. Manifests are separately validated because port output
  carries time-varying `ported_at` metadata; the stored November manifest also retains its
  release-specific `run`, `bootstrap`, and explicit upstream-`inherited` declarations.
- `PY-PROTO-OTHER-001` RED, 2026-11-15:
  `pytest -q tests/test_freeze_cardano.py -k semantic_validator` failed because an edit to a known
  KeepAlive message was described as repaired solely from canonical CBOR encoding, even though no
  validator proved its cookie/state correlation.
- `PY-PROTO-OTHER-001` GREEN, 2026-11-15:
  the focused test passed. Known mini-protocol edits without a dedicated semantic validator now
  refuse before emission with the exact missing invariant; explicit stale-negative-control mode
  remains available and visibly lists the unvalidated invariant.
- `PY/RS-STALE-BYPASS-001` RED, 2026-11-15:
  the focused Python client test failed with `TypeError: FreezeClient.replace() got an unexpected
  keyword argument 'stale_negative_control'`, and the focused Rust wire test failed because
  `ReplaceRawStale`/`ReplaceStale` did not exist. This demonstrated that a per-edit UI choice could
  still enter the global cryptographic coordinator and be dropped when repair was enabled.
- `PY/RS-STALE-BYPASS-001` GREEN, 2026-11-15:
  the Python client test and both release-applied `amaru-freeze` wire tests pass. The FCP server
  accepts the flag only with exact raw bytes, binds it to the held sequence's verdict, and the
  protocol gate forwards precisely that candidate without consulting or clearing the global
  repair coordinator. Concurrent items and later replacements retain their independent policy.
  The first correctly resynced canonical run also caught three older fake-agent assertions that
  expected only `$raw`; after they were updated to require the new request-local policy evidence,
  all focused cases and the full suite passed. An unrelated TUI timing failure passed unchanged on
  its focused rerun.
- `RS-RESIGN-OUT-001` RED, 2026-11-15:
  both release tests failed to compile on the absent `ChainSyncDirection::Outbound`, direction
  argument, disconnect cleanup and outbound gate call. This proved the old coordinator had only
  an unqualified inbound ancestry stream.
- `RS-RESIGN-OUT-001` GREEN, 2026-11-15:
  the September focused outbound ChainSync tests passed `2 passed`; the November `outbound_`
  selection passed all four outbound ChainSync/BlockFetch cases. Ancestry is keyed by
  `inbound:{mux}`/`outbound:{mux}`, reconnect removes both, and protected consensus inputs refuse
  before key loading.
- `RS-BF-RESPONDER-001/002` RED, 2026-11-15:
  both release tests failed on the absent responder request constructor, mux-qualified lease and
  responder block rewrite API. The old outbound protocol-3 path had no request/source identity.
- `RS-BF-RESPONDER-001/002` GREEN, 2026-11-15:
  focused tests pass in both release trees. The custom responder now binds RequestRange to its mux,
  gates StartBatch/Block/BatchDone/NoBlocks at the actual outbound boundary, consumes only the
  exact lease, and rejects wrong-mux, duplicate, finished, restarted or ambiguous mappings. Tests
  cover two peers and multi-block ranges; there is no global fallback.
- `RS-TYPED-RECEIPT-001` RED, 2026-11-15:
  the focused pure-stage test timed out waiting for `item.done` after malformed edited input,
  effect and result reconstruction; old code silently retained or executed the original.
- `RS-TYPED-RECEIPT-001` GREEN, 2026-11-15:
  the focused pure-stage test passed in both release trees. Rejections carry
  `edit_reconstruction_failed`; invalid wait/schedule/send/result paths skip the original and
  terminate or resume according to the typed effect outcome.
- `RS-STORE-BLOCK-001` RED, 2026-11-15:
  the November focused store test expected `repair:true` to fail before mutation, but received
  `Ok(())`. Semantic source inspection then falsified automatic repair: StateProvider supplies no
  mux/request/source identity and ChainStore has no atomic header-plus-body write.
- `RS-STORE-BLOCK-001` GREEN, 2026-11-15:
  `blocks_are_written_under_their_own_hash_unless_forced` passed in both releases. Repair requests
  refuse with `store_block_repair_unavailable`; non-force writes require a byte-equivalent stored
  header and otherwise refuse with `store_block_header_not_preexisting`; `force:true` remains the
  explicit negative control.
- `PY-PROTECTED-CODES-001` RED, 2026-11-15:
  `pytest -q tests/test_freeze_cardano.py -k edit_impact` failed five focused cases because no
  stable `code` field existed and outbound controlled repair was still described as unavailable.
  The public-network TUI test separately failed because the inspector hid the new code.
- `PY-PROTECTED-CODES-001` GREEN, 2026-11-15:
  the focused classifier passed `6 passed, 2 deselected`, and the public-network inspector test
  passed. Slot, issuer, VRF identity/proof, opcert, TxSubmission witness and public-network signing
  refusals now have stable codes; controlled outbound ChainSync reports its real repair chain.
- `RS-PROTOCOL-BOUNDARY-001` RED, 2026-11-16:
  the direct Rust boundary test did not compile because no protocol emission repair boundary
  existed. An authenticated FCP raw verdict could therefore reach ChainSync, BlockFetch, or another
  protocol while controlled signing was unavailable, disabled, or public-network-ineligible; the
  Python preflight was the only refusal layer.
- `RS-PROTOCOL-BOUNDARY-001` GREEN, 2026-11-16:
  both release trees pass direct Rust tests proving that changed protocol bytes are refused with
  `controlled_devnet_signing_unavailable`, `controlled_devnet_signing_disabled`, or
  `protocol_dependency_graph_unavailable` before network emission. Enabled repair admits only
  ChainSync and BlockFetch and must complete their typed repair chain. Only the exact held item's
  authenticated `ReplaceRawStale` verdict bypasses repair as an explicit negative control.
- `RS-PROTOCOL-ALIAS-001` RED/GREEN, 2026-11-16:
  direct boundary tests first proved that responder-role mux protocol numbers `32770` and `32771`
  missed the ChainSync/BlockFetch gate because only initiator numbers `2` and `3` were recognized.
  The boundary now masks the mux role bit once, returns a typed `RepairProtocol` used by dispatch,
  and applies identical controlled/public/disabled refusal rules to both roles on both releases.
- `RS-PROTOCOL-TRANSITION-001` RED/GREEN, 2026-11-16:
  direct raw tests first exposed unchanged forwarding for a malformed or non-RollForward ChainSync
  candidate and candidate-only acceptance for a generic outbound BlockFetch message. Both typed
  gates now decode original and replacement, reject malformed data with
  `edited_message_not_decodable`, reject variant changes with `mini_protocol_transition_changed`,
  and reject unsupported same-variant edits with `protocol_dependency_graph_unavailable`.
- `PY/RS-ERA-INVARIANT-001` RED, 2026-11-16:
  the Python RollForward classifier reported an era-tag mutation as auto-repairable, while the
  Rust header repair returned replacement bytes unchanged whenever either header was not Conway.
  This could preserve a false repair claim across the classifier and emission gate.
- `PY/RS-ERA-INVARIANT-001` GREEN, 2026-11-16:
  Python now refuses a changed era identity with `era_identity_change_forbidden`, and refuses an
  unsupported unchanged era with `unsupported_header_era`. Both release Rust coordinators enforce
  the same invariant and codes; focused Python classifier and Rust boundary tests prove no
  auto-repair claim survives an era-tag mutation.
- `RS-TYPED-RAW-001` RED, 2026-11-16:
  an authenticated FCP raw replacement for a typed internal input/result returned a successful
  replace/released event, then the pure-stage boundary converted `ReplaceRaw`/`ReplaceRawStale` to
  `Forward` and ran or returned the original typed value. The first refusal implementation also
  surfaced only the generic FCP code `failed`.
- `RS-TYPED-RAW-001` GREEN, 2026-11-16:
  real FCP server and pure-stage graph tests pass in both releases. Typed internal raw and raw-stale
  verdicts are rejected before release, never execute or return the original, preserve the held
  item, and expose the stable envelope code `typed_raw_replacement_unsupported`.
- `RS-BF-LEASE-001` RED, 2026-11-16:
  a stage-level injected `PointsRange::request_range`/store failure left the responder request
  coordinator's finish count at zero because the lease was created before the fallible read and
  the error path returned without finishing it.
- `RS-BF-LEASE-001` GREEN, 2026-11-16:
  a responder-scoped RAII lease now finishes the exact mux/request on every terminal path. Both
  release stage suites exercise actual `BatchDone`, `NoBlocks`, injected store error, outbound
  timeout, and a real Tokio `StageGraph` abort/disconnect. Request-specific lease IDs prove cleanup
  belongs to that responder even when initiator requests run concurrently; duplicate finish
  remains harmless and no global request fallback exists.
- `RS-STORE-ATOMIC-001` RED, 2026-11-16:
  the injected-failure test could not be expressed because the test chain-store wrapper had no
  `store_block` failure hook. This left the no-partial-write/no-success-event invariant unproved.
- `RS-STORE-ATOMIC-001` GREEN, 2026-11-16:
  both release node suites inject a block-store failure and prove the chain-store state is
  unchanged and no successful write event is reported. Repair still refuses before write when the
  store lacks mux/request/source identity or an exact pre-existing header.
- `RS-BODY-SIZE-001` GREEN, 2026-11-16:
  both releases pass focused coordinator tests for an edited body that disagrees with its
  preannounced size and for a wrong declared size on an otherwise unchanged body. The repair chain
  refuses rather than silently accepting either envelope inconsistency.

## Verification evidence from the pre-extension checkpoint

The following results describe the candidate immediately before the outbound/responder/runtime/store
extension above. They are retained as provenance, not presented as final verification of the new
patches. The current-candidate gates are recorded after they rerun.

- Canonical combined DWARF layout after the final candidate resync:
  `pytest -q tests/test_freeze_*.py` -> `290 passed in 114.10s`.
- Both release-applied trees: `cargo test -p amaru-protocols --test resign` -> `22 passed` each.
- Both release-applied trees: complete affected suites for `amaru-freeze`, `amaru-node`,
  `amaru-protocols`, and `amaru-pure-stage` passed, including package and documentation tests.
- Both release-applied trees: `cargo fmt --all --check` passed. Strict all-target/all-feature Clippy
  passed on pinned `nightly-2026-09-04`; the toolchain emitted only its known
  `generic_const_exprs` next-solver warning.
- Fresh pristine worktrees at `eaf8ac3...` and `c773f848...` passed `git apply --check`. Root and
  `v10.11.20260925` compatibility patches remain byte-identical at SHA-256
  `626d5eea9c83bef869c3ed11aa46c40499a6749c709fa53be82e5b5e4413b8ec`.
- Regeneration reproduced both pinned hook patches and the November crate overlay; manifest source,
  release commands, inheritance, capability and spec checks passed. Current recipe digests are
  `98fc4d8783844386aa895119b7d5ed499f324213d3d35bca1ef02e9332771a40` and
  `d948f0953e18b44ba252499fc1817e0f83e947774389ba3e5c450037210559fe`.

## Final extension verification

These gates apply to the outbound/responder/runtime/store extension documented above and supersede
the pre-extension counts:

- `pytest -q tests/test_freeze_*.py` in the canonical combined DWARF layout: `294 passed in
  119.97s`.
- Pinned `nightly-2026-09-04` complete Rust suites for `amaru-freeze`, `amaru-protocols` and
  `amaru-pure-stage` passed on both releases, including UI compile-fail tests and documentation
  tests. The focused repair suites passed `30` tests on September and `29` on November.
- Complete `amaru-node` suites passed with offline staged peer snapshots: September `91 passed,
  2 ignored`; November `157 passed, 3 ignored`; package doctests passed on both. The macOS runner
  used a raised file-descriptor limit for the concurrent five-node RocksDB tests.
- `cargo clippy --workspace --all-targets --all-features -- -D warnings` passed on both releases
  with the pinned Clippy build. Its final RED run found one needless return in the responder lease
  constructor; the GREEN source retains request-specific test instrumentation without that lint.
  The toolchain emitted only its known `generic_const_exprs` next-solver and upstream manifest
  notices. `cargo fmt --all -- --check` passed on both releases.
- Fresh detached worktrees at `eaf8ac3fa309f2a174c045454c276194c9312b8d` and
  `c773f8486e5b64284506824daa3974020e0c8bf6` passed clean apply and `git diff --check`.
  Every generated file was compared byte-for-byte with the final semantic source: `7955` files
  (`61` patch paths and `14` overlay files) for September, and `8168` files (`60` patch paths and
  `14` overlay files) for November.
- Root and September patches are byte-identical at SHA-256
  `0e9251cc39c7a52ff751848d4084c969347cd79c379d7c6b54f9c0ff8f5a42aa`; November is
  `5de0d8e3a4604a090b8eef9733141d0bdd2c2c9a395f2ef43748d43fc5ef8a6d`. Final recipe digests,
  including manifests and crate overlays, are
  `999d833e4fce99f531e0d3f56516bf7dff98103d4b10a9421dbcdc935bd22018` and
  `2c632ecbb9715a13c226491818916b4c0773f55faff7b274ebd2371663e349c0`.
  The stored manifest SHA-256 values are
  `67459d92171457ef1c47c1ef215ce897337a55494566f8897d7c48aa64684a12` and
  `88c219263175f356f35815eca8d3d23fffccd00858b553ea735b45b27d758d25`.
- Fresh independent semantic review of the final regenerated Rust and Python candidate returned
  `PASS` on 2026-11-16. It independently rechecked the authenticated Rust emission boundary,
  responder-role protocol aliases and direct message transitions, Python/Rust era invariants,
  typed raw refusal across the FCP server and real stage graph, responder lease terminal paths,
  store atomicity, declared body-size checks, both-release semantic equivalence, and the patch,
  manifest and recipe hashes. It reported no remaining security or correctness regression.
