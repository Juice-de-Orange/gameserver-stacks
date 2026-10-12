"""Arma Reforger reads only its config.json (`ARMA_CONFIG`): the public address and the ports
have to be written there, at creation and after every change on the edit page."""
import asyncio
import json
from pathlib import Path

import pytest
from fastapi import HTTPException
from sqlmodel import Session

from app.config import settings
from app.models import Role, User
from app.models.server import ConsoleKind, GameServer, GameType, ManagementMode
from app.routers import servers as servers_router
from app.services import install_settings
from app.services.drivers.reforger import ReforgerDriver

ADMIN = User(id=1, display_name="Admin", username="admin", pw_hash="x", role=Role.admin)


@pytest.fixture(autouse=True)
def _no_global_address(monkeypatch):
    monkeypatch.setattr(install_settings, "_cache", {})
    monkeypatch.setattr(settings, "public_host", "")


def _srv(tmp_path, **over) -> GameServer:
    base = dict(key="rf-1", game_type=GameType.arma_reforger, management_mode=ManagementMode.dashboard,
                container_name="rf-1", data_dir=f"{tmp_path}/rf-1", image_repo="ghcr.io/acemod/arma-reforger",
                image_tag="latest", console_kind=ConsoleKind.battleye_rcon, rcon_host="rf-1", rcon_port=19999,
                game_ports='{"game":"2001/udp","a2s":"17777/udp"}')
    base.update(over)
    return GameServer(**base)


def test_a_new_config_carries_the_public_ipv4_and_the_ports(tmp_path):
    srv = _srv(tmp_path, extra=json.dumps({"public_host": "203.0.113.10"}),
               game_ports='{"game":"24611/udp","a2s":"24612/udp"}')
    cfg = ReforgerDriver().initial_config(srv, "rconsecret")
    assert cfg["publicAddress"] == "203.0.113.10"
    assert cfg["bindPort"] == cfg["publicPort"] == 24611
    assert cfg["a2s"]["port"] == 24612


@pytest.mark.parametrize("host", ["play.example.com", "2001:db8::10", ""])
def test_a_host_name_or_ipv6_leaves_the_address_to_the_backend(tmp_path, host):
    srv = _srv(tmp_path, extra=json.dumps({"public_host": host}))
    assert ReforgerDriver().initial_config(srv, "rconsecret")["publicAddress"] == ""


def test_the_global_address_counts_too(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "public_host", "198.51.100.7")
    assert ReforgerDriver().initial_config(_srv(tmp_path), "s")["publicAddress"] == "198.51.100.7"


def test_sync_writes_address_ports_and_password_and_keeps_the_rest(tmp_path):
    drv = ReforgerDriver()
    srv = _srv(tmp_path)
    drv.ensure_config(srv, "old-secret")
    path = Path(drv._config_path(srv))
    cfg = json.loads(path.read_text())
    cfg["game"]["name"] = "Kept"
    path.write_text(json.dumps(cfg))

    srv.game_ports = '{"game":"24611/udp","a2s":"24612/udp"}'
    srv.extra = json.dumps({"public_host": "203.0.113.10"})
    keys = drv.sync_config_from_registry(srv, "new-secret")
    cfg = json.loads(path.read_text())
    assert keys == ["a2s.port", "bindPort", "publicAddress", "publicPort", "rcon.password", "rcon.port"]
    assert (cfg["bindPort"], cfg["publicPort"], cfg["a2s"]["port"]) == (24611, 24611, 24612)
    assert cfg["publicAddress"] == "203.0.113.10"
    assert cfg["rcon"]["password"] == "new-secret" and cfg["rcon"]["port"] == 19999
    assert cfg["game"]["name"] == "Kept" and cfg["a2s"]["address"] == "0.0.0.0"


def test_sync_without_a_file_does_nothing(tmp_path):
    assert ReforgerDriver().sync_config_from_registry(_srv(tmp_path), "s") == []


def test_sync_refuses_a_password_reforger_would_reject(tmp_path):
    drv = ReforgerDriver()
    srv = _srv(tmp_path)
    drv.ensure_config(srv, "old-secret")
    path = Path(drv._config_path(srv))
    before = path.read_text()
    with pytest.raises(Exception, match="32 characters"):
        drv.sync_config_from_registry(srv, "has space")
    assert path.read_text() == before


def test_the_edit_page_moves_port_and_address_into_the_file(mem_engine, tmp_path, monkeypatch):
    monkeypatch.setattr("app.db.engine", mem_engine)
    monkeypatch.setattr(settings, "allowed_data_roots", [str(tmp_path)])
    monkeypatch.setenv("RF_PW", "rconsecret")
    drv = ReforgerDriver()
    srv = _srv(tmp_path, rcon_password_env="RF_PW")
    with Session(mem_engine) as s:
        s.add(srv)
        s.commit()
        s.refresh(srv)
    drv.ensure_config(srv, "rconsecret")

    body = servers_router.ServerUpdate(game_ports={"game": "24611/udp", "a2s": "24612/udp"},
                                       extra={"public_host": "203.0.113.10"})
    with Session(mem_engine) as s:
        asyncio.run(servers_router.update_server("rf-1", body, session=s, user=ADMIN))
    cfg = json.loads(Path(drv._config_path(srv)).read_text())
    assert (cfg["bindPort"], cfg["publicPort"], cfg["a2s"]["port"], cfg["publicAddress"]) == (
        24611, 24611, 24612, "203.0.113.10")

    # A file that cannot follow stops the save: nothing in the registry, nothing half-written.
    monkeypatch.setenv("RF_PW", "x" * 40)
    with Session(mem_engine) as s, pytest.raises(HTTPException) as ei:
        asyncio.run(servers_router.update_server(
            "rf-1", servers_router.ServerUpdate(game_ports={"game": "24613/udp", "a2s": "24612/udp"}),
            session=s, user=ADMIN))
    assert ei.value.detail["code"] == "server.config_sync_failed"
    with Session(mem_engine) as s:
        assert json.loads(s.get(GameServer, srv.id).game_ports)["game"] == "24611/udp"
