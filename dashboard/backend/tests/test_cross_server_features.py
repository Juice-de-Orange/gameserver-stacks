"""Presence, leaderboard, come-over, announce (the harmless cross-server features)."""
import asyncio
import datetime as dt

import pytest
from sqlmodel import Session

from app.models.cross_server import CrossServerSettings
from app.models.player import PlayerEvent, PlayerSession
from app.models.server import GameServer, GameType
from app.services.cross_server import features


@pytest.fixture(autouse=True)
def _reset():
    features.reset_state()
    yield
    features.reset_state()


def _srv(session, key, sid, game=GameType.pz, **kw):
    s = GameServer(key=key, game_type=game, display_name=key.upper(), container_name=key, **kw)
    s.id = sid
    session.add(s)
    return s


# ----------------------------------------------------------------- Presence

def test_presence_groups_open_sessions_by_server(mem_engine):
    with Session(mem_engine) as s:
        _srv(s, "mc", 1, GameType.minecraft)
        _srv(s, "pz", 2, GameType.pz)
        now = dt.datetime(2026, 7, 7, 12, 0, 0)
        s.add(PlayerSession(username="Bob", joined_at=now, server_id=1))
        s.add(PlayerSession(username="Alice", joined_at=now, server_id=1))
        s.add(PlayerSession(username="Cara", joined_at=now, server_id=2))
        s.add(PlayerSession(username="Gone", joined_at=now, left_at=now, server_id=2))  # left → ignored
        s.commit()
        out = features.presence(s)
    by_key = {r["server_key"]: r for r in out}
    assert by_key["mc"]["count"] == 2 and by_key["mc"]["players"] == ["Alice", "Bob"]
    assert by_key["pz"]["count"] == 1 and by_key["pz"]["players"] == ["Cara"]  # "Gone" not included


def test_presence_off_when_disabled(mem_engine):
    with Session(mem_engine) as s:
        s.add(CrossServerSettings(presence_enabled=False))
        _srv(s, "mc", 1, GameType.minecraft)
        s.add(PlayerSession(username="Bob", joined_at=dt.datetime(2026, 7, 7), server_id=1))
        s.commit()
        assert features.presence(s) == []


# ----------------------------------------------------------------- Leaderboard

def test_leaderboard_playtime_and_events(mem_engine):
    with Session(mem_engine) as s:
        _srv(s, "mc", 1, GameType.minecraft)
        j = dt.datetime(2026, 7, 7, 10, 0, 0)
        s.add(PlayerSession(username="Bob", joined_at=j, left_at=j + dt.timedelta(hours=3), server_id=1))
        s.add(PlayerSession(username="Alice", joined_at=j, left_at=j + dt.timedelta(hours=1), server_id=1))
        s.add(PlayerEvent(username="Bob", kind="kill", at=j))
        s.add(PlayerEvent(username="Bob", kind="death", at=j))
        s.commit()
        lb = features.leaderboard(s)
    assert lb[0]["player"] == "Bob" and lb[0]["playtime_hours"] == 3.0
    assert lb[0]["kills"] == 1 and lb[0]["deaths"] == 1
    assert lb[0]["games"] == {"minecraft": 3.0}
    assert lb[1]["player"] == "Alice"


# ----------------------------------------------------------------- Come-over / Announce

class _Sink:
    def __init__(self):
        self.announced = []

    def capabilities(self):
        return frozenset({"announce", "console", "players"})  # can announce (the gate lets it through)

    async def announce(self, server, text):
        self.announced.append((server.key, text))


class _Docker:
    async def state(self, name):
        return {"running": True}


def test_come_over_excludes_origin_and_cooldown(monkeypatch, mem_engine):
    from app.services import servers as servers_registry

    with Session(mem_engine) as s:
        a = _srv(s, "a", 1)
        b = _srv(s, "b", 2)
        s.commit()
        s.refresh(a)
        s.refresh(b)
        sink_a, sink_b = _Sink(), _Sink()
        monkeypatch.setattr(features, "docker", _Docker())
        monkeypatch.setattr(servers_registry, "list_enabled_with_driver", lambda: [(a, sink_a), (b, sink_b)])

        res = asyncio.run(features.come_over(s, "a", "Bob"))
        assert res["sent"] is True and res["servers"] == ["b"]      # only to B, not to the origin A
        assert sink_b.announced and sink_a.announced == []
        assert "Bob" in sink_b.announced[0][1] and "A" in sink_b.announced[0][1]  # origin named

        res2 = asyncio.run(features.come_over(s, "a", "Bob"))       # again right away → cooldown
        assert res2["sent"] is False and res2["reason"] == "cooldown"


def test_announce_all_only_announce_targets(monkeypatch, mem_engine):
    from app.services import servers as servers_registry

    with Session(mem_engine) as s:
        a = _srv(s, "a", 1, announce_target=True)
        b = _srv(s, "b", 2, announce_target=False)
        s.commit()
        s.refresh(a)
        s.refresh(b)
        sink_a, sink_b = _Sink(), _Sink()
        monkeypatch.setattr(features, "docker", _Docker())
        monkeypatch.setattr(servers_registry, "list_enabled_with_driver", lambda: [(a, sink_a), (b, sink_b)])

        res = asyncio.run(features.announce_all(s, "Maintenance at 8 pm"))
        assert res["sent"] == ["a"]                 # only announce_target servers
        assert sink_a.announced and sink_b.announced == []


def test_announce_all_skips_games_without_announce_cap(monkeypatch, mem_engine):
    # Reforger and others have no broadcast verb → capabilities() without "announce" → skipped (not
    # in `sent`) and announce() is NOT called (no counting into the void).
    from app.services import servers as servers_registry

    class _NoAnnounce:
        def __init__(self):
            self.called = False

        def capabilities(self):
            return frozenset({"console", "players"})

        async def announce(self, server, text):
            self.called = True

    with Session(mem_engine) as s:
        a = _srv(s, "a", 1, announce_target=True)
        s.commit()
        s.refresh(a)
        drv = _NoAnnounce()
        monkeypatch.setattr(features, "docker", _Docker())
        monkeypatch.setattr(servers_registry, "list_enabled_with_driver", lambda: [(a, drv)])
        res = asyncio.run(features.announce_all(s, "Maintenance"))
        assert res["sent"] == [] and drv.called is False


def test_announce_all_off_when_disabled(monkeypatch, mem_engine):
    from app.services import servers as servers_registry

    with Session(mem_engine) as s:
        s.add(CrossServerSettings(announcements_enabled=False))
        a = _srv(s, "a", 1, announce_target=True)
        s.commit()
        s.refresh(a)
        monkeypatch.setattr(features, "docker", _Docker())
        monkeypatch.setattr(servers_registry, "list_enabled_with_driver", lambda: [(a, _Sink())])
        res = asyncio.run(features.announce_all(s, "hello"))
        assert res["sent"] == [] and res["reason"] == "announcements_disabled"
