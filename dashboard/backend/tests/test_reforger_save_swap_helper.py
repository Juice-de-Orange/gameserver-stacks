"""The Reforger save-swap helper runs as root with only the capabilities the swap needs, and
goes with its anonymous volumes.

Before: Docker's default capabilities, no `no-new-privileges`, and `remove` without `v=1`, so the
game image's `VOLUME`s left anonymous volumes behind on every mode switch. The swap script itself
was run under exactly these capabilities against a tree with API-owned directories and a
root-owned save (A13 lead check): SWAP_DONE, owners kept.
"""
from __future__ import annotations

import asyncio

from app.models.server import GameServer, GameType, ManagementMode
from app.routers import server_ops


def test_the_swap_helper_is_confined_and_removed_with_its_volumes(monkeypatch):
    created: dict = {}
    removed: list[bool] = []

    class _Dk:
        async def remove(self, name, force=False, volumes=False):
            removed.append(volumes)

        async def create(self, name, payload):
            created.update(payload)

        async def action(self, name, act, **kw):
            pass

        async def state(self, name):
            return {"running": False}

        async def logs(self, name, tail=20):
            return "SWAP_DONE"

    class _Drv:
        def save_swap_script(self):
            return "echo SWAP_DONE"

    srv = GameServer(key="rf-1", game_type=GameType.arma_reforger, management_mode=ManagementMode.dashboard,
                     container_name="rf-1", data_dir="/tmp/rf-1", image_repo="example/reforger", image_tag="1")
    monkeypatch.setattr(server_ops, "docker", _Dk())

    asyncio.run(server_ops._run_save_swap(srv, _Drv(), "a", "b"))

    host = created["HostConfig"]
    assert host["NetworkMode"] == "none"
    assert host["CapDrop"] == ["ALL"]
    assert sorted(host["CapAdd"]) == ["CHOWN", "DAC_OVERRIDE", "FOWNER"]
    assert host["SecurityOpt"] == ["no-new-privileges"]
    assert removed and all(removed)
