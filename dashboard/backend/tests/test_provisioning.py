"""The "Create container" button (`services/provisioning.py`, `POST /api/servers/{key}/container`).

What must hold, beyond what `test_cli_create_container.py` pins for the shared service:

* every game gets its data directory and every bind source created by the API — with the API's
  uid, not by Docker as root (before A2 only Factorio, Reforger, Avorion and Valheim did);
* the image download reports its progress in bytes to the operation;
* Minecraft is refused server-side without a recorded EULA acceptance;
* a stopped container is only replaced on request, a running one never.
"""
import asyncio
import json
import os

import pytest
from fastapi import HTTPException
from sqlmodel import Session, select

from app.config import settings
from app.models import AuditLog, Role, User
from app.models.game_settings import GameSetting
from app.models.operation import Operation
from app.models.server import ConsoleKind, GameServer, GameType, ManagementMode
from app.routers import servers as servers_router
from app.services import provisioning

ADMIN = User(id=1, display_name="Admin", username="admin", pw_hash="x", role=Role.admin)


class _Docker:
    def __init__(self, exists=False, running=False, image_present=True, frames=()):
        self.exists, self.running, self.image_present = exists, running, image_present
        self.frames = list(frames)
        self.calls: list[tuple] = []
        self.helpers: list[dict] = []
        self.seed_exit = 0

    async def state(self, name):
        return {"exists": self.exists, "running": self.running,
                "status": ("running" if self.running else "exited") if self.exists else "missing"}

    async def image_exists(self, ref):
        return self.image_present

    async def running_containers(self):  # the port check (`services/ports.py`): nothing holds a port
        return []

    async def pull(self, ref, on_progress=None):
        self.calls.append(("pull", ref))
        for frame in self.frames:
            on_progress(frame)
        self.image_present = True

    async def remove(self, name, force=False, volumes=False):
        if name.endswith("-cleanup"):
            return
        self.calls.append(("remove", name))
        self.exists = False

    async def create(self, name, payload):
        if name.endswith("-cleanup"):   # the helper that copies files out of the image
            self.helpers.append(payload)
            return
        self.calls.append(("create", name, payload))
        self.exists = True

    async def action(self, name, act, **kw):
        """The helper's `cp -a /steamcmd/. /x/`, played: the image's SteamCMD lands in the bind."""
        payload = self.helpers[-1]
        host = payload["HostConfig"]["Binds"][0].split(":")[0]
        if self.seed_exit == 0 and payload["Entrypoint"] + payload["Cmd"] == ["cp", "-a", "/steamcmd/.", "/x/"]:
            with open(os.path.join(host, "steamcmd.sh"), "w", encoding="utf-8") as f:
                f.write("#!/bin/sh\n")

    async def inspect(self, name):
        return {"State": {"ExitCode": self.seed_exit}}


class _Steps:
    def __init__(self):
        self.seen: list[tuple[str, int, int]] = []

    def step(self, text, *, current=0, total=0):
        self.seen.append((text, current, total))


def _server(mem_engine, tmp_path, game=GameType.minecraft, **over) -> GameServer:
    base = dict(key=f"{game.value[:6]}-1", game_type=game, container_name=f"c-{game.value}",
                data_dir=f"{tmp_path}/{game.value}", image_repo="example/image", image_tag="1",
                management_mode=ManagementMode.dashboard, rcon_port=27015,
                console_kind=ConsoleKind.none if game == GameType.valheim else ConsoleKind.source_rcon,
                game_version="1.22.6" if game == GameType.vintagestory else "",
                extra=json.dumps({"eula_accepted": True}) if game == GameType.minecraft else "{}")
    base.update(over)
    with Session(mem_engine) as s:
        srv = GameServer(**base)
        s.add(srv)
        s.commit()
        s.refresh(srv)
        return srv


@pytest.fixture()
def wired(mem_engine, monkeypatch, tmp_path):
    from app.services import operations

    monkeypatch.setattr("app.db.engine", mem_engine)
    monkeypatch.setattr(operations, "engine", mem_engine)
    monkeypatch.setattr(settings, "allowed_data_roots", [str(tmp_path)])

    def _wire(dk):
        monkeypatch.setattr("app.services.docker_client.docker", dk)
        return dk

    return _wire


@pytest.mark.parametrize("game", list(GameType))
def test_every_game_gets_its_bind_sources_created_by_the_api(game, mem_engine, tmp_path, wired):
    srv = _server(mem_engine, tmp_path, game)
    dk = wired(_Docker())
    result = asyncio.run(provisioning.provision(srv.key))
    assert [c[0] for c in dk.calls] == ["create"]

    binds = dk.calls[0][2]["HostConfig"]["Binds"]
    assert binds, game
    for bind in binds:
        host = bind.split(":", 1)[0]
        assert os.path.isdir(host), f"{game.value}: {host} was left for Docker to create as root"
        assert os.stat(host).st_uid == os.getuid(), host
    assert set(result["directories"]) >= {b.split(":", 1)[0] for b in binds}


def test_the_download_reports_bytes_to_the_operation(mem_engine, tmp_path, wired):
    srv = _server(mem_engine, tmp_path)
    frames = [
        {"id": "a", "status": "Downloading", "progressDetail": {"current": 10, "total": 100}},
        {"id": "b", "status": "Downloading", "progressDetail": {"current": 50, "total": 300}},
        {"id": "a", "status": "Downloading", "progressDetail": {"current": 100, "total": 100}},
    ]
    dk = wired(_Docker(image_present=False, frames=frames))
    steps = _Steps()
    result = asyncio.run(provisioning.provision(srv.key, progress=steps))
    assert result["pulled"] is True and ("pull", "example/image:1") in dk.calls
    measured = [(c, t) for text, c, t in steps.seen if text == "Downloading the image…" and t]
    assert measured == [(10, 100), (60, 400), (150, 400)]
    texts = [text for text, _, _ in steps.seen]
    assert texts.index("Creating the data directories…") < texts.index("Writing the configuration…") \
        < texts.index("Creating the container…")


def test_minecraft_without_a_recorded_eula_gets_no_container(mem_engine, tmp_path, wired):
    srv = _server(mem_engine, tmp_path, extra="{}")
    dk = wired(_Docker())
    with pytest.raises(HTTPException) as ei:
        asyncio.run(provisioning.provision(srv.key))
    assert ei.value.detail["code"] == "provision.eula_required"
    assert dk.calls == []


def test_an_existing_container_is_kept_unless_replacing_a_stopped_one(mem_engine, tmp_path, wired):
    srv = _server(mem_engine, tmp_path)
    dk = wired(_Docker(exists=True))
    with pytest.raises(HTTPException) as ei:
        asyncio.run(provisioning.provision(srv.key))
    assert ei.value.detail["code"] == "provision.container_exists" and dk.calls == []

    result = asyncio.run(provisioning.provision(srv.key, replace=True))
    assert result["replaced"] is True
    assert [c[0] for c in dk.calls] == ["remove", "create"]


def test_a_running_container_is_never_replaced(mem_engine, tmp_path, wired):
    srv = _server(mem_engine, tmp_path)
    dk = wired(_Docker(exists=True, running=True))
    with pytest.raises(HTTPException) as ei:
        asyncio.run(provisioning.provision(srv.key, replace=True))
    assert ei.value.detail["code"] == "provision.container_running" and dk.calls == []


def test_a_data_dir_outside_the_roots_creates_nothing(mem_engine, tmp_path, wired):
    srv = _server(mem_engine, tmp_path, data_dir="/etc/games")
    dk = wired(_Docker())
    with pytest.raises(HTTPException) as ei:
        asyncio.run(provisioning.provision(srv.key))
    assert ei.value.detail["code"] == "provision.data_dir_invalid" and dk.calls == []


# ------------------------------------------------------------------ the routes

def _activate(mem_engine):
    with Session(mem_engine) as s:
        s.add(GameSetting(game_type="minecraft", active=True))
        s.commit()


def _create_body(tmp_path, **over):
    body = dict(key="mc-1", game_type=GameType.minecraft, container_name="mc-1",
                data_dir=f"{tmp_path}/mc-1", image_repo="itzg/minecraft-server", image_tag="t",
                rcon_host="mc-1", rcon_port=25575)
    body.update(over)
    return servers_router.ServerCreate(**body)


def test_create_refuses_minecraft_without_the_eula(mem_engine, tmp_path, wired):
    """Not only the checkbox in the form: a direct API call is refused just the same."""
    _activate(mem_engine)
    with Session(mem_engine) as s, pytest.raises(HTTPException) as ei:
        asyncio.run(servers_router.create_server(_create_body(tmp_path), session=s, user=ADMIN))
    assert ei.value.detail["code"] == "server.eula_required"
    with Session(mem_engine) as s:
        assert s.exec(select(GameServer)).all() == []


def test_create_records_the_eula_acceptance(mem_engine, tmp_path, wired):
    _activate(mem_engine)
    with Session(mem_engine) as s:
        out = asyncio.run(servers_router.create_server(_create_body(tmp_path, eula_accepted=True), session=s, user=ADMIN))
    assert out["extra"]["eula_accepted"] is True


def test_the_button_runs_the_service_as_an_operation(mem_engine, tmp_path, wired):
    srv = _server(mem_engine, tmp_path)
    dk = wired(_Docker())

    async def _run():
        with Session(mem_engine) as s:
            op = await servers_router.create_container(srv.key, servers_router.ProvisionIn(), session=s, user=ADMIN)
        for _ in range(100):
            await asyncio.sleep(0.01)
            with Session(mem_engine) as s:
                row = s.get(Operation, op["id"])
                if row.status != "running":
                    return op, row
        raise AssertionError("the operation did not finish")

    op, row = asyncio.run(_run())
    assert op["kind"] == "server.provision"
    assert row.status == "ok", row.detail
    assert row.detail == "The container was created."
    assert [c[0] for c in dk.calls] == ["create"]
    with Session(mem_engine) as s:
        latest = servers_router.provision_state(srv.key, session=s, _=ADMIN)
        actions = [a.action for a in s.exec(select(AuditLog)).all()]
    assert latest["id"] == op["id"] and latest["status"] == "ok"
    assert "servers.provision" in actions


def test_the_button_records_a_missing_eula_and_refuses_without_it(mem_engine, tmp_path, wired):
    srv = _server(mem_engine, tmp_path, extra="{}")
    wired(_Docker())
    with Session(mem_engine) as s, pytest.raises(HTTPException) as ei:
        asyncio.run(servers_router.create_container(srv.key, servers_router.ProvisionIn(), session=s, user=ADMIN))
    assert ei.value.detail["code"] == "provision.eula_required"

    async def _accept():
        with Session(mem_engine) as s:
            return await servers_router.create_container(
                srv.key, servers_router.ProvisionIn(eula_accepted=True), session=s, user=ADMIN)

    asyncio.run(_accept())
    with Session(mem_engine) as s:
        assert json.loads(s.get(GameServer, srv.id).extra)["eula_accepted"] is True


def test_the_button_refuses_a_compose_row_and_replacing_an_enabled_one(mem_engine, tmp_path, wired):
    comp = _server(mem_engine, tmp_path, GameType.factorio, management_mode=ManagementMode.compose)
    on = _server(mem_engine, tmp_path, GameType.avorion, enabled=True)
    wired(_Docker())
    with Session(mem_engine) as s, pytest.raises(HTTPException) as ei:
        asyncio.run(servers_router.create_container(comp.key, servers_router.ProvisionIn(), session=s, user=ADMIN))
    assert ei.value.detail["code"] == "provision.compose_managed"
    with Session(mem_engine) as s, pytest.raises(HTTPException) as ei:
        asyncio.run(servers_router.create_container(
            on.key, servers_router.ProvisionIn(replace=True), session=s, user=ADMIN))
    assert ei.value.detail["code"] == "provision.replace_enabled"


# ------------------------------------------------------------------ Reforger: game + SteamCMD in the data dir

def _reforger(mem_engine, tmp_path):
    return _server(mem_engine, tmp_path, GameType.arma_reforger, image_repo="ghcr.io/acemod/arma-reforger",
                   image_tag="sha-d042142", console_kind=ConsoleKind.battleye_rcon, rcon_port=19999)


def test_reforger_keeps_the_game_and_steamcmd_in_its_data_dir(mem_engine, tmp_path, wired):
    """Only profile, Configs and workshop were bound: the 10 GB game lived in the container
    layer and every recreate downloaded it again (about 16 minutes of downtime), and every
    container left an anonymous `/steamcmd` volume behind."""
    srv = _reforger(mem_engine, tmp_path)
    dk = wired(_Docker())
    asyncio.run(provisioning.provision(srv.key))
    data = srv.data_dir
    binds = dk.calls[-1][2]["HostConfig"]["Binds"]
    assert f"{data}/install:/reforger" in binds
    assert f"{data}/steamcmd:/steamcmd" in binds


def test_steamcmd_is_copied_out_of_the_image_before_it_is_bound(mem_engine, tmp_path, wired):
    """A bind hides the image's `/steamcmd/steamcmd.sh`, which the launcher calls. The helper
    copies it from the server's own image into the empty folder first."""
    srv = _reforger(mem_engine, tmp_path)
    dk = wired(_Docker())
    asyncio.run(provisioning.provision(srv.key))
    (helper,) = dk.helpers
    assert helper["Image"] == "ghcr.io/acemod/arma-reforger:sha-d042142"
    # `-a` with CHOWN: SteamCMD writes as the owner of its folder; one of the API's uid made its
    # self-update fail on the real stack.
    assert helper["Entrypoint"] + helper["Cmd"] == ["cp", "-a", "/steamcmd/.", "/x/"]
    assert helper["HostConfig"]["CapAdd"] == ["CHOWN", "DAC_OVERRIDE", "FOWNER"]
    assert helper["HostConfig"]["Binds"] == [f"{srv.data_dir}/steamcmd:/x"]
    assert helper["HostConfig"]["NetworkMode"] == "none"
    assert os.path.isfile(f"{srv.data_dir}/steamcmd/steamcmd.sh")

    dk.exists = False
    asyncio.run(provisioning.provision(srv.key))
    assert len(dk.helpers) == 1, "a filled folder is not copied again"


def test_without_the_copy_steamcmd_is_not_bound(mem_engine, tmp_path, wired):
    """A failed copy must not leave the launcher without SteamCMD: the container is created
    without that bind, as before."""
    srv = _reforger(mem_engine, tmp_path)
    dk = _Docker()
    dk.seed_exit = 1
    wired(dk)
    asyncio.run(provisioning.provision(srv.key))
    binds = dk.calls[-1][2]["HostConfig"]["Binds"]
    assert not any(b.endswith(":/steamcmd") for b in binds)
    assert f"{srv.data_dir}/install:/reforger" in binds
