"""A recreate is not a version change: own operation kind, lock name, backup label and texts.

Measured: a recreate from the edit page showed up as "Version change …" on the version page, its
mandatory backup was called `pre-version-1.22.6` (the version did not change) and its failure
said "Version change failed".
"""
from __future__ import annotations

import asyncio

import pytest

from app.models.server import GameServer, GameType, ManagementMode
from app.services import operations, version_manager as vm, world_lock


def _srv():
    s = GameServer(key="vs-1", game_type=GameType.vintagestory, management_mode=ManagementMode.dashboard,
                   game_version="1.22.6", enabled=True, container_name="vs-1", data_dir="/tmp/vs-1")
    s.id = 1
    return s


@pytest.fixture()
def swap(monkeypatch):
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
        seen["lock"] = world_lock.held_by(server.key)
        seen["backup_label"] = label
        seen["announce"] = kw.get("announce_text")
        return True, {"name": f"20261012-000000-{label}.tar.gz"}

    async def _boot(*a, **kw):
        return True

    async def _no_blockers(server):
        return []

    async def _no_cleanup(*a, **kw):
        return []

    monkeypatch.setattr(vm, "docker", _Dk())
    monkeypatch.setattr(vm, "_load", lambda k: _srv())
    monkeypatch.setattr(vm, "get_driver", lambda s: _Drv())
    monkeypatch.setattr(vm, "_record_pending", lambda server, direction, *a: seen.update(direction=direction) or 1)
    monkeypatch.setattr(vm, "_finish", lambda *a: None)
    monkeypatch.setattr(vm, "_persist_version", lambda *a, **kw: {})
    monkeypatch.setattr(vm, "build_create_payload", lambda *a, **kw: {})
    monkeypatch.setattr(vm, "_cleanup_old_images", _no_cleanup)
    monkeypatch.setattr(vm.servers_registry, "resolve_secret", lambda s: "x")
    monkeypatch.setattr(vm.lifecycle, "stop_and_snapshot", _stop_and_snapshot)
    monkeypatch.setattr(vm.mod_state, "guard_boot", _no_blockers)
    monkeypatch.setattr(vm, "verify_boot", _boot)
    return seen


def test_a_recreate_is_named_for_what_it_does(swap):
    asyncio.run(vm.change_version("vs-1", rebuild=True))

    assert swap["backup_label"] == "pre-recreate-1.22.6"
    assert swap["lock"] == "version.recreate"
    assert "Version change" not in swap["announce"]
    assert swap["direction"] == "recreate"
    assert world_lock.OPERATION_LABELS["version.recreate"] == "a container recreate"


def test_a_version_change_keeps_its_names(swap):
    asyncio.run(vm.change_version("vs-1", game_version="1.22.7"))

    assert swap["backup_label"] == "pre-version-1.22.7"
    assert swap["lock"] == "version.change"


def test_the_recreate_endpoint_starts_its_own_operation_kind(monkeypatch):
    from types import SimpleNamespace

    from app.routers import versions

    started: list[str] = []
    monkeypatch.setattr(versions.operations, "start", lambda server, kind, work: started.append(kind) or {"id": 1})
    monkeypatch.setattr(versions, "record", lambda *a, **kw: None)
    srv = SimpleNamespace(key="vs-1", id=1)

    asyncio.run(versions.recreate_container("vs-1", server=srv, session=None, user=None))

    assert started == ["version.recreate"]
    assert operations._final_text({}, "version.recreate") == "The container was recreated with the saved settings."
