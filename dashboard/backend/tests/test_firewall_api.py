"""The API's side of the firewall helper: desired state, preview → apply, the token, closing.

The helper runs for real here — its HTTP server on a Unix socket in a thread, the API's httpx
client talking to it — only iptables is the in-memory fake (`firewall_fake.py`).
"""
from __future__ import annotations

import asyncio
import shutil
import tempfile
import threading

import pytest
from fastapi import HTTPException
from firewall_fake import FakeIptables  # first: puts dashboard/firewall on sys.path
from gss_firewall.service import Helper, UnixHTTPServer, make_handler
from sqlmodel import Session, select

from app.config import settings
from app.models import AuditLog, Role, User
from app.models.server import GameServer, GameType
from app.routers import firewall as firewall_router
from app.services import firewall, game_settings

ADMIN = User(id=1, display_name="Admin", username="admin", pw_hash="x", role=Role.admin)
TOKEN = "t" * 40


def _server(key, game=GameType.minecraft, ports='{"game":"25565/tcp"}', bind="0.0.0.0", enabled=True):
    return GameServer(key=key, game_type=game, game_ports=ports, bind_address=bind, enabled=enabled)


@pytest.fixture()
def db(mem_engine, monkeypatch):
    monkeypatch.setattr(firewall, "engine", mem_engine)
    with Session(mem_engine) as s:
        for game in (GameType.minecraft, GameType.valheim):
            game_settings.set_active(s, game, True)
        s.add(_server("mc-1"))
        s.add(_server("vh-1", GameType.valheim, '{"game":"2456/udp","query":"2457/udp"}'))
        s.add(_server("mc-local", ports='{"game":"25566/tcp"}', bind="127.0.0.1"))
        s.add(_server("mc-off", ports='{"game":"25567/tcp"}', enabled=False))
        s.add(_server("fa-1", GameType.factorio, '{"game":"34197/udp"}'))  # game not active
        s.commit()
    return mem_engine


@pytest.fixture()
def helper(monkeypatch):
    """A running helper with an in-memory iptables, and the API pointed at it."""
    folder = tempfile.mkdtemp(prefix="fw", dir="/tmp")  # a socket path has at most 107 bytes
    fake = FakeIptables()
    h = Helper("auto", (1024, 65535), f"{folder}/desired.json", runner=fake, reader=fake.reader)
    server = UnixHTTPServer(f"{folder}/helper.sock", make_handler(h, TOKEN))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setattr(settings, "firewall_token", TOKEN)
    monkeypatch.setattr(settings, "firewall_socket", f"{folder}/helper.sock")
    monkeypatch.setattr(settings, "firewall_sources", [])
    yield h, fake
    server.shutdown()
    server.server_close()
    thread.join(5)
    shutil.rmtree(folder, ignore_errors=True)


def _get(session):
    return asyncio.run(firewall_router.get_firewall(session, ADMIN))


def _apply(session, plan_id):
    return asyncio.run(firewall_router.apply_firewall(firewall_router.ApplyBody(plan_id=plan_id), session, ADMIN))


# ------------------------------------------------------------------ desired state


def test_desired_is_the_published_ports_of_enabled_servers_of_active_games(db, monkeypatch):
    monkeypatch.setattr(settings, "firewall_sources", ["198.51.100.0/24"])
    with Session(db) as s:
        got = firewall.desired(s)
    assert got["sources"] == ["198.51.100.0/24"]
    assert sorted((a["server"], a["port"], a["proto"], a["address"]) for a in got["allows"]) == [
        ("mc-1", 25565, "tcp", ""),
        ("vh-1", 2456, "udp", ""),
        ("vh-1", 2457, "udp", ""),
    ], "switched off, loopback-only and inactive games are left out"


def test_a_specific_bind_address_is_kept_and_every_interface_is_empty(db):
    with Session(db) as s:
        s.add(_server("mc-ip", ports='{"game":"25570/tcp"}', bind="192.0.2.10"))
        s.add(_server("mc-v6", ports='{"game":"25571/tcp"}', bind="::"))
        s.add(_server("mc-v6lo", ports='{"game":"25572/tcp"}', bind="::1"))
        s.commit()
        got = {a["server"]: a["address"] for a in firewall.desired(s)["allows"]}
    assert got["mc-ip"] == "192.0.2.10" and got["mc-v6"] == "" and "mc-v6lo" not in got


def test_sources_are_validated_at_start():
    from app.config import Settings

    assert Settings(GSS_FIREWALL_SOURCES="198.51.100.7, 2001:db8::/32").firewall_sources == [
        "198.51.100.7/32", "2001:db8::/32"]
    with pytest.raises(ValueError, match="GSS_FIREWALL_SOURCES"):
        Settings(GSS_FIREWALL_SOURCES="198.51.100.0/24,example.com")


# ------------------------------------------------------------------ off / errors


def test_without_a_token_the_page_says_off_and_apply_refuses(db, monkeypatch):
    monkeypatch.setattr(settings, "firewall_token", "")
    with Session(db) as s:
        got = _get(s)
        assert got["status"] == "off" and len(got["desired"]["allows"]) == 3 and got["plan"] is None
        with pytest.raises(HTTPException) as ei:
            _apply(s, "x")
    assert ei.value.detail["code"] == "firewall.off"


def test_a_short_token_is_named(db, monkeypatch):
    monkeypatch.setattr(settings, "firewall_token", "short")
    with Session(db) as s:
        got = _get(s)
    assert got["status"] == "error" and got["error"]["code"] == "firewall.token_too_short"


def test_no_helper_on_the_socket_is_unreachable(db, monkeypatch):
    monkeypatch.setattr(settings, "firewall_token", TOKEN)
    monkeypatch.setattr(settings, "firewall_socket", "/tmp/no-such-dir-gss/helper.sock")
    with Session(db) as s:
        got = _get(s)
    assert got["status"] == "error" and got["error"]["code"] == "firewall.unreachable"


def test_a_wrong_token_is_refused_by_the_helper(db, helper, monkeypatch):
    monkeypatch.setattr(settings, "firewall_token", "w" * 40)
    with Session(db) as s:
        got = _get(s)
    assert got["status"] == "error" and got["error"]["code"] == "firewall.token_refused"
    _h, fake = helper
    assert not fake.calls, "the helper did not even look at iptables"


def test_a_port_outside_the_helpers_range_is_refused_with_the_reason(db, helper):
    with Session(db) as s:
        s.add(_server("odd", ports='{"game":"80/tcp"}'))
        s.commit()
        got = _get(s)
    assert got["status"] == "error" and got["error"]["code"] == "firewall.refused"
    assert any("port 80 is outside the allowed range" in p for p in got["error"]["problems"])


# ------------------------------------------------------------------ preview → apply


def test_preview_then_apply_opens_exactly_the_previewed_rules_and_audits(db, helper):
    _h, fake = helper
    with Session(db) as s:
        got = _get(s)
        assert got["status"] == "ready"
        plan = got["plan"]
        assert plan["changes"] > 0 and "GSS-ALLOW" not in fake.tables["ipv4"], "the preview wrote nothing"
        added = plan["families"]["ipv4"]["plan"]["add"]
        done = _apply(s, plan["plan_id"])
        assert done["plan"]["applied"] == plan["changes"]
        assert sorted(fake.tables["ipv4"]["GSS-ALLOW"]) == sorted(added)
        audit = s.exec(select(AuditLog).where(AuditLog.action == "firewall.apply")).all()
        assert len(audit) == 1 and "mc-1 25565/tcp" in audit[0].payload
        again = _get(s)
    assert again["plan"]["changes"] == 0
    assert {a["state"] for a in again["plan"]["allows"]} == {"open"}


def test_apply_after_a_change_is_refused_until_the_new_preview(db, helper):
    with Session(db) as s:
        plan_id = _get(s)["plan"]["plan_id"]
        s.add(_server("mc-2", ports='{"game":"25580/tcp"}'))
        s.commit()
        with pytest.raises(HTTPException) as ei:
            _apply(s, plan_id)
    assert ei.value.status_code == 409 and ei.value.detail["code"] == "firewall.plan_changed"


# ------------------------------------------------------------------ closing


def test_switching_a_server_off_closes_its_ports_and_opens_nothing_new(db, helper):
    _h, fake = helper
    with Session(db) as s:
        _apply(s, _get(s)["plan"]["plan_id"])
        vh = s.exec(select(GameServer).where(GameServer.key == "vh-1")).one()
        vh.enabled = False
        s.add(vh)
        s.add(_server("mc-new", ports='{"game":"25590/tcp"}'))  # not applied yet: stays closed
        s.commit()
    closed = asyncio.run(firewall.close_stale())
    assert sorted((a["server"], a["port"]) for a in closed) == [("vh-1", 2456), ("vh-1", 2457)]
    rules = " ".join(fake.tables["ipv4"]["GSS-ALLOW"])
    assert "25565" in rules and "2456" not in rules and "25590" not in rules
    with Session(db) as s:
        audit = s.exec(select(AuditLog).where(AuditLog.action == "firewall.close")).all()
        assert len(audit) == 1 and audit[0].username == "system" and "vh-1 2456/udp" in audit[0].target
    assert asyncio.run(firewall.close_stale()) == [], "nothing left to close"


def test_removing_a_server_or_its_game_closes_its_ports(db, helper):
    _h, fake = helper
    with Session(db) as s:
        _apply(s, _get(s)["plan"]["plan_id"])
        s.delete(s.exec(select(GameServer).where(GameServer.key == "mc-1")).one())
        game_settings.set_active(s, GameType.valheim, False)
        s.commit()
    asyncio.run(firewall.close_stale())
    assert fake.tables["ipv4"]["GSS-ALLOW"] == []


def test_closing_is_a_no_op_without_the_helper(db, monkeypatch):
    monkeypatch.setattr(settings, "firewall_token", "")
    assert asyncio.run(firewall.close_stale()) == []
    monkeypatch.setattr(settings, "firewall_token", TOKEN)
    monkeypatch.setattr(settings, "firewall_socket", "/tmp/no-such-dir-gss/helper.sock")
    assert asyncio.run(firewall.close_stale()) == []


# ------------------------------------------------------------------ routing


def test_both_routes_are_admin_only(api_client):
    assert api_client.get("/api/settings/firewall").status_code == 401
    assert api_client.post("/api/settings/firewall/apply", json={"plan_id": "x"}).status_code == 401
    from app.main import app
    from tests.route_walk import flat_routes

    deps = {r.path: {d.call.__name__ for d in r.dependant.dependencies}
            for r in flat_routes(app) if r.path.startswith("/api/settings/firewall")}
    assert deps and all("require_admin" in names for names in deps.values()), deps


def test_the_close_job_is_scheduled():
    import inspect

    from app import main

    assert "firewall_close_stale" in inspect.getsource(main.lifespan)
