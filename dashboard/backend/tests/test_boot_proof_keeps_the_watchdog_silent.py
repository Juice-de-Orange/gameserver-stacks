"""Whoever proves a boot keeps the watchdog silent for as long as the proof may take.

Setting the silence **before** stop and mandatory backup (`stop_and_snapshot(...,
suppress_minutes=15)` on a version change, `SUPPRESS_MINUTES=20` on a mod apply) is not enough:
the boot proof afterwards has `timeout=900` (15 minutes) **itself** and only begins once the stop
(up to `stop_grace`) and the whole pack run are through. Such a window is **arithmetically
guaranteed** to end before the boot deadline.

What happens then: `watchdog._check_server` sees a running container without an RCON answer,
counts up three 60 s rounds and calls `lifecycle.restart_server`. `verify_boot` then sees
`running=False` after `was_up=True` and returns `False`. Result: **rollback on an actually
healthy boot** — the new container gone, `extract_backup` over the already migrated world. Exactly
the long boot window the 900 s timeout was introduced for ("against false-negative rollback")
would be uncovered.

**Why the rule belongs at the choke point.** The rollback paths do it the right way round:
`mods/apply._rollback` and `foreign_apply._rollback` call `watchdog.suppress` right before their
`verify_boot`. The same pattern as with `stop_and_confirm` and the backup pack lock — hence the
same answer: `verify_boot` silences the watchdog itself, for the duration it takes.
`watchdog.suppress` extends via `max()` and never shortens, so a double call is harmless.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

from app.services import version_manager as vm


class _Driver:
    # `verify_boot` also asks the driver whether `players()` serves as boot proof
    # (Valheim has no RCON, but the Steam query). `False` = the RCON-only behaviour.
    boot_proof_via_players = False

    def players_usable_as_boot_proof(self, server):
        return self.boot_proof_via_players

    def liveness(self, server):  # pragma: no cover — not called for every kind
        return None


def _srv():
    return SimpleNamespace(id=1, key="pz-1", container_name="pz-1",
                           console_kind="none", game_type=None)


def test_the_boot_proof_silences_the_watchdog_for_its_own_duration(monkeypatch):
    calls: list[tuple[str, float]] = []
    monkeypatch.setattr(vm.watchdog, "suppress", lambda k, m: calls.append((k, m)))

    class _Dk:
        async def state(self, name):
            return {"exists": True, "running": True, "restarting": False, "restarts": 0}
    monkeypatch.setattr(vm, "docker", _Dk())

    asyncio.run(vm.verify_boot(_srv(), _Driver(), timeout=900))

    assert calls, "the boot proof did not silence the watchdog at all"
    key, minutes = calls[0]
    assert key == "pz-1"
    assert minutes >= 15, (
        f"silent for {minutes} min, but the proof may take 15 min — the window expires "
        f"in the middle of the boot and the watchdog restarts the container that is booting"
    )


def test_the_silence_grows_with_the_timeout(monkeypatch):
    """Cross-check: a fixed value would be the same error with a different number."""
    calls: list[float] = []
    monkeypatch.setattr(vm.watchdog, "suppress", lambda k, m: calls.append(m))

    class _Dk:
        async def state(self, name):
            return {"exists": True, "running": True, "restarting": False, "restarts": 0}
    monkeypatch.setattr(vm, "docker", _Dk())

    asyncio.run(vm.verify_boot(_srv(), _Driver(), timeout=1800))
    assert calls[0] >= 30, f"with a 30 min proof only {calls[0]} min silent"
