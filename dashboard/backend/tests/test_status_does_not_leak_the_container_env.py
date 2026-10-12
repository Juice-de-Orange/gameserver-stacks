"""`/status` and `/overview` must not hand out the container's environment.

Both are open to every user with `can_view_status` (every member by default), and the status
strip polls `/overview` for everyone. `docker.state()` returns the container's FULL env — it is
the only record of what the running container has set (`services/docker_client.py`) — and with
it RCON_PASSWORD, PZ's ADMINPASSWORD, Valheim's SERVER_PASS. The two payloads passed that
dict through unchanged.
"""
import asyncio

from sqlmodel import Session

from app.models.server import GameServer, GameType
from app.routers import server as server_router

SECRET = "rcon-secret-value-1234"


class _Docker:
    async def state(self, name):
        return {
            "exists": True, "running": True, "restarting": False, "restarts": 0, "status": "running",
            "started_at": "2026-10-12T07:00:00.000000000Z", "finished_at": None, "health": None,
            "id": "abc", "max_heap_bytes": 1024**3, "memory": 2 * 1024**3, "nano_cpus": 0,
            "env": {"RCON_PASSWORD": SECRET, "EULA": "TRUE"},
        }

    async def stats(self, name):
        return {"mem_bytes": 1, "mem_limit": 2, "cpu_percent": 0.0}


class _Drv:
    async def players(self, server):
        return {"count": 0, "names": []}

    def overview_stats(self, server):
        return {}


def _srv():
    s = GameServer(key="mc-1", game_type=GameType.minecraft, container_name="mc-1", data_dir="/tmp/mc-1")
    s.id = 1
    return s


def test_status_and_overview_carry_no_env(monkeypatch, mem_engine):
    monkeypatch.setattr(server_router, "docker", _Docker())
    monkeypatch.setattr(server_router, "get_driver", lambda s: _Drv())
    monkeypatch.setattr(server_router.restarts, "next_restart", lambda **kw: None)
    monkeypatch.setattr("app.db.engine", mem_engine)
    status = asyncio.run(server_router.status_payload(_srv()))
    with Session(mem_engine) as session:
        overview = asyncio.run(server_router.overview_payload(_srv(), session))
    for payload in (status, overview):
        assert "env" not in payload["container"]
        assert SECRET not in repr(payload)
        # What the pages use is still there.
        assert payload["container"]["running"] is True and payload["container"]["status"] == "running"
