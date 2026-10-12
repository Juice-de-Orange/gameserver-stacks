"""The boot gate must apply before EVERY start — including the two that write the world.

`guard_boot` names in its own docstring the paths that would otherwise boot "the unchecked
configuration anyway" — a scheduled restart, a watchdog autoheal, a manual action or a
**version change**. Without the gate in `version_manager` and `restore`, it would block the
harmless nightly restart and stand open on exactly the two paths that have just overwritten the
`.ini`.

Restore weighs heaviest: `extract_backup` writes `Server/` back, and in it lies the `<name>.ini`
with `Mods=`, `WorkshopItems=` and `Map=`. If the archive is older than the last mod change, the
restored `.ini` lists a map whose download is meanwhile in quarantine. `verify_boot` does not
catch that: it only proves that RCON answers — a map without a provider is a **join** crash. The
server boots, reports "backup … restored", and every player is thrown out on connecting, without
a warning anywhere.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.services import restore as restore_svc
from app.services import version_manager as vm


class _Blocker:
    code = "MAP_WITHOUT_PROVIDER"
    message = "The map Riverside no longer has a provider."


def _srv():
    return SimpleNamespace(id=1, key="pz-1", container_name="pz-1",
                           console_kind="none", game_type=None, data_dir="/nowhere")


class _Spec:
    data_dir = "/nowhere"
    world_subdirs = ("Saves", "Server")
    pre_hook = None
    post_hook = None


class _Driver:
    def backup_spec(self, server):
        return _Spec()


def test_the_restore_does_not_start_into_a_broken_mod_configuration(monkeypatch, tmp_path):
    """The restore has just overwritten the `.ini` — this is exactly when to check."""
    started: list[str] = []

    monkeypatch.setattr(restore_svc.bk, "valid_backup_name", lambda n: True)
    monkeypatch.setattr(restore_svc.os.path, "exists", lambda p: True)
    monkeypatch.setattr(restore_svc.bk, "extract_backup", lambda *a, **k: None)
    # The archive is a name here, not a file — the authenticity check
    # (`check_restorable`, in front of BOTH restore paths) would otherwise trip
    # over the missing file before the actual case is reached.
    monkeypatch.setattr(restore_svc.bk, "check_restorable", lambda *a, **k: None)
    monkeypatch.setattr(restore_svc.bk, "backup_path", lambda n, **k: "/nowhere/x.tar.gz")

    async def _stop_and_snapshot(server, driver, label, **kw):
        return True, {"name": "pre-restore.tar.gz"}
    monkeypatch.setattr(restore_svc.lifecycle, "stop_and_snapshot", _stop_and_snapshot)

    class _Dk:
        async def action(self, name, act, **kw):
            started.append(act)
    monkeypatch.setattr(restore_svc, "docker", _Dk())
    monkeypatch.setattr(restore_svc, "mod_state", _GateStub([_Blocker()]), raising=False)

    async def _verify(server, driver, timeout=0):
        return True
    monkeypatch.setattr(restore_svc, "verify_boot", _verify)

    with pytest.raises(HTTPException) as e:
        asyncio.run(restore_svc.restore(_srv(), _Driver(), "20260101-000000-weekly.tar.gz"))

    assert "start" not in started, "the server was started into the broken configuration"
    detail = str(e.value.detail)
    assert "Riverside" in detail, "the message does not name the blocker"


def test_the_restore_starts_normally_when_the_configuration_is_right(monkeypatch):
    """Cross-check — otherwise the gate would only be a lock and not a guard."""
    started: list[str] = []

    monkeypatch.setattr(restore_svc.bk, "valid_backup_name", lambda n: True)
    monkeypatch.setattr(restore_svc.os.path, "exists", lambda p: True)
    monkeypatch.setattr(restore_svc.bk, "extract_backup", lambda *a, **k: None)
    # The archive is a name here, not a file — the authenticity check
    # (`check_restorable`, in front of BOTH restore paths) would otherwise trip
    # over the missing file before the actual case is reached.
    monkeypatch.setattr(restore_svc.bk, "check_restorable", lambda *a, **k: None)
    monkeypatch.setattr(restore_svc.bk, "backup_path", lambda n, **k: "/nowhere/x.tar.gz")

    async def _stop_and_snapshot(server, driver, label, **kw):
        return True, {"name": "pre-restore.tar.gz"}
    monkeypatch.setattr(restore_svc.lifecycle, "stop_and_snapshot", _stop_and_snapshot)

    class _Dk:
        async def action(self, name, act, **kw):
            started.append(act)
    monkeypatch.setattr(restore_svc, "docker", _Dk())
    monkeypatch.setattr(restore_svc, "mod_state", _GateStub([]), raising=False)

    async def _verify(server, driver, timeout=0):
        return True
    monkeypatch.setattr(restore_svc, "verify_boot", _verify)

    result = asyncio.run(restore_svc.restore(_srv(), _Driver(), "20260101-000000-weekly.tar.gz"))
    assert started == ["start"]
    assert result["ok"] is True


def _wire_version_change(monkeypatch, blockers):
    """The swap of `change_version` with Docker, DB and driver stubbed; returns the Docker trace
    and the rollback calls."""
    from app.models.server import GameServer, GameType, ManagementMode

    trace: list[str] = []
    rollbacks: list[str] = []

    class _Dk:
        async def image_exists(self, image):
            return True

        async def state(self, name):
            return {"exists": not name.endswith("-prev"), "running": "stop" not in trace}

        async def action(self, name, act, **kw):
            trace.append(act)

        async def remove(self, name, force=False):
            trace.append("remove")

        async def rename(self, a, b):
            trace.append("rename")

        async def create(self, name, payload):
            trace.append("create")

    class _Drv:
        def backup_spec(self, server):
            from app.services.drivers.base import BackupSpec
            return BackupSpec(data_dir="/nowhere", world_subdirs=("Saves",))

        async def graceful_shutdown(self, server):
            return True

        async def announce(self, server, text):
            return None

    def _srv_row():
        s = GameServer(key="pz-1", game_type=GameType.pz, management_mode=ManagementMode.dashboard,
                       image_repo="repo", image_tag="42.19.0-unstable", enabled=True)
        s.id = 1
        return s

    async def _rollback_swap(*a, **kw):
        rollbacks.append("rollback")
        return True, True

    async def _boot(*a, **kw):
        return True

    dk = _Dk()
    monkeypatch.setattr(vm, "docker", dk)
    monkeypatch.setattr(vm, "_load", lambda k: _srv_row())
    monkeypatch.setattr(vm, "get_driver", lambda s: _Drv())
    monkeypatch.setattr(vm, "_record_pending", lambda *a, **kw: 1)
    monkeypatch.setattr(vm, "_finish", lambda *a, **kw: None)
    monkeypatch.setattr(vm, "_persist_version", lambda *a, **kw: None)
    monkeypatch.setattr(vm, "build_create_payload", lambda *a, **kw: {})
    monkeypatch.setattr(vm, "_rollback_swap", _rollback_swap)
    monkeypatch.setattr(vm, "_cleanup_old_images", lambda *a, **kw: asyncio.sleep(0, []))
    monkeypatch.setattr(vm, "verify_boot", _boot)
    monkeypatch.setattr(vm, "mod_state", _GateStub(blockers))
    monkeypatch.setattr(vm.servers_registry, "resolve_secret", lambda s: "x")
    monkeypatch.setattr(vm.lifecycle, "docker", dk)
    monkeypatch.setattr(vm.lifecycle, "_STOP_MARK_DELAY", 0)
    monkeypatch.setattr(vm.lifecycle, "_suppress", lambda *a, **k: None)
    monkeypatch.setattr(vm.lifecycle, "_driver", lambda s: _Drv())
    monkeypatch.setattr(vm.lifecycle, "stop_grace", lambda s: 1)
    monkeypatch.setattr(vm.lifecycle, "_locks", {})
    monkeypatch.setattr(vm.lifecycle, "create_backup",
                        lambda label, **kw: {"name": f"{label}.tar.gz"})
    return trace, rollbacks


def test_the_version_change_does_not_start_into_a_broken_mod_configuration(monkeypatch):
    """The docstring of `guard_boot` names the version change explicitly. A blocker after the
    new container is created must lead into the rollback — the new container never starts."""
    trace, rollbacks = _wire_version_change(monkeypatch, [_Blocker()])

    with pytest.raises(HTTPException) as e:
        asyncio.run(vm.change_version("pz-1", image_tag="42.20.0-release"))

    assert "create" in trace, f"the gate must sit after the create, trace: {trace}"
    assert "start" not in trace, "the new container was started into the broken configuration"
    assert rollbacks == ["rollback"], "a blocked version change must roll back"
    assert "Riverside" in str(e.value.detail), "the message does not name the blocker"


def test_the_version_change_starts_normally_when_the_configuration_is_right(monkeypatch):
    """Cross-check for the case above."""
    trace, rollbacks = _wire_version_change(monkeypatch, [])

    result = asyncio.run(vm.change_version("pz-1", image_tag="42.20.0-release"))

    assert result["status"] == "ok"
    assert trace.count("start") == 1 and rollbacks == []


class _GateStub:
    """Minimal stand-in for `mods.state`, so the test need not build half the mod world."""

    def __init__(self, blockers):
        self._b = blockers

    async def guard_boot(self, server):
        return self._b

    def guard_message(self, blockers):
        first = blockers[0].message if blockers else ""
        return f"The mod configuration is faulty — a start would damage the server: {first}"
