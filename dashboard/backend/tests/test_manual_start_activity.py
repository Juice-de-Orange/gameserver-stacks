"""A human pressing Start is activity — an automatic restart is not.

The full rule: the clock counts 20 days; it starts over when a player joins OR when someone starts
the server in the dashboard. The scheduled nightly restart and the watchdog autoheal explicitly do
not count.

Without the second trigger `last_active_at` had only the source "player seen". Once the clock was
past the timeout, the next hourly round stopped every manually started server again after the
grace period — permanently, because without a real join the clock never got back under the
timeout.

The cross-check (automatic paths do NOT set the clock) does not rest on a promise about
`perform_action` but on those paths not going through it at all. That is exactly what
`test_the_automatic_paths_bypass_perform_action` checks.
"""
import asyncio
import datetime as dt

import pytest
from sqlmodel import Session, select

from app.models.player import utcnow_naive
from app.models.server import GameServer, GameType
from app.models.user import Role, User, UserStatus
from app.routers import server as server_router


class _Docker:
    def __init__(self):
        self.calls: list[tuple[str, str]] = []

    async def action(self, name, action, **kw):
        self.calls.append((name, action))

    async def state(self, name):
        return {"running": True, "started_at": None}


def _prepare(monkeypatch, mem_engine, old, *, real_audit=False):
    """Put a server with an old clock into the DB, neutralise all side paths.

    `real_audit=True` lets `record()` really run — see
    `test_the_audit_survives_the_commit_of_the_clock`.
    """
    monkeypatch.setattr(server_router, "docker", _Docker())

    # The start runs through `lifecycle` — under the same lock as stop and restart. Without this
    # stub the test would reach the real Docker client.
    async def _start(server):
        return None

    monkeypatch.setattr(server_router.lifecycle, "start_server", _start)
    monkeypatch.setattr(server_router.watchdog, "suppress", lambda *a, **k: None)
    async def _no_blocker(server):
        return []

    monkeypatch.setattr(server_router.mod_state, "guard_boot", _no_blocker)
    if not real_audit:
        monkeypatch.setattr(server_router, "record", lambda *a, **k: None)

    async def _ok(server):
        return True, 0, []

    monkeypatch.setattr(server_router.budget_svc, "can_start", _ok)

    srv = GameServer(key="pz-1", game_type=GameType.pz, container_name="pz-1")
    srv.id = 1
    srv.last_active_at = old
    with Session(mem_engine, expire_on_commit=False) as s:
        s.add(srv)
        s.commit()
    # The user is not persisted: `perform_action` only passes it on to `record`, which is
    # patched. All that matters is THAT there is one.
    user = User(id=1, username="admin", display_name="Admin", pw_hash="x",
                role=Role.admin, status=UserStatus.active)
    return srv, user


def _clock(mem_engine):
    with Session(mem_engine) as s:
        return s.get(GameServer, 1).last_active_at


@pytest.mark.parametrize("action", ["start", "restart"])
def test_manual_start_sets_the_clock(monkeypatch, mem_engine, action):
    old = utcnow_naive() - dt.timedelta(days=27)
    srv, user = _prepare(monkeypatch, mem_engine, old)

    async def _noop(server, **kw):
        return None

    monkeypatch.setattr(server_router.lifecycle, "restart_server", _noop)

    with Session(mem_engine) as s:
        asyncio.run(server_router.perform_action(srv, action, s, user))

    new = _clock(mem_engine)
    assert new is not None and new > old
    assert (utcnow_naive() - new).total_seconds() < 60


def test_stopping_does_not_set_the_clock(monkeypatch, mem_engine):
    """Otherwise a server shut down on purpose would get another 20 days of grace."""
    old = utcnow_naive() - dt.timedelta(days=27)
    srv, user = _prepare(monkeypatch, mem_engine, old)

    async def _noop(server, **kw):
        return None

    monkeypatch.setattr(server_router.lifecycle, "stop_server", _noop)

    with Session(mem_engine) as s:
        asyncio.run(server_router.perform_action(srv, "stop", s, user))

    assert _clock(mem_engine) == old


def test_the_audit_survives_the_commit_of_the_clock(monkeypatch, mem_engine):
    """The gap all other tests here leave open: they patch `record()` away.

    For real it runs AFTER `session.commit()` for the clock — and a commit makes SQLAlchemy expire
    all objects of the session. `record()` then reads `user.id` and `user.username`. If that went
    wrong, **every start and restart from the dashboard** would end in a 500, and only in
    production: the action itself would long have run by then.
    """
    from app.models import AuditLog

    old = utcnow_naive() - dt.timedelta(days=27)
    srv, user = _prepare(monkeypatch, mem_engine, old, real_audit=True)

    with Session(mem_engine) as s:
        s.add(user)              # so `user` lives in the SAME session as in a real request
        s.commit()
        asyncio.run(server_router.perform_action(srv, "start", s, user))

    with Session(mem_engine) as s:
        entry = s.exec(select(AuditLog).where(AuditLog.action == "server.start")).one()
        assert entry.username == "admin"
        assert entry.server_id == 1
        assert s.get(GameServer, 1).last_active_at > old


def test_without_a_user_the_clock_stays(monkeypatch, mem_engine):
    """`perform_action(..., session=None, user=None)` is the call form without a human.

    Only tests use it today — but if someone routes an automatic path through it, it must not
    set the idle clock by accident.
    """
    old = utcnow_naive() - dt.timedelta(days=27)
    srv, _ = _prepare(monkeypatch, mem_engine, old)

    asyncio.run(server_router.perform_action(srv, "start", session=None, user=None))

    assert _clock(mem_engine) == old


def test_the_automatic_paths_bypass_perform_action():
    """The actual guarantee: whoever starts without a human does not pass through here.

    If this test breaks, someone routed an automatic start through `perform_action` — and thereby
    silently disabled the idle stop for every server with a restart schedule.
    """
    import inspect

    from app.services import restarts, watchdog
    from app import cli

    for module in (restarts, watchdog, cli):
        source = inspect.getsource(module)
        assert "perform_action" not in source, (
            f"{module.__name__} calls perform_action — an automatic start would set the "
            "idle clock and defeat the auto-stop"
        )


def test_the_timeout_default_is_twenty_days():
    """The default as a number. 28800 min = 20 d — whoever changes it should justify it here."""
    from app.models.resource_budget import ResourceBudget

    assert ResourceBudget().default_idle_timeout_minutes == 20 * 24 * 60
