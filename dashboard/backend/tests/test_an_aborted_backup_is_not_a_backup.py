"""A half-packed archive must not pass as the "last backup".

`_pack` used to write straight to the final name, and cleaning up the torso lived only in the
`except BaseException`. On a SIGKILL that branch never runs, and SIGKILL happens in normal
operation: `scheduler.shutdown(wait=False)` waits for no running job, the `console-api` service
has no `stop_grace_period` (so Docker's default of 10 s), and a 4 GB Minecraft tar takes minutes.
A redeploy while the 04:00 weekly run fans out over the server list left half a `.tar.gz` under a
valid name.

The damage is not the lost backup but the wrong one: `list_backups` sorts descending,
`routers/server` sets `last_backup = backups[0]`, so the torso stood at the top as the **last
backup**, and `prune_backups` additionally protected it from deletion via `keep_min`. It is found
on the day the way back is needed.
"""
from __future__ import annotations

import os
import tarfile

import pytest

from app.services import backup


def _world(tmp_path):
    d = tmp_path / "data"
    (d / "world").mkdir(parents=True)
    (d / "world" / "map.bin").write_bytes(b"x" * 512)
    return str(d)


def test_an_aborted_run_leaves_no_valid_archive(tmp_path, monkeypatch):
    """**The case is SIGKILL, not an exception**, so the cleanup does NOT run.

    A test that raises an exception stays green on the broken code too: the
    `except BaseException: os.remove(path)` catches it. But Docker kills the process after 10 s,
    and then no `except` runs any more. So the cleanup is disabled here: what is left over must
    still be harmless.
    """
    base = _world(tmp_path)
    real = tarfile.open
    monkeypatch.setattr(backup.os, "remove", lambda p: None)   # the process is dead

    open_ = {}

    def _dies(name, mode="r", *a, **k):
        tar = real(name, mode, *a, **k)
        if "w" in mode:
            # Write something first, then abort hard, like a SIGKILL in the middle of packing.
            open_["tar"] = tar
            tar.add(os.path.join(base, "world"), arcname="world")
            raise KeyboardInterrupt("SIGKILL")
        return tar

    monkeypatch.setattr(backup.tarfile, "open", _dies)
    with pytest.raises(KeyboardInterrupt):
        backup.create_backup("weekly", data_dir=base, world_subdirs=("world",))

    # **Clean up after the simulated SIGKILL.** A real process death returns the file handle to
    # the operating system; here the process keeps running, and the open gzip object lingers
    # until the garbage collector finalises it at some point. `pytest.ini` turns an unraisable
    # exception into an error, which then lands on a COMPLETELY UNRELATED test that runs later
    # (only in the full run, never alone).
    # Deliberately `fileobj.close()` and NOT `tar.close()`: the latter would write the tar
    # trailer and turn the torso into a valid archive, exactly what this test rules out.
    torso = open_.get("tar")
    if torso is not None:
        # For "w:gz" `torso.fileobj` is the GzipFile and closes its source file along with it
        # (`myfileobj`). `_extfileobj` is a BOOL in `tarfile`, never a file object, so it is no
        # second path to close.
        try:
            torso.fileobj.close()
        except Exception:  # noqa: BLE001 — cleanup must not topple the test
            pass

    assert backup.list_backups(data_dir=base) == [], \
        "an aborted run must not show up in any list"


def test_the_torso_carries_no_valid_name_until_the_end(tmp_path, monkeypatch):
    """The core: WHILE packing, the `.tar.gz` does not exist yet at all."""
    base = _world(tmp_path)
    seen = {}
    real_add = tarfile.TarFile.add

    def _add(self, name, *a, **k):
        seen.setdefault("listing", backup.list_backups(data_dir=base))
        seen.setdefault("files", sorted(os.listdir(backup.backup_dir(base))))
        return real_add(self, name, *a, **k)

    monkeypatch.setattr(backup.tarfile.TarFile, "add", _add)
    backup.create_backup("weekly", data_dir=base, world_subdirs=("world",))

    assert seen["listing"] == [], "there must be no archive while packing"
    assert all(n.endswith(".part") for n in seen["files"]), seen["files"]
    # and afterwards there is one
    finished = backup.list_backups(data_dir=base)
    assert len(finished) == 1 and finished[0]["name"].endswith(".tar.gz")
    assert not any(n.endswith(".part") for n in os.listdir(backup.backup_dir(base)))


def test_the_prune_clears_leftover_work_files(tmp_path):
    base = _world(tmp_path)
    bdir = backup.backup_dir(base)
    old = os.path.join(bdir, "20260101-000000-weekly.tar.gz.part")
    with open(old, "wb") as f:
        f.write(b"half")
    os.utime(old, (0, 0))          # two days old is enough, the zero date all the more
    fresh = os.path.join(bdir, "20260825-000000-weekly.tar.gz.part")
    with open(fresh, "wb") as f:
        f.write(b"running right now")

    backup.prune_backups(keep_days=90, data_dir=base)
    remaining = os.listdir(bdir)
    assert "20260101-000000-weekly.tar.gz.part" not in remaining
    assert "20260825-000000-weekly.tar.gz.part" in remaining, \
        "a pack run that is running RIGHT NOW must not be cleared away from under itself"
