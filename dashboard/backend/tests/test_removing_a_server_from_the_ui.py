""""Remove" on the edit page (`POST /api/servers/{key}/remove`, `services/provisioning.remove_server`).

The CLI `remove-server` only ever deleted rows; the UI also removes the container and, if the
admin chooses so, the data directory. What must hold: only a switched-off server, only after the
key was typed, "keep data" leaves every file in place, "delete data" deletes exactly this
server's folder and nothing that another row uses, and the audit entry outlives the row.
"""
import asyncio
import os

import pytest
from fastapi import HTTPException
from sqlmodel import Session, select

from app.config import settings
from app.models import AuditLog, Role, ServerIncident, User
from app.models.server import GameServer, GameType, ManagementMode
from app.routers import servers as servers_router

ADMIN = User(id=1, display_name="Admin", username="admin", pw_hash="x", role=Role.admin)


class _Docker:
    """The game container, plus the cleanup helper `provisioning` starts for root-owned files.

    The helper's `find /x -mindepth 1 -delete` is played as root would: write permission does
    not matter. `helper_works=False` plays a helper that deletes nothing.
    """

    def __init__(self, exists=True, fail=False, helper_works=True):
        self.exists, self.fail, self.helper_works = exists, fail, helper_works
        self.removed: list[str] = []
        self.removed_volumes: dict[str, bool] = {}
        self.helpers: list[dict] = []

    async def state(self, name):
        if self.fail:
            raise RuntimeError("proxy down")
        if name.endswith("-cleanup"):
            return {"exists": True, "running": False, "status": "exited"}
        return {"exists": self.exists, "running": False, "status": "exited" if self.exists else "missing"}

    async def remove(self, name, force=False, volumes=False):
        if name.endswith("-cleanup"):
            self.removed_volumes[name] = volumes
            return
        self.removed.append(name)
        self.removed_volumes[name] = volumes
        self.exists = False

    async def image_exists(self, image):
        return True

    async def create(self, name, payload):
        self.helpers.append(payload)

    async def action(self, name, act, **kw):
        payload = self.helpers[-1]
        host = payload["HostConfig"]["Binds"][0].split(":")[0]
        if self.helper_works and payload["Entrypoint"] + payload["Cmd"] == ["find", "/x", "-mindepth", "1", "-delete"]:
            for entry in os.listdir(host):
                _force_remove(os.path.join(host, entry))

    async def inspect(self, name):
        return {"State": {"ExitCode": 0}}


def _force_remove(path):
    """What root can do and the API cannot: delete in a folder without write permission."""
    import shutil

    for dirpath, _, _ in os.walk(path):
        os.chmod(dirpath, 0o755)
    if os.path.isdir(path):
        shutil.rmtree(path)
    else:
        os.remove(path)


@pytest.fixture()
def world(mem_engine, monkeypatch, tmp_path):
    monkeypatch.setattr("app.db.engine", mem_engine)
    monkeypatch.setattr(settings, "allowed_data_roots", [str(tmp_path)])
    data = tmp_path / "mc-1"
    (data / "data" / "world").mkdir(parents=True)
    (data / "data" / "world" / "level.dat").write_text("x", encoding="utf-8")
    with Session(mem_engine) as s:
        srv = GameServer(key="mc-1", game_type=GameType.minecraft, container_name="mc-1",
                         data_dir=str(data), enabled=False, management_mode=ManagementMode.dashboard)
        s.add(srv)
        s.commit()
        s.refresh(srv)
        s.add(ServerIncident(kind="rcon_hang", server_id=srv.id))
        s.add(AuditLog(action="servers.create", username="admin", server_id=srv.id))
        s.commit()
    dk = _Docker()
    monkeypatch.setattr("app.services.docker_client.docker", dk)
    return dk, data


def _remove(mem_engine, key="mc-1", confirm="mc-1", delete_data=False):
    with Session(mem_engine) as s:
        return asyncio.run(servers_router.remove_server(
            key, servers_router.RemoveIn(confirm_key=confirm, delete_data=delete_data), session=s, user=ADMIN))


def _rows(mem_engine):
    with Session(mem_engine) as s:
        return (s.exec(select(GameServer)).all(), s.exec(select(ServerIncident)).all(),
                s.exec(select(AuditLog)).all())


def test_keep_data_removes_container_and_rows_but_no_file(mem_engine, world):
    dk, data = world
    out = _remove(mem_engine)
    assert out["container_removed"] is True and out["data_deleted"] is False
    assert dk.removed == ["mc-1"]
    assert (data / "data" / "world" / "level.dat").is_file()
    servers, incidents, audit = _rows(mem_engine)
    assert servers == [] and incidents == []
    assert sorted(a.action for a in audit) == ["servers.create", "servers.remove"]
    assert [a.payload for a in audit if a.action == "servers.remove"] == ["data kept"]


def test_delete_data_removes_exactly_this_folder(mem_engine, world, tmp_path):
    _, data = world
    (tmp_path / "other-file").write_text("stays", encoding="utf-8")
    out = _remove(mem_engine, delete_data=True)
    assert out["data_deleted"] is True
    assert not os.path.exists(data)
    assert (tmp_path / "other-file").read_text(encoding="utf-8") == "stays"


def test_the_key_must_be_typed(mem_engine, world):
    dk, _ = world
    with pytest.raises(HTTPException) as ei:
        _remove(mem_engine, confirm="mc")
    assert ei.value.detail["code"] == "server.remove_confirm"
    assert dk.removed == [] and _rows(mem_engine)[0]


def test_an_enabled_server_is_refused(mem_engine, world):
    dk, _ = world
    with Session(mem_engine) as s:
        srv = s.exec(select(GameServer)).one()
        srv.enabled = True
        s.add(srv)
        s.commit()
    with pytest.raises(HTTPException) as ei:
        _remove(mem_engine)
    assert ei.value.detail["code"] == "server.remove_enabled" and dk.removed == []


def test_a_folder_another_server_uses_is_never_deleted(mem_engine, world):
    dk, data = world
    with Session(mem_engine) as s:
        s.add(GameServer(key="mc-2", game_type=GameType.minecraft, data_dir=str(data / "data")))
        s.commit()
    with pytest.raises(HTTPException) as ei:
        _remove(mem_engine, delete_data=True)
    assert ei.value.detail["code"] == "server.remove_data_shared"
    assert (data / "data" / "world" / "level.dat").is_file() and dk.removed == []


def test_a_data_root_is_never_deleted(mem_engine, world, tmp_path):
    dk, _ = world
    with Session(mem_engine) as s:
        srv = s.exec(select(GameServer)).one()
        srv.data_dir = str(tmp_path)
        s.add(srv)
        s.commit()
    with pytest.raises(HTTPException) as ei:
        _remove(mem_engine, delete_data=True)
    assert ei.value.detail["code"] == "server.remove_data_is_root" and dk.removed == []


def _point_data_dir_to(mem_engine, path):
    with Session(mem_engine) as s:
        srv = s.exec(select(GameServer).where(GameServer.key == "mc-1")).one()
        srv.data_dir = str(path)
        s.add(srv)
        s.commit()


def test_a_symlink_out_of_the_data_root_is_never_followed(mem_engine, world, tmp_path):
    """As text `<root>/mc-1-link` is a fine folder of this server; on disk it is somewhere else."""
    dk, _ = world
    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    outside.mkdir()
    (outside / "precious").write_text("stays", encoding="utf-8")
    (tmp_path / "mc-1-link").symlink_to(outside, target_is_directory=True)
    _point_data_dir_to(mem_engine, tmp_path / "mc-1-link")
    with pytest.raises(HTTPException) as ei:
        _remove(mem_engine, delete_data=True)
    assert ei.value.detail["code"] == "server.remove_data_link"
    assert (outside / "precious").read_text(encoding="utf-8") == "stays" and dk.removed == []


def test_a_path_through_a_symlink_into_another_servers_folder_is_refused(mem_engine, world, tmp_path):
    """`<root>/via/world` is not a link itself, but its parent leads into mc-2's folder."""
    dk, _ = world
    theirs = tmp_path / "mc-2"
    (theirs / "world").mkdir(parents=True)
    (theirs / "world" / "level.dat").write_text("x", encoding="utf-8")
    with Session(mem_engine) as s:
        s.add(GameServer(key="mc-2", game_type=GameType.minecraft, data_dir=str(theirs)))
        s.commit()
    (tmp_path / "via").symlink_to(theirs, target_is_directory=True)
    _point_data_dir_to(mem_engine, tmp_path / "via" / "world")
    with pytest.raises(HTTPException) as ei:
        _remove(mem_engine, delete_data=True)
    assert ei.value.detail["code"] == "server.remove_data_shared"
    assert (theirs / "world" / "level.dat").is_file() and dk.removed == []


def test_if_the_container_cannot_be_removed_nothing_else_changes(mem_engine, world):
    dk, data = world
    dk.fail = True
    with pytest.raises(HTTPException) as ei:
        _remove(mem_engine, delete_data=True)
    assert ei.value.detail["code"] == "server.remove_container_failed"
    assert data.is_dir() and len(_rows(mem_engine)[0]) == 1


def test_files_that_cannot_be_deleted_keep_the_row(mem_engine, world, monkeypatch):
    from app.services import provisioning

    dk, data = world
    monkeypatch.setattr(provisioning, "_delete_tree", lambda path: [f"{path}/data/world/region.mca"])
    with pytest.raises(HTTPException) as ei:
        _remove(mem_engine, delete_data=True)
    assert ei.value.detail["code"] == "server.remove_data_failed"
    assert len(dk.helpers) == 1, "the cleanup helper was tried before giving up"
    assert len(_rows(mem_engine)[0]) == 1, "the row must stay so the admin can retry"


def _root_owned(data):
    """Files the API cannot delete: what a game running as root leaves behind (Arma Reforger's
    `profile/profile/.db`). A folder without write permission plays that role here."""
    locked = data / "profile" / "profile" / ".db"
    locked.mkdir(parents=True)
    (locked / "save").write_text("x", encoding="utf-8")
    locked.chmod(0o555)
    return locked


def test_root_owned_files_are_deleted_by_the_cleanup_helper(mem_engine, world, tmp_path):
    """Measured on the real stack: "8 entries … could not be deleted" (HTTP 500), the server
    stayed listed. The helper deletes them as root; the row and the folder are gone."""
    dk, data = world
    locked = _root_owned(data)
    try:
        out = _remove(mem_engine, delete_data=True)
    finally:
        if locked.exists():
            locked.chmod(0o755)
    assert out["data_deleted"] is True
    assert not os.path.exists(data)
    assert _rows(mem_engine)[0] == []


def test_the_cleanup_helper_sees_only_this_servers_folder(mem_engine, world, tmp_path):
    """Root with a mount: the server's own (real) folder is the only one, no network, no
    capabilities beyond deleting, and it goes with its anonymous volumes."""
    dk, data = world
    _root_owned(data)
    _remove(mem_engine, delete_data=True)
    (helper,) = dk.helpers
    assert helper["HostConfig"]["Binds"] == [f"{os.path.realpath(data)}:/x"]
    assert helper["User"] == "0:0"
    assert helper["HostConfig"]["NetworkMode"] == "none"
    assert helper["HostConfig"]["CapDrop"] == ["ALL"]
    assert helper["HostConfig"]["CapAdd"] == ["DAC_OVERRIDE", "FOWNER"]
    assert helper["Entrypoint"] + helper["Cmd"] == ["find", "/x", "-mindepth", "1", "-delete"]
    assert dk.removed_volumes["mc-1-cleanup"] is True


def test_without_root_owned_files_no_helper_runs(mem_engine, world):
    dk, data = world
    _remove(mem_engine, delete_data=True)
    assert dk.helpers == [] and not os.path.exists(data)


def test_the_containers_anonymous_volumes_go_with_it(mem_engine, world):
    """Arma Reforger's image declares `VOLUME /steamcmd`: every container left an anonymous
    volume of about 195 MB behind that removing the server did not remove."""
    dk, _ = world
    _remove(mem_engine)
    assert dk.removed == ["mc-1"] and dk.removed_volumes["mc-1"] is True
