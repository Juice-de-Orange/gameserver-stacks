"""Admin stats endpoints must scope cleanly per server/game via `?server=`.

Without a scope, players with the same name on different games (PZ "Steve" vs. MC "Steve") merge
into ONE person in summary/leaderboards/feed. These tests pin the separation against a real
in-memory DB (`mem_engine`).
"""
import datetime as dt

import fastapi
import pytest
from sqlmodel import Session

from app.models import PlayerEvent, PlayerSession
from app.models.server import GameServer, GameType
from app.routers import stats


def _seed(mem_engine, monkeypatch):
    """Two servers (PZ=1 default, MC=2), the same name "Steve" on both."""

    now = dt.datetime(2026, 1, 1, 12, 0, 0)
    with Session(mem_engine) as s:
        pz = GameServer(key="pz-1", game_type=GameType.pz, container_name="pz-1")
        pz.id = 1
        mc = GameServer(key="mc", game_type=GameType.minecraft, container_name="mc")
        mc.id = 2
        s.add(pz)
        s.add(mc)
        # 2 PZ sessions + 1 MC session, all "Steve"
        s.add(PlayerSession(username="Steve", joined_at=now, left_at=now, duration_seconds=100, server_id=1))
        s.add(PlayerSession(username="Steve", joined_at=now, left_at=now, duration_seconds=100, server_id=1))
        s.add(PlayerSession(username="Steve", joined_at=now, left_at=now, duration_seconds=100, server_id=2))
        # 1 PZ death + 2 MC deaths
        s.add(PlayerEvent(username="Steve", kind="death", at=now, server_id=1))
        s.add(PlayerEvent(username="Steve", kind="death", at=now, server_id=2))
        s.add(PlayerEvent(username="Steve", kind="death", at=now, server_id=2))
        s.commit()


def test_summary_scoped_by_server(mem_engine, monkeypatch):
    _seed(mem_engine, monkeypatch)
    with Session(mem_engine) as s:
        pz = stats.summary(server="pz-1", session=s, _=None)
        assert len(pz) == 1 and pz[0]["username"] == "Steve" and pz[0]["sessions"] == 2
        mc = stats.summary(server="mc", session=s, _=None)
        assert len(mc) == 1 and mc[0]["sessions"] == 1
        # without a filter: deliberately mixed across games (3 sessions)
        allrows = stats.summary(server=None, session=s, _=None)
        assert len(allrows) == 1 and allrows[0]["sessions"] == 3


def test_leaderboards_deaths_scoped_by_server(mem_engine, monkeypatch):
    _seed(mem_engine, monkeypatch)
    with Session(mem_engine) as s:
        mc = stats.leaderboards(server="mc", session=s, _=None)
        assert mc["deaths"] == [{"username": "Steve", "value": 2}]      # MC deaths only
        pz = stats.leaderboards(server="pz-1", session=s, _=None)
        assert pz["deaths"] == [{"username": "Steve", "value": 1}]      # PZ death only


def test_feed_and_online_scoped_by_server(mem_engine, monkeypatch):
    _seed(mem_engine, monkeypatch)
    with Session(mem_engine) as s:
        feed_mc = stats.feed(server="mc", session=s, _=None)
        assert len(feed_mc) == 2 and all(e["kind"] == "death" for e in feed_mc)
        feed_pz = stats.feed(server="pz-1", session=s, _=None)
        assert len(feed_pz) == 1


def test_unknown_server_key_is_404(mem_engine, monkeypatch):
    _seed(mem_engine, monkeypatch)
    with Session(mem_engine) as s:
        with pytest.raises(fastapi.HTTPException) as ei:
            stats.summary(server="does-not-exist", session=s, _=None)
    assert ei.value.status_code == 404
