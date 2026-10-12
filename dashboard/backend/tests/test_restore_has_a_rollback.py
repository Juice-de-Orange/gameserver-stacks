"""If the boot fails after a restore, the container does not stay in a restart loop.

A failed `verify_boot(...)` must not just raise a 502 saying the backup is ready: the restore has
to stop the container and actually restore the pre-restore backup, like the other two
world-writing operations (`version_manager._rollback_swap`, `mods/apply._rollback`) do.

`verify_boot` returns `False` among other things exactly when the restart counter rises — in a
crash loop. The containers run `unless-stopped` and the watchdog deliberately does NOT act on
`not running`, so without the rollback the server would flap indefinitely after the world lock
was released. Typical trigger: restoring an archive that does not match the running version or
mod list, e.g. a weekly backup from before the last mod apply.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.services import restore as restore_svc


class _Spec:
    data_dir = "/any"
    world_subdirs = ("Saves", "Server")
    pre_hook = None
    post_hook = None


class _Driver:
    def backup_spec(self, server):
        return _Spec()


def _srv():
    return SimpleNamespace(id=1, key="pz-1", container_name="pz-1",
                           console_kind="none", game_type=None, data_dir="/any")


class _GateOpen:
    async def guard_boot(self, server):
        return []

    def guard_message(self, blockers):
        return ""


def _base_setup(monkeypatch, actions, *, boot_ok: bool):
    monkeypatch.setattr(restore_svc.bk, "valid_backup_name", lambda n: True)
    monkeypatch.setattr(restore_svc.os.path, "exists", lambda p: True)
    monkeypatch.setattr(restore_svc.bk, "backup_path", lambda n, **k: "/any/x.tar.gz")
    monkeypatch.setattr(restore_svc.bk, "extract_backup",
                        lambda name, **k: actions.append(f"extract:{name}"))
    # The archive is only a name here, not a file — otherwise the authenticity check
    # (`check_restorable`, in front of BOTH restore paths) trips over the missing file
    # before the case under test is reached.
    monkeypatch.setattr(restore_svc.bk, "check_restorable", lambda *a, **k: None)
    monkeypatch.setattr(restore_svc, "mod_state", _GateOpen(), raising=False)

    async def _stop_and_snapshot(server, driver, label, **kw):
        return True, {"name": "20260816-100000-pre-restore.tar.gz"}
    monkeypatch.setattr(restore_svc.lifecycle, "stop_and_snapshot", _stop_and_snapshot)

    async def _stop_and_confirm(server, **kw):
        actions.append("stop_and_confirm")
        return True, True
    monkeypatch.setattr(restore_svc.lifecycle, "stop_and_confirm", _stop_and_confirm)

    class _Dk:
        async def action(self, name, act, **kw):
            actions.append(act)
    monkeypatch.setattr(restore_svc, "docker", _Dk())

    async def _verify(server, driver, timeout=0):
        return boot_ok
    monkeypatch.setattr(restore_svc, "verify_boot", _verify)


def test_a_failed_boot_does_not_leave_the_container_flapping(monkeypatch):
    actions: list[str] = []
    _base_setup(monkeypatch, actions, boot_ok=False)

    with pytest.raises(HTTPException) as e:
        asyncio.run(restore_svc.restore(_srv(), _Driver(), "20260101-000000-weekly.tar.gz"))

    assert "stop_and_confirm" in actions, (
        "the container keeps running — unless-stopped brings it up endlessly in a crash loop"
    )
    # The rollback is actually CARRIED OUT, not just mentioned.
    assert any(a.startswith("extract:20260816-100000-pre-restore") for a in actions), (
        "the earlier backup was only mentioned, not restored"
    )
    assert "502" in str(e.value.status_code) or e.value.status_code == 502


def test_the_success_case_is_unchanged(monkeypatch):
    """Cross-check: after a successful boot nothing is rolled back."""
    actions: list[str] = []
    _base_setup(monkeypatch, actions, boot_ok=True)

    result = asyncio.run(restore_svc.restore(_srv(), _Driver(), "20260101-000000-weekly.tar.gz"))
    assert result["ok"] is True
    assert "stop_and_confirm" not in actions
    assert actions.count("start") == 1
