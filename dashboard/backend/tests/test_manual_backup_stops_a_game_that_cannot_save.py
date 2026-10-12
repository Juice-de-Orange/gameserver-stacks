""""Back up now" stops a game that cannot save, exactly like the weekly backup does.

Valheim has no console and no stdin, so nothing can make it save from outside
(`BackupSpec.stop_for_consistency`). The weekly run stopped it, archived and started it again; the
manual button packed the running server. The container's `StartedAt` stayed the same, the
archive held only the last autosave (the first one, before any autosave, was 0 kB), and the page
still said "completed". Both paths now go through `backup.backup_with_stop`.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.models import Role, User
from app.models.server import GameServer, GameType
from app.routers import server_ops
from app.services import backup as bk
from app.services import lifecycle
from app.services.drivers.pz import PZDriver
from app.services.drivers.valheim import ValheimDriver

ADMIN = User(id=1, display_name="Admin", username="admin", pw_hash="x", role=Role.admin)


class _Progress:
    def __init__(self):
        self.steps: list[str] = []

    def step(self, text, *, current=0, total=0):
        self.steps.append(text)


def _world(tmp_path, game):
    if game == GameType.valheim:
        (tmp_path / "config" / "worlds_local").mkdir(parents=True)
        (tmp_path / "config" / "worlds_local" / "Dedicated.db").write_bytes(b"world")
    else:
        (tmp_path / "data" / "Saves").mkdir(parents=True)
        (tmp_path / "data" / "Saves" / "map.bin").write_bytes(b"world")
    return GameServer(id=7, key="srv-1", game_type=game, container_name="srv-1", data_dir=str(tmp_path))


@pytest.fixture()
def events(monkeypatch):
    """What happens to the server, in order. It is running when the backup starts."""
    seen: list[str] = []

    async def stop_and_confirm(server, **kw):
        seen.append("stop")
        return True, True

    async def start_server(server):
        seen.append("start")

    real_create = bk.create_backup

    def create_backup(label, **kw):
        seen.append("pack" if kw.get("pre_hook") is None else "pack-hot")
        return real_create(label, **kw)

    monkeypatch.setattr(lifecycle, "stop_and_confirm", stop_and_confirm)
    monkeypatch.setattr(lifecycle, "start_server", start_server)
    monkeypatch.setattr(lifecycle, "create_backup", create_backup)
    monkeypatch.setattr(bk, "create_backup", create_backup)
    monkeypatch.setattr(bk, "_incident", lambda server, kind, detail: seen.append(f"incident:{kind}"))
    return seen


def _press_back_up_now(monkeypatch, server, driver) -> dict:
    captured = {}

    def start(srv, kind, work):
        captured["work"] = work
        return {"id": 1}

    monkeypatch.setattr(server_ops.operations, "start", start)
    monkeypatch.setattr(server_ops.operations, "running", lambda srv, kind: None)
    monkeypatch.setattr(server_ops, "record", lambda *a, **kw: None)
    asyncio.run(server_ops.create_backup(server=server, driver=driver, session=None, user=ADMIN))
    progress = _Progress()
    result = asyncio.run(captured["work"](progress))
    return {**result, "steps": progress.steps}


def test_back_up_now_stops_valheim_archives_and_starts_it_again(tmp_path, monkeypatch, events):
    result = _press_back_up_now(monkeypatch, _world(tmp_path, GameType.valheim), ValheimDriver())
    assert events == ["stop", "pack", "start"]
    assert result["summary"]["code"] == "backup.created_with_stop"
    assert "Stopping the server for a consistent backup…" in result["steps"]
    assert result.get("quiesce_warning") is None, "a stopped world needs no 'copy while running' note"


def test_a_game_that_can_save_is_still_backed_up_while_running(tmp_path, monkeypatch, events):
    result = _press_back_up_now(monkeypatch, _world(tmp_path, GameType.pz), PZDriver())
    assert events == ["pack"]
    assert result["summary"]["code"] == "backup.created"


def test_a_server_that_does_not_come_back_fails_the_operation_and_names_the_backup(
        tmp_path, monkeypatch, events):
    async def start_fails(server):
        raise RuntimeError("port already in use")

    monkeypatch.setattr(lifecycle, "start_server", start_fails)
    with pytest.raises(HTTPException) as e:
        _press_back_up_now(monkeypatch, _world(tmp_path, GameType.valheim), ValheimDriver())
    assert e.value.detail["code"] == "backup.created_restart_failed"
    assert e.value.detail["params"]["name"].endswith(".tar.gz")
    assert "port already in use" in e.value.detail["message"]
    assert events == ["stop", "pack", "incident:restart_failed"]


def test_a_stopped_server_is_not_started_by_its_backup(tmp_path, monkeypatch, events):
    async def was_off(server, **kw):
        events.append("stop")
        return False, True

    monkeypatch.setattr(lifecycle, "stop_and_confirm", was_off)
    result = _press_back_up_now(monkeypatch, _world(tmp_path, GameType.valheim), ValheimDriver())
    assert events == ["stop", "pack"]
    assert result["summary"]["code"] == "backup.created"


def test_the_weekly_backup_takes_the_same_path(tmp_path, monkeypatch, events):
    server = _world(tmp_path, GameType.valheim)
    monkeypatch.setattr(bk.servers_registry, "list_enabled_with_driver",
                        lambda: [(server, ValheimDriver())])
    monkeypatch.setattr(bk, "prune_backups", lambda *a, **kw: 0)
    monkeypatch.setattr(bk, "settings", SimpleNamespace(
        backup_retention_days=30, backup_max_gb_per_server=0, backup_keep_min=1))
    [result] = asyncio.run(bk.scheduled_backup())
    assert events == ["stop", "pack", "start"]
    assert result["stopped"] is True and "error" not in result
