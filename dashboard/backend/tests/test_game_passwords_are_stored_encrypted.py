"""Project Zomboid's admin and join passwords and Valheim's SERVER_PASS go through the encrypted
store (`services/secrets.py`) like the console password: never clear text in the row, an
environment variable still wins, the server list never carries them, and what an older version
left in `extra` is moved at start."""
import asyncio
import json

import pytest
from fastapi import HTTPException
from sqlmodel import Session, select

from app.config import settings
from app.models import Role, User
from app.models.game_settings import GameSetting
from app.models.server import ConsoleKind, GameServer, GameType, ManagementMode
from app.models.server_secret import ServerSecret
from app.routers import servers as servers_router
from app.services import secrets
from app.services.drivers.pz import PZDriver
from app.services.drivers.valheim import ValheimDriver

ADMIN = User(id=1, display_name="Admin", username="admin", pw_hash="x", role=Role.admin)
JOIN = "join-pw-12345"


@pytest.fixture()
def db(mem_engine, monkeypatch, tmp_path):
    monkeypatch.setattr("app.db.engine", mem_engine)
    monkeypatch.setattr(settings, "allowed_data_roots", [str(tmp_path)])
    for name in ("PZ_ADMIN_PW", "PZ_SERVER_PW", "PZ_RCON_PW"):
        monkeypatch.delenv(name, raising=False)
    return mem_engine


@pytest.fixture()
def pz_ini(tmp_path, monkeypatch):
    """The `.ini` the PZ editor reads its other fields from."""
    from app.services import ini_editor as ini

    path = tmp_path / "servertest.ini"
    path.write_text("Password=from-the-image\nPVP=true\n", encoding="utf-8")
    monkeypatch.setattr(ini, "ini_path", lambda s: str(path))
    return path


def _add(engine, **over) -> GameServer:
    with Session(engine) as s:
        srv = GameServer(**over)
        s.add(srv)
        s.commit()
        s.refresh(srv)
        return srv


def _pz(engine, **over) -> GameServer:
    base = dict(key="pz-1", game_type=GameType.pz, management_mode=ManagementMode.dashboard,
                container_name="pz-1", rcon_port=27015,
                extra=json.dumps({"admin_password_env": "PZ_ADMIN_PW", "password_env": "PZ_SERVER_PW"}))
    base.update(over)
    return _add(engine, **base)


def _valheim(engine, **over) -> GameServer:
    base = dict(key="vh-1", game_type=GameType.valheim, management_mode=ManagementMode.dashboard,
                container_name="vh-1", console_kind=ConsoleKind.none, game_ports='{"game":"2456/udp"}')
    base.update(over)
    return _add(engine, **base)


def _fresh(engine, srv) -> GameServer:
    with Session(engine) as s:
        return s.get(GameServer, srv.id)


def _secret_rows(engine) -> list[ServerSecret]:
    with Session(engine) as s:
        return list(s.exec(select(ServerSecret)).all())


# ------------------------------------------------------------------ Project Zomboid

def test_pz_join_and_admin_password_come_from_the_store_and_the_env_wins(db, monkeypatch):
    srv = _pz(db)
    with Session(db) as s:
        secrets.store(s, srv.id, JOIN, secrets.JOIN)
        secrets.store(s, srv.id, "admin-pw", secrets.ADMIN)
        s.commit()
    env = PZDriver().container_env(srv, "rcon-pw")
    assert env["PASSWORD"] == JOIN and env["ADMINPASSWORD"] == "admin-pw" and env["RCONPASSWORD"] == "rcon-pw"

    monkeypatch.setenv("PZ_SERVER_PW", "from-env")
    monkeypatch.setenv("PZ_ADMIN_PW", "admin-from-env")
    env = PZDriver().container_env(srv, "rcon-pw")
    assert env["PASSWORD"] == "from-env" and env["ADMINPASSWORD"] == "admin-from-env"


def test_pz_without_any_source_keeps_the_old_fallbacks(db):
    env = PZDriver().container_env(_pz(db), "rcon-pw")
    assert env["ADMINPASSWORD"] == "rcon-pw", "admin falls back to the console password"
    assert "PASSWORD" not in env, "no join password: left to the image"


def test_the_pz_config_editor_writes_the_join_password_into_the_store(db, pz_ini):
    srv = _pz(db)
    PZDriver().set_config(srv, {"Password": JOIN})
    row = _fresh(db, srv)
    assert "password" not in json.loads(row.extra) and JOIN not in row.extra
    (stored,) = _secret_rows(db)
    assert stored.slot == secrets.JOIN and JOIN not in stored.ciphertext
    field = next(f for f in PZDriver().config_fields(row) if f.key == "Password")
    assert field.value == JOIN and not field.env_managed, "the join password stays visible to the editor"
    assert PZDriver().connect_info(row).password_required is True

    PZDriver().set_config(row, {"Password": ""})
    assert _secret_rows(db) == [] and PZDriver().connect_info(_fresh(db, srv)).password_required is False


def test_a_join_password_set_by_the_env_is_system_managed_in_the_editor(db, monkeypatch, pz_ini):
    monkeypatch.setenv("PZ_SERVER_PW", "from-env")
    field = next(f for f in PZDriver().config_fields(_pz(db)) if f.key == "Password")
    assert field.env_managed is True and field.value == "", "an env-held secret is not sent to the editor"


def test_the_admin_password_has_its_own_slot_in_the_api(db, monkeypatch):
    class _Docker:
        async def state(self, name):
            return {"exists": False, "running": False, "status": "missing"}

    monkeypatch.setattr(servers_router, "docker", _Docker())
    srv = _pz(db)
    with Session(db) as s:
        out = servers_router.set_secret("pz-1", servers_router.SecretIn(value="admin-pw", slot="admin"),
                                        session=s, user=ADMIN)
    assert out["source"] == "stored" and "admin-pw" not in json.dumps(out)
    assert PZDriver().container_env(_fresh(db, srv), "rcon")["ADMINPASSWORD"] == "admin-pw"

    with Session(db) as s:
        with pytest.raises(HTTPException) as ei:
            servers_router.set_secret("pz-1", servers_router.SecretIn(value="x", slot="join"), session=s, user=ADMIN)
        assert ei.value.detail["code"] == "server.secret_slot_invalid"
    setup = asyncio.run(servers_router.setup_state("pz-1", session=Session(db), _=ADMIN))
    assert setup["secrets"]["admin"]["source"] == "stored"
    assert "admin-pw" not in json.dumps(setup, default=str)


def test_an_old_pz_join_password_in_extra_is_moved_at_start(db):
    srv = _pz(db, extra=json.dumps({"password": JOIN, "password_env": "PZ_SERVER_PW", "servername": "s"}))
    assert PZDriver().container_env(srv, "r")["PASSWORD"] == JOIN, "read until it is moved"
    assert secrets.migrate_literals(db) == 1
    assert secrets.migrate_literals(db) == 0, "idempotent"
    row = _fresh(db, srv)
    assert json.loads(row.extra) == {"password_env": "PZ_SERVER_PW", "servername": "s"}
    assert PZDriver().container_env(row, "r")["PASSWORD"] == JOIN


# ------------------------------------------------------------------ Valheim

def test_valheim_create_puts_server_pass_into_the_store(db, tmp_path):
    with Session(db) as s:
        s.add(GameSetting(game_type="valheim", active=True))
        s.commit()
    body = servers_router.ServerCreate(
        key="vh-1", game_type=GameType.valheim, container_name="vh-1", data_dir=f"{tmp_path}/vh-1",
        image_repo="ghcr.io/community-valheim-tools/valheim-server", image_tag="1.4.0",
        console_kind=ConsoleKind.none, game_ports={"game": "2456/udp", "a2s": "2457/udp"},
        extra={"env": {"SERVER_PASS": JOIN, "SERVER_NAME": "Vikings"}},
    )
    with Session(db) as s:
        out = asyncio.run(servers_router.create_server(body, session=s, user=ADMIN))
        row = s.exec(select(GameServer)).one()
    assert JOIN not in json.dumps(out, default=str) and JOIN not in row.extra
    assert json.loads(row.extra)["env"] == {"SERVER_NAME": "Vikings"}
    assert ValheimDriver().container_env(row, "")["SERVER_PASS"] == JOIN
    assert ValheimDriver().connect_info(row).password_required is True


def test_valheim_config_editor_stores_the_password_and_keeps_empty_as_no_password(db):
    srv = _valheim(db, extra=json.dumps({"env": {"SERVER_NAME": "Vikings"}}))
    assert "SERVER_PASS" not in ValheimDriver().container_env(srv, ""), "never set: the image default"

    ValheimDriver().set_config(srv, {"SERVER_PASS": JOIN})
    row = _fresh(db, srv)
    assert JOIN not in row.extra and len(_secret_rows(db)) == 1
    assert ValheimDriver().container_env(row, "")["SERVER_PASS"] == JOIN
    field = next(f for f in ValheimDriver().config_fields(row) if f.key == "SERVER_PASS")
    assert field.value == JOIN

    ValheimDriver().set_config(row, {"SERVER_PASS": ""})
    row = _fresh(db, srv)
    assert _secret_rows(db) == []
    assert json.loads(row.extra)["env"]["SERVER_PASS"] == "", "empty = open server, kept as the statement"
    assert ValheimDriver().container_env(row, "")["SERVER_PASS"] == ""


def test_an_old_valheim_server_pass_is_moved_at_start(db):
    srv = _valheim(db, extra=json.dumps({"env": {"SERVER_PASS": JOIN, "SERVER_NAME": "Vikings"}}))
    open_srv = _valheim(db, key="vh-2", container_name="vh-2", extra=json.dumps({"env": {"SERVER_PASS": ""}}))
    assert secrets.migrate_literals(db) == 1
    assert json.loads(_fresh(db, srv).extra)["env"] == {"SERVER_NAME": "Vikings"}
    assert json.loads(_fresh(db, open_srv).extra)["env"] == {"SERVER_PASS": ""}
    assert ValheimDriver().container_env(_fresh(db, srv), "")["SERVER_PASS"] == JOIN


def test_the_server_list_never_carries_a_join_password(db):
    srv = _valheim(db)
    with Session(db) as s:
        secrets.store(s, srv.id, JOIN, secrets.JOIN)
        s.commit()
    assert JOIN not in json.dumps(servers_router.server_public(_fresh(db, srv)), default=str)


def test_a_masked_value_sent_back_is_no_new_password(db):
    """A form loaded before the move still has the masked literal and sends it back."""
    srv = _valheim(db)
    with Session(db) as s:
        secrets.store(s, srv.id, JOIN, secrets.JOIN)
        s.commit()
    body = servers_router.ServerUpdate(extra={"env": {"SERVER_PASS": servers_router.MASKED}})
    with Session(db) as s:
        asyncio.run(servers_router.update_server("vh-1", body, session=s, user=ADMIN))
    assert ValheimDriver().container_env(_fresh(db, srv), "")["SERVER_PASS"] == JOIN
