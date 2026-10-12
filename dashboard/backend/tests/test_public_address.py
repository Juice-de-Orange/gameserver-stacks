"""The public address: Settings → Network over PUBLIC_HOST, a server's own over both, shown on
the fleet, the server page and the join page from one source (`driver.connect_info`)."""
import asyncio
import json

import pytest
from fastapi import HTTPException
from sqlmodel import Session, select

from app.config import settings
from app.models import AuditLog, Role, User
from app.models.install_setting import InstallSetting
from app.models.server import GameServer, GameType
from app.routers import servers as servers_router
from app.routers import settings_network
from app.services import install_settings
from app.services.drivers.base import public_host
from app.services.fleet import connect_of

ADMIN = User(id=1, display_name="Admin", username="admin", pw_hash="x", role=Role.admin)


@pytest.fixture(autouse=True)
def _fresh_cache(monkeypatch):
    monkeypatch.setattr(install_settings, "_cache", {})
    monkeypatch.setattr(settings, "public_host", "")


@pytest.mark.parametrize("raw, stored", [
    ("", ""), ("  ", ""), ("play.example.com", "play.example.com"), ("Play.Example.COM.", "play.example.com"),
    ("203.0.113.10", "203.0.113.10"), ("[2001:db8::1]", "2001:db8::1"), ("my_host.example.com", "my_host.example.com"),
])
def test_a_public_address_is_a_host_name_or_an_ip(raw, stored):
    assert install_settings.normalize_public_host(raw) == stored


@pytest.mark.parametrize("raw", ["http://play.example.com", "play.example.com:25565", "play example.com",
                                 "play.example.com/join", "-bad.example.com"])
def test_anything_else_is_refused_with_a_code(raw):
    with pytest.raises(HTTPException) as ei:
        install_settings.normalize_public_host(raw)
    assert ei.value.detail["code"] == "settings.public_host_invalid"


def test_the_ui_value_wins_over_the_environment_and_a_server_over_both(mem_engine, monkeypatch):
    monkeypatch.setattr(settings, "public_host", "env.example.com")
    srv = GameServer(key="mc-1", game_type=GameType.minecraft, game_ports='{"game":"25565/tcp"}')
    assert public_host(srv) == "env.example.com"
    with Session(mem_engine) as s:
        install_settings.put(s, install_settings.PUBLIC_HOST, "ui.example.com")
    assert public_host(srv) == "ui.example.com"
    srv.extra = json.dumps({"public_host": "own.example.com"})
    assert public_host(srv) == "own.example.com"
    with Session(mem_engine) as s:
        install_settings.put(s, install_settings.PUBLIC_HOST, "")
        assert s.exec(select(InstallSetting)).all() == []
    srv.extra = "{}"
    assert public_host(srv) == "env.example.com", "clearing falls back to PUBLIC_HOST"


def test_the_value_survives_a_restart(mem_engine, monkeypatch):
    with Session(mem_engine) as s:
        install_settings.put(s, install_settings.PUBLIC_HOST, "ui.example.com")
    monkeypatch.setattr(install_settings, "_cache", {})
    install_settings.load(mem_engine)
    assert install_settings.public_host() == "ui.example.com"


def test_the_start_loads_it():
    import inspect

    from app import main

    assert "install_settings.load()" in inspect.getsource(main.lifespan)


def test_the_settings_route_saves_reports_the_source_and_audits(mem_engine, monkeypatch):
    monkeypatch.setattr(settings, "public_host", "env.example.com")
    assert settings_network.get_network(ADMIN)["source"] == "env"
    with Session(mem_engine) as s:
        out = settings_network.put_network(settings_network.NetworkUpdate(public_host=" Play.Example.com "),
                                           session=s, user=ADMIN)
        assert out == {"public_host": "play.example.com", "stored": "play.example.com",
                       "env": "env.example.com", "source": "stored"}
        with pytest.raises(HTTPException):
            settings_network.put_network(settings_network.NetworkUpdate(public_host="http://x"), session=s, user=ADMIN)
        out = settings_network.put_network(settings_network.NetworkUpdate(public_host=""), session=s, user=ADMIN)
        assert out["source"] == "env" and out["public_host"] == "env.example.com"
        audit = [a.action for a in s.exec(select(AuditLog)).all()]
    assert audit == ["settings.public_host", "settings.public_host"]


def test_the_route_is_admin_only():
    from fastapi.routing import APIRoute

    from app.auth.deps import require_admin

    routes = [r for r in settings_network.router.routes if isinstance(r, APIRoute)]
    assert {m for r in routes for m in r.methods} == {"GET", "PUT"}
    for r in routes:
        assert any(d.call is require_admin for d in r.dependant.dependencies), r.path


def test_the_fleet_and_the_server_page_show_the_connect_address(monkeypatch):
    monkeypatch.setattr(settings, "public_host", "play.example.com")
    srv = GameServer(key="mc-1", game_type=GameType.minecraft, game_ports='{"game":"25565/tcp"}')
    assert connect_of(srv) == {"host": "play.example.com", "port": 25565, "protocol": "tcp"}
    monkeypatch.setattr(settings, "public_host", "")
    assert connect_of(srv)["host"] == "", "no made-up address"

    class _Broken:
        def connect_info(self, server):
            raise ValueError("config.json is not JSON")

    assert connect_of(srv, _Broken()) is None


def test_a_servers_own_address_is_checked_on_create_and_save(mem_engine, tmp_path, monkeypatch):
    from app.models.game_settings import GameSetting

    monkeypatch.setattr("app.db.engine", mem_engine)
    monkeypatch.setattr(settings, "allowed_data_roots", [str(tmp_path)])
    with Session(mem_engine) as s:
        s.add(GameSetting(game_type="minecraft", active=True))
        s.commit()
    body = dict(key="mc-1", game_type=GameType.minecraft, container_name="mc-1", data_dir=f"{tmp_path}/mc-1",
                image_repo="itzg/minecraft-server", image_tag="t", rcon_host="mc-1", rcon_port=25575,
                game_ports={"game": "25565/tcp"}, eula_accepted=True)
    with Session(mem_engine) as s:
        with pytest.raises(HTTPException) as ei:
            asyncio.run(servers_router.create_server(
                servers_router.ServerCreate(**body, extra={"public_host": "http://x.example.com"}), session=s, user=ADMIN))
        assert ei.value.detail["code"] == "settings.public_host_invalid"
        out = asyncio.run(servers_router.create_server(
            servers_router.ServerCreate(**body, extra={"public_host": "Play.Example.com"}), session=s, user=ADMIN))
        assert out["extra"]["public_host"] == "play.example.com"
        out = asyncio.run(servers_router.update_server(
            "mc-1", servers_router.ServerUpdate(extra={**out["extra"], "public_host": ""}), session=s, user=ADMIN))
    assert "public_host" not in out["extra"]
