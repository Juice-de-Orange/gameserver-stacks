"""Restoring the quarantine IS writing to the world, with everything that comes with it.

**Three cases in `restore_quarantine`/`delete_quarantine`:**

1. **World lock.** `restore_quarantine` moves any number of world files back into `Saves/` via
   `_restore_world`. Checking only "is the container running?" opens exactly in the dangerous
   state: a STOPPED container is what `restore.py`, `version_manager.py` and `mods/apply.py`
   produce themselves while they work on the same world for minutes. A restore of the
   quarantine in that window drops world cells into a running `extract_backup`.
2. **The same hole in `delete_quarantine`:** an `shutil.rmtree` on exactly the directory that a
   running `mods/apply` needs as its **only rollback**.
3. **An unreadable manifest must not report success.** Swallowing the read error leaves `world`
   and `downloads` empty and answers `{"restored_world_files": 0, "restored_downloads": []}`, a
   green result that did nothing. The manifest IS the quarantine; without it nobody knows what
   belongs back.

**Why the existing guard covers this.** `test_world_lock_complete.py` checks that every name in
`world_lock.OPERATION_LABELS` actually takes the lock. A world writer that never registers is
invisible to that rule; both operations are named there, so the guard applies to them.
"""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.services import world_lock
from app.services.mods import apply as mod_apply


def _srv():
    return SimpleNamespace(id=1, key="pz-1", container_name="pz-1")


@pytest.fixture(autouse=True)
def _free_locks(monkeypatch):
    monkeypatch.setattr(world_lock, "_locks", {})
    monkeypatch.setattr(world_lock, "_holders", {})


def _quarantine(tmp_path, *, manifest: dict | None | str = None):
    q = tmp_path / "20260816-120000"
    (q / "world").mkdir(parents=True)
    if manifest is not None:
        content = manifest if isinstance(manifest, str) else json.dumps(manifest)
        (q / "manifest.json").write_text(content, encoding="utf-8")
    return q


def test_restoring_is_refused_while_a_world_operation_runs(monkeypatch, tmp_path):
    """A restore holds the lock, so the container is STOPPED."""
    q = _quarantine(tmp_path, manifest={"world": [], "downloads": []})
    monkeypatch.setattr(mod_apply, "_quarantine_path", lambda s, n: str(q))
    monkeypatch.setattr(mod_apply.docker, "state", _state(running=False))

    async def scenario():
        async with world_lock.hold("pz-1", "backup.restore"):
            with pytest.raises(HTTPException) as e:
                await mod_apply.restore_quarantine(_srv(), "20260816-120000")
            return e.value

    error = asyncio.run(scenario())
    assert error.status_code == 409
    assert "a restore" in str(error.detail), "the message does not name the blocking operation"


def test_deleting_is_refused_while_a_world_operation_runs(monkeypatch, tmp_path):
    """`rmtree` on the only rollback of a running mod apply."""
    q = _quarantine(tmp_path, manifest={"world": [], "downloads": []})
    monkeypatch.setattr(mod_apply, "_quarantine_path", lambda s, n: str(q))

    async def scenario():
        async with world_lock.hold("pz-1", "mods.apply"):
            with pytest.raises(HTTPException) as e:
                await mod_apply.delete_quarantine(_srv(), "20260816-120000")
            return e.value

    error = asyncio.run(scenario())
    assert error.status_code == 409
    assert q.is_dir(), "the quarantine was deleted despite a running operation"


def test_an_unreadable_manifest_fails_loudly(monkeypatch, tmp_path):
    """Not an empty result reported as green: the manifest IS the quarantine."""
    q = _quarantine(tmp_path, manifest="{broken")
    monkeypatch.setattr(mod_apply, "_quarantine_path", lambda s, n: str(q))
    monkeypatch.setattr(mod_apply.docker, "state", _state(running=False))

    with pytest.raises(HTTPException) as e:
        asyncio.run(mod_apply.restore_quarantine(_srv(), "20260816-120000"))
    assert e.value.status_code in (409, 422, 500)
    assert "anifest" in str(e.value.detail)


def test_a_missing_manifest_fails_too(monkeypatch, tmp_path):
    q = _quarantine(tmp_path, manifest=None)
    monkeypatch.setattr(mod_apply, "_quarantine_path", lambda s, n: str(q))
    monkeypatch.setattr(mod_apply.docker, "state", _state(running=False))

    with pytest.raises(HTTPException):
        asyncio.run(mod_apply.restore_quarantine(_srv(), "20260816-120000"))


def test_the_normal_case_still_goes_through(monkeypatch, tmp_path):
    """Cross-check: without a running operation and with a readable manifest it must work."""
    q = _quarantine(tmp_path, manifest={"world": ["1_1.lotheader"], "downloads": []})
    monkeypatch.setattr(mod_apply, "_quarantine_path", lambda s, n: str(q))
    monkeypatch.setattr(mod_apply.docker, "state", _state(running=False))
    monkeypatch.setattr(mod_apply, "_restore_world", lambda *a, **k: 1)
    monkeypatch.setattr(mod_apply.ini, "world_save_path", lambda s: str(tmp_path / "Saves"))
    monkeypatch.setattr(mod_apply.state, "invalidate", lambda k: None)

    result = asyncio.run(mod_apply.restore_quarantine(_srv(), "20260816-120000"))
    assert result["restored_world_files"] == 1


def _state(*, running: bool):
    async def state(name):
        return {"exists": True, "running": running}
    return state
