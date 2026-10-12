"""Switching a server off stops its container (`routers/servers._stop_for_disable`).

Before A2 `enabled=False` only took the row out of the fan-out. The container kept running under
`unless-stopped` — players stayed on a server without watchdog, backups or restarts, and its RAM
stayed booked. Now the stop comes first; if Docker refuses it, the row stays switched on.
"""
import asyncio

import pytest
from fastapi import HTTPException
from sqlmodel import Session

from app.models import Role, User
from app.models.server import GameServer, GameType
from app.routers import servers as servers_router
from app.services import lifecycle, world_lock

ADMIN = User(id=1, display_name="Admin", username="admin", pw_hash="x", role=Role.admin)


class _Docker:
    def __init__(self, running=True, fail=False):
        self.running, self.fail = running, fail

    async def state(self, name):
        if self.fail:
            raise RuntimeError("proxy down")
        return {"exists": True, "running": self.running, "status": "running" if self.running else "exited"}


@pytest.fixture()
def row(mem_engine, monkeypatch):
    stops: list[str] = []

    async def stop_server(server, *, announce_text=None, wait_timeout=None, precondition=None):
        stops.append(server.key)
        return True

    monkeypatch.setattr(lifecycle, "stop_server", stop_server)
    monkeypatch.setattr(servers_router.restarts, "reload_jobs", lambda: None)
    monkeypatch.setattr("app.services.broadcasts.reload_jobs", lambda: None)
    with Session(mem_engine) as s:
        s.add(GameServer(key="mc-1", game_type=GameType.minecraft, container_name="mc-1", enabled=True))
        s.commit()
    return stops


def _disable(mem_engine):
    with Session(mem_engine) as s:
        return asyncio.run(servers_router.update_server(
            "mc-1", servers_router.ServerUpdate(enabled=False), session=s, user=ADMIN))


def _enabled(mem_engine) -> bool:
    with Session(mem_engine) as s:
        return s.get(GameServer, 1).enabled


def test_disabling_a_running_server_stops_its_container(mem_engine, monkeypatch, row):
    monkeypatch.setattr(servers_router, "docker", _Docker(running=True))
    assert _disable(mem_engine)["enabled"] is False
    assert row == ["mc-1"]


def test_a_stopped_container_is_not_stopped_again(mem_engine, monkeypatch, row):
    monkeypatch.setattr(servers_router, "docker", _Docker(running=False))
    assert _disable(mem_engine)["enabled"] is False
    assert row == []


def test_if_docker_cannot_stop_it_the_server_stays_on(mem_engine, monkeypatch, row):
    monkeypatch.setattr(servers_router, "docker", _Docker(fail=True))
    with pytest.raises(HTTPException) as ei:
        _disable(mem_engine)
    assert ei.value.status_code == 502 and ei.value.detail["code"] == "server.disable_stop_failed"
    assert _enabled(mem_engine) is True


def test_not_in_the_middle_of_a_world_operation(mem_engine, monkeypatch, row):
    monkeypatch.setattr(servers_router, "docker", _Docker(running=True))
    monkeypatch.setitem(world_lock._holders, "mc-1", "version.change")
    with pytest.raises(HTTPException) as ei:
        _disable(mem_engine)
    assert ei.value.status_code == 409
    assert row == [] and _enabled(mem_engine) is True
