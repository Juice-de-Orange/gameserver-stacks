"""The archive is opened and inspected — the spec alone proves nothing.

**Why this test hangs on the archive and not on `backup_spec`.** If `backup._pack` only accepts
`os.path.isdir(full)`, entries that are not folders are **silently skipped**. Adding
`adminlist.txt` to the spec alone would *look* fixed and *be* unfixed — no error, no warning,
the same gap.

What is at stake: Valheim has no console. Banning, permitting and admin rights work exclusively
through these three files. A restore without them brings the world back and then lets
**everyone in** — a missing `permittedlist.txt` is not an empty whitelist but none at all.

**Cross-check:** with `os.path.isdir` instead of `isdir or isfile` in `_pack` the three list
cases fail; with the old spec (`world_subdirs=("worlds_local",)`) as well.
"""
from __future__ import annotations

import os
import tarfile

import pytest

from app.models.server import GameServer, GameType
from app.services import backup as bk
from app.services.drivers.valheim import ValheimDriver


def _valheim_folder(tmp_path) -> GameServer:
    """A data directory as the image lays it out."""
    cfg = tmp_path / "config"
    (cfg / "worlds_local").mkdir(parents=True)
    (cfg / "worlds_local" / "Dedicated.db").write_bytes(b"world")
    (cfg / "worlds_local" / "Dedicated.fwl").write_bytes(b"meta")
    (cfg / "worlds_local" / "Dedicated.db.old").write_bytes(b"world-old")
    (cfg / "bepinex" / "plugins").mkdir(parents=True)
    (cfg / "bepinex" / "plugins" / "Example.dll").write_bytes(b"mod")
    (cfg / "adminlist.txt").write_text("76561198000000001\n", encoding="utf-8")
    (cfg / "bannedlist.txt").write_text("76561198000000002\n", encoding="utf-8")
    (cfg / "permittedlist.txt").write_text("76561198000000003\n", encoding="utf-8")
    (cfg / "prefs").write_text("whatever", encoding="utf-8")
    # The folder in which the image puts ITS backups — and in which ours land.
    (cfg / "backups").mkdir()
    (cfg / "backups" / "something.tar.gz").write_bytes(b"old")

    s = GameServer(key="vh", game_type=GameType.valheim, container_name="valheim",
                   data_dir=str(tmp_path), display_name="Valheim")
    s.id = 1
    return s


def _pack_raw(server) -> tuple[str, list[str]]:
    spec = ValheimDriver().backup_spec(server)
    # For Valheim the lead-up ALWAYS raises (`QuiesceWarning`, structural) — that is not an error
    # but the announcement "there is no save command here". `create_backup` catches it.
    result = bk.create_backup(
        "test", data_dir=spec.data_dir, world_subdirs=spec.world_subdirs,
        config_subdirs=spec.config_subdirs, pre_hook=spec.pre_hook, server=server,
    )
    path = os.path.join(bk.backup_dir(spec.data_dir), result["name"])
    with tarfile.open(path, "r:gz") as tar:
        return result["name"], tar.getnames()


def _pack(server) -> list[str]:
    return _pack_raw(server)[1]


def test_the_world_is_included(tmp_path):
    names = _pack(_valheim_folder(tmp_path))
    assert "worlds_local/Dedicated.db" in names
    assert "worlds_local/Dedicated.db.old" in names, "the .old file is the consistent save"


@pytest.mark.parametrize("file", ["adminlist.txt", "bannedlist.txt", "permittedlist.txt"])
def test_the_moderation_lists_are_included(tmp_path, file):
    assert file in _pack(_valheim_folder(tmp_path))


def test_the_mods_are_included(tmp_path):
    assert "bepinex/plugins/Example.dll" in _pack(_valheim_folder(tmp_path))


def test_the_backup_folder_is_NOT_included(tmp_path):
    """Otherwise each archive nests into the next and the size doubles per run."""
    names = _pack(_valheim_folder(tmp_path))
    assert not [n for n in names if n.startswith("backups")]


def test_without_a_world_no_archive_is_created(tmp_path):
    """The sharpened guard: the three text files alone are no rollback.

    A plain `if not packed` is not enough. As long as `world_subdirs` contained only world
    folders it was the same thing — since Valheim passes its lists along, it is not: 300 bytes
    that look like a backup.
    """
    server = _valheim_folder(tmp_path)
    import shutil

    shutil.rmtree(tmp_path / "config" / "worlds_local")
    with pytest.raises(RuntimeError, match="world content"):
        _pack(server)


def test_the_config_part_can_be_restored_on_its_own(tmp_path):
    """`config_subdirs` must be findable in the archive — otherwise the button only answers 400."""
    server = _valheim_folder(tmp_path)
    spec = ValheimDriver().backup_spec(server)
    # Take the name from the result, not from the directory: the backup folder also holds the
    # dummy of the image's backup, and that is deliberately not a valid tar.gz.
    name, _ = _pack_raw(server)
    missing = bk.missing_parts(name, spec.config_subdirs, data_dir=spec.data_dir)
    assert missing == [], f"not restorable on its own: {missing}"
