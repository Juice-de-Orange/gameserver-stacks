"""A port or console password changed after the first start must reach the game, not only the env.

The itzg image writes `server-port` and `rcon.password` into server.properties only when it
creates the file; with `OVERRIDE_SERVER_PROPERTIES=false` it never touches it again. A port
changed on the edit page would otherwise be published by Docker while the game keeps listening
on the old one, and a new console password would lock the dashboard out of RCON.
"""
import asyncio
import json

from sqlmodel import Session

from app.models import Role, User
from app.models.server import GameServer, GameType
from app.routers import servers as servers_router
from app.services.drivers.minecraft import MinecraftDriver

ADMIN = User(id=1, display_name="Admin", username="admin", pw_hash="x", role=Role.admin)


def _props(tmp_path) -> dict:
    lines = (tmp_path / "data" / "server.properties").read_text(encoding="utf-8").splitlines()
    return dict(line.split("=", 1) for line in lines if "=" in line and not line.startswith("#"))


def _row(mem_engine, tmp_path, monkeypatch):
    monkeypatch.setattr("app.db.engine", mem_engine)
    monkeypatch.delenv("MC_PW", raising=False)
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "server.properties").write_text(
        "motd=hi\nserver-port=25565\nrcon.port=25575\nrcon.password=old\nenable-rcon=true\n", encoding="utf-8")
    with Session(mem_engine) as s:
        s.add(GameServer(key="mc-1", game_type=GameType.minecraft, data_dir=str(tmp_path), rcon_port=25575,
                         game_ports=json.dumps({"game": "25565/tcp"}), rcon_password_env="MC_PW"))
        s.commit()


def test_a_changed_port_is_written_into_server_properties(mem_engine, tmp_path, monkeypatch):
    _row(mem_engine, tmp_path, monkeypatch)
    body = servers_router.ServerUpdate(game_ports={"game": "24512/tcp"},
                                       extra={"env": {"SERVER_PORT": "24512"}})
    with Session(mem_engine) as s:
        asyncio.run(servers_router.update_server("mc-1", body, session=s, user=ADMIN))
    props = _props(tmp_path)
    assert props["server-port"] == "24512" and props["motd"] == "hi"


def test_a_new_password_is_written_too_unless_the_env_wins(mem_engine, tmp_path, monkeypatch):
    _row(mem_engine, tmp_path, monkeypatch)
    with Session(mem_engine) as s:
        servers_router.set_secret("mc-1", servers_router.SecretIn(value="brand-new"), session=s, user=ADMIN)
    assert _props(tmp_path)["rcon.password"] == "brand-new"

    monkeypatch.setenv("MC_PW", "from-env")
    with Session(mem_engine) as s:
        servers_router.set_secret("mc-1", servers_router.SecretIn(value="ignored"), session=s, user=ADMIN)
    assert _props(tmp_path)["rcon.password"] == "from-env"


def test_no_file_yet_means_nothing_to_do(tmp_path):
    srv = GameServer(key="mc", game_type=GameType.minecraft, data_dir=str(tmp_path),
                     game_ports=json.dumps({"game": "25565/tcp"}))
    assert MinecraftDriver().sync_config_from_registry(srv, "pw") == []
    assert not (tmp_path / "data").exists()
