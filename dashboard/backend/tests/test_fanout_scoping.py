"""server_id scoping in the scheduler fan-out.

Core risk: without per-server_id scoping, the poll of server A would close the open sessions of
server B, and the log dedup would suppress events of other servers. These tests pin the
isolation against a real in-memory DB (`mem_engine`).
"""
import asyncio
import datetime as dt

from sqlmodel import Session, select

from app.models import MetricSample, PlayerEvent, PlayerSession
from app.models.player import utcnow_naive
from app.models.server import GameServer, GameType
from app.services.drivers.base import ServerEvent


def _srv(key, sid, container=None):
    s = GameServer(key=key, game_type=GameType.pz, container_name=container or key)
    s.id = sid
    return s


class _Docker:
    def __init__(self, running=True, logs="", stats=None):
        self.running = running
        self._logs = logs
        self._stats = stats or {}

    async def state(self, name):
        return {"running": self.running, "max_heap_bytes": None}

    async def logs(self, name, tail=1500, timestamps=True):
        return self._logs

    async def stats(self, name):
        return self._stats

    async def action(self, name, action, **kw):
        pass


class _EventDriver:
    """Driver stub whose parse_events returns fixed events."""

    def __init__(self, events):
        self._events = events

    def parse_events(self, server, raw):
        return list(self._events)

    # **The DRIVER says where the events come from** — an empty path list alone no longer means
    # "it is on stdout". For Project Zomboid the very same empty list means "I did not find the
    # files", and the parser then fell back to the Docker log, which structurally contains
    # nothing for PZ. That this dummy had to declare it is correct; a `getattr(..., False)` in
    # the seam would have silently held it to the old behaviour.
    events_from_files = False

    def event_log_paths(self, server):
        return []  # like the base default: events come from the Docker log

    async def players(self, server):
        return {"count": 0, "names": []}

    async def console(self, server, cmd, *, timeout=8.0):
        return ""


# ---------------------------------------------------------------- stats_poller

def test_poll_does_not_close_other_servers_session(monkeypatch, mem_engine, fake_driver_cls):
    from app.services import stats_poller

    a = _srv("a", 1)  # B is not needed here
    monkeypatch.setattr(stats_poller, "engine", mem_engine)
    monkeypatch.setattr(stats_poller, "docker", _Docker(running=True))

    now = utcnow_naive()
    with Session(mem_engine) as s:
        s.add(PlayerSession(username="alice", joined_at=now, server_id=1))
        s.add(PlayerSession(username="bob", joined_at=now, server_id=2))
        s.commit()

    # A polls (nobody online) → closes ONLY its own session (alice), not bob's (server B).
    asyncio.run(stats_poller._poll_server(a, fake_driver_cls(names=[])))
    with Session(mem_engine) as s:
        rows = {r.username: r for r in s.exec(select(PlayerSession)).all()}
    assert rows["alice"].left_at is not None
    assert rows["bob"].left_at is None


def test_null_sessions_belong_to_nobody(monkeypatch, mem_engine, fake_driver_cls):
    """A session without `server_id` is not inherited by any server (no default server)."""
    from app.services import stats_poller

    a, b = _srv("a", 1), _srv("b", 2)
    monkeypatch.setattr(stats_poller, "engine", mem_engine)
    monkeypatch.setattr(stats_poller, "docker", _Docker(running=True))

    with Session(mem_engine) as s:
        s.add(PlayerSession(username="orphan", joined_at=utcnow_naive(), server_id=None))
        s.commit()

    for srv in (a, b):
        asyncio.run(stats_poller._poll_server(srv, fake_driver_cls(names=[])))
        with Session(mem_engine) as s:
            assert s.exec(select(PlayerSession)).first().left_at is None


def test_new_session_tagged_with_server_id(monkeypatch, mem_engine, fake_driver_cls):
    from app.services import stats_poller

    b = _srv("b", 2)
    monkeypatch.setattr(stats_poller, "engine", mem_engine)
    monkeypatch.setattr(stats_poller, "docker", _Docker(running=True))

    asyncio.run(stats_poller._poll_server(b, fake_driver_cls(names=["neo"])))
    with Session(mem_engine) as s:
        row = s.exec(select(PlayerSession).where(PlayerSession.username == "neo")).first()
    assert row.server_id == 2 and row.left_at is None


# ---------------------------------------------------------------- log_parser

def test_log_dedup_is_per_server(monkeypatch, mem_engine):
    from app.services import log_parser

    a = _srv("a", 1)
    monkeypatch.setattr(log_parser, "engine", mem_engine)
    monkeypatch.setattr(log_parser, "docker", _Docker(running=True, logs="x"))

    at = dt.datetime(2026, 1, 1, 12, 0, 0)
    with Session(mem_engine) as s:  # an identical event already exists for server 2
        s.add(PlayerEvent(username="alice", kind="death", at=at, server_id=2))
        s.commit()

    drv = _EventDriver([ServerEvent(kind="death", username="alice", at=at)])
    asyncio.run(log_parser._poll_server(a, drv))  # A: same (u,kind,at) → NEW row (not blocked by S2)
    with Session(mem_engine) as s:
        rows = s.exec(select(PlayerEvent)).all()
    assert len(rows) == 2
    assert any(r.server_id == 1 for r in rows)

    asyncio.run(log_parser._poll_server(a, drv))  # second run for A → dedup, no new row
    with Session(mem_engine) as s:
        assert len(s.exec(select(PlayerEvent)).all()) == 2


# ---------------------------------------------------------------- metrics

def test_metrics_writes_server_id_and_downtime_row(monkeypatch, mem_engine, fake_driver_cls):
    from app.services import metrics

    a = _srv("a", 1)
    monkeypatch.setattr(metrics, "engine", mem_engine)
    monkeypatch.setattr(metrics, "docker", _Docker(running=False))  # stopped

    asyncio.run(metrics._sample_server(a, fake_driver_cls()))
    with Session(mem_engine) as s:
        rows = s.exec(select(MetricSample)).all()
    assert len(rows) == 1
    assert rows[0].server_id == 1
    assert not rows[0].running   # downtime row written on purpose
    assert rows[0].players == 0


# ---------------------------------------------------------------- Fan-out source

def test_list_enabled_with_driver_skips_unimplemented(monkeypatch):
    from app.services import servers as servers_registry

    pz = _srv("pz", 1)
    broken = _srv("broken", 2)  # simulate a game_type without a driver (every real one has one)
    monkeypatch.setattr(servers_registry, "list_enabled", lambda: [pz, broken])

    def fake_get_driver(s):
        if s.key == "broken":
            raise NotImplementedError("no driver (test)")
        return object()

    monkeypatch.setattr(servers_registry, "get_driver", fake_get_driver)
    servers_registry._skipped_drivers.clear()

    out = servers_registry.list_enabled_with_driver()
    assert [srv.key for srv, _ in out] == ["pz"]        # server without a driver skipped
    assert "broken" in servers_registry._skipped_drivers

    out2 = servers_registry.list_enabled_with_driver()  # idempotent, no crash
    assert [srv.key for srv, _ in out2] == ["pz"]


# ------------------------------------------------ Scoped /history + /incidents

def test_scoped_history_and_incidents_scoped_to_server(monkeypatch, mem_engine):
    from app.models import MetricSample, ServerIncident
    from app.routers import servers as servers_router

    a = _srv("a", 1)  # default PZ (id=1)
    now = utcnow_naive()
    with Session(mem_engine) as s:
        s.add(MetricSample(running=True, players=1, cpu_percent=1, mem_bytes=1, mem_pct=1, server_id=1, at=now))
        s.add(MetricSample(running=True, players=9, cpu_percent=9, mem_bytes=9, mem_pct=9, server_id=2, at=now))
        s.add(ServerIncident(kind="rcon_hang", detail="own", server_id=1, at=now))
        s.add(ServerIncident(kind="rcon_hang", detail="foreign", server_id=2, at=now))
        s.commit()

        # call the scoped router functions directly (server passed as the dependency value)
        hist = servers_router.scoped_history(hours=24, server=a, session=s, _=None)
        assert len(hist) == 1 and hist[0]["players"] == 1     # only server 1, not the foreign server 2

        incs = servers_router.scoped_incidents(limit=20, server=a, session=s, _=None)
        assert [i["detail"] for i in incs] == ["own"]         # foreign incident hidden
