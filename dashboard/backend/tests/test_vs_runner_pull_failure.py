"""A failed pull of the Vintage Story runner names the local build.

Measured: with the default GHCR runner the "Create container" step failed with the raw
`pull … → 500: error from registry: denied` — nothing pointed at the Dockerfile in this
repository that builds the image in a minute. The form hint says it; the error must say it too.
"""
import asyncio
import json

import pytest
from fastapi import HTTPException
from sqlmodel import Session

from app.config import settings
from app.models.server import ConsoleKind, GameServer, GameType, ManagementMode
from app.services import provisioning


class _Docker:
    def __init__(self):
        self.calls: list[tuple] = []

    async def state(self, name):
        return {"exists": False, "running": False, "status": "missing"}

    async def image_exists(self, ref):
        return False

    async def running_containers(self):
        return []

    async def pull(self, ref, on_progress=None):
        self.calls.append(("pull", ref))
        raise RuntimeError(f'pull {ref} → 500: {{"message":"error from registry: denied\\ndenied"}}')

    async def create(self, name, payload):  # pragma: no cover — must not be reached
        self.calls.append(("create", name))


def _server(mem_engine, tmp_path, game, **over):
    base = dict(key=f"{game.value[:6]}-1", game_type=game, container_name=f"c-{game.value}",
                data_dir=f"{tmp_path}/{game.value}", image_repo="ghcr.io/example/runner",
                image_tag="1", management_mode=ManagementMode.dashboard, rcon_port=27015,
                console_kind=ConsoleKind.source_rcon,
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
    monkeypatch.setattr("app.db.engine", mem_engine)
    monkeypatch.setattr(settings, "allowed_data_roots", [str(tmp_path)])
    dk = _Docker()
    monkeypatch.setattr("app.services.docker_client.docker", dk)
    return dk


def test_a_failed_runner_pull_names_the_local_build(mem_engine, tmp_path, wired):
    srv = _server(mem_engine, tmp_path, GameType.vintagestory)
    with pytest.raises(HTTPException) as e:
        asyncio.run(provisioning.provision(srv.key))

    detail = e.value.detail
    assert detail["code"] == "provision.vintagestory_runner_pull_failed"
    assert "docker build -t stacks-console/vintagestory-runner:1 dashboard/images/vintagestory" in detail["message"]
    assert "edit page" in detail["message"]
    # The registry's own answer stays visible: it says WHY (denied, timeout, no space…).
    assert "denied" in detail["params"]["reason"]
    assert detail["params"]["image"] == "ghcr.io/example/runner:1"
    assert [c[0] for c in wired.calls] == ["pull"], "nothing after a failed pull"


def test_other_games_keep_the_plain_pull_error(mem_engine, tmp_path, wired):
    srv = _server(mem_engine, tmp_path, GameType.minecraft)
    with pytest.raises(HTTPException) as e:
        asyncio.run(provisioning.provision(srv.key))
    assert e.value.detail["code"] == "provision.pull_failed"
