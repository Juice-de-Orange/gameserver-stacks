"""The old container of a successful recreate goes with its anonymous volumes.

An image with a `VOLUME` (Reforger's `/steamcmd` before A12, others still) gets a fresh
anonymous volume per container; removing `{name}-prev` without `v=1` left one more on the host
with every recreate. Binds and named volumes are never touched by `v=1`.
"""
from __future__ import annotations

import asyncio

from app.models.server import GameServer, GameType, ManagementMode
from app.services import version_manager as vm


def test_the_old_container_is_removed_with_its_anonymous_volumes(monkeypatch):
    removed: list[tuple[str, bool]] = []

    class _Dk:
        async def state(self, name):
            return {"exists": not name.endswith("-prev"), "running": True}

        async def action(self, name, act, **kw):
            pass

        async def rename(self, a, b):
            pass

        async def create(self, name, payload):
            pass

        async def remove(self, name, force=False, volumes=False):
            removed.append((name, volumes))

    class _Drv:
        version_required = False

        def backup_spec(self, server):
            from app.services.drivers.base import BackupSpec
            return BackupSpec(data_dir="/tmp", world_subdirs=("world",))

    def _srv():
        s = GameServer(key="rf-1", game_type=GameType.arma_reforger, management_mode=ManagementMode.dashboard,
                       game_version="", enabled=True, container_name="rf-1", data_dir="/tmp/rf-1")
        s.id = 1
        return s

    async def _stop_and_snapshot(server, drv, label, **kw):
        return True, {"name": f"20261012-000000-{label}.tar.gz"}

    async def _ok(*a, **kw):
        return True

    async def _none(*a, **kw):
        return []

    monkeypatch.setattr(vm, "docker", _Dk())
    monkeypatch.setattr(vm, "_load", lambda k: _srv())
    monkeypatch.setattr(vm, "get_driver", lambda s: _Drv())
    monkeypatch.setattr(vm, "_record_pending", lambda *a: 1)
    monkeypatch.setattr(vm, "_finish", lambda *a: None)
    monkeypatch.setattr(vm, "_persist_version", lambda *a, **kw: {})
    monkeypatch.setattr(vm, "build_create_payload", lambda *a, **kw: {})
    monkeypatch.setattr(vm, "_cleanup_old_images", _none)
    monkeypatch.setattr(vm.servers_registry, "resolve_secret", lambda s: "x")
    monkeypatch.setattr(vm.lifecycle, "stop_and_snapshot", _stop_and_snapshot)
    monkeypatch.setattr(vm.mod_state, "guard_boot", _none)
    monkeypatch.setattr(vm, "verify_boot", _ok)

    asyncio.run(vm.change_version("rf-1", rebuild=True))

    # The leftover check before the swap and the cleanup after it both remove `-prev`.
    assert removed and all(name == "rf-1-prev" and volumes for name, volumes in removed)
