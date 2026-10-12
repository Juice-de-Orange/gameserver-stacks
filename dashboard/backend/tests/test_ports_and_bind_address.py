"""Host ports of a game server: the bind address (`services/ports.py`, `container_spec`)."""
import asyncio
import json

import pytest
from fastapi import HTTPException
from sqlmodel import Session, select

from app.models import Role, User
from app.models.server import ConsoleKind, GameServer, GameType, ManagementMode
from app.routers import servers as servers_router
from app.services import ports
from app.services.container_spec import build_create_payload

ADMIN = User(id=1, display_name="Admin", username="admin", pw_hash="x", role=Role.admin)


def _server(**over) -> GameServer:
    base = dict(
        key="mc-1", container_name="mc-1", game_type=GameType.minecraft,
        management_mode=ManagementMode.dashboard,
        data_dir="/srv/gameservers/mc-1", image_repo="itzg/minecraft-server", image_tag="t",
        console_kind=ConsoleKind.source_rcon, rcon_port=25575,
        game_ports='{"game":"25565/tcp"}', game_version="1.21.4",
    )
    base.update(over)
    return GameServer(**base)


# ------------------------------------------------------------------ bind address

@pytest.mark.parametrize("raw, stored", [
    ("", "0.0.0.0"), (None, "0.0.0.0"), (" 127.0.0.1 ", "127.0.0.1"), ("0.0.0.0", "0.0.0.0"),
    ("::1", "::1"), ("2001:DB8::10", "2001:db8::10"), ("192.0.2.10", "192.0.2.10"),
])
def test_a_bind_address_is_an_ip_address(raw, stored):
    assert ports.normalize_bind_address(raw) == stored


@pytest.mark.parametrize("raw", ["localhost", "play.example.com", "192.0.2.1/24", "256.1.1.1", "0.0.0.0:80"])
def test_anything_else_is_refused_with_a_code(raw):
    with pytest.raises(HTTPException) as ei:
        ports.normalize_bind_address(raw)
    assert ei.value.status_code == 400
    assert ei.value.detail["code"] == "server.bind_address_invalid"


def test_the_default_publishes_on_every_interface_like_before():
    """0.0.0.0 leaves `HostIp` out: Docker's default (IPv4 and IPv6), as before the field existed."""
    hc = build_create_payload(_server(), "x")["HostConfig"]
    assert hc["PortBindings"] == {"25565/tcp": [{"HostPort": "25565"}]}


def test_loopback_publishes_on_loopback_only():
    hc = build_create_payload(_server(bind_address="127.0.0.1"), "x")["HostConfig"]
    assert hc["PortBindings"] == {"25565/tcp": [{"HostIp": "127.0.0.1", "HostPort": "25565"}]}


def test_the_field_is_created_saved_and_listed(mem_engine, tmp_path, monkeypatch):
    from app.config import settings
    from app.models.game_settings import GameSetting

    monkeypatch.setattr("app.db.engine", mem_engine)
    monkeypatch.setattr(settings, "allowed_data_roots", [str(tmp_path)])
    with Session(mem_engine) as s:
        s.add(GameSetting(game_type="minecraft", active=True))
        s.commit()
    body = servers_router.ServerCreate(
        key="mc-1", game_type=GameType.minecraft, container_name="mc-1", data_dir=f"{tmp_path}/mc-1",
        image_repo="itzg/minecraft-server", image_tag="t", rcon_host="mc-1", rcon_port=25575,
        game_ports={"game": "25565/tcp"}, eula_accepted=True, bind_address="127.0.0.1",
    )
    with Session(mem_engine) as s:
        out = asyncio.run(servers_router.create_server(body, session=s, user=ADMIN))
    assert out["bind_address"] == "127.0.0.1"

    with Session(mem_engine) as s:
        out = asyncio.run(servers_router.update_server(
            "mc-1", servers_router.ServerUpdate(bind_address=""), session=s, user=ADMIN))
        assert out["bind_address"] == "0.0.0.0"
        with pytest.raises(HTTPException) as ei:
            asyncio.run(servers_router.update_server(
                "mc-1", servers_router.ServerUpdate(bind_address="example.com"), session=s, user=ADMIN))
        assert ei.value.detail["code"] == "server.bind_address_invalid"
        assert s.exec(select(GameServer)).one().bind_address == "0.0.0.0"
    assert json.loads(json.dumps(out, default=str))["bind_address"] == "0.0.0.0"


def test_the_migration_matches_the_model_default():
    from app.db import _COLUMN_MIGRATIONS

    (stmt,) = [m for m in _COLUMN_MIGRATIONS if "bind_address" in m]
    assert "DEFAULT '0.0.0.0'" in stmt and GameServer().bind_address == "0.0.0.0"


# ------------------------------------------------------------------ format

@pytest.mark.parametrize("game_ports", [{}, {"game": "25565/tcp"}, {"game": "2456/udp", "a2s": "2457/udp"},
                                        {"game": "27000/tcp", "game_udp": "27000/udp"}, {"x": "65535/udp"}])
def test_well_formed_ports_pass(game_ports):
    ports.check_format(game_ports)


@pytest.mark.parametrize("spec", ["25565", "25565/sctp", "0/tcp", "70000/udp", " 25565/tcp", "25565/TCP",
                                  "abc/tcp", 25565, None])
def test_a_malformed_port_is_refused_with_a_code(spec):
    with pytest.raises(HTTPException) as ei:
        ports.check_format({"game": spec})
    assert ei.value.status_code == 400 and ei.value.detail["code"] == "server.port_format"


def test_one_port_twice_in_one_server_is_refused():
    with pytest.raises(HTTPException) as ei:
        ports.check_format({"game": "2456/udp", "a2s": "2456/udp"})
    assert ei.value.detail["code"] == "server.port_duplicate"
    ports.check_format({"game": "2456/udp", "rcon": "2456/tcp"})  # another protocol is another port


# ------------------------------------------------------------------ overlap

@pytest.mark.parametrize("a, b, overlap", [
    (("", 25565, "tcp"), ("", 25565, "tcp"), True),
    (("", 25565, "tcp"), ("127.0.0.1", 25565, "tcp"), True),      # every interface includes loopback
    (("0.0.0.0", 25565, "tcp"), ("::", 25565, "tcp"), True),
    (("127.0.0.1", 25565, "tcp"), ("127.0.0.1", 25565, "tcp"), True),
    (("127.0.0.1", 25565, "tcp"), ("192.0.2.10", 25565, "tcp"), False),
    (("", 25565, "tcp"), ("", 25565, "udp"), False),
    (("", 25565, "tcp"), ("", 25566, "tcp"), False),
])
def test_which_bindings_overlap(a, b, overlap):
    assert ports.Binding(*a).overlaps(ports.Binding(*b)) is overlap
    assert ports.Binding(*b).overlaps(ports.Binding(*a)) is overlap


def test_the_label_is_the_same_in_every_language():
    assert ports.Binding("", 25565, "tcp").label() == "0.0.0.0:25565/tcp"
    assert ports.Binding("::1", 2456, "udp").label() == "[::1]:2456/udp"


# ------------------------------------------------------------------ other servers

def _row(session, **over) -> GameServer:
    srv = _server(**over)
    session.add(srv)
    session.commit()
    session.refresh(srv)
    return srv


def test_a_port_of_another_server_is_refused_even_when_that_one_is_off(mem_engine):
    with Session(mem_engine) as s:
        _row(s, key="mc-old", container_name="mc-old", enabled=False)
        with pytest.raises(HTTPException) as ei:
            ports.check_servers(_server(key="mc-new", container_name="mc-new"), s)
    assert ei.value.status_code == 409
    d = ei.value.detail
    assert d["code"] == "server.port_in_use_server" and d["holder"] == "mc-old"
    assert d["params"]["port"] == "0.0.0.0:25565/tcp"


def test_different_addresses_and_protocols_do_not_conflict(mem_engine):
    with Session(mem_engine) as s:
        _row(s, key="mc-a", container_name="mc-a", bind_address="127.0.0.1")
        _row(s, key="mc-b", container_name="mc-b", game_ports='{"game":"25565/udp"}')
        ports.check_servers(_server(key="mc-c", container_name="mc-c", bind_address="192.0.2.10"), s)
        with pytest.raises(HTTPException):  # every interface includes 127.0.0.1
            ports.check_servers(_server(key="mc-c", container_name="mc-c"), s)


def test_the_row_itself_is_no_conflict(mem_engine):
    with Session(mem_engine) as s:
        row = _row(s)
        ports.check_servers(row, s)


# ------------------------------------------------------------------ running containers

CONTAINERS = [
    {"Names": ["/console-web"], "Ports": [{"IP": "127.0.0.1", "PrivatePort": 80, "PublicPort": 24610, "Type": "tcp"}]},
    {"Names": ["/foreign"], "Ports": [
        {"IP": "0.0.0.0", "PrivatePort": 25565, "PublicPort": 25565, "Type": "tcp"},
        {"IP": "::", "PrivatePort": 25565, "PublicPort": 25565, "Type": "tcp"},
        {"PrivatePort": 25575, "Type": "tcp"},  # exposed only, not on the host
    ]},
    {"Names": ["/mc-1"], "Ports": [{"IP": "0.0.0.0", "PrivatePort": 25566, "PublicPort": 25566, "Type": "tcp"}]},
]


def test_published_ports_are_read_from_the_container_list():
    found = ports.published_by_containers(CONTAINERS, exclude={"mc-1"})
    assert (ports.Binding("127.0.0.1", 24610, "tcp"), "console-web") in found
    assert (ports.Binding("0.0.0.0", 25565, "tcp"), "foreign") in found
    assert all(b.port != 25575 for b, _ in found), "an exposed-only port is not on the host"
    assert all(holder != "mc-1" for _, holder in found), "the server's own container is left out"


def _containers(monkeypatch, listing=CONTAINERS, fail=False):
    from app.services.docker_client import DockerClient

    async def _list(self):
        if fail:
            raise RuntimeError("dockerproxy unreachable")
        return listing

    monkeypatch.setattr(DockerClient, "running_containers", _list)


def test_a_running_foreign_container_holding_the_port_is_named(monkeypatch):
    _containers(monkeypatch)
    with pytest.raises(HTTPException) as ei:
        asyncio.run(ports.check_containers(_server(key="mc-2", container_name="mc-2"), strict=False))
    assert ei.value.detail["code"] == "server.port_in_use_container"
    assert ei.value.detail["holder"] == "foreign"


def test_its_own_container_and_the_dashboard_on_loopback_are_no_conflict(monkeypatch):
    _containers(monkeypatch)
    asyncio.run(ports.check_containers(_server(game_ports='{"game":"25566/tcp"}'), strict=False))
    with pytest.raises(HTTPException) as ei:  # 0.0.0.0 includes the dashboard's 127.0.0.1:24610
        asyncio.run(ports.check_containers(_server(game_ports='{"game":"24610/tcp"}'), strict=False))
    assert ei.value.detail["holder"] == "console-web"
    asyncio.run(ports.check_containers(
        _server(game_ports='{"game":"24610/tcp"}', bind_address="192.0.2.10"), strict=False))


def test_without_docker_a_row_is_still_saved_but_a_container_is_not_created(monkeypatch):
    _containers(monkeypatch, fail=True)
    asyncio.run(ports.check_containers(_server(), strict=False))
    with pytest.raises(HTTPException) as ei:
        asyncio.run(ports.check_containers(_server(), strict=True))
    assert ei.value.status_code == 502 and ei.value.detail["code"] == "server.port_check_failed"


def test_dockers_own_refusal_becomes_a_coded_answer():
    e = RuntimeError("500: driver failed programming external connectivity: Bind for 0.0.0.0:25565 "
                     "failed: port is already allocated")
    out = ports.docker_error(_server(), e)
    assert out.status_code == 409 and out.detail["code"] == "server.port_in_use_host"
    assert ports.docker_error(_server(), RuntimeError("no such image")) is None


# ------------------------------------------------------------------ create, save, provision, start

def _active(mem_engine, monkeypatch, tmp_path):
    from app.config import settings
    from app.models.game_settings import GameSetting

    monkeypatch.setattr("app.db.engine", mem_engine)
    monkeypatch.setattr(settings, "allowed_data_roots", [str(tmp_path)])
    with Session(mem_engine) as s:
        s.add(GameSetting(game_type="minecraft", active=True))
        s.commit()


def _create_body(tmp_path, key, **over):
    body = dict(key=key, game_type=GameType.minecraft, container_name=key, data_dir=f"{tmp_path}/{key}",
                image_repo="itzg/minecraft-server", image_tag="t", rcon_host=key, rcon_port=25575,
                game_ports={"game": "25565/tcp"}, eula_accepted=True)
    body.update(over)
    return servers_router.ServerCreate(**body)


def test_create_refuses_a_taken_port_and_creates_nothing(mem_engine, tmp_path, monkeypatch):
    _active(mem_engine, monkeypatch, tmp_path)
    with Session(mem_engine) as s:
        asyncio.run(servers_router.create_server(_create_body(tmp_path, "mc-1"), session=s, user=ADMIN))
        with pytest.raises(HTTPException) as ei:
            asyncio.run(servers_router.create_server(_create_body(tmp_path, "mc-2"), session=s, user=ADMIN))
        assert ei.value.detail["code"] == "server.port_in_use_server"
        with pytest.raises(HTTPException) as ei:
            asyncio.run(servers_router.create_server(
                _create_body(tmp_path, "mc-3", game_ports={"game": "25565"}), session=s, user=ADMIN))
        assert ei.value.detail["code"] == "server.port_format"
        assert [r.key for r in s.exec(select(GameServer)).all()] == ["mc-1"]


def test_save_checks_the_row_as_it_will_be(mem_engine, tmp_path, monkeypatch):
    _active(mem_engine, monkeypatch, tmp_path)
    with Session(mem_engine) as s:
        asyncio.run(servers_router.create_server(_create_body(tmp_path, "mc-1"), session=s, user=ADMIN))
        asyncio.run(servers_router.create_server(
            _create_body(tmp_path, "mc-2", game_ports={"game": "25566/tcp"}), session=s, user=ADMIN))
        update = servers_router.ServerUpdate(game_ports={"game": "25565/tcp"})
        with pytest.raises(HTTPException) as ei:
            asyncio.run(servers_router.update_server("mc-2", update, session=s, user=ADMIN))
        assert ei.value.detail["code"] == "server.port_in_use_server"
        # On another address the same port is free.
        update = servers_router.ServerUpdate(game_ports={"game": "25565/tcp"}, bind_address="192.0.2.10")
        asyncio.run(servers_router.update_server("mc-1", servers_router.ServerUpdate(bind_address="127.0.0.1"),
                                                 session=s, user=ADMIN))
        out = asyncio.run(servers_router.update_server("mc-2", update, session=s, user=ADMIN))
    assert out["game_ports"] == {"game": "25565/tcp"} and out["bind_address"] == "192.0.2.10"


def test_save_names_a_foreign_container_but_not_its_own(mem_engine, tmp_path, monkeypatch):
    _active(mem_engine, monkeypatch, tmp_path)
    with Session(mem_engine) as s:
        asyncio.run(servers_router.create_server(
            _create_body(tmp_path, "mc-1", game_ports={"game": "25566/tcp"}), session=s, user=ADMIN))
        _containers(monkeypatch)
        # mc-1's own running container holds 25566: saving it unchanged is fine.
        asyncio.run(servers_router.update_server(
            "mc-1", servers_router.ServerUpdate(game_ports={"game": "25566/tcp"}, display_name="x"),
            session=s, user=ADMIN))
        with pytest.raises(HTTPException) as ei:
            asyncio.run(servers_router.update_server(
                "mc-1", servers_router.ServerUpdate(game_ports={"game": "25565/tcp"}), session=s, user=ADMIN))
    assert ei.value.detail["code"] == "server.port_in_use_container"
    assert ei.value.detail["params"]["holder"] == "foreign"


def test_create_container_stops_before_the_download(mem_engine, tmp_path, monkeypatch):
    from app.services import provisioning

    monkeypatch.setattr("app.db.engine", mem_engine)
    monkeypatch.setattr("app.config.settings.allowed_data_roots", [str(tmp_path)])
    pulled: list[str] = []

    class _Docker:
        async def state(self, name):
            return {"exists": False, "running": False, "status": "missing"}

        async def running_containers(self):
            return CONTAINERS

        async def image_exists(self, ref):
            return False

        async def pull(self, ref, on_progress=None):
            pulled.append(ref)

    monkeypatch.setattr("app.services.docker_client.docker", _Docker())
    with Session(mem_engine) as s:
        _row(s, key="mc-2", container_name="mc-2", data_dir=f"{tmp_path}/mc-2",
             extra=json.dumps({"eula_accepted": True}))
    with pytest.raises(HTTPException) as ei:
        asyncio.run(provisioning.provision("mc-2"))
    assert ei.value.detail["code"] == "server.port_in_use_container"
    assert pulled == [], "nothing is downloaded for a container that could not start"


def test_start_names_the_holder_instead_of_dockers_error(monkeypatch):
    from app.routers import server as server_router

    async def _fits(server):
        return True, 0, []

    async def _no_blockers(server):
        return []

    started: list[str] = []

    async def _start(server):
        started.append(server.key)

    monkeypatch.setattr(server_router.budget_svc, "can_start", _fits)
    monkeypatch.setattr(server_router.mod_state, "guard_boot", _no_blockers)
    monkeypatch.setattr(server_router.lifecycle, "start_server", _start)
    _containers(monkeypatch)
    with pytest.raises(HTTPException) as ei:
        asyncio.run(server_router.perform_action(_server(key="mc-2", container_name="mc-2"), "start", None, ADMIN))
    assert ei.value.status_code == 409 and ei.value.detail["holder"] == "foreign"
    assert started == []


def test_start_translates_dockers_port_error(monkeypatch):
    from app.routers import server as server_router

    async def _fits(server):
        return True, 0, []

    async def _no_blockers(server):
        return []

    async def _start(server):
        raise RuntimeError("Bind for 0.0.0.0:25565 failed: port is already allocated")

    monkeypatch.setattr(server_router.budget_svc, "can_start", _fits)
    monkeypatch.setattr(server_router.mod_state, "guard_boot", _no_blockers)
    monkeypatch.setattr(server_router.lifecycle, "start_server", _start)
    with pytest.raises(HTTPException) as ei:
        asyncio.run(server_router.perform_action(_server(game_ports='{"game":"25599/tcp"}'), "start", None, ADMIN))
    assert ei.value.status_code == 409 and ei.value.detail["code"] == "server.port_in_use_host"
