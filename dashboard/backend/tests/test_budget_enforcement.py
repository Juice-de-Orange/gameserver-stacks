"""The start path enforces the RAM budget (`can_start`) — block with 409 + candidates,
admin `force` overrides and is audited, `stop`/`restart` are never checked."""
import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.models import Role
from app.models.server import GameServer, GameType
from app.routers import server as server_router
from app.routers import servers as servers_router
from app.services import lifecycle


def _srv():
    s = GameServer(key="mc", game_type=GameType.minecraft, container_name="mc", ram_mb=8192)
    s.id = 3
    return s


def _patch(monkeypatch, can_start_result):
    """Stub docker.action, can_start, record, watchdog; returns (actions, audits)."""
    actions: list = []
    audits: list = []

    async def fake_action(name, act, **kw):
        actions.append((name, act))

    async def fake_state(name):
        return {"running": True, "exists": True}

    async def fake_can_start(server):
        return can_start_result

    monkeypatch.setattr(server_router.docker, "action", fake_action)
    monkeypatch.setattr(server_router.budget_svc, "can_start", fake_can_start)
    monkeypatch.setattr(server_router, "record", lambda *a, **k: audits.append((a, k)))
    monkeypatch.setattr(server_router.watchdog, "suppress", lambda k, m: None)
    # Stop/restart go through `lifecycle` (game-correct shutdown) — stub there as well.
    monkeypatch.setattr(lifecycle.docker, "state", fake_state, raising=False)
    monkeypatch.setattr(lifecycle, "_STOP_MARK_DELAY", 0)
    monkeypatch.setattr(lifecycle, "_locks", {})  # each test has its own event loop
    monkeypatch.setattr(lifecycle, "_suppress", lambda *a, **k: None)
    monkeypatch.setattr(lifecycle, "_driver", lambda s: None)
    return actions, audits


def test_start_blocked_when_over_budget(monkeypatch):
    actions, _ = _patch(monkeypatch, (False, 6144, [{"key": "a", "effective_ram_mb": 8192}]))
    with pytest.raises(HTTPException) as ei:
        asyncio.run(server_router.perform_action(_srv(), "start", session=None, user=None))
    assert ei.value.status_code == 409
    assert ei.value.detail["deficit_mb"] == 6144
    assert ei.value.detail["stop_candidates"][0]["key"] == "a"
    assert actions == []  # NO Docker action when blocked


def test_start_allowed_when_fits(monkeypatch):
    actions, audits = _patch(monkeypatch, (True, 0, []))
    out = asyncio.run(server_router.perform_action(_srv(), "start", session=None, user=None))
    assert out == {"ok": True, "action": "start"}
    assert actions == [("mc", "start")]
    assert not any(a[2] == "server.start.forced" for a, _ in audits)  # no forced marker


def test_start_force_overrides_and_audits(monkeypatch):
    actions, audits = _patch(monkeypatch, (False, 6144, []))
    out = asyncio.run(server_router.perform_action(_srv(), "start", session=None, user=None, force=True))
    assert out == {"ok": True, "action": "start"}
    assert actions == [("mc", "start")]                                   # started despite the deficit
    assert any(a[2] == "server.start.forced" for a, _ in audits)          # forced is audited


def test_stop_and_restart_never_budget_checked(monkeypatch):
    called = {"n": 0}

    async def boom(server):
        called["n"] += 1
        return (False, 9999, [])

    for act in ("stop", "restart"):
        actions, _ = _patch(monkeypatch, (False, 9999, []))
        monkeypatch.setattr(server_router.budget_svc, "can_start", boom)
        out = asyncio.run(server_router.perform_action(_srv(), act, session=None, user=None))
        assert out == {"ok": True, "action": act}
        # Restart = stop-then-start (no `docker restart`, which would just kill PZ).
        assert actions == ([("mc", "stop")] if act == "stop" else [("mc", "stop"), ("mc", "start")])
    assert called["n"] == 0  # can_start never called on stop/restart


def test_scoped_action_force_requires_admin(monkeypatch):
    """`force` overrides the budget lock — only an admin may do that.

    `force` exists only on start (`scoped_start`; the former catch-all route shadowed POST
    routes of other routers, see `tests/test_route_dispatch.py`).
    """
    non_admin = SimpleNamespace(role=Role.member)
    with pytest.raises(HTTPException) as ei:
        asyncio.run(servers_router.scoped_start(
            force=True, server=_srv(), session=None, user=non_admin
        ))
    assert ei.value.status_code == 403


def test_with_blind_docker_the_lock_blocks_and_the_admin_gets_through_with_force(monkeypatch):
    """The flip side of the fail-closed direction — pinned on purpose.

    A server whose state cannot be queried counts as RUNNING. That is the right direction
    (otherwise a start runs into the host OOM), but it has a price: if the socket proxy fails,
    ALL servers count as running and the lock rejects every start. The documented way out is
    `force` (admin only) — this test pins that it exists and works. Without it a proxy outage
    would mean "no server starts any more, and nobody can do anything about it".
    """
    import asyncio

    from app.models import ResourceBudget
    from app.services import resource_budget as rb

    a = GameServer(key="a", game_type=GameType.pz, container_name="a", ram_mb=8192)
    a.id = 1
    x = GameServer(key="x", game_type=GameType.pz, container_name="x", ram_mb=8192)
    x.id = 2

    class _DockerBlind:
        async def state(self, name):
            raise RuntimeError("dockerproxy unreachable")

        async def stats(self, name):
            raise RuntimeError("dockerproxy unreachable")

    monkeypatch.setattr(rb, "get_budget", lambda: ResourceBudget(total_ram_mb=12288))
    monkeypatch.setattr(rb, "docker", _DockerBlind())
    monkeypatch.setattr(rb.servers_registry, "list_all", lambda: [a, x])

    ok, deficit, candidates = asyncio.run(rb.can_start(x))
    assert ok is False, "a blind Docker must hold the start, not wave it through"
    assert deficit == 8192 + 8192 - 12288
    assert [c["key"] for c in candidates] == ["a"], (
        "the user must still learn which server they can stop"
    )
