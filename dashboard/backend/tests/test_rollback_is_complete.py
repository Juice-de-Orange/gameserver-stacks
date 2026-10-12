"""The mod rollback reports "running again" only when it really was complete.

1. **The downloads count.** `_restore_downloads` returns the items it could not restore and
   `_rollback` checks the result, like it does for configuration and world files. If an item
   stays in quarantine, the restored old `.ini` lists the item and its map while the files are
   missing — the state `preflight.check` treats as a BLOCKER (`ITEM_NOT_DOWNLOADED`,
   `MAP_WITHOUT_PROVIDER`), except the server runs (RCON answered) and joining players hit the
   map crash.

2. **What the failed run wrote NEW is not visible to the rollback by itself.** `_stash_world`
   records only what exists at the time of the stop; `_restore_world` moves exactly that list
   back. In between, the server runs for up to 900 s with the new configuration and writes fresh
   terrain for the cleared cells — chunks that had no file before. They are not in `moved`, so
   the completeness check (`restored != len(moved)`) cannot see them.

   For (2) the new files are **measured and reported, not cleaned up**: which of them belong to
   a restored cell and which to an unrelated one cannot be separated cleanly here, and
   `mods/apply` deliberately restores only the quarantined cells, not the whole world. Removing
   something that may be legitimate progress would be worse than an honest message: what cannot
   be decided cleanly is measured and the measurement recorded.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

from app.services.mods import apply as mod_apply


def _srv():
    return SimpleNamespace(id=1, key="pz-1", container_name="pz-1")


def _setup(monkeypatch, *, failed_downloads=(), new_files=()):
    async def _stop_and_confirm(server, **kw):
        return True, True
    monkeypatch.setattr(mod_apply.lifecycle, "stop_and_confirm", _stop_and_confirm)
    monkeypatch.setattr(mod_apply, "_restore_config", lambda s, c: True)
    monkeypatch.setattr(mod_apply, "_restore_world", lambda sd, qp, m: len(m))
    monkeypatch.setattr(mod_apply.watchdog, "suppress", lambda *a, **k: None)

    class _Dk:
        async def action(self, name, act, **kw):
            pass
    monkeypatch.setattr(mod_apply, "docker", _Dk())

    async def _verify(server, driver, timeout=0):
        return True
    monkeypatch.setattr(mod_apply, "verify_boot", _verify)

    def _restore_downloads(server, qpath, wids):
        return list(failed_downloads)
    monkeypatch.setattr(mod_apply, "_restore_downloads", _restore_downloads)
    monkeypatch.setattr(mod_apply, "_new_world_files", lambda sd, moved, before: list(new_files))


def test_a_download_left_behind_makes_the_rollback_incomplete(monkeypatch):
    _setup(monkeypatch, failed_downloads=["2822286426"])
    ok = asyncio.run(mod_apply._rollback(
        _srv(), "pz-1", {}, "/any/Saves", "/any/q", ["map/1/1.bin"], None,
        dropped_downloads=["2822286426"],
    ))
    assert ok is False, "a mod download left behind counted as a complete rollback"


def test_newly_written_world_files_make_the_rollback_incomplete(monkeypatch):
    _setup(monkeypatch, new_files=["map/46/1503.bin"])
    ok = asyncio.run(mod_apply._rollback(
        _srv(), "pz-1", {}, "/any/Saves", "/any/q", ["map/46/1502.bin"], None,
    ))
    assert ok is False, (
        "terrain newly written by the failed attempt went unnoticed — the rollback reported success"
    )


def test_a_clean_rollback_still_reports_success(monkeypatch):
    """Cross-check — otherwise every rollback would now be incomplete."""
    _setup(monkeypatch)
    ok = asyncio.run(mod_apply._rollback(
        _srv(), "pz-1", {}, "/any/Saves", "/any/q", ["map/1/1.bin"], None,
        dropped_downloads=["2822286426"],
    ))
    assert ok is True


def test_new_world_files_detects_only_REALLY_new_ones(tmp_path):
    """The trap: `map/<x>/` also holds chunks of unrelated cells that were always there.

    Comparing against `moved` instead of a real before-snapshot would count every file in the
    same folder as new, and EVERY rollback would be reported as incomplete. The before-snapshot
    is therefore taken when quarantining.
    """
    save = tmp_path / "Saves"
    (save / "map" / "46").mkdir(parents=True)
    # The cell that was quarantined:
    quarantined = "map/46/1502.bin"
    # A chunk of an UNRELATED cell in the same column — always was there:
    (save / "map" / "46" / "1400.bin").write_bytes(b"old")
    # And one the failed attempt wrote new:
    (save / "map" / "46" / "1503.bin").write_bytes(b"new")

    before = {"map/46/1400.bin", quarantined}
    new = mod_apply._new_world_files(str(save), [quarantined], before)
    assert new == ["map/46/1503.bin"], f"wrongly detected: {new}"
