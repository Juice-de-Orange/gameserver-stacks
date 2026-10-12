"""Without an RCON signal, "booted" only holds after several stable rounds — not after five seconds.

`verify_boot` must not return `True` in the FIRST loop iteration for every
`console_kind != source_rcon` — five seconds after `docker start`. Crash detection via the restart
counter needs at least two rounds, because the first only sets the baseline.

In a version change `{name}-prev` is removed with `force=True` right afterwards — so the fast
rollback is gone before the new container has even loaded.

Affected is exactly one server: only `battleye_rcon` (Reforger) is != `source_rcon`; Minecraft and
Vintage Story are `source_rcon`.

(The rollback's world restore replaces the archived folders since `extract_backup` stopped
overlaying them — `tests/test_restore_replaces_the_world.py`.)
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from app.models.server import ConsoleKind
from app.services import version_manager as vm


class _Docker:
    """Returns a given sequence of states, then the last one forever."""

    def __init__(self, sequence):
        self.sequence = list(sequence)
        self.calls = 0

    async def state(self, name):
        self.calls += 1
        i = min(self.calls - 1, len(self.sequence) - 1)
        return self.sequence[i]


class _Driver:
    # `verify_boot` asks the driver whether `players()` is usable as boot proof — not
    # `console_kind` alone. Valheim and Reforger set it to `True`, because there the container
    # has long been running while SteamCMD is still fetching the game.
    boot_proof_via_players = False

    def players_usable_as_boot_proof(self, server):
        # `verify_boot` asks this METHOD rather than the ClassVar: for Valheim the answer depends
        # on the server (`SERVER_PUBLIC` switches the Steam query off), not on the game. The dummy
        # follows the base-class rule and reads its own ClassVar.
        return self.boot_proof_via_players

    async def players(self, server):
        raise RuntimeError("RCON not ready")


class _DriverWithProof(_Driver):
    """A driver that offers `players()` as proof — and delivers it."""

    boot_proof_via_players = True

    async def players(self, server):
        return {"count": 0, "names": []}


class _DriverProofNeverArrives(_Driver):
    """Offers the proof but never delivers it — the server is not up yet."""

    boot_proof_via_players = True


def _srv(kind=ConsoleKind.battleye_rcon):
    return SimpleNamespace(key="rf-1", container_name="reforger-rf-1", console_kind=kind)


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    """No real waiting — otherwise the test takes minutes."""
    async def _no_sleep(_):
        return None
    monkeypatch.setattr(vm.asyncio, "sleep", _no_sleep)
    monkeypatch.setattr(vm.watchdog, "suppress", lambda k, m: None)


def test_a_container_that_crashes_right_away_does_not_count_as_booted(monkeypatch):
    """The core: one round of `running=True` is no proof if the counter rises afterwards."""
    dk = _Docker([
        {"exists": True, "running": True, "restarting": False, "restarts": 3},
        {"exists": True, "running": True, "restarting": False, "restarts": 4},   # crashed
    ])
    monkeypatch.setattr(vm, "docker", dk)

    assert asyncio.run(vm.verify_boot(_srv(), _Driver(), timeout=60)) is False, (
        "the boot counted as proven after the first round — crash detection via the restart "
        "counter was never reached"
    )


def test_a_stable_container_counts_as_booted_but_not_immediately(monkeypatch):
    """Cross-check: stable still means `True` — just not after a single round."""
    dk = _Docker([{"exists": True, "running": True, "restarting": False, "restarts": 3}])
    monkeypatch.setattr(vm, "docker", dk)

    assert asyncio.run(vm.verify_boot(_srv(), _Driver(), timeout=60)) is True
    assert dk.calls >= 3, (
        f"reported as booted after {dk.calls} poll(s) — that proves nothing about stability"
    )


def test_a_restarting_container_still_does_not_count_as_booted(monkeypatch):
    """Cross-check for the existing rule."""
    dk = _Docker([{"exists": True, "running": True, "restarting": True, "restarts": 0}])
    monkeypatch.setattr(vm, "docker", dk)
    assert asyncio.run(vm.verify_boot(_srv(), _Driver(), timeout=30)) is False


def test_rcon_servers_stay_unchanged(monkeypatch):
    """PZ/MC/VS have a real proof — it must not be delayed artificially."""
    dk = _Docker([{"exists": True, "running": True, "restarting": False, "restarts": 0}])
    monkeypatch.setattr(vm, "docker", dk)

    class _RconUp:
        async def players(self, server):
            return []

    assert asyncio.run(
        vm.verify_boot(_srv(ConsoleKind.source_rcon), _RconUp(), timeout=60)
    ) is True
    assert dk.calls == 1, "the real RCON proof may take effect immediately"


def test_without_rcon_a_driver_can_still_offer_a_proof(monkeypatch):
    """Valheim: the container runs while SteamCMD is still fetching the game.

    "Three stable rounds" only proves there that Docker did not restart it — not that the game
    is up. If the Steam query answers, that is the real proof, and it comes sooner than the three
    rounds.
    """
    dk = _Docker([{"exists": True, "running": True, "restarting": False, "restarts": 0}])
    monkeypatch.setattr(vm, "docker", dk)
    assert asyncio.run(vm.verify_boot(_srv(ConsoleKind.none), _DriverWithProof(), timeout=60)) is True
    assert dk.calls == 1, "a real proof may take effect immediately"


def test_an_offered_proof_that_never_arrives_does_not_count_as_a_boot(monkeypatch):
    """And it must not fall back to "three stable rounds".

    Whoever offers a proof must deliver it — otherwise the seam would be a free pass instead of a
    tightening: the server would count as up after 15 seconds although SteamCMD is still
    downloading, and a mod rollback would never trigger.
    """
    dk = _Docker([{"exists": True, "running": True, "restarting": False, "restarts": 0}])
    monkeypatch.setattr(vm, "docker", dk)
    assert asyncio.run(vm.verify_boot(_srv(ConsoleKind.none), _DriverProofNeverArrives(), timeout=30)) is False
