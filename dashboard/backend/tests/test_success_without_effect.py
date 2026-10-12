"""Three places that reported "ok" without the work having been done.

All the same class — and the most expensive one, because the operator stops looking. The pattern
each time: a return value nobody reads, or an `except: pass` at a place where the error changes
the result.
"""
from __future__ import annotations

import asyncio
import os
import tarfile

import pytest

from app.services import backup as bk
from app.services import version_manager as vm
from app.services.mods import apply as mod_apply


# ------------------------------------------------------------------ 1. empty mandatory backup

def test_an_archive_without_a_single_world_folder_is_not_a_backup(tmp_path):
    """`create_backup` must not skip missing folders SILENTLY and report a 51-byte tar as success.

    The damage shows on the rollback: `_rollback_swap` calls `extract_backup` without `subdirs`, an
    empty archive extracts without an exception, `world_restored` becomes `True`, and the operation
    reports "rolled back" — while the world is still in the state of the new version. The same for
    a full restore: it slips past `missing_parts` and answers "restored" without writing a file.
    The check therefore sits at the choke point, not at one caller.
    """
    base = tmp_path / "data"
    base.mkdir()
    with pytest.raises(RuntimeError) as e:
        bk.create_backup("pre-x", data_dir=str(base), world_subdirs=("Saves", "Server"))
    assert "Saves" in str(e.value) and "Server" in str(e.value)
    leftover = list((base / "backups").glob("*.tar.gz"))
    assert not leftover, f"the empty archive was left behind: {leftover}"


def test_a_partially_present_world_folder_is_enough(tmp_path):
    """Cross-check: if only ONE of the folders is missing, that is not an error — otherwise no backup is possible.

    Reforger has `("profile", "save-slots")`, and `save-slots` only exists after the first save.
    An "all must be present" rule would have prevented every backup there.
    """
    base = tmp_path / "data"
    (base / "Saves").mkdir(parents=True)
    (base / "Saves" / "world.bin").write_bytes(b"x" * 128)
    result = bk.create_backup("pre-x", data_dir=str(base), world_subdirs=("Saves", "Server"))
    with tarfile.open(os.path.join(bk.backup_dir(str(base)), result["name"])) as tar:
        assert any(m.name.startswith("Saves") for m in tar.getmembers())


# ------------------------------------------------------------------ 2. rollback without restart

def test_a_failed_restart_does_not_count_as_rolled_back(monkeypatch):
    """A start wrapped in `try/except: pass` followed by an unconditional `return True, …`.

    `container_restored=True` then only attests that `rename`/`create` worked — not that the
    server runs. There is no boot proof here, and the watchdog is silenced for 15 minutes right
    afterwards, so autoheal does not bring it up either. The operation would end as "rolled_back"
    with the game server down and neither message nor log saying so.
    """
    class _Dk:
        async def remove(self, name, force=False): pass
        async def rename(self, old, new): pass
        async def create(self, name, payload): pass
        async def state(self, name): return {"exists": True, "running": False}
        async def action(self, name, act, **kw):
            raise RuntimeError("500 from the socket proxy on start")

    monkeypatch.setattr(vm, "docker", _Dk())
    monkeypatch.setattr(vm, "_restore_version_fields", lambda k, snap: None)
    monkeypatch.setattr(vm, "_load", lambda k: object())
    monkeypatch.setattr(vm.watchdog, "suppress", lambda *a, **k: None)

    async def _stop_and_confirm(srv, **kw):
        return True, True
    monkeypatch.setattr(vm.lifecycle, "stop_and_confirm", _stop_and_confirm)
    monkeypatch.setattr(vm, "extract_backup", lambda *a, **k: None)
    # The archive is only a name here, not a file — the authenticity check (`check_restorable`,
    # in front of BOTH restore paths) would otherwise trip over the missing file before the
    # actual case is reached.
    monkeypatch.setattr(vm, "check_restorable", lambda *a, **k: None)

    container_restored, _ = asyncio.run(vm._rollback_swap(
        "pz-1", "pz-1", "pz-1-prev", spec=_Spec(), backup_name="pre.tar.gz",
        was_running=True, driver=None, old_existed=True, renamed=True,
    ))
    assert container_restored is False, (
        "the rollback reports rolled back although the server did not come back up"
    )


class _Spec:
    data_dir = "/irrelevant"


# ------------------------------------------------------------------ 3. swallowed downloads

def test_a_download_that_cannot_be_moved_aborts_the_apply(tmp_path, monkeypatch):
    """World files are compared and raise on failure — the downloads need exactly the same.

    The preflight relies on it: `check(..., prune_orphans=True)` suppresses the ORPHAN_MAP_DOWNLOAD
    blocker on the grounds that this operation clears the downloads too. If a map download stays
    behind, the PZ server keeps loading it — join crash without warning — and the next live check
    turns it into a blocker that the boot gate holds against every start. The server can then no
    longer be started, after a change reported as "ok".
    """
    content = tmp_path / "content"
    (content / "111").mkdir(parents=True)
    (content / "222").mkdir(parents=True)
    monkeypatch.setattr(mod_apply.inventory, "content_dir", lambda s: str(content))

    def _move_fails(src, dst):
        raise OSError("EBUSY — the file is open")
    monkeypatch.setattr(mod_apply.shutil, "move", _move_fails)

    planned = mod_apply.planned_downloads(object(), set())
    assert planned == ["111", "222"]          # the target set is fixed before anything moves
    with pytest.raises(RuntimeError) as e:
        mod_apply._stash_downloads(object(), planned, str(tmp_path / "q"))
    assert "111" in str(e.value) or "222" in str(e.value)


def test_an_unreadable_download_directory_does_not_count_as_nothing_to_do(tmp_path, monkeypatch):
    """`os.listdir` fails → empty list → "nothing to move". That is a claim, not a measurement.

    The measurement sits in `planned_downloads` — it is needed BEFORE the first file moves, so
    the quarantine manifest can be written first.

    The distinction is deliberate: a directory that DOES NOT EXIST is a fresh server without
    downloads — there really is nothing to do. One that exists but cannot be read is a measurement
    error and must not pass as an empty set.
    """
    content = tmp_path / "content"
    content.mkdir()
    monkeypatch.setattr(mod_apply.inventory, "content_dir", lambda s: str(content))

    def _listdir_fails(p):
        raise OSError("Permission denied")
    monkeypatch.setattr(mod_apply.os, "listdir", _listdir_fails)

    with pytest.raises(RuntimeError):
        mod_apply.planned_downloads(object(), set())


def test_a_fresh_server_without_a_download_directory_is_not_an_error(tmp_path, monkeypatch):
    """Cross-check for the rule above — otherwise no apply would be possible on a new server."""
    monkeypatch.setattr(mod_apply.inventory, "content_dir", lambda s: str(tmp_path / "doesnotexist"))
    assert mod_apply.planned_downloads(object(), set()) == []
