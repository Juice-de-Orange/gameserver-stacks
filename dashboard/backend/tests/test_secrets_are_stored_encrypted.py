"""A console password entered in the UI is stored encrypted and never leaves the API again.

Before A2 the "value" mode of "Add server" wrote the password in clear text into
`game_servers.rcon_password` — readable for anybody with the database or one of its nightly
dumps. Now it goes through `services/secrets.py` (Fernet, key derived from GSS_SECRET_KEY); an
environment variable of the same name still wins, so existing installations keep working.
"""
import asyncio
import json

import pytest
from cryptography.fernet import InvalidToken
from fastapi import HTTPException
from sqlmodel import Session, select

from app import config, main
from app.config import settings
from app.models import AuditLog, Role, User
from app.models.server import GameServer, GameType
from app.models.server_secret import ServerSecret
from app.routers import servers as servers_router
from app.services import secrets
from app.services import servers as servers_registry

ADMIN = User(id=1, display_name="Admin", username="admin", pw_hash="x", role=Role.admin)
PASSWORD = "hunter2-correct-horse"


# ------------------------------------------------------------------ the key

@pytest.mark.parametrize("key", ["", "short", "x" * (config.SECRET_KEY_MIN_LENGTH - 1)])
def test_a_missing_or_short_key_is_refused_with_the_way_to_make_one(key):
    problem = config.secret_key_problem(key)
    assert problem and "GSS_SECRET_KEY" in problem and "openssl rand -hex 32" in problem


def test_a_long_enough_key_is_accepted():
    assert config.secret_key_problem("x" * config.SECRET_KEY_MIN_LENGTH) is None


def test_the_api_refuses_to_start_without_the_key(monkeypatch):
    """Fail closed, like JWT_SECRET: without the key nothing could be stored or read."""
    monkeypatch.setattr(config.settings, "secret_key", "")
    reached = []
    monkeypatch.setattr(main, "init_db", lambda: reached.append("init_db"))

    async def _start():
        async with main.lifespan(main.app):
            reached.append("running")

    with pytest.raises(RuntimeError, match="GSS_SECRET_KEY"):
        asyncio.run(_start())
    assert reached == [], "the start went past the key check"


def test_the_key_is_a_known_setting():
    assert "GSS_SECRET_KEY" in config.setting_env_names()


# ------------------------------------------------------------------ encryption

def test_the_ciphertext_does_not_contain_the_password_and_round_trips():
    token = secrets.encrypt(PASSWORD)
    assert PASSWORD not in token
    assert secrets.decrypt(token) == PASSWORD
    assert secrets.encrypt(PASSWORD) != token, "Fernet tokens are randomised"


def test_another_key_cannot_read_it():
    token = secrets.encrypt(PASSWORD, key="a" * 40)
    with pytest.raises(InvalidToken):
        secrets.decrypt(token, key="b" * 40)


# ------------------------------------------------------------------ resolution order

def _row(mem_engine, **over) -> GameServer:
    base = dict(key="mc-1", game_type=GameType.minecraft, enabled=False, container_name="mc-1",
                rcon_password_env="MC_RCON_PW")
    base.update(over)
    with Session(mem_engine) as s:
        srv = GameServer(**base)
        s.add(srv)
        s.commit()
        s.refresh(srv)
        return srv


def test_env_wins_then_stored_then_nothing(mem_engine, monkeypatch):
    monkeypatch.setattr("app.db.engine", mem_engine)
    srv = _row(mem_engine)
    monkeypatch.delenv("MC_RCON_PW", raising=False)
    assert servers_registry.resolve_secret(srv) == ""

    with Session(mem_engine) as s:
        secrets.store(s, srv.id, PASSWORD)
        s.commit()
    assert servers_registry.resolve_secret(srv) == PASSWORD

    monkeypatch.setenv("MC_RCON_PW", "from-the-env")
    assert servers_registry.resolve_secret(srv) == "from-the-env", "an existing .env must keep working"
    state = secrets.status(srv)
    assert state["source"] == "env" and state["stored"] is True
    assert PASSWORD not in json.dumps(state) and "from-the-env" not in json.dumps(state)


def test_a_stored_value_of_another_key_counts_as_unreadable(mem_engine, monkeypatch):
    monkeypatch.setattr("app.db.engine", mem_engine)
    monkeypatch.delenv("MC_RCON_PW", raising=False)
    srv = _row(mem_engine)
    with Session(mem_engine) as s:
        s.add(ServerSecret(server_id=srv.id, slot=secrets.RCON, ciphertext=secrets.encrypt("x", key="z" * 40)))
        s.commit()
    assert servers_registry.resolve_secret(srv) == ""
    assert secrets.status(srv) | {} == {"source": "none", "env_name": "MC_RCON_PW", "env_set": False,
                                         "stored": False, "unreadable": True}


def test_an_old_plain_text_password_is_moved_into_encrypted_storage(mem_engine, monkeypatch):
    monkeypatch.setattr("app.db.engine", mem_engine)
    monkeypatch.delenv("MC_RCON_PW", raising=False)
    srv = _row(mem_engine, rcon_password=PASSWORD, rcon_password_env="")
    assert secrets.migrate_literals(mem_engine) == 1
    assert secrets.migrate_literals(mem_engine) == 0, "idempotent"
    with Session(mem_engine) as s:
        fresh = s.get(GameServer, srv.id)
        (row,) = s.exec(select(ServerSecret)).all()
    assert fresh.rcon_password == ""
    assert PASSWORD not in row.ciphertext
    assert servers_registry.resolve_secret(fresh) == PASSWORD


def test_the_start_runs_the_migration():
    import inspect

    assert "migrate_literals" in inspect.getsource(main.lifespan)


# ------------------------------------------------------------------ the API never returns it

def _create(mem_engine, tmp_path, monkeypatch, **over) -> dict:
    from app.models.game_settings import GameSetting

    monkeypatch.setattr(settings, "allowed_data_roots", [str(tmp_path)])
    with Session(mem_engine) as s:
        s.add(GameSetting(game_type="minecraft", active=True))
        s.commit()
    body = dict(key="mc-1", game_type=GameType.minecraft, container_name="mc-1",
                data_dir=f"{tmp_path}/mc-1", image_repo="itzg/minecraft-server", image_tag="t",
                rcon_host="mc-1", rcon_port=25575, rcon_password=PASSWORD, eula_accepted=True)
    body.update(over)
    with Session(mem_engine) as s:
        return asyncio.run(servers_router.create_server(servers_router.ServerCreate(**body), session=s, user=ADMIN))


def test_create_stores_the_password_encrypted_and_not_in_the_row(mem_engine, tmp_path, monkeypatch):
    monkeypatch.setattr("app.db.engine", mem_engine)
    out = _create(mem_engine, tmp_path, monkeypatch)
    assert PASSWORD not in json.dumps(out, default=str)
    with Session(mem_engine) as s:
        srv = s.exec(select(GameServer)).one()
        (row,) = s.exec(select(ServerSecret)).all()
    assert srv.rcon_password == ""
    assert row.server_id == srv.id and PASSWORD not in row.ciphertext
    assert servers_registry.resolve_secret(srv) == PASSWORD


def test_the_secret_route_stores_clears_and_answers_without_the_value(mem_engine, tmp_path, monkeypatch):
    monkeypatch.setattr("app.db.engine", mem_engine)
    monkeypatch.delenv("MC_RCON", raising=False)
    _create(mem_engine, tmp_path, monkeypatch, rcon_password="", rcon_password_env="MC_RCON")
    with Session(mem_engine) as s:
        out = servers_router.set_secret("mc-1", servers_router.SecretIn(value=PASSWORD), session=s, user=ADMIN)
    assert out["source"] == "stored" and out["stored"] is True
    assert PASSWORD not in json.dumps(out)
    with Session(mem_engine) as s:
        out = servers_router.set_secret("mc-1", servers_router.SecretIn(value=""), session=s, user=ADMIN)
        assert out["source"] == "none"
        assert s.exec(select(ServerSecret)).all() == []
        audit = [a.payload for a in s.exec(select(AuditLog)).all() if a.action == "servers.secret"]
    assert audit == ["set", "cleared"], "the value never goes into the audit log either"


def test_the_secret_route_404s_for_an_unknown_server(mem_engine):
    with Session(mem_engine) as s, pytest.raises(HTTPException) as ei:
        servers_router.set_secret("nope", servers_router.SecretIn(value="x"), session=s, user=ADMIN)
    assert ei.value.status_code == 404


def test_the_server_list_names_no_secret_variable(mem_engine):
    """`GET /api/servers` is open to every member: no password, and no names of the variables
    that hold one (`rcon_password_env`, `extra.*_env`) either."""
    srv = GameServer(key="pz-1", game_type=GameType.pz, rcon_password_env="PZ_RCON_PW",
                     extra=json.dumps({"admin_password_env": "PZ_ADMIN_PW", "servername": "s"}))
    srv.id = 1
    out = servers_router.server_public(srv)
    text = json.dumps(out, default=str)
    assert "PZ_RCON_PW" not in text and "PZ_ADMIN_PW" not in text
    assert out["rcon_password_env"] == servers_router.MASKED
    assert out["extra"]["admin_password_env"] == servers_router.MASKED
    assert out["extra"]["servername"] == "s"


def test_a_masked_name_sent_back_changes_nothing(mem_engine, monkeypatch):
    """The edit form sends what it got. A masked name must not become the new name."""
    with Session(mem_engine) as s:
        s.add(GameServer(key="pz-1", game_type=GameType.pz, rcon_password_env="PZ_RCON_PW",
                         extra=json.dumps({"admin_password_env": "PZ_ADMIN_PW"})))
        s.commit()
    body = servers_router.ServerUpdate(rcon_password_env=servers_router.MASKED,
                                       extra={"admin_password_env": servers_router.MASKED})
    with Session(mem_engine) as s:
        asyncio.run(servers_router.update_server("pz-1", body, session=s, user=ADMIN))
        srv = s.exec(select(GameServer)).one()
    assert srv.rcon_password_env == "PZ_RCON_PW"
    assert json.loads(srv.extra)["admin_password_env"] == "PZ_ADMIN_PW"
