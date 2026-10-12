""""Nothing was changed" may only be said when it is true.

* `lifecycle.stop_and_snapshot` starts a previously running server again after a failed
  mandatory backup and then raises "mandatory backup failed — nothing was changed". If that
  start fails too, the server is OFF, the watchdog silenced via `suppress_minutes`, and the
  operator reads that nothing happened. Through `stop_and_snapshot` this affects `restore.py`,
  `version_manager.py` and `mods/foreign_apply.py`.
* `mods/apply.py` needs the same answer without a second hand-made copy of those lines.

**The answer is not another copy but the choke point.** `ensure_restart` alone knows whether the
server came back. So IT writes the truth into the exception leaving it — then every caller
inherits the honest message without anyone having to remember it.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.services import lifecycle

NOTE = "the server was running before and is now OFF"


def _srv():
    return SimpleNamespace(id=1, key="pz-1", container_name="pz-1")


class _StartFails:
    async def action(self, name, act, **kw):
        raise RuntimeError("socket proxy not responding")


class _StartWorks:
    def __init__(self):
        self.calls: list[str] = []

    async def action(self, name, act, **kw):
        self.calls.append(act)


# ------------------------------------------------------------------ the choke point

def test_a_failed_restart_is_in_the_message(monkeypatch):
    """Text message: the user reads the server state, not only the first error."""
    monkeypatch.setattr(lifecycle, "docker", _StartFails())
    monkeypatch.setattr(lifecycle, "_incident", lambda *a, **k: None, raising=False)

    async def scenario():
        async with lifecycle.ensure_restart(_srv(), True, what="mandatory backup"):
            raise HTTPException(502, detail="mandatory backup failed - nothing changed: full")

    with pytest.raises(HTTPException) as ei:
        asyncio.run(scenario())

    assert NOTE in str(ei.value.detail), (
        "The message still claims 'nothing changed' although the server is off:\n  "
        + str(ei.value.detail)
    )


def test_a_dict_message_gets_the_truth_too(monkeypatch):
    """The mod paths answer with `detail={...}` — there it belongs in a key of its own."""
    monkeypatch.setattr(lifecycle, "docker", _StartFails())
    monkeypatch.setattr(lifecycle, "_incident", lambda *a, **k: None, raising=False)

    async def scenario():
        async with lifecycle.ensure_restart(_srv(), True, what="mod change"):
            raise HTTPException(409, detail={"message": "nothing changed", "blockers": []})

    with pytest.raises(HTTPException) as ei:
        asyncio.run(scenario())

    d = ei.value.detail
    assert isinstance(d, dict) and "restart_warning" in d, (
        f"The server state is missing from the structured message: {d!r}"
    )
    assert NOTE in d["restart_warning"]


def test_a_successful_restart_does_not_change_the_message(monkeypatch):
    """Cross-check: if the server came back, 'nothing changed' is true — and stays untouched."""
    dk = _StartWorks()
    monkeypatch.setattr(lifecycle, "docker", dk)

    async def scenario():
        async with lifecycle.ensure_restart(_srv(), True, what="mandatory backup"):
            raise HTTPException(502, detail="mandatory backup failed - nothing changed: full")

    with pytest.raises(HTTPException) as ei:
        asyncio.run(scenario())

    assert dk.calls == ["start"]
    assert NOTE not in str(ei.value.detail)


def test_a_previously_stopped_server_gets_no_note(monkeypatch):
    """Cross-check: if it was off before, 'nothing changed' is the truth."""
    monkeypatch.setattr(lifecycle, "docker", _StartFails())

    async def scenario():
        async with lifecycle.ensure_restart(_srv(), False, what="mandatory backup"):
            raise HTTPException(502, detail="nothing changed")

    with pytest.raises(HTTPException) as ei:
        asyncio.run(scenario())
    assert NOTE not in str(ei.value.detail)


# ------------------------------------------------------------------ the seam before it

def test_stop_and_snapshot_inherits_the_truth(wire_lifecycle, monkeypatch):
    """The caller has to do nothing — the message comes from the choke point."""
    class _Dk:
        def __init__(self):
            self.calls: list[str] = []

        async def state(self, name):
            return {"exists": True, "running": self.running, "restarting": False, "restarts": 0}

        async def action(self, name, act, **kw):
            self.calls.append(act)
            if act == "stop":
                self.running = False
            if act == "start":
                raise RuntimeError("socket proxy not responding")

    dk = _Dk()
    dk.running = True

    class _Driver:
        async def graceful_shutdown(self, server, **kw):
            return True

        def backup_spec(self, server):
            return SimpleNamespace(data_dir="/tmp/x", world_subdirs=(), pre_hook=None,
                                   post_hook=None)

    wire_lifecycle(dk, _Driver())
    monkeypatch.setattr(lifecycle, "_incident", lambda *a, **k: None, raising=False)

    def _boom(label, **kw):
        raise OSError("disk full")
    monkeypatch.setattr(lifecycle, "create_backup", _boom)

    with pytest.raises(HTTPException) as ei:
        asyncio.run(lifecycle.stop_and_snapshot(_srv(), _Driver(), "pre-test"))

    assert ei.value.status_code == 502
    assert NOTE in str(ei.value.detail), (
        "The 502 claims 'nothing changed' although the server is off after the failed "
        "restart:\n  " + str(ei.value.detail)
    )
