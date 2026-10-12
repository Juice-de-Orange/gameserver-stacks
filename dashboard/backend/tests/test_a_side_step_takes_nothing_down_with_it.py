"""A failed side step must not take the actual statement down with it.

1. **The mod rollback.** In the `except` branch of `_apply_locked`, `_finish(change_id, ...)` sat
   unprotected between the rollback and the error message. If the DB access raised, the
   **result of the rollback** was lost, the only thing the operator really needs: did it go
   through ("The previous configuration is running again.") or not ("WARNING: rollback
   incomplete")? Instead the operator saw a raw DB error, and the history row stayed `pending`
   until the next backend restart and then `unknown` instead of the real outcome.

   The project already has the right pattern: `backup._incident` encapsulates its DB access
   precisely so that a side record does not topple the main operation.

2. **The restore.** `docker.action(..., "start")` stood without a `try` in front of the boot
   proof. `action` is an httpx POST with `raise_for_status()`: a ConnectError or a 500 from the
   socket proxy flies out raw, and with it the whole restore message disappears, including the
   **name of the safety archive**. That name is exactly what is needed in this situation.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.services import restore as restore_svc
from app.services.mods import apply as mod_apply


class _Spec:
    data_dir = "/whatever"
    world_subdirs = ("Saves", "Server")
    pre_hook = None
    post_hook = None


class _Driver:
    def backup_spec(self, server):
        return _Spec()


def _srv():
    return SimpleNamespace(id=1, key="pz-1", container_name="pz-1",
                           console_kind="none", game_type=None, data_dir="/whatever")


class _GateOpen:
    async def guard_boot(self, server):
        return []

    def guard_message(self, blockers):
        return ""


def test_the_restore_does_not_lose_its_message_at_the_start(monkeypatch):
    monkeypatch.setattr(restore_svc.bk, "valid_backup_name", lambda n: True)
    monkeypatch.setattr(restore_svc.os.path, "exists", lambda p: True)
    monkeypatch.setattr(restore_svc.bk, "backup_path", lambda n, **k: "/whatever/x.tar.gz")
    monkeypatch.setattr(restore_svc.bk, "extract_backup", lambda *a, **k: None)
    # The archive is a name here, not a file: the authenticity check (`check_restorable`, in
    # front of BOTH restore paths) would otherwise trip over the missing file before the actual
    # case comes up.
    monkeypatch.setattr(restore_svc.bk, "check_restorable", lambda *a, **k: None)
    monkeypatch.setattr(restore_svc, "mod_state", _GateOpen(), raising=False)

    async def _stop_and_snapshot(server, driver, label, **kw):
        return True, {"name": "20260816-100000-pre-restore.tar.gz"}
    monkeypatch.setattr(restore_svc.lifecycle, "stop_and_snapshot", _stop_and_snapshot)

    async def _stop_and_confirm(server, **kw):
        return True, True
    monkeypatch.setattr(restore_svc.lifecycle, "stop_and_confirm", _stop_and_confirm)

    class _Dk:
        async def action(self, name, act, **kw):
            raise RuntimeError("502 from the socket proxy")
    monkeypatch.setattr(restore_svc, "docker", _Dk())

    with pytest.raises(HTTPException) as e:
        asyncio.run(restore_svc.restore(_srv(), _Driver(), "20260101-000000-weekly.tar.gz"))

    text = str(e.value.detail)
    assert "20260816-100000-pre-restore" in text, (
        "the name of the safety archive disappeared with the raw Docker error"
    )


def test_the_mod_rollback_reports_its_result_even_without_a_database(monkeypatch):
    """`_finish` raises; the operator must still learn whether the rollback went through."""
    def _finish_broken(change_id, status, detail):
        raise RuntimeError("console db just gone")
    monkeypatch.setattr(mod_apply, "_finish", _finish_broken)
    monkeypatch.setattr(mod_apply.state, "invalidate", lambda k: None)

    async def _rollback_ok(*a, **kw):
        return True
    monkeypatch.setattr(mod_apply, "_rollback", _rollback_ok)

    with pytest.raises(HTTPException) as e:
        asyncio.run(mod_apply._report_rollback(
            _srv(), "pz-1", container="pz-1", saved_config={}, save_dir="/whatever",
            qpath="/whatever/q", moved=[], driver=_Driver(), dropped_downloads=[], change_id=1,
            error=RuntimeError("the actual error"), step=lambda *a, **k: None,
        ))
    text = str(e.value.detail)
    assert e.value.detail["code"] == "mods.change_failed_rolled_back_history_open", "the result of the rollback got lost"
    assert "the actual error" in text
