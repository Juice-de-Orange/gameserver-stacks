"""The `.ini` belongs to nobody else during a world operation.

On rollback `mods/apply._restore_config` puts back the COMPLETE `.ini` from before the operation.
Applying mods takes up to 20 minutes (boot proof). A setting saved through the dashboard in that
window would be lost without a trace: the rollback lays the old file over it, and the audit records
a change that no longer exists.

The config write routes (`PUT config/ini`, `config/raw`, `config/sandbox`,
`config/sandbox-presets`) therefore ask the world lock like `mod_config`, `server_ops` and
`versions` do: the rule lives at the choke point `world_lock.refuse_during_world_operation` and is
bound in `routers/config.py` as a `Depends`. `tests/test_config_routes_ask_the_world_lock.py` pins
that no write route gets past it.
"""
import asyncio
import json

import pytest
from fastapi import HTTPException

from app.models.server import GameServer, GameType
from app.routers import config as cfg
from app.services import world_lock


def _srv(tmp_path, key="pz-test", servername="servertest") -> GameServer:
    s = GameServer(
        key=key, game_type=GameType.pz, container_name=key, data_dir=str(tmp_path),
        extra=json.dumps({"servername": servername}),
    )
    s.id = 7
    return s


def _create_files(tmp_path, servername="servertest"):
    d = tmp_path / "data" / "Server"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{servername}.ini").write_text("PVP=true\nMaxPlayers=16\n", encoding="utf-8")
    (d / f"{servername}_SandboxVars.lua").write_text(
        "SandboxVars = {\n    Zombies = 3,\n}\n", encoding="utf-8"
    )
    return d


@pytest.fixture(autouse=True)
def fresh_locks(monkeypatch):
    monkeypatch.setattr(world_lock, "_locks", {})
    monkeypatch.setattr(world_lock, "_holders", {})


def test_a_running_world_operation_locks_the_config_routes(tmp_path):
    """All four write routes must answer 409 while a world operation is running."""
    _create_files(tmp_path)
    srv = _srv(tmp_path)

    async def scenario():
        async with world_lock.hold("pz-test", "mods.apply"):
            with pytest.raises(HTTPException) as ei:
                cfg._no_world_operation(srv)
            assert ei.value.status_code == 409
            assert "a mod change" in str(ei.value.detail)

    asyncio.run(scenario())


def test_without_a_world_operation_the_config_route_gets_through(tmp_path):
    """Cross-check — the latch must not get in the way in the normal case."""
    _create_files(tmp_path)
    srv = _srv(tmp_path)
    assert cfg._no_world_operation(srv) is srv


def test_the_rule_lives_in_world_lock_and_lifecycle_delegates():
    """The choke point is `world_lock` — `lifecycle` calls it instead of rebuilding it."""
    assert hasattr(world_lock, "refuse_during_world_operation")

    async def scenario():
        async with world_lock.hold("pz-1", "version.change"):
            with pytest.raises(HTTPException) as ei:
                world_lock.refuse_during_world_operation("pz-1", "Start")
            assert ei.value.status_code == 409
            assert "a version change" in str(ei.value.detail)

    asyncio.run(scenario())


def test_the_rollback_writes_the_backed_up_version_back(tmp_path):
    """The rollback restores the backed-up version unconditionally — no fingerprint comparison.

    A check that tries to detect and set aside a foreign change cannot tell the container's own
    boot from a foreign change (the PZ image writes to the `.ini` on every boot) and would report
    clean rollbacks as incomplete. A rollback that lies about itself costs more than the gap, and
    the gap is closed at the root: all writing config routes ask the world lock.
    """
    import os

    from app.services import ini_editor as ini
    from app.services.mods import apply as mod_apply

    _create_files(tmp_path)
    srv = _srv(tmp_path)
    qpath = str(tmp_path / "q")
    os.makedirs(os.path.join(qpath, "config"), exist_ok=True)
    saved = mod_apply._stash_config(srv, qpath)

    ini.write_raw(ini.ini_path(srv), "PVP=false" + chr(10) + "MaxPlayers=99" + chr(10))

    assert mod_apply._restore_config(srv, saved) is True
    with open(ini.ini_path(srv), encoding="utf-8") as f:
        assert "MaxPlayers=16" in f.read(), "the backed-up version did not come back"
