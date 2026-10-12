"""Crash-safe mod management for Project Zomboid.

Layout:
- `cells.py`    — cell maths (map conflicts, world↔map assignment). Pure, no network.
- `preflight.py`— check catalogue with stable error codes.
- `state.py`    — draft (DB) ↔ live (.ini), diff, traffic-light status.
- `apply.py`    — the transactional apply, incl. world quarantine and auto-rollback.

Guiding idea: **editing and applying are separate.** Nothing changes on the running server
until "apply" has passed the preflight, and then as a transaction that either succeeds
completely or is rolled back in full.
"""
