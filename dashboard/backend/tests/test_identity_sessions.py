"""Game account ID, log-exact session times and the map of death locations.

All three hang off the same source: since the join/leave lines are read from `data/Logs`, the
SteamID and the exact time are available. Without them the stats poller only knows "is here now"
(± poll interval) and no ID at all — `player_sessions.steam_id` stayed empty for good.

The HYBRID nature matters: the log line improves things but is not a precondition. Where it is
missing (Minecraft, Vintage Story, Reforger), everything must work as before.
"""
import asyncio
import datetime as dt

from sqlmodel import Session, select

from app.models import PlayerEvent, PlayerSession
from app.models.player import utcnow_naive
from app.models.server import GameServer, GameType
from app.services import stats_calc


def _srv(key="pz-1", sid=1):
    s = GameServer(key=key, game_type=GameType.pz, container_name=key)
    s.id = sid
    return s


class _Docker:
    async def state(self, name):
        return {"running": True}


class _Drv:
    def __init__(self, names):
        self.names = names

    async def players(self, server):
        return {"count": len(self.names), "names": list(self.names)}


def _poll(monkeypatch, mem_engine, server, drv):
    from app.services import stats_poller

    monkeypatch.setattr(stats_poller, "engine", mem_engine)
    monkeypatch.setattr(stats_poller, "docker", _Docker())
    asyncio.run(stats_poller._poll_server(server, drv))


# ------------------------------------------------------------------ Sessions from the log line

def test_session_takes_time_and_id_from_the_log_line(monkeypatch, mem_engine):
    srv = _srv()
    # Inside the time window (LOG_WINDOW) — an older line deliberately no longer counts.
    exact = utcnow_naive() - dt.timedelta(minutes=3)
    with Session(mem_engine) as s:
        s.add(PlayerEvent(username="Ada", kind="join", at=exact,
                          game_id="76561198000000001", server_id=1, x=1, y=2, z=0))
        s.commit()

    _poll(monkeypatch, mem_engine, srv, _Drv(["Ada"]))

    with Session(mem_engine) as s:
        row = s.exec(select(PlayerSession)).one()
    assert row.joined_at == exact                    # not the poll time
    assert row.steam_id == "76561198000000001"


def test_without_a_log_line_everything_stays_as_before(monkeypatch, mem_engine):
    """Minecraft/VS/Reforger deliver no such events — that must not break anything."""
    srv = _srv()
    before = utcnow_naive()
    _poll(monkeypatch, mem_engine, srv, _Drv(["Ada"]))

    with Session(mem_engine) as s:
        row = s.exec(select(PlayerSession)).one()
    assert row.steam_id == ""
    assert row.joined_at >= before.replace(microsecond=0)   # poll time as the fallback


def test_an_old_log_line_is_no_longer_used(monkeypatch, mem_engine):
    """Otherwise a new session would get the time of a join from the day before yesterday."""
    srv = _srv()
    ancient = utcnow_naive() - dt.timedelta(days=2)
    with Session(mem_engine) as s:
        s.add(PlayerEvent(username="Ada", kind="join", at=ancient, game_id="1", server_id=1))
        s.commit()

    _poll(monkeypatch, mem_engine, srv, _Drv(["Ada"]))

    with Session(mem_engine) as s:
        row = s.exec(select(PlayerSession)).one()
    assert row.joined_at > ancient + dt.timedelta(days=1)


def test_session_end_comes_from_the_leave_line(monkeypatch, mem_engine):
    srv = _srv()
    joined = utcnow_naive() - dt.timedelta(hours=2)
    left = joined + dt.timedelta(hours=1)
    with Session(mem_engine) as s:
        s.add(PlayerSession(username="Ada", joined_at=joined, server_id=1))
        s.add(PlayerEvent(username="Ada", kind="leave", at=left, game_id="42", server_id=1))
        s.commit()

    _poll(monkeypatch, mem_engine, srv, _Drv([]))        # nobody online any more

    with Session(mem_engine) as s:
        row = s.exec(select(PlayerSession)).one()
    assert row.left_at == left
    assert row.duration_seconds == 3600                  # exact, not rounded to the poll
    assert row.steam_id == "42"


def test_a_leave_line_before_the_join_is_ignored(monkeypatch, mem_engine):
    """A leave BEFORE the session start would produce a negative duration."""
    srv = _srv()
    joined = utcnow_naive() - dt.timedelta(minutes=10)
    with Session(mem_engine) as s:
        s.add(PlayerSession(username="Ada", joined_at=joined, server_id=1))
        s.add(PlayerEvent(username="Ada", kind="leave", at=joined - dt.timedelta(hours=1), server_id=1))
        s.commit()

    _poll(monkeypatch, mem_engine, srv, _Drv([]))

    with Session(mem_engine) as s:
        row = s.exec(select(PlayerSession)).one()
    assert row.left_at >= joined and row.duration_seconds >= 0


# ------------------------------------------------------------------ Matching by ID

def test_a_name_change_stays_one_person():
    """Without matching by ID the leaderboard listed the same person twice."""
    t = dt.datetime(2026, 7, 1, 12, 0)
    sessions = [
        PlayerSession(username="OldName", steam_id="7656", joined_at=t,
                      left_at=t + dt.timedelta(hours=1), duration_seconds=3600),
        PlayerSession(username="NewName", steam_id="7656", joined_at=t + dt.timedelta(days=1),
                      left_at=t + dt.timedelta(days=1, hours=2), duration_seconds=7200),
    ]
    alias = stats_calc.canonical_names(sessions)
    assert alias == {"OldName": "NewName", "NewName": "NewName"}   # the most recently used name wins

    summ = stats_calc.summarize(sessions, alias=alias)
    assert len(summ) == 1
    assert summ[0]["username"] == "NewName"
    assert summ[0]["total_seconds"] == 10800
    assert summ[0]["days_active"] == 2


def test_sessions_without_an_id_are_not_guessed():
    t = dt.datetime(2026, 7, 1, 12, 0)
    sessions = [
        PlayerSession(username="A", steam_id="", joined_at=t, duration_seconds=60),
        PlayerSession(username="B", steam_id="", joined_at=t, duration_seconds=60),
    ]
    assert stats_calc.canonical_names(sessions) == {}
    assert len(stats_calc.summarize(sessions)) == 2


def test_different_ids_stay_different():
    t = dt.datetime(2026, 7, 1, 12, 0)
    sessions = [
        PlayerSession(username="A", steam_id="1", joined_at=t, duration_seconds=60),
        PlayerSession(username="B", steam_id="2", joined_at=t, duration_seconds=60),
    ]
    assert len(stats_calc.summarize(sessions, alias=stats_calc.canonical_names(sessions))) == 2
