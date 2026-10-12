"""Display and shutdown come from the SAME calculation.

The fleet view shows "Auto-stop: earliest in 3 d 4 h". That number must not be a second version
of the shutdown rule — whoever writes down a rule must check WHO follows it, and a display lies
when it reads the wrong side. On Valheim this kind of split once had an operator hand out a join
password the server had never seen.

Hence two halves:

1. **Behaviour** — for a matrix of states: `_check_server` queries the player count (and stops
   if needed) exactly when `idle_status(...).reason == "due"`. That is the substantive guarantee.
2. **Form** — `_check_server` does not calculate on its own. An AST look forbids it the building
   blocks of the rule (`START_GRACE_MIN`, `idle_timeout_minutes`, comparisons on
   `last_active_at`). Without this half someone could write the rule next to it, and half 1 would
   stay green as long as both versions happen to agree.

**Cross-check:** with no `idle_status` and the rule written out literally in `_check_server`,
both halves are red.
"""
import ast
import asyncio
import datetime as dt
import inspect

import pytest
from sqlmodel import Session

from app.models.player import utcnow_naive
from app.models.server import GameServer, GameType
from app.services import idle_autostop as ia
from app.services.docker_client import start_time as docker_start_time

DEFAULT_TIMEOUT = 28800  # the budget's default timeout (~20 days)


def _srv(*, last_active=None, enabled=True, timeout=None):
    s = GameServer(
        key="s", game_type=GameType.pz, container_name="c",
        idle_autostop_enabled=enabled, idle_timeout_minutes=timeout, last_active_at=last_active,
    )
    s.id = 1
    return s


class _Docker:
    def __init__(self, running=True, started_at=None):
        self.running, self.started_at = running, started_at

    async def state(self, name):
        return {"running": self.running, "started_at": self.started_at}


class _Drv:
    # Before stopping, the idle stop asks whether the same data folder is being packed right now —
    # the other direction of the guard the weekly run already has.
    def backup_spec(self, server):
        from types import SimpleNamespace
        return SimpleNamespace(data_dir="/tmp/dummy")

    """Remembers WHETHER it was asked — that is the actual measure here."""

    def __init__(self):
        self.asked = False

    async def players(self, server):
        self.asked = True
        return {"count": 0, "names": []}


def _iso(minutes_ago: int) -> str:
    t = dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=minutes_ago)
    return t.strftime("%Y-%m-%dT%H:%M:%S.%f000Z")


# (name, server, container running, started X minutes ago, expected reason)
STATES = [
    ("disabled",            _srv(enabled=False, last_active=utcnow_naive() - dt.timedelta(days=40)), True, 999, "off"),
    ("timeout 0",           _srv(timeout=0, last_active=utcnow_naive() - dt.timedelta(days=40)), True, 999, "no_timeout"),
    ("container off",       _srv(last_active=utcnow_naive() - dt.timedelta(days=40)), False, 999, "not_running"),
    ("never seen",          _srv(last_active=None), True, 999, "never_seen"),
    ("freshly started",     _srv(last_active=utcnow_naive() - dt.timedelta(days=40)), True, 10, "grace_period"),
    ("still counting",      _srv(timeout=120, last_active=utcnow_naive() - dt.timedelta(minutes=30)), True, 999, "counting"),
    ("due",                 _srv(timeout=120, last_active=utcnow_naive() - dt.timedelta(minutes=300)), True, 999, "due"),
]


@pytest.mark.parametrize("name,srv,running,started_ago,expected", STATES, ids=[case[0] for case in STATES])
def test_status_and_job_agree(monkeypatch, mem_engine, name, srv, running, started_ago, expected):
    status = ia.idle_status(
        srv,
        running=running,
        started_at=docker_start_time(_iso(started_ago)),
        default_timeout=DEFAULT_TIMEOUT,
        now=utcnow_naive(),
    )
    assert status.reason == expected, f"{name}: idle_status says {status.reason!r}"

    monkeypatch.setattr(ia, "engine", mem_engine)
    monkeypatch.setattr(ia, "docker", _Docker(running=running, started_at=_iso(started_ago)))
    stopped: list[str] = []

    class _LC:
        @staticmethod
        async def stop_server(server, *, precondition=None, **kw):
            # **The precondition is asked INSIDE the lock**: otherwise the idle stop's decision
            # dates from before a wait that takes up to two minutes on Project Zomboid — and the
            # server it then stops may have been brought up by a scheduled restart meanwhile.
            if precondition is not None and not await precondition():
                return False
            stopped.append(server.key)
            return True

    monkeypatch.setattr(ia, "lifecycle", _LC)
    with Session(mem_engine, expire_on_commit=False) as s:
        s.add(srv)
        s.commit()

    drv = _Drv()
    asyncio.run(ia._check_server(srv, drv, DEFAULT_TIMEOUT))

    # The job queries the player count exactly when the status says "due".
    assert drv.asked is (expected == "due"), (
        f"{name}: the job did {'' if drv.asked else 'NOT '}ask for players, "
        f"status says {expected!r}"
    )
    assert bool(stopped) is (expected == "due")


def test_check_server_does_not_calculate_on_its_own():
    """The job may only USE the rule, not rebuild it."""
    tree = ast.parse(inspect.getsource(ia._check_server))
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    attributes = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}

    assert "idle_status" in names, "_check_server does not ask the shared function at all"
    for forbidden in ("START_GRACE_MIN", "timedelta"):
        assert forbidden not in names, f"_check_server rebuilds the rule ({forbidden})"
    for forbidden in ("idle_autostop_enabled", "idle_timeout_minutes"):
        assert forbidden not in attributes, f"_check_server rebuilds the rule ({forbidden})"

    # `last_active_at` may be read (the message names the idle duration) — but not compared,
    # because the comparison IS the rule.
    comparisons = [n for n in ast.walk(tree) if isinstance(n, ast.Compare)]
    for cmp in comparisons:
        involved = {n.attr for n in ast.walk(cmp) if isinstance(n, ast.Attribute)}
        assert "last_active_at" not in involved, "_check_server compares last_active_at itself"
