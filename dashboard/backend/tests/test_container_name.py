"""The container name of a registry row follows Docker's name rule — checked before anything is stored.

Before, an empty `container_name` was accepted on create, and "Create container" then failed with
Docker's bare 301 redirect (`POST /containers/create?name=`), which told the admin nothing.
"""
import asyncio

import pytest
from fastapi import HTTPException
from sqlmodel import Session, select

from app.models import Role, User
from app.models.server import GameServer, GameType
from app.routers import servers as servers_router

ADMIN = User(id=1, display_name="Admin", username="admin", pw_hash="x", role=Role.admin)


@pytest.fixture()
def active(mem_engine, monkeypatch, tmp_path):
    from app.config import settings
    from app.models.game_settings import GameSetting

    monkeypatch.setattr("app.db.engine", mem_engine)
    monkeypatch.setattr(settings, "allowed_data_roots", [str(tmp_path)])
    with Session(mem_engine) as s:
        s.add(GameSetting(game_type="minecraft", active=True))
        s.commit()
    return mem_engine


def _body(tmp_path, **over):
    body = dict(key="mc-1", game_type=GameType.minecraft, container_name="mc-1", data_dir=f"{tmp_path}/mc-1",
                image_repo="itzg/minecraft-server", image_tag="t", rcon_host="mc-1", rcon_port=25575,
                game_ports={"game": "25565/tcp"}, eula_accepted=True)
    body.update(over)
    return servers_router.ServerCreate(**body)


@pytest.mark.parametrize("name", ["mc-1", "MC_1", "a1", "game.server-2", "x" * 128])
def test_docker_names_are_accepted(name):
    servers_router._validate_container_name(name)


@pytest.mark.parametrize("name", ["", " ", "a", "-mc", ".mc", "_mc", "mc 1", "mc/1", "/mc-1", "mc:1", "mc-ü",
                                  "x" * 129])
def test_anything_else_is_refused_with_a_code(name):
    with pytest.raises(HTTPException) as ei:
        servers_router._validate_container_name(name)
    assert ei.value.status_code == 400
    assert ei.value.detail["code"] == "server.container_name_invalid"
    assert ei.value.detail["params"] == {}


def test_create_without_a_container_name_stores_nothing(active, tmp_path):
    with Session(active) as s:
        with pytest.raises(HTTPException) as ei:
            asyncio.run(servers_router.create_server(_body(tmp_path, container_name=""), session=s, user=ADMIN))
        assert ei.value.detail["code"] == "server.container_name_invalid"
        # Omitted entirely: the model default is empty, so the same answer.
        body =servers_router.ServerCreate(**{k: v for k, v in _body(tmp_path).model_dump().items()
                                              if k != "container_name"})
        with pytest.raises(HTTPException) as ei:
            asyncio.run(servers_router.create_server(body, session=s, user=ADMIN))
        assert ei.value.detail["code"] == "server.container_name_invalid"
        assert s.exec(select(GameServer)).first() is None


def test_create_with_a_docker_name_works(active, tmp_path):
    with Session(active) as s:
        out = asyncio.run(servers_router.create_server(_body(tmp_path), session=s, user=ADMIN))
    assert out["container_name"] == "mc-1"


def test_save_refuses_an_invalid_name_and_keeps_the_old_one(active, tmp_path):
    with Session(active) as s:
        asyncio.run(servers_router.create_server(_body(tmp_path), session=s, user=ADMIN))
        for name in ("", "bad name"):
            with pytest.raises(HTTPException) as ei:
                asyncio.run(servers_router.update_server(
                    "mc-1", servers_router.ServerUpdate(container_name=name), session=s, user=ADMIN))
            assert ei.value.detail["code"] == "server.container_name_invalid"
    with Session(active) as s:
        assert s.exec(select(GameServer)).one().container_name == "mc-1"
        out = asyncio.run(servers_router.update_server(
            "mc-1", servers_router.ServerUpdate(container_name="mc-one"), session=s, user=ADMIN))
    assert out["container_name"] == "mc-one"
