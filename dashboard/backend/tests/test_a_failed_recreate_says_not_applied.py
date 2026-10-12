"""A rolled-back recreate says that the saved settings are not the running ones.

Measured: the edit page saved RAM limit 4096, the recreate rolled back (previous container,
`Memory: 0`), and the page kept showing 4096 with "Version change failed" as the only message.
The rollback brings the previous container back, so the honest statement is "saved, NOT applied":
in the failure message here, and on the edit page from the history row (`direction="recreate"`).
"""
from __future__ import annotations

import asyncio

import pytest
from fastapi import HTTPException

from app.models.server import GameServer, GameType, ManagementMode
from app.services import version_manager as vm


def _srv():
    s = GameServer(key="vs-1", game_type=GameType.vintagestory, management_mode=ManagementMode.dashboard,
                   game_version="1.22.6", enabled=True, container_name="vs-1", data_dir="/tmp/vs-1")
    s.id = 1
    return s


@pytest.fixture()
def failing_swap(monkeypatch):
    seen: dict = {}

    class _Dk:
        async def state(self, name):
            return {"exists": not name.endswith("-prev"), "running": True}

        async def action(self, name, act, **kw):
            pass

        async def rename(self, a, b):
            pass

        async def create(self, name, payload):
            pass

        async def remove(self, name, force=False):
            pass

    class _Drv:
        version_required = False

        def backup_spec(self, server):
            from app.services.drivers.base import BackupSpec
            return BackupSpec(data_dir="/tmp", world_subdirs=("Saves",))

    async def _stop_and_snapshot(server, driver, label, **kw):
        seen["backup_label"] = label
        seen["announce"] = kw.get("announce_text")
        return True, {"name": f"20261012-000000-{label}.tar.gz"}

    async def _no_boot(*a, **kw):
        return False

    async def _rolled_back(*a, **kw):
        return True, True

    async def _no_blockers(server):
        return []

    def _record(server, direction, *a):
        seen["direction"] = direction
        return 7

    monkeypatch.setattr(vm, "docker", _Dk())
    monkeypatch.setattr(vm, "_load", lambda k: _srv())
    monkeypatch.setattr(vm, "get_driver", lambda s: _Drv())
    monkeypatch.setattr(vm, "_record_pending", _record)
    monkeypatch.setattr(vm, "_finish", lambda vc_id, st, d: seen.update(status=st))
    monkeypatch.setattr(vm, "_persist_version", lambda *a, **kw: {})
    monkeypatch.setattr(vm, "build_create_payload", lambda *a, **kw: {})
    monkeypatch.setattr(vm.servers_registry, "resolve_secret", lambda s: "x")
    monkeypatch.setattr(vm.lifecycle, "stop_and_snapshot", _stop_and_snapshot)
    monkeypatch.setattr(vm.mod_state, "guard_boot", _no_blockers)
    monkeypatch.setattr(vm, "verify_boot", _no_boot)
    monkeypatch.setattr(vm, "_rollback_swap", _rolled_back)
    return seen


def test_a_rolled_back_recreate_says_the_settings_are_not_applied(failing_swap):
    with pytest.raises(HTTPException) as e:
        asyncio.run(vm.change_version("vs-1", rebuild=True))

    assert e.value.detail["code"] == "version.recreate_failed_rolled_back"
    assert "NOT applied" in e.value.detail["message"]
    # The edit page reads this row: newest entry, direction recreate, not ok.
    assert failing_swap["direction"] == "recreate"
    assert failing_swap["status"] == "rolled_back"


def test_a_version_change_keeps_its_own_message(failing_swap):
    with pytest.raises(HTTPException) as e:
        asyncio.run(vm.change_version("vs-1", game_version="1.22.7"))
    assert e.value.detail["code"] == "version.change_failed_rolled_back"
