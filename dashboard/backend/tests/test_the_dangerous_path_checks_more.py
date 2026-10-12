"""The full restore checks the archive before it touches the world — not afterwards.

The only pre-check of the archive in `_restore_locked` (`missing_parts`) sits behind `if subdirs:`,
so on its own it only covers the PARTIAL restore ("configuration only"). The full restore, which
overwrites the whole world, would check nothing but `os.path.exists`.

**That inverts the direction of protection:** the harmless path is guarded, the dangerous one is
not. A truncated or empty archive would only show up in the middle of extracting — by then the
server is stopped, the safety archive is written, and the error branch reports "the server stays
stopped, the data may be incomplete".

Also: `missing_parts` must catch `tarfile.TarError` — otherwise a truncated archive raises raw,
in the partial restore too.
"""
import gzip
import os
import tarfile

import pytest
from fastapi import HTTPException

from app.services import backup as bk


def _world(tmp_path):
    d = tmp_path / "Saves"
    d.mkdir(parents=True, exist_ok=True)
    (d / "world.bin").write_text("world data", encoding="utf-8")
    return str(tmp_path)


def _archive(base: str, name: str, *, content=("Saves",)) -> str:
    os.makedirs(bk.backup_dir(base), exist_ok=True)
    path = os.path.join(bk.backup_dir(base), name)
    with tarfile.open(path, "w:gz") as tar:
        for sub in content:
            full = os.path.join(base, sub)
            if os.path.isdir(full):
                tar.add(full, arcname=sub)
    return path


def test_a_truncated_archive_is_caught_before_the_stop(tmp_path):
    base = _world(tmp_path)
    path = _archive(base, "20260818-120000-manual.tar.gz")
    # Cut off the last bytes — valid name, broken content.
    with open(path, "rb") as f:
        raw = f.read()
    with open(path, "wb") as f:
        f.write(raw[: len(raw) // 2])

    with pytest.raises(HTTPException) as ei:
        bk.check_restorable("20260818-120000-manual.tar.gz", data_dir=base,
                              world_subdirs=("Saves",))
    assert ei.value.status_code == 400
    assert ei.value.detail["code"] == "backup.archive_unreadable"


def test_an_empty_archive_is_rejected(tmp_path):
    """A valid, empty tar.gz extracts without an exception — and changes nothing."""
    base = _world(tmp_path)
    os.makedirs(bk.backup_dir(base), exist_ok=True)
    path = os.path.join(bk.backup_dir(base), "20260818-130000-manual.tar.gz")
    with tarfile.open(path, "w:gz"):
        pass

    with pytest.raises(HTTPException):
        bk.check_restorable("20260818-130000-manual.tar.gz", data_dir=base,
                              world_subdirs=("Saves", "Server"))


def test_an_intact_archive_passes(tmp_path):
    """Cross-check — the normal case must not start failing."""
    base = _world(tmp_path)
    _archive(base, "20260818-140000-manual.tar.gz")
    bk.check_restorable("20260818-140000-manual.tar.gz", data_dir=base,
                          world_subdirs=("Saves", "Server"))


def test_one_missing_world_folder_alone_is_not_enough_to_reject(tmp_path):
    """Reforger's `save-slots` is legitimately missing as long as any world folder is inside.

    Same reasoning as in `create_backup`: missing folders are skipped deliberately, only ALL of
    them missing is not allowed.
    """
    base = _world(tmp_path)
    _archive(base, "20260818-150000-manual.tar.gz", content=("Saves",))
    bk.check_restorable("20260818-150000-manual.tar.gz", data_dir=base,
                          world_subdirs=("Saves", "save-slots"))


def test_the_partial_restore_still_checks_exactly_and_survives_a_broken_archivee(tmp_path):
    """`subdirs` requires ALL named parts — and a broken archive does not raise raw errors."""
    base = _world(tmp_path)
    _archive(base, "20260818-160000-manual.tar.gz", content=("Saves",))

    with pytest.raises(HTTPException):
        bk.check_restorable("20260818-160000-manual.tar.gz", data_dir=base,
                              world_subdirs=("Saves",), subdirs=("Server",))

    # broken: `missing_parts` must not let a raw tarfile error escape here
    path = os.path.join(bk.backup_dir(base), "20260818-170000-manual.tar.gz")
    with open(path, "wb") as f:
        f.write(gzip.compress(b"not a tar"))
    with pytest.raises(HTTPException):
        bk.check_restorable("20260818-170000-manual.tar.gz", data_dir=base,
                              world_subdirs=("Saves",), subdirs=("Saves",))
