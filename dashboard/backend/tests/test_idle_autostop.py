"""Idle auto-stop — stops only on confirmed idleness, never when RCON is unknown."""
import asyncio
import datetime as dt

from sqlmodel import Session, select

from app.models import ServerIncident
from app.models.player import utcnow_naive
from app.models.server import GameServer, GameType
from app.services import idle_autostop as ia


def _srv(sid, last_active=None, enabled=True, timeout=None, container="c"):
    s = GameServer(
        key="s", game_type=GameType.pz, container_name=container,
        idle_autostop_enabled=enabled, idle_timeout_minutes=timeout, last_active_at=last_active,
    )
    s.id = sid
    return s


class _Docker:
    def __init__(self, running=True, started_at=None):
        self.running = running
        self.started_at = started_at
        self.actions: list[tuple[str, str]] = []

    async def state(self, name):
        return {"running": self.running, "started_at": self.started_at}

    async def action(self, name, action, **kw):
        self.actions.append((name, action))


class _Drv:
    # Before stopping, the idle stop asks whether the same data folder is being packed right now —
    # a stop in the middle of the tar run yields the torso the weekly run already guards against
    # in the other direction.
    def backup_spec(self, server):
        from types import SimpleNamespace
        return SimpleNamespace(data_dir="/tmp/not-a-real-folder")

    def __init__(self, count=0, fail=False):
        self.count = count
        self.fail = fail

    async def players(self, server):
        if self.fail:
            raise RuntimeError("rcon down")
        return {"count": self.count, "names": []}


def _seed(mem_engine, srv):
    # expire_on_commit=False → srv attributes stay readable after the commit (no DetachedInstanceError;
    # _check_server reads the fields from the passed object, _touch/_incident write by id).
    with Session(mem_engine, expire_on_commit=False) as s:
        s.add(srv)
        s.commit()


def _run(monkeypatch, mem_engine, srv, drv, wire_lifecycle=None, started_at=None):
    monkeypatch.setattr(ia, "engine", mem_engine)
    d = _Docker(running=True, started_at=started_at)
    monkeypatch.setattr(ia, "docker", d)
    if wire_lifecycle is not None:
        wire_lifecycle(d, drv)  # the stop goes through `lifecycle`
    _seed(mem_engine, srv)
    asyncio.run(ia._check_server(srv, drv, default_timeout=28800))
    return d


def test_stops_when_idle_and_empty(monkeypatch, mem_engine, wire_lifecycle):
    srv = _srv(1, last_active=utcnow_naive() - dt.timedelta(minutes=200), timeout=60)
    d = _run(monkeypatch, mem_engine, srv, _Drv(count=0), wire_lifecycle)
    assert d.actions == [("c", "stop")]
    with Session(mem_engine) as s:
        assert len(s.exec(select(ServerIncident)).all()) == 1  # idle_autostop incident


def test_no_stop_when_rcon_unknown(monkeypatch, mem_engine):
    srv = _srv(1, last_active=utcnow_naive() - dt.timedelta(minutes=200), timeout=60)
    d = _run(monkeypatch, mem_engine, srv, _Drv(fail=True))
    assert d.actions == []  # RCON unreachable = unknown → never stop


def test_touch_when_players_present(monkeypatch, mem_engine):
    srv = _srv(1, last_active=utcnow_naive() - dt.timedelta(minutes=200), timeout=60)
    d = _run(monkeypatch, mem_engine, srv, _Drv(count=3))
    assert d.actions == []
    with Session(mem_engine) as s:
        assert (utcnow_naive() - s.get(GameServer, 1).last_active_at).total_seconds() < 60  # touched


def test_no_stop_before_timeout(monkeypatch, mem_engine):
    srv = _srv(1, last_active=utcnow_naive() - dt.timedelta(minutes=30), timeout=60)
    d = _run(monkeypatch, mem_engine, srv, _Drv(count=0))
    assert d.actions == []


def test_grace_on_first_seen(monkeypatch, mem_engine):
    srv = _srv(1, last_active=None, timeout=60)
    d = _run(monkeypatch, mem_engine, srv, _Drv(count=0))
    assert d.actions == []
    with Session(mem_engine) as s:
        assert s.get(GameServer, 1).last_active_at is not None  # clock started, not stopped


def test_skip_when_disabled(monkeypatch, mem_engine):
    srv = _srv(1, last_active=utcnow_naive() - dt.timedelta(minutes=99999), enabled=False, timeout=60)
    d = _run(monkeypatch, mem_engine, srv, _Drv(count=0))
    assert d.actions == []


# --- Grace period after the start ------------------------------------------------------------
# `last_active_at` is only set when players are seen, never on start. Once the idle time was
# above the timeout, the next hourly round stopped every manually started server right away —
# such servers could not be started at all.
#
# The grace period only SUSPENDS the stop; it does NOT rewind the clock. That is the whole
# difference: a nightly restart plan makes `started_at` fresh every day and would disable the
# idle stop for good with a clock reset.
#
# Every case runs with `wire_lifecycle`, so `d.actions` is real evidence: without the fixture
# nobody calls `_Docker.action`, the list would necessarily be empty — and the real lifecycle
# client would attempt network access.


def _stamp(minutes_ago, frac=".263540726"):
    return (utcnow_naive() - dt.timedelta(minutes=minutes_ago)).strftime(f"%Y-%m-%dT%H:%M:%S{frac}Z")


def test_fresh_start_gets_a_grace_window(monkeypatch, mem_engine, wire_lifecycle):
    """Just started: do not stop, although the clock is far above the timeout."""
    srv = _srv(1, last_active=utcnow_naive() - dt.timedelta(minutes=38978), timeout=28800)
    d = _run(monkeypatch, mem_engine, srv, _Drv(count=0), wire_lifecycle, started_at=_stamp(5))
    assert d.actions == []  # NO stop


def test_grace_does_not_rewind_the_clock(monkeypatch, mem_engine, wire_lifecycle):
    """The crux: the grace period does NOT set `last_active_at` — otherwise a nightly restart plan
    disables the idle stop forever."""
    old = utcnow_naive() - dt.timedelta(minutes=38978)
    srv = _srv(1, last_active=old, timeout=28800)
    _run(monkeypatch, mem_engine, srv, _Drv(count=0), wire_lifecycle, started_at=_stamp(5))
    with Session(mem_engine) as s:
        assert s.get(GameServer, 1).last_active_at == old  # unchanged


def test_nightly_restart_does_not_disable_the_idle_stop(monkeypatch, mem_engine, wire_lifecycle):
    """Server with a restart plan: once the grace period is over, the stop applies anyway."""
    srv = _srv(1, last_active=utcnow_naive() - dt.timedelta(minutes=38978), timeout=28800)
    started = _stamp(ia.START_GRACE_MIN + 30)  # restarted last night, grace period over
    d = _run(monkeypatch, mem_engine, srv, _Drv(count=0), wire_lifecycle, started_at=started)
    assert d.actions == [("c", "stop")]


def test_grace_boundary(monkeypatch, mem_engine, wire_lifecycle):
    """Exactly on the boundary it stops (`<`, not `<=`) — no off-by-one in the window."""
    srv = _srv(1, last_active=utcnow_naive() - dt.timedelta(minutes=200), timeout=60)
    d = _run(monkeypatch, mem_engine, srv, _Drv(count=0), wire_lifecycle,
             started_at=_stamp(ia.START_GRACE_MIN + 1))
    assert d.actions == [("c", "stop")]


def test_player_during_grace_still_sets_the_clock(monkeypatch, mem_engine, wire_lifecycle):
    """If someone joins within the grace period, the player branch sets the clock — not the grace period.

    Without that the clock would stay old, and the server would be stopped right after the grace
    period ends although someone is playing on it. The player branch comes AFTER the grace check,
    so it is tested here with a start time OUTSIDE the grace period."""
    srv = _srv(1, last_active=utcnow_naive() - dt.timedelta(minutes=38978), timeout=28800)
    d = _run(monkeypatch, mem_engine, srv, _Drv(count=2), wire_lifecycle,
             started_at=_stamp(ia.START_GRACE_MIN + 30))
    assert d.actions == []
    with Session(mem_engine) as s:
        assert (utcnow_naive() - s.get(GameServer, 1).last_active_at).total_seconds() < 60


def test_missing_started_at_falls_back_to_the_old_behaviour(monkeypatch, mem_engine, wire_lifecycle):
    """If Docker delivers no start time, the plain player criterion applies."""
    srv = _srv(1, last_active=utcnow_naive() - dt.timedelta(minutes=200), timeout=60)
    d = _run(monkeypatch, mem_engine, srv, _Drv(count=0), wire_lifecycle, started_at=None)
    assert d.actions == [("c", "stop")]


def test_parse_docker_ts():
    from app.services.docker_client import start_time as p
    assert p("2026-08-01T18:52:03.263540726Z") == dt.datetime(2026, 8, 1, 18, 52, 3, 263540)
    assert p("2026-08-01T17:52:18Z") == dt.datetime(2026, 8, 1, 17, 52, 18)
    assert p("2026-08-01T18:52:03.25Z") == dt.datetime(2026, 8, 1, 18, 52, 3, 250000)
    assert p("  2026-08-01T17:52:18Z  ") == dt.datetime(2026, 8, 1, 17, 52, 18)
    # An offset is CONVERTED, not misread as UTC (20:52+02:00 == 18:52 UTC).
    assert p("2026-08-01T20:52:03+02:00") == dt.datetime(2026, 8, 1, 18, 52, 3)
    assert p("0001-01-01T00:00:00Z") is None      # never started
    assert p("2026-08-01T18:52:03Zjunk") is None
    assert p("2026-13-45T00:00:00Z") is None      # impossible date
    assert p(None) is None
    assert p("") is None
    assert p("broken") is None
