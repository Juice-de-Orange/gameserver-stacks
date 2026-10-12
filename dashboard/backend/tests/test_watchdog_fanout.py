"""Watchdog fan-out: state isolated per server_key, incidents carry server_id."""
import asyncio
import datetime as dt
import time

from sqlmodel import Session, select

from app.config import settings
from app.models import ServerIncident
from app.models.server import GameServer, GameType
from app.services import watchdog


def _srv(key, sid, container=None):
    s = GameServer(key=key, game_type=GameType.pz, container_name=container or key)
    s.id = sid
    return s


class _Docker:
    def __init__(self, running=True):
        self.running = running
        self.actions: list[tuple[str, str]] = []

    async def state(self, name):
        # **`started_at` belongs in the answer because the watchdog knows a boot window**: a
        # container that has been running for only minutes may still be silent. A HANG — the
        # case of this test — has been running longer, and that is what the dummy models here.
        # Without the field it would hit the boot branch and the test would check that a booting
        # server is not restarted — something other than what it claims.
        old = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None) - dt.timedelta(hours=3)
        return {"running": self.running, "id": "c-" + name,
                "started_at": old.isoformat() + "Z"}

    async def action(self, name, action, **kw):
        self.actions.append((name, action))


def test_suppress_is_per_key():
    watchdog._states.clear()
    watchdog.suppress("a", 5)
    assert watchdog._state("a").suppress_until > 0
    assert watchdog._state("b").suppress_until == 0.0


def test_fail_counter_isolated_and_restart(monkeypatch, mem_engine, fake_driver_cls, wire_lifecycle):
    watchdog._states.clear()
    fake = _Docker(running=True)
    monkeypatch.setattr(watchdog, "docker", fake)
    wire_lifecycle(fake)  # the auto restart runs through `lifecycle`
    monkeypatch.setattr(watchdog, "engine", mem_engine)
    monkeypatch.setattr(settings, "watchdog_fail_threshold", 3, raising=False)
    monkeypatch.setattr(settings, "watchdog_cooldown_min", 0, raising=False)  # no cooldown → restart may fire
    a = _srv("a", 1, "cont-a")
    b = _srv("b", 2, "cont-b")
    dead = fake_driver_cls(fail_rcon=True)
    healthy = fake_driver_cls(names=["x"])
    for _ in range(3):
        asyncio.run(watchdog._check_server(a, dead))
    asyncio.run(watchdog._check_server(b, healthy))
    assert fake.actions == [("cont-a", "stop"), ("cont-a", "start")]  # only A restarted
    assert watchdog._state("b").fail_count == 0          # B counter unaffected
    with Session(mem_engine) as s:
        incs = s.exec(select(ServerIncident)).all()
    assert incs and all(i.server_id == 1 for i in incs)  # incidents carry A.id
    assert {i.kind for i in incs} >= {"rcon_hang", "auto_restart"}


def test_cooldown_blocks_second_restart(monkeypatch, mem_engine, fake_driver_cls):
    watchdog._states.clear()
    fake = _Docker(running=True)
    monkeypatch.setattr(watchdog, "docker", fake)
    monkeypatch.setattr(watchdog, "engine", mem_engine)
    monkeypatch.setattr(settings, "watchdog_fail_threshold", 3, raising=False)
    monkeypatch.setattr(settings, "watchdog_cooldown_min", 20, raising=False)
    a = _srv("a", 1, "cont-a")
    watchdog._state("a").last_restart = time.monotonic()  # just restarted → cooldown active
    dead = fake_driver_cls(fail_rcon=True)
    for _ in range(5):
        asyncio.run(watchdog._check_server(a, dead))
    assert fake.actions == []


def test_not_running_resets_without_incident(monkeypatch, mem_engine, fake_driver_cls):
    watchdog._states.clear()
    monkeypatch.setattr(watchdog, "docker", _Docker(running=False))
    monkeypatch.setattr(watchdog, "engine", mem_engine)
    a = _srv("a", 1)
    watchdog._state("a").fail_count = 5
    asyncio.run(watchdog._check_server(a, fake_driver_cls(fail_rcon=True)))
    assert watchdog._state("a").fail_count == 0
    with Session(mem_engine) as s:
        assert s.exec(select(ServerIncident)).all() == []


def test_with_an_unreadable_container_state_the_watchdog_does_not_heal_by_itself(monkeypatch, caplog):
    """When Docker does not answer, the watchdog skips the check.

    Two assertions, and the second is the more important one:

    1. The skip leaves a trace. Without log and incident, a failed socket proxy would make the
       watchdog silently ineffective while the UI still shows "monitored".
    2. It does NOT intervene by itself. An unreadable state is no permission to restart a
       container — that would be the more dangerous failure direction: during a proxy outage the
       watchdog would restart running worlds one after another.
    """
    import logging

    class _DockerBlind:
        def __init__(self):
            self.actions: list[tuple[str, str]] = []

        async def state(self, name):
            raise RuntimeError("dockerproxy unreachable")

        async def action(self, name, action, **kw):
            self.actions.append((name, action))

    d = _DockerBlind()
    monkeypatch.setattr(watchdog, "docker", d)

    class _Drv:
        async def players(self, server):
            raise AssertionError("must not even be asked when the state is unreadable")

    with caplog.at_level(logging.WARNING, logger="uvicorn.error"):
        asyncio.run(watchdog._check_server(_srv("pz-1", 1), _Drv()))

    assert d.actions == [], "the watchdog intervened by itself on an unreadable state"
    assert any("cannot be read" in r.message or "cannot be read" in r.getMessage()
               for r in caplog.records), (
        "The skip left no trace — exactly what would make the watchdog silently ineffective "
        "during a proxy outage."
    )
