"""Minecraft added again on a KEPT data directory must answer RCON with the NEW password.

"Remove, keep data" leaves `server.properties` behind, with the old `rcon.password`. The itzg
image writes the file only when it creates it (`OVERRIDE_SERVER_PROPERTIES=false`), so a server
re-added on that directory got the new password in its container env while the game kept the old
one: the console said "RCON authentication failed", "Back up now" failed at `save-off` and a
recreate rolled back after its boot check timed out. Creating the server and creating its
container both have to carry the registry's values into the existing file.
"""
import asyncio
import json

from sqlmodel import Session

from app.config import settings
from app.models import Role, User
from app.models.game_settings import GameSetting
from app.models.server import ConsoleKind, GameServer, GameType, ManagementMode
from app.routers import servers as servers_router
from app.services import provisioning
from app.services.drivers.minecraft import MinecraftDriver

ADMIN = User(id=1, display_name="Admin", username="admin", pw_hash="x", role=Role.admin)

OLD = (
    "motd=kept world\nserver-port=25565\nrcon.port=25575\nrcon.password=old-one\n"
    "enable-rcon=true\nlevel-name=world\n"
)


def _props(data_dir) -> dict:
    lines = (data_dir / "data" / "server.properties").read_text(encoding="utf-8").splitlines()
    return dict(line.split("=", 1) for line in lines if "=" in line and not line.startswith("#"))


def _kept_dir(tmp_path):
    data_dir = tmp_path / "mc-1"
    (data_dir / "data").mkdir(parents=True)
    (data_dir / "data" / "server.properties").write_text(OLD, encoding="utf-8")
    return data_dir


class _Docker:
    """Records what the game would find in its file at the moment the container is created."""

    def __init__(self, data_dir):
        self.data_dir = data_dir
        self.password_at_create = None

    async def state(self, name):
        return {"exists": False, "running": False, "status": "missing"}

    async def image_exists(self, ref):
        return True

    async def running_containers(self):
        return []

    async def create(self, name, payload):
        self.password_at_create = _props(self.data_dir)["rcon.password"]
        self.env_password = next(
            e.split("=", 1)[1] for e in payload["Env"] if e.startswith("RCON_PASSWORD=")
        )


def test_creating_the_container_writes_the_new_password_into_the_kept_file(mem_engine, tmp_path, monkeypatch):
    from app.services import operations

    monkeypatch.setattr("app.db.engine", mem_engine)
    monkeypatch.setattr(operations, "engine", mem_engine)
    monkeypatch.setattr(settings, "allowed_data_roots", [str(tmp_path)])
    monkeypatch.setattr("app.services.servers.resolve_secret", lambda server: "new-one")
    data_dir = _kept_dir(tmp_path)
    with Session(mem_engine) as s:
        s.add(GameServer(
            key="mc-1", game_type=GameType.minecraft, container_name="mc-1", data_dir=str(data_dir),
            image_repo="itzg/minecraft-server", image_tag="1", management_mode=ManagementMode.dashboard,
            rcon_port=25575, console_kind=ConsoleKind.source_rcon,
            game_ports=json.dumps({"game": "25566/tcp"}), extra=json.dumps({"eula_accepted": True}),
        ))
        s.commit()
    dk = _Docker(data_dir)
    monkeypatch.setattr("app.services.docker_client.docker", dk)

    asyncio.run(provisioning.provision("mc-1"))

    assert dk.env_password == "new-one"
    assert dk.password_at_create == "new-one", "the game would refuse the container's password"
    props = _props(data_dir)
    assert props["server-port"] == "25566"
    # Everything else in the kept file is the owner's and stays.
    assert props["motd"] == "kept world" and props["level-name"] == "world"


def test_creating_the_server_on_a_kept_dir_writes_the_new_password(mem_engine, tmp_path, monkeypatch):
    monkeypatch.setattr("app.db.engine", mem_engine)
    monkeypatch.setattr(settings, "allowed_data_roots", [str(tmp_path)])
    data_dir = _kept_dir(tmp_path)
    body = servers_router.ServerCreate(
        key="mc-1", game_type=GameType.minecraft, container_name="mc-1", data_dir=str(data_dir),
        image_repo="itzg/minecraft-server", image_tag="1", rcon_port=25575,
        game_ports={"game": "25565/tcp"}, rcon_password="new-one",
        eula_accepted=True,
    )
    with Session(mem_engine) as s:
        s.add(GameSetting(game_type="minecraft", active=True))
        s.commit()
        asyncio.run(servers_router.create_server(body, session=s, user=ADMIN))
    assert _props(data_dir)["rcon.password"] == "new-one"


def test_a_fresh_dir_is_left_to_the_image(tmp_path):
    srv = GameServer(key="mc", game_type=GameType.minecraft, data_dir=str(tmp_path),
                     game_ports=json.dumps({"game": "25565/tcp"}))
    MinecraftDriver().ensure_config(srv, "pw")
    assert not (tmp_path / "data").exists()
