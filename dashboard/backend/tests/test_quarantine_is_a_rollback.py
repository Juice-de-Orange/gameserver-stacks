"""The quarantine is the ONLY rollback of a mod change, so it must actually work as one.

All four cases live in `mods/apply.py` and share one shape: the rollback exists on paper and
does not hold when it is needed.

* **The manifest must exist before the first move.** `list_quarantines` and `restore_quarantine`
  read only the manifest. If the operation dies between the first `shutil.move` and the
  manifest (e.g. a deploy while mods are being applied), the UI shows an empty quarantine folder
  while the files sit in it. The list is planned and known before the first file moves.
* **The saved `.ini` must come back too.** Restoring world and downloads without the config
  leaves `Mods=`/`Map=` listing the new state while the downloads are back:
  `ORPHAN_MAP_DOWNLOAD`, and the boot gate refuses every start, while the file that would fix it
  sits unused in the same folder.
* **`shutil.move` does not merge.** If the target already exists, the quarantine folder lands as
  `<wid>/<wid>`, without an error, reported as "restored".
* **Report what was done, not what was intended.** The answer must not come from the manifest.
"""
import asyncio
import json
import os
from pathlib import Path

import pytest
from fastapi import HTTPException

from app.models.server import GameServer, GameType, ManagementMode
from app.services import ini_editor as ini
from app.services.mods import apply as mod_apply
from app.services.mods import state as mod_state
from app.services.mods.preflight import BASE_MAP, ModPlan


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


def _remove_one_map(env):
    """Starting point: a map mod is active and the world has generated its cell."""
    root, srv = env["root"], env["server"]
    _item(root, "111", mod_ids=["TownMod"], maps={"Newtown": [(3, 4)]})
    _ini(root, mods="TownMod", items="111", maps=f"Newtown;{BASE_MAP}")
    _write(_world(root, "map", "96", "128.bin"))          # cell (3,4)
    mod_state.set_draft(srv, ModPlan((), (), (BASE_MAP,)))


# --------------------------------------------------------------------- The manifest

def test_the_manifest_exists_before_the_first_file_is_moved(env):
    """If the operation dies mid-move, the quarantine must still be readable."""
    _remove_one_map(env)
    srv = env["server"]

    manifest_present: list[bool] = []
    real = mod_apply._stash_world

    def _stash_world_checking_manifest(save_dir, rel_paths, qpath):
        manifest_present.append(os.path.isfile(os.path.join(qpath, "manifest.json")))
        return real(save_dir, rel_paths, qpath)

    env["monkeypatch"].setattr(mod_apply, "_stash_world", _stash_world_checking_manifest)

    result = asyncio.run(mod_apply.apply_draft("pz-test", confirm_world_reset=True))

    assert manifest_present == [True], "the manifest is only written after the move"
    # And in the end it lists what was really moved.
    manifest = json.loads(Path(os.path.join(mod_apply.quarantine_root(srv), result["quarantine"],
                                            "manifest.json")).read_text(encoding="utf-8"))
    assert manifest["world"] and manifest["downloads"] == ["111"]


# --------------------------------------------------------------------- The rollback

def test_restoring_brings_back_the_config_too(env):
    """World and downloads back but not the `.ini` is exactly the mixed state that cannot boot."""
    _remove_one_map(env)
    srv = env["server"]

    result = asyncio.run(mod_apply.apply_draft("pz-test", confirm_world_reset=True))
    assert mod_state.live_plan(srv).maps == (BASE_MAP,)          # .ini carries the new state

    env["docker"].running = False                                # server stopped
    restored = asyncio.run(mod_apply.restore_quarantine(srv, result["quarantine"]))

    mod_state.invalidate(srv.key)
    live = mod_state.live_plan(srv)
    assert live.maps == ("Newtown", BASE_MAP), "the old map list did not come back"
    assert live.workshop_items == ("111",)
    assert restored["restored_config"], "the response leaves out the config"
    assert os.path.isfile(ini.ini_path(srv))


def test_an_existing_download_is_not_moved_into_itself(env):
    """`shutil.move` does not merge: it puts the folder INTO the existing one."""
    _remove_one_map(env)
    root, srv = env["root"], env["server"]

    result = asyncio.run(mod_apply.apply_draft("pz-test", confirm_world_reset=True))
    # The download is back by now (downloaded again, or it never fully left).
    target = os.path.join(root, "workshop-mods", "content", "108600", "111")
    _write(os.path.join(target, "42", "mod.info"), "id=TownMod\n")

    env["docker"].running = False
    restored = asyncio.run(mod_apply.restore_quarantine(srv, result["quarantine"]))

    assert not os.path.isdir(os.path.join(target, "111")), "folder moved into itself"
    assert "111" not in restored["restored_downloads"], "reported as restored although it was not"
    assert restored["downloads_left"] == ["111"]


def test_the_response_reports_what_was_done_not_what_was_intended(env):
    """Three of 1200 files must not look like full success because the count came from the manifest."""
    _remove_one_map(env)
    srv = env["server"]

    result = asyncio.run(mod_apply.apply_draft("pz-test", confirm_world_reset=True))
    qpath = os.path.join(mod_apply.quarantine_root(srv), result["quarantine"])
    manifest = json.loads(Path(os.path.join(qpath, "manifest.json")).read_text(encoding="utf-8"))

    # One of the quarantined world files is gone (disk, permissions, manual change).
    missing = manifest["world"][0]
    os.remove(os.path.join(qpath, "world", missing))

    env["docker"].running = False
    restored = asyncio.run(mod_apply.restore_quarantine(srv, result["quarantine"]))

    assert restored["world_files_total"] == len(manifest["world"])
    assert restored["restored_world_files"] < restored["world_files_total"], \
        "the failure is not visible in the response"


def test_restoring_requires_a_stopped_server(env):
    """The existing guarantee must survive the rework."""
    _remove_one_map(env)
    srv = env["server"]
    result = asyncio.run(mod_apply.apply_draft("pz-test", confirm_world_reset=True))

    env["docker"].running = True
    with pytest.raises(HTTPException) as ei:
        asyncio.run(mod_apply.restore_quarantine(srv, result["quarantine"]))
    assert ei.value.status_code == 409
