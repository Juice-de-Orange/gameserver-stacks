"""`restore-old-*` copies a game wrote as root are deleted by the cleanup helper, nothing else is.

Measured on the real stack: an Arma Reforger restore left `profile.restore-old-<ts>` behind,
because Reforger runs as root and the API cannot delete what it wrote. `extract_backup` returns
those copies; `provisioning.remove_leftovers_as_root` deletes them through a short-lived root
container whose only mount is the server's own folder.
"""
import asyncio
import os
import shutil

import pytest

from app.config import settings
from app.services import provisioning
from app.services.backup import _ASIDE_SUFFIX


class _Server:
    def __init__(self, data_dir):
        self.key = "rf-1"
        self.container_name = "rf-1"
        self.image_repo = "ghcr.io/acemod/arma-reforger"
        self.image_tag = "sha-d042142"
        self.data_dir = str(data_dir)


class _Docker:
    """Plays the helper's `rm -rf -- /x/<rel>…` as root would."""

    def __init__(self):
        self.helpers: list[dict] = []
        self.removed_volumes: dict[str, bool] = {}

    async def image_exists(self, image):
        return True

    async def create(self, name, payload):
        self.helpers.append(payload)

    async def action(self, name, act, **kw):
        payload = self.helpers[-1]
        host = payload["HostConfig"]["Binds"][0].split(":")[0]
        argv = payload["Entrypoint"] + payload["Cmd"]
        assert argv[:3] == ["rm", "-rf", "--"]
        for target in argv[3:]:
            path = os.path.join(host, os.path.relpath(target, "/x"))
            for dirpath, _, _ in os.walk(path):
                os.chmod(dirpath, 0o755)
            shutil.rmtree(path)

    async def state(self, name):
        return {"exists": True, "running": False}

    async def inspect(self, name):
        return {"State": {"ExitCode": 0}}

    async def remove(self, name, force=False, volumes=False):
        self.removed_volumes[name] = volumes


@pytest.fixture()
def world(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "allowed_data_roots", [str(tmp_path)])
    dk = _Docker()
    monkeypatch.setattr("app.services.docker_client.docker", dk)
    data = tmp_path / "rf-1"
    aside = data / f"profile{_ASIDE_SUFFIX}20261012-120000"
    (aside / "profile" / ".db").mkdir(parents=True)
    (aside / "profile" / ".db" / "save").write_text("old", encoding="utf-8")
    (aside / "profile").chmod(0o555)   # root's: the API cannot delete it
    (data / "profile").mkdir()
    yield dk, data, aside
    for p in (aside / "profile", aside):
        if p.exists():
            p.chmod(0o755)


def test_a_restore_old_copy_is_deleted_as_root(world):
    dk, data, aside = world
    left = asyncio.run(provisioning.remove_leftovers_as_root(_Server(data), [str(aside)]))
    assert left == [] and not aside.exists()
    (helper,) = dk.helpers
    assert helper["HostConfig"]["Binds"] == [f"{os.path.realpath(data)}:/x"]
    assert helper["Entrypoint"] + helper["Cmd"] == ["rm", "-rf", "--", f"/x/{aside.name}"]
    assert helper["HostConfig"]["NetworkMode"] == "none"
    # The server's own image: already on the host, nothing to download.
    assert helper["Image"] == "ghcr.io/acemod/arma-reforger:sha-d042142"
    assert (data / "profile").is_dir(), "the restored world stays"


@pytest.mark.parametrize("bad", ["profile", "../elsewhere.restore-old-1", "/etc"])
def test_anything_but_a_restore_old_copy_inside_the_folder_is_refused(world, bad):
    dk, data, _ = world
    path = bad if bad.startswith("/") else str(data / bad)
    left = asyncio.run(provisioning.remove_leftovers_as_root(_Server(data), [path]))
    assert left == [path] if os.path.lexists(path) else left == []
    assert dk.helpers == [], "no helper is started for a path it must not touch"
