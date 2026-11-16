# Editing and Dependent-Value Repair Implementation Plan

**Goal:** Give every Freeze editing surface an explicit repaired, stale-negative-control, or
refused outcome, extending controlled-devnet repair where identity and request invariants prove it.

**Architecture:** A shared edit-impact report classifies operator changes before release and is
propagated by interactive and automated wrappers. Protocol-specific Rust coordinators remain the
only place that can claim cryptographic repair. They use stream/request-bound identities and exact
preimage checks; public or ambiguous paths fail closed.

**Tech stack:** Python 3, Textual, byte-preserving CBOR, Rust, Amaru mini-protocol types, KES/VRF
libraries, pytest, cargo test/clippy/fmt, generated patch recipes.

---

### Task 1: Shared Cardano edit-impact report

**Files:** `dwarf/freeze/cardano.py`, `tests/test_freeze_cardano.py`.

1. Write focused failing tests for exact dependency damage and refusal reasons.
2. Run only those tests and record the expected assertion failures in the audit evidence section.
3. Implement the smallest pure classifier over original/edited raw bytes and changed named paths.
4. Re-run focused tests; add malformed/unknown fail-closed cases.

### Task 2: Per-edit TUI report and negative-control wording

**Files:** `dwarf/freeze/tui.py`, `tests/test_freeze_tui.py`, `dwarf/freeze/glossary-cardano.yaml`.

1. Write failing inspector tests for damage, repairs, remaining stale dependencies and exact refusal.
2. Run the focused tests and capture RED evidence.
3. Render the shared report and rename the global toggle so it describes policy, not universal
   capability.
4. Re-run tests, including narrow-terminal layout.

### Task 3: Propagate reports through wrappers

**Files:** `dwarf/freeze/repeater.py`, `dwarf/freeze/runs.py`, `dwarf/freeze/scripting.py`,
`dwarf/freeze/rules.py`, their focused tests.

1. Add failing tests proving each wrapper preserves the target repair/refusal state.
2. Implement one shared preflight call and fail-closed ambiguity handling.
3. Re-run focused wrapper tests.

### Task 4: Outbound/scripted-peer ChainSync repair

**Files:** applied Amaru `crates/amaru-protocols/src/{freeze,resign}.rs`,
`crates/amaru-protocols/tests/resign.rs`, both generated recipes.

1. Add outbound RED tests for exact producer KES repair, protected inputs, stream isolation and
reconnect.
2. Invoke the coordinator for outbound ChainSync with direction-qualified stream identities.
3. Re-run focused tests on both releases.

### Task 5: Request-bound responder BlockFetch repair

**Files:** same Rust coordinator/gate files and tests.

1. Add RED tests for mux-bound request translation, outbound block reseal, concurrency, duplicate
commitments, stale request, restart, BatchDone and NoBlocks.
2. Add responder request identities; never use the unbound fallback for an edited body.
3. Re-run focused tests on both releases.

### Task 6: Exact refusal for unsupported cryptographic edits

**Files:** Rust gate/coordinator tests, TUI tests, protocol docs.

1. Add RED tests for TxSubmission witness edits, slot, issuer, opcert and VRF/leader proof.
2. Return stable refusal codes and exact reasons before emission.
3. Prove generic `run` has no signing mode and cannot claim repair.

### Task 7: Store-write integration

**Files:** `crates/amaru-node/src/freeze_stores.rs`, coordinator API, Rust tests.

1. Add RED tests for controlled block reseal and for explicit `force:true` stale control.
2. Reuse the typed block repair chain with exact identity and atomic write checks.
3. Re-run chain/ledger store suites.

### Task 8: Regenerate and verify both recipes

**Files:** release patches, overlays, manifests, audit evidence.

1. Export from exact clean Amaru tags; do not hand-edit generated patches.
2. Verify clean apply, byte regeneration, manifests/provenance/checksums, and root/v10.11.20260925
identity.
3. Run the canonical Python suite, focused and affected Rust suites for both releases, strict Clippy
and rustfmt.
4. Inspect applied Rust/Python source semantically and record remaining ledger blockers.

No task includes a commit or push.
