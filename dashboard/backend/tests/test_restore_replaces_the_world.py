"""A restore REPLACES what the archive holds — it does not lay the archive over it.

`extract_backup` used to be a plain `tar.extractall`: files created since the archive
(`map/<x>/<y>.bin`, `chunkdata_*`, `zpop_*`, new `r.x.z.mca` with Minecraft) survived while the
archived files were reset — a **mixed world from two points in time**, reported as "restored". The
rollback of a failed version change went through the same call and kept the new version's
migration artefacts.

"Clear first, then extract" was rejected because an abort in the middle would leave an empty
world. The replacement therefore sets each archived folder aside by RENAME (instant, no copy),
extracts, and only then deletes the set-aside copy; if extracting fails, the partial result goes
and the set-aside folder comes back.
"""
from __future__ import annotations

import asyncio
import os
import tarfile
from types import SimpleNamespace

import pytest

from app.services import backup as bk
from app.services import restore as restore_svc


def _write(path, text="x"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def _leftovers(root):
    return [n for n in os.listdir(root) if ".restore-old" in n]


def test_files_created_after_the_archive_are_gone_after_the_restore(tmp_path):
    data = str(tmp_path)
    _write(os.path.join(data, "Saves", "map", "1_1.bin"), "old")
    _write(os.path.join(data, "Server", "servertest.ini"), "Mods=A")
    res = bk.create_backup("manual", data_dir=data, world_subdirs=("Saves", "Server"))

    # Since the archive: one file changed, one new, the config edited.
    _write(os.path.join(data, "Saves", "map", "1_1.bin"), "new")
    _write(os.path.join(data, "Saves", "map", "9_9.bin"), "created later")
    _write(os.path.join(data, "Server", "servertest.ini"), "Mods=B")
    _write(os.path.join(data, "unrelated", "keep.txt"), "not in the archive")

    bk.extract_backup(res["name"], data_dir=data)

    assert _read(os.path.join(data, "Saves", "map", "1_1.bin")) == "old"
    assert not os.path.exists(os.path.join(data, "Saves", "map", "9_9.bin")), \
        "a file from after the archive survived — a world from two points in time"
    assert _read(os.path.join(data, "Server", "servertest.ini")) == "Mods=A"
    assert _read(os.path.join(data, "unrelated", "keep.txt")) == "not in the archive", \
        "only what the archive holds is replaced"
    assert os.path.isdir(os.path.join(data, "backups")), "the backups folder is not a world folder"
    assert _leftovers(data) == []


def test_a_partial_restore_replaces_only_the_named_part(tmp_path):
    data = str(tmp_path)
    _write(os.path.join(data, "saves", "world.zip"), "old world")
    _write(os.path.join(data, "config", "server-settings.json"), "{}")
    res = bk.create_backup("manual", data_dir=data, world_subdirs=("saves", "config"),
                           config_subdirs=("config",))
    _write(os.path.join(data, "saves", "later.zip"), "played since")
    _write(os.path.join(data, "config", "added-later.json"), "{}")

    bk.extract_backup(res["name"], data_dir=data, subdirs=("config",))

    assert not os.path.exists(os.path.join(data, "config", "added-later.json"))
    assert os.path.exists(os.path.join(data, "saves", "later.zip")), "the world was not asked for"
    assert _leftovers(data) == []


def test_a_nested_world_folder_leaves_its_siblings_alone(tmp_path):
    """Vintage Story archives `vs/Saves`: the unit is that folder, not all of `vs/`."""
    data = str(tmp_path)
    _write(os.path.join(data, "vs", "Saves", "default.vcdbs"), "old")
    _write(os.path.join(data, "vs", "serverconfig.json"), "{}")
    res = bk.create_backup("manual", data_dir=data, world_subdirs=("vs/Saves",))
    _write(os.path.join(data, "vs", "Saves", "later.vcdbs"), "new")

    bk.extract_backup(res["name"], data_dir=data)

    assert not os.path.exists(os.path.join(data, "vs", "Saves", "later.vcdbs"))
    assert os.path.exists(os.path.join(data, "vs", "serverconfig.json"))
    assert _leftovers(os.path.join(data, "vs")) == []


def test_single_files_are_replaced_too(tmp_path):
    """Valheim archives three moderation lists as files next to the world folder."""
    data = str(tmp_path)
    _write(os.path.join(data, "worlds_local", "w.db"), "old")
    _write(os.path.join(data, "adminlist.txt"), "alice")
    res = bk.create_backup("manual", data_dir=data, world_subdirs=("worlds_local", "adminlist.txt"))
    _write(os.path.join(data, "adminlist.txt"), "alice\nmallory")

    bk.extract_backup(res["name"], data_dir=data)

    assert _read(os.path.join(data, "adminlist.txt")) == "alice"


def test_a_failed_extraction_puts_the_world_back_as_it_was(tmp_path, monkeypatch):
    """The reason the old code did not clear first: an abort must not leave an empty world."""
    data = str(tmp_path)
    _write(os.path.join(data, "Saves", "a.bin"), "old")
    res = bk.create_backup("manual", data_dir=data, world_subdirs=("Saves",))
    _write(os.path.join(data, "Saves", "a.bin"), "current")
    _write(os.path.join(data, "Saves", "b.bin"), "current too")

    def _disk_full(self, path, *a, **kw):
        os.makedirs(os.path.join(path, "Saves"), exist_ok=True)
        _write(os.path.join(path, "Saves", "half.bin"), "partial")
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(tarfile.TarFile, "extractall", _disk_full)

    with pytest.raises(OSError, match="No space"):
        bk.extract_backup(res["name"], data_dir=data)

    assert _read(os.path.join(data, "Saves", "a.bin")) == "current"
    assert _read(os.path.join(data, "Saves", "b.bin")) == "current too"
    assert not os.path.exists(os.path.join(data, "Saves", "half.bin"))
    assert _leftovers(data) == []


# --------------------------------------------------------------------- what the caller says

class _Spec:
    data_dir = "/any"
    world_subdirs = ("Saves", "Server")
    pre_hook = None
    post_hook = None


class _Driver:
    def backup_spec(self, server):
        return _Spec()


class _GateOpen:
    async def guard_boot(self, server):
        return []

    def guard_message(self, blockers):
        return ""


def _srv():
    return SimpleNamespace(id=1, key="pz-1", container_name="pz-1",
                           console_kind="none", game_type=None, data_dir="/any")


def test_the_response_no_longer_claims_an_overlay(monkeypatch):
    monkeypatch.setattr(restore_svc.bk, "valid_backup_name", lambda n: True)
    monkeypatch.setattr(restore_svc.os.path, "exists", lambda p: True)
    monkeypatch.setattr(restore_svc.bk, "backup_path", lambda n, **k: "/any/x.tar.gz")
    monkeypatch.setattr(restore_svc.bk, "extract_backup", lambda *a, **k: None)
    monkeypatch.setattr(restore_svc.bk, "check_restorable", lambda *a, **k: None)
    monkeypatch.setattr(restore_svc, "mod_state", _GateOpen(), raising=False)

    async def _stop_and_snapshot(server, driver, label, **kw):
        return True, {"name": "pre-restore.tar.gz"}
    monkeypatch.setattr(restore_svc.lifecycle, "stop_and_snapshot", _stop_and_snapshot)

    class _Dk:
        async def action(self, name, act, **kw):
            pass
    monkeypatch.setattr(restore_svc, "docker", _Dk())

    async def _verify(server, driver, timeout=0):
        return True
    monkeypatch.setattr(restore_svc, "verify_boot", _verify)

    result = asyncio.run(restore_svc.restore(_srv(), _Driver(), "20260101-000000-weekly.tar.gz"))
    assert result["ok"] is True
    assert "overlay_note" not in result
