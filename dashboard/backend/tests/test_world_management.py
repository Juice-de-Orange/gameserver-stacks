"""Several worlds lie on disk — and they can be reached from the dashboard.

Factorio keeps several `saves/*.zip` side by side, Valheim several `worlds_local/<name>.db`.
Without a world management they are physically present but unreachable, and switching worlds means
editing the container env by hand. (Reforger's `save_slots` answers a different question: "which
game mode has a save?")

**Four things that can go wrong and are pinned down here:**

1. **"Active" is not a property of the file for Factorio.** `LOAD_LATEST_SAVE=true` — the image's
   default — loads the NEWEST save, whatever `SAVE_NAME` says. Showing only `SAVE_NAME` claims
   something false.
2. **A Valheim world is TWO files.** `.db` is the world, `.fwl` its metadata; a `.db` without
   `.fwl` cannot be loaded. And the `.old` saves next to them are the backup of the same world,
   not a second save — as a row of their own they would invite loading "the old world" and
   overwriting the current one.
3. **The active save is not removed.** For Valheim the result would not be an error but a NEW,
   empty world under the same name on the next start — which looks like data loss.
4. **The switch takes effect only after a RECREATE**, not after a restart. Exactly this
   difference can hand out a join password the server never saw. The returned note must say so.
"""
from __future__ import annotations

import json
import os

import pytest

from app.models.server import GameServer, GameType
from app.services.drivers.factorio import FactorioDriver
from app.services.drivers.valheim import ValheimDriver
from tests.file_permissions import ONLY_WITH_PERMISSION_REVOCATION


# ------------------------------------------------------------------ Factorio

def _fac(tmp_path, **env) -> GameServer:
    os.makedirs(tmp_path / "data" / "saves", exist_ok=True)
    s = GameServer(key="fac", game_type=GameType.factorio, container_name="factorio",
                   data_dir=str(tmp_path), display_name="Factorio",
                   extra=json.dumps({"env": env}) if env else "{}")
    s.id = 1
    return s


def _save(server, name, *, age=0):
    import time

    path = os.path.join(server.data_dir, "data", "saves", f"{name}.zip")
    with open(path, "wb") as f:
        f.write(b"PK\x03\x04" * 4)
    if age:
        t = time.time() - age
        os.utime(path, (t, t))
    return path


def test_factorio_lists_its_saves(tmp_path):
    server = _fac(tmp_path)
    _save(server, "_autosave1", age=3600)
    _save(server, "Factory-North")
    names = sorted(e["name"] for e in FactorioDriver().world_saves(server))
    assert names == ["Factory-North", "_autosave1"]


def test_for_factorio_the_newest_wins_when_the_image_says_so(tmp_path):
    """`LOAD_LATEST_SAVE=true` is the default — `SAVE_NAME` then has no effect."""
    server = _fac(tmp_path, SAVE_NAME="Factory-North")
    _save(server, "Factory-North", age=7200)
    _save(server, "_autosave1")
    active = [e for e in FactorioDriver().world_saves(server) if e["active"]]
    assert [e["name"] for e in active] == ["_autosave1"], (
        "SAVE_NAME was reported as active although LOAD_LATEST_SAVE loads the newest"
    )
    assert "newest" in active[0]["reason"]


def test_the_selection_switches_off_the_automatism(tmp_path):
    """Otherwise the selection would have no effect — and the field would look as if it had."""
    server = _fac(tmp_path)
    _save(server, "Factory-North", age=7200)
    _save(server, "_autosave1")

    class _Drv(FactorioDriver):
        def __init__(self):
            self.written = {}

        def write_registry_env(self, srv, env):  # noqa: D102
            self.written = env
            srv.extra = json.dumps({"env": env})

    d = _Drv()
    note = d.select_world(server, "Factory-North")
    assert d.written == {"SAVE_NAME": "Factory-North", "LOAD_LATEST_SAVE": "false"}
    assert "recreated" in note


def test_an_unknown_save_is_rejected(tmp_path):
    with pytest.raises(ValueError) as error:
        FactorioDriver().select_world(_fac(tmp_path), "doesnotexist")
    assert error.value.code == "world.factorio_save_not_found"


def test_the_active_save_is_not_removed(tmp_path):
    server = _fac(tmp_path)
    _save(server, "_autosave1")
    with pytest.raises(ValueError) as error:
        FactorioDriver().remove_world(server, "_autosave1")
    assert error.value.code == "world.factorio_active_save_not_removable"


def test_a_removed_save_is_moved_not_deleted(tmp_path):
    server = _fac(tmp_path)
    _save(server, "_autosave1")
    path = _save(server, "Old", age=99999)

    result = FactorioDriver().remove_world(server, "Old")
    assert not os.path.exists(path)
    set_aside = os.listdir(os.path.join(str(tmp_path), "data", "saves.removed"))
    assert [n.endswith("-Old.zip") for n in set_aside] == [True]
    assert result["files"] == ["Old.zip"]


@ONLY_WITH_PERMISSION_REVOCATION
def test_an_unreadable_folder_is_not_empty(tmp_path):
    server = _fac(tmp_path)
    _save(server, "_autosave1")
    folder = os.path.join(str(tmp_path), "data", "saves")
    os.chmod(folder, 0o000)
    try:
        with pytest.raises(ValueError) as error:
            FactorioDriver().world_saves(server)
        assert error.value.code == "world.factorio_saves_unreadable"
    finally:
        os.chmod(folder, 0o755)


# ------------------------------------------------------------------ Valheim

def _vh(tmp_path, world="Dedicated") -> GameServer:
    os.makedirs(tmp_path / "config" / "worlds_local", exist_ok=True)
    s = GameServer(key="vh", game_type=GameType.valheim, container_name="valheim",
                   data_dir=str(tmp_path), display_name="Valheim",
                   extra=json.dumps({"env": {"WORLD_NAME": world}}))
    s.id = 1
    return s


def _world(server, name, *, with_db=True, with_fwl=True):
    folder = os.path.join(server.data_dir, "config", "worlds_local")
    # `with`, not `open(...).write(...)`: `pytest.ini` treats `ResourceWarning` as an ERROR — and
    # rightly so, an open file handle in a test hides exactly the kind of leak that gets expensive
    # in production.
    if with_db:
        with open(os.path.join(folder, f"{name}.db"), "wb") as f:
            f.write(b"world")
    if with_fwl:
        with open(os.path.join(folder, f"{name}.fwl"), "wb") as f:
            f.write(b"meta")


def test_valheim_lists_its_worlds(tmp_path):
    server = _vh(tmp_path)
    _world(server, "Dedicated")
    _world(server, "SecondWorld")
    saves = ValheimDriver().world_saves(server)
    assert sorted(e["name"] for e in saves) == ["Dedicated", "SecondWorld"]
    active = [e for e in saves if e["active"]]
    assert [e["name"] for e in active] == ["Dedicated"]


def test_a_world_is_two_files(tmp_path):
    server = _vh(tmp_path)
    _world(server, "Dedicated")
    entry = ValheimDriver().world_saves(server)[0]
    assert set(entry["files"]) == {"Dedicated.db", "Dedicated.fwl"}


def test_metadata_without_a_world_does_not_count(tmp_path):
    """A `.fwl` without `.db` is not a loadable world — offering it as a save would be a trap."""
    server = _vh(tmp_path)
    _world(server, "Dedicated")
    _world(server, "MetaOnly", with_db=False)
    assert [e["name"] for e in ValheimDriver().world_saves(server)] == ["Dedicated"]


def test_the_automatic_backups_are_not_worlds_of_their_own(tmp_path):
    """Otherwise someone loads "the old world" and overwrites the current one with it."""
    server = _vh(tmp_path)
    _world(server, "Dedicated")
    _world(server, "Dedicated_backup_auto-20260823201643")
    assert [e["name"] for e in ValheimDriver().world_saves(server)] == ["Dedicated"]


def test_the_switch_mentions_the_recreate(tmp_path):
    server = _vh(tmp_path)
    _world(server, "Dedicated")
    _world(server, "SecondWorld")

    class _Drv(ValheimDriver):
        def __init__(self):
            self.written = {}

        def write_registry_env(self, srv, env):  # noqa: D102
            self.written = env

    d = _Drv()
    note = d.select_world(server, "SecondWorld")
    assert d.written["WORLD_NAME"] == "SecondWorld"
    assert "recreated" in note


def test_the_active_world_is_not_removed(tmp_path):
    """Otherwise the server creates a NEW, empty world under that name on the next start."""
    server = _vh(tmp_path)
    _world(server, "Dedicated")
    with pytest.raises(ValueError) as error:
        ValheimDriver().remove_world(server, "Dedicated")
    assert error.value.code == "world.valheim_active_world_not_removable"


def test_both_files_are_set_aside_together(tmp_path):
    server = _vh(tmp_path)
    _world(server, "Dedicated")
    _world(server, "Old")

    result = ValheimDriver().remove_world(server, "Old")
    assert sorted(result["files"]) == ["Old.db", "Old.fwl"]
    folder = os.path.join(str(tmp_path), "config", "worlds_local")
    assert not [f for f in os.listdir(folder) if f.startswith("Old.")]
    assert len(os.listdir(folder + ".removed")) == 2


# ------------------------------------------------------------------ Seam

def test_games_without_world_selection_say_so_clearly():
    """The base default for selecting raises — it does not quietly accept a selection that looks
    like "it works"."""
    from app.services.drivers.minecraft import MinecraftDriver

    srv = GameServer(key="mc", game_type=GameType.minecraft, container_name="mc", data_dir="/x")
    srv.id = 1
    assert MinecraftDriver().world_saves(srv) == []
    with pytest.raises(NotImplementedError):
        MinecraftDriver().select_world(srv, "x")


def test_the_route_answers_409_with_the_coded_detail_for_the_active_save(tmp_path):
    """The route decides 409 vs 400 by the driver's code and passes the coded detail on."""
    from fastapi import HTTPException

    from app.routers import server_ops

    server = _fac(tmp_path)
    _save(server, "_autosave1")
    with pytest.raises(HTTPException) as error:
        server_ops.remove_world("_autosave1", server=server, driver=FactorioDriver(), session=None, user=None)
    assert error.value.status_code == 409
    assert error.value.detail["code"] == "world.factorio_active_save_not_removable"

    with pytest.raises(HTTPException) as error:
        server_ops.remove_world("no-such-save", server=server, driver=FactorioDriver(), session=None, user=None)
    assert error.value.status_code == 400
