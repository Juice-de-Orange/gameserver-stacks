"""The stats poll records `last_active_at` — the clock the idle auto-stop hangs on.

If only `idle_autostop` set the field, and only AFTER the timeout had passed, a short session
that ended before the next hourly check would leave no trace: the server would still count as
empty for weeks and be stopped right afterwards. The poll sees the players every minute anyway.
"""
import asyncio
import datetime as dt

from sqlmodel import Session

from app.models.server import GameServer, GameType
from app.models.player import utcnow_naive
from app.services import stats_poller as sp


def _srv(sid=1, container="c"):
    s = GameServer(key="pz-1", game_type=GameType.pz, container_name=container)
    s.id = sid
    return s


class _Docker:
    def __init__(self, running=True):
        self.running = running

    async def state(self, name):
        return {"running": self.running}


class _Drv:
    def __init__(self, names):
        self.names = names

    async def players(self, server):
        return {"count": len(self.names), "names": self.names}


def _run(monkeypatch, mem_engine, srv, names, old=None):
    monkeypatch.setattr(sp, "engine", mem_engine)
    monkeypatch.setattr(sp, "docker", _Docker())
    with Session(mem_engine, expire_on_commit=False) as s:
        srv.last_active_at = old
        s.add(srv)
        s.commit()
    asyncio.run(sp._poll_server(srv, _Drv(names)))
    with Session(mem_engine) as s:
        return s.get(GameServer, srv.id).last_active_at


def test_player_online_sets_the_clock(monkeypatch, mem_engine):
    old = utcnow_naive() - dt.timedelta(days=27)
    now = _run(monkeypatch, mem_engine, _srv(), ["Steve"], old=old)
    assert now is not None and (utcnow_naive() - now).total_seconds() < 60


def test_empty_server_leaves_the_clock_alone(monkeypatch, mem_engine):
    """An empty server must NOT set the clock — otherwise it would never be stopped."""
    old = utcnow_naive() - dt.timedelta(days=27)
    assert _run(monkeypatch, mem_engine, _srv(), [], old=old) == old


def test_clock_starts_even_when_it_was_never_set(monkeypatch, mem_engine):
    assert _run(monkeypatch, mem_engine, _srv(), ["Steve"], old=None) is not None
