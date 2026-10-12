"""A recreate and a version change carry the registry's values into the game's own file first.

The edit page syncs Minecraft's server.properties (and Reforger's config.json) when it saves, and
creating a server or its container syncs it too (A12). A recreate creates a new container from
the saved row as well, so it must not boot the game against a file that drifted from the
registry: otherwise RCON refuses the stored password and the boot check rolls the recreate back.
"""
from __future__ import annotations

import asyncio

from app.models.server import GameServer, GameType, ManagementMode
from app.services import version_manager as vm


def _srv():
    s = GameServer(key="mc-1", game_type=GameType.minecraft, management_mode=ManagementMode.dashboard,
                   game_version="26.2", enabled=True, container_name="mc-1", data_dir="/tmp/mc-1")
    s.id = 1
    return s


def _run(monkeypatch, driver):
    order: list[str] = []

    class _Dk:
        async def state(self, name):
            return {"exists": not name.endswith("-prev"), "running": True}

        async def action(self, name, act, **kw):
            pass

        async def rename(self, a, b):
            pass

        async def create(self, name, payload):
            order.append("create")

        async def remove(self, name, force=False):
            pass

    async def _stop_and_snapshot(server, drv, label, **kw):
        return True, {"name": f"20261012-000000-{label}.tar.gz"}

    async def _ok(*a, **kw):
        return True

    async def _none(*a, **kw):
        return []

    driver.order = order
    monkeypatch.setattr(vm, "docker", _Dk())
    monkeypatch.setattr(vm, "_load", lambda k: _srv())
    monkeypatch.setattr(vm, "get_driver", lambda s: driver)
    monkeypatch.setattr(vm, "_record_pending", lambda *a: 1)
    monkeypatch.setattr(vm, "_finish", lambda *a: None)
    monkeypatch.setattr(vm, "_persist_version", lambda *a, **kw: {})
    monkeypatch.setattr(vm, "build_create_payload", lambda *a, **kw: {})
    monkeypatch.setattr(vm, "_cleanup_old_images", _none)
    monkeypatch.setattr(vm.servers_registry, "resolve_secret", lambda s: "stored-secret")
    monkeypatch.setattr(vm.lifecycle, "stop_and_snapshot", _stop_and_snapshot)
    monkeypatch.setattr(vm.mod_state, "guard_boot", _none)
    monkeypatch.setattr(vm, "verify_boot", _ok)
    return order


class _SyncingDriver:
    version_required = False

    def backup_spec(self, server):
        from app.services.drivers.base import BackupSpec
        return BackupSpec(data_dir="/tmp", world_subdirs=("world",))

    def sync_config_from_registry(self, server, secret):
        self.order.append(f"sync:{server.key}:{secret}")
        return ["rcon.password"]


class _PlainDriver:
    version_required = False

    def backup_spec(self, server):
        from app.services.drivers.base import BackupSpec
        return BackupSpec(data_dir="/tmp", world_subdirs=("world",))


def test_a_recreate_syncs_the_file_before_the_new_container_exists(monkeypatch):
    order = _run(monkeypatch, _SyncingDriver())
    asyncio.run(vm.change_version("mc-1", rebuild=True))
    assert order == ["sync:mc-1:stored-secret", "create"]


def test_a_version_change_syncs_it_too(monkeypatch):
    order = _run(monkeypatch, _SyncingDriver())
    asyncio.run(vm.change_version("mc-1", game_version="26.3"))
    assert order == ["sync:mc-1:stored-secret", "create"]


def test_a_driver_without_the_seam_is_left_alone(monkeypatch):
    order = _run(monkeypatch, _PlainDriver())
    asyncio.run(vm.change_version("mc-1", rebuild=True))
    assert order == ["create"]
