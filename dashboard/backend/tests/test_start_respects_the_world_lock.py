"""No start and no restart while a world-writing operation is running.

`restart_server` and `start_server` must consult `world_lock`: the lock is taken by the writing
operations and checked by the routers, and every start/restart caller (server router, scheduled
restarts, watchdog) goes through these two functions — so the check sits at that choke point.

**The dangerous phase is exactly the one in which the container IS RUNNING.** `mods/apply._apply_locked`
and `version_manager` wait up to 900 s in `verify_boot` after the start. `restarts._do_restart`
only checks `is_running(server)` right before — `True` in that phase — and then calls
`restart_server`.

Two kinds of damage at once:

1. After `was_up=True`, `verify_boot` sees `running=False` and returns `False`: a **healthy** mod
   change is rolled back completely, a healthy version change loses its new container.
2. `restart_server` holds the `lifecycle` lock across stop AND start, but `_rollback` in
   `mods/apply.py` moves the world files back AFTER its `stop_and_confirm`, i.e. outside that
   lock. If the rollback wins the race, `restart_server` then runs through and issues its
   `docker start` in `finally` — in the middle of `_restore_world`/`_restore_config`. The server
   boots on a half-restored world with a half-rewritten `.ini`.

Trigger: a mod apply that overlaps a scheduled restart.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.services import lifecycle, world_lock


def _srv():
    return SimpleNamespace(id=1, key="pz-1", container_name="pz-1")


@pytest.fixture(autouse=True)
def _free_locks(monkeypatch):
    monkeypatch.setattr(world_lock, "_locks", {})
    monkeypatch.setattr(world_lock, "_holders", {})
    monkeypatch.setattr(lifecycle, "_locks", {})


def test_no_restart_while_a_mod_apply_is_running(monkeypatch):
    actions: list[str] = []

    class _Dk:
        async def action(self, name, act, **kw):
            actions.append(act)
        async def state(self, name):
            return {"exists": True, "running": True}
    monkeypatch.setattr(lifecycle, "docker", _Dk())

    async def scenario():
        async with world_lock.hold("pz-1", "mods.apply"):
            with pytest.raises(HTTPException) as e:
                await lifecycle.restart_server(_srv())
            return e.value

    error = asyncio.run(scenario())
    assert error.status_code == 409
    assert "a mod change" in str(error.detail), "the message does not name the blocking operation"
    assert actions == [], f"the server was touched anyway: {actions}"


def test_no_start_while_a_version_change_is_running(monkeypatch):
    actions: list[str] = []

    class _Dk:
        async def action(self, name, act, **kw):
            actions.append(act)
        async def state(self, name):
            return {"exists": True, "running": False}
    monkeypatch.setattr(lifecycle, "docker", _Dk())

    async def scenario():
        async with world_lock.hold("pz-1", "version.change"):
            with pytest.raises(HTTPException):
                await lifecycle.start_server(_srv())

    asyncio.run(scenario())
    assert actions == []


def test_without_a_running_world_operation_everything_starts_normally(monkeypatch):
    """Cross-check — otherwise the lock would be a brake, not a guard."""
    actions: list[str] = []

    class _Dk:
        async def action(self, name, act, **kw):
            actions.append(act)
        async def state(self, name):
            return {"exists": True, "running": False}
    monkeypatch.setattr(lifecycle, "docker", _Dk())

    asyncio.run(lifecycle.start_server(_srv()))
    assert "start" in actions
