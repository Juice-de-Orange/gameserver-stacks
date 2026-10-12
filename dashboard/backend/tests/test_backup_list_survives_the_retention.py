"""The backup list does not die because an archive is being cleared away right now.

`list_backups` calls `os.stat(full)` on every file name `os.listdir` returned. Between the two
calls there is a window, and in operation it is hit regularly: `prune_backups` deletes in the same
directory, and the weekly run calls it right after packing. If the file vanishes in between,
`os.stat` raises `FileNotFoundError` — and the whole list must not end in a 500 instead of
showing the remaining archives.

`prune_backups` does it right at the same place: its `stat` sits in a `try`.
"""
from __future__ import annotations

import os

from app.services import backup as bk


def test_a_vanished_archive_does_not_topple_the_whole_list(tmp_path, monkeypatch):
    base = tmp_path / "data"
    bdir = base / "backups"
    bdir.mkdir(parents=True)
    for n in ("20260816-100000-weekly.tar.gz", "20260816-110000-manual.tar.gz"):
        (bdir / n).write_bytes(b"x" * 64)

    real_stat = os.stat

    def _stat_with_race(path, *a, **kw):
        # Exactly the window: retention deleted the file between listdir and stat.
        if str(path).endswith("20260816-100000-weekly.tar.gz"):
            raise FileNotFoundError(2, "No such file or directory", str(path))
        return real_stat(path, *a, **kw)

    monkeypatch.setattr(bk.os, "stat", _stat_with_race)

    listing = bk.list_backups(data_dir=str(base))
    assert [e["name"] for e in listing] == ["20260816-110000-manual.tar.gz"], (
        "the list topples instead of skipping the vanished archive"
    )


def test_the_normal_case_lists_everything(tmp_path):
    """Cross-check — skipping must not turn into swallowing."""
    base = tmp_path / "data"
    bdir = base / "backups"
    bdir.mkdir(parents=True)
    for n in ("20260816-100000-weekly.tar.gz", "20260816-110000-manual.tar.gz"):
        (bdir / n).write_bytes(b"x" * 64)
    assert len(bk.list_backups(data_dir=str(base))) == 2
