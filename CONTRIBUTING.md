# Contributing

DWARF Freeze is distributed as an overlay for a DWARF checkout. Keep changes within the
existing Amaru support boundary: cardano-node support is limited to DWARF's external
proxy/fake-peer coverage and shared node-independent Cardano naming.

## Development setup

1. Clone [DWARF](https://github.com/pragma-org/dwarf) and this repository side by side.
2. Copy this overlay into DWARF as described in [README.md](README.md#deploy).
3. Create the documented Python environment and install `pytest`.
4. Run the complete Python suite from the DWARF checkout:

   ```bash
   .venv-freeze/bin/python -m pytest -q -p no:asyncio tests/test_freeze_*.py
   ```

## Change requirements

- Add or update tests for behavior changes.
- Preserve fail-closed edit, signing, authentication, and store-write boundaries.
- Preserve generated Amaru patch provenance and update the matching target manifest when
  generated patch bytes change.
- Keep documentation commands, links, target revisions, and stated support boundaries
  accurate.
- Do not commit tokens, recordings, node data, build output, or local environment files.
- Run `git diff --check` and the complete Python suite before submitting a change.

By submitting a contribution, you represent that you have the right to submit it. No
project-wide license is currently specified; contribution acceptance does not by itself
establish one.