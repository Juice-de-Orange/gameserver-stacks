"""The world binding must see the same set of maps as the cleanup of the downloads.

Whether world cells are reset depends on two measurements, and each must see enough:

1. **The set.** `plan_world_reset` and the world binding in the preflight must not take their
   candidates from `Map=` alone. `_stash_downloads` moves **every** workshop directory that is no
   longer in the draft — including one that was never entered in `Map=`. The PZ server loads
   downloaded maps on its own, so their cells are in the world without the map list ever knowing.
   The download would go into quarantine while its world cells stay: a cell without a map
   provider, a crash on join without warning — reported as `status: ok`.

2. **The timing.** Measuring after the confirmed stop is not enough when the *decision*
   (`confirm_world_reset`) comes from the preflight **before** the stop. PZ's `save`+`quit` still
   writes chunks while shutting down. If the preflight found nothing before, `world_files` would
   stay empty — although cells are there right after.

Both cases end the same way: the map is gone, its terrain is not.
"""
import asyncio
import json
import os

import pytest
from fastapi import HTTPException

from app.models.server import GameServer, GameType, ManagementMode
from app.services.mods import apply as mod_apply
from app.services.mods import state as mod_state
from app.services.mods.preflight import BASE_MAP, ModPlan


# --------------------------------------------------------------------- Setup (as in test_mod_apply)

def _server(root) -> GameServer:
    srv = GameServer(
        key="pz-test", game_type=GameType.pz, management_mode=ManagementMode.dashboard,
        container_name="pz-test-container", data_dir=str(root), enabled=True,
        extra=json.dumps({"servername": "servertest"}),
    )
    srv.id = 1
    return srv


def _write(path, text="x"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def _ini(root, *, mods="", items="", maps=BASE_MAP):
    _write(os.path.join(root, "data", "Server", "servertest.ini"),
           f"Mods={mods}\nWorkshopItems={items}\nMap={maps}\nPVP=true\n")


def _item(root, wid, *, mod_ids=(), maps=None):
    base = os.path.join(root, "workshop-mods", "content", "108600", str(wid), "42")
    _write(os.path.join(base, "mod.info"), "\n".join(f"id={m}" for m in mod_ids) + "\n")
    for name, cell_list in (maps or {}).items():
        for x, y in cell_list:
            _write(os.path.join(base, "media", "maps", name, f"{x}_{y}.lotheader"))


def _world(root, *rel) -> str:
    return os.path.join(root, "data", "Saves", "Multiplayer", "servertest", *rel)


class FakeDriver:
    def __init__(self, root):
        self.root = root

    async def announce(self, server, text):
        pass

    async def save(self, server):
        pass

    def backup_spec(self, server):
        from app.services.drivers.base import BackupSpec
        return BackupSpec(data_dir=os.path.join(self.root, "data"), world_subdirs=("Saves", "Server"))


async def _boot_ok(server, driver, timeout=900.0):
    return True


@pytest.fixture()
def env(tmp_path, monkeypatch, mem_engine, wire_lifecycle, fake_docker_cls):
    root = str(tmp_path)
    srv = _server(root)
    _ini(root)

    monkeypatch.setattr(mod_state, "engine", mem_engine)
    monkeypatch.setattr(mod_apply, "engine", mem_engine)
    monkeypatch.setattr("app.services.servers.get_by_key", lambda k: srv)

    docker = fake_docker_cls()
    monkeypatch.setattr(mod_apply, "docker", docker)
    wire_lifecycle(docker, FakeDriver(root))
    monkeypatch.setattr(mod_apply, "get_driver", lambda s: FakeDriver(root))
    monkeypatch.setattr(mod_apply.watchdog, "suppress", lambda k, m: None)
    monkeypatch.setattr(mod_apply, "create_backup",
                        lambda label, **kw: {"name": f"{label}.tar.gz", "size": 1})
    monkeypatch.setattr(mod_apply, "verify_boot", _boot_ok)
    mod_state.invalidate(srv.key)
    return {"root": root, "server": srv, "docker": docker, "monkeypatch": monkeypatch}


# --------------------------------------------------------------------- The set

def test_a_map_without_a_map_entry_takes_its_world_cells_along(env):
    """Never listed in `Map=`, loaded anyway — and terrain in the world anyway.

    This state is the normal case after a `clean-orphan-downloads`, and it is the reason
    `_stash_downloads` exists at all: the server loads maps from the download directory, whatever
    the map list says.
    """
    root, srv = env["root"], env["server"]
    # Downloaded and active, provides map "Ghosttown" (cell 5_6) — but `Map=` does not know it.
    # The world has already written terrain for this cell (chunk 160/192).
    _item(root, "222", mod_ids=["GhostMod"], maps={"Ghosttown": [(5, 6)]})
    _ini(root, mods="GhostMod", items="222", maps=BASE_MAP)
    chunk = _world(root, "map", "160", "192.bin")
    _write(chunk)

    mod_state.set_draft(srv, ModPlan((), (), (BASE_MAP,)))   # item deselected → download goes away

    # Without confirmation the preflight must report the world reference …
    with pytest.raises(HTTPException) as ei:
        asyncio.run(mod_apply.apply_draft("pz-test"))
    assert ei.value.status_code == 409
    assert "WORLD_MAP_REMOVED" in {b["code"] for b in ei.value.detail["blockers"]}

    # … and with confirmation the cells go into quarantine along with the download.
    result = asyncio.run(mod_apply.apply_draft("pz-test", confirm_world_reset=True))
    assert result["downloads_removed"] == ["222"]
    assert not os.path.exists(chunk), "The download is gone, its terrain is still in the world"
    assert os.path.isfile(os.path.join(mod_apply.quarantine_root(srv), result["quarantine"],
                                       "world", "map", "160", "192.bin"))


# --------------------------------------------------------------------- The timing

def test_cells_that_appear_only_during_shutdown_are_not_skipped(env):
    """PZ still writes chunks on `quit` — the decision must not be made before that.

    Before the stop `generated_cells` finds nothing, so the preflight reports no world reference
    and the caller confirms none. While shutting down the game persists the loaded cells. After
    that: either reset (confirmed) or do not touch at all — but never remove the map and leave the
    terrain.
    """
    root, srv = env["root"], env["server"]
    _item(root, "111", mod_ids=["TownMod"], maps={"Newtown": [(3, 4)]})
    _ini(root, mods="TownMod", items="111", maps=f"Newtown;{BASE_MAP}")
    chunk = _world(root, "map", "96", "128.bin")          # cell (3,4) → chunk (96,128)
    assert not os.path.exists(chunk)                     # no terrain before the stop

    docker = env["docker"]
    real_action = docker.action

    async def action_with_final_save(name, act, **kw):
        await real_action(name, act, **kw)
        if act == "stop":
            _write(chunk)                                # `save`+`quit` persists the cell

    docker.action = action_with_final_save
    mod_state.set_draft(srv, ModPlan((), (), (BASE_MAP,)))

    with pytest.raises(HTTPException) as ei:
        asyncio.run(mod_apply.apply_draft("pz-test"))    # without confirmation

    assert ei.value.status_code == 409
    assert ei.value.detail["code"] == "mods.world_reset_unconfirmed"
    # Nothing changed, and the server is back up.
    assert os.path.exists(chunk), "terrain was touched without confirmation"
    assert mod_state.live_plan(srv).maps == ("Newtown", BASE_MAP)
    assert docker.calls == ["stop", "start"]
    assert os.path.isdir(os.path.join(root, "workshop-mods", "content", "108600", "111"))
