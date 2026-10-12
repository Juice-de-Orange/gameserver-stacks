"""The follow-up runs even when the lead-up fails halfway.

`create_backup` calls `pre_hook()` deliberately OUTSIDE its `try` — the reasoning there knows
exactly two outcomes: quiesced or not quiesced. But Minecraft's hook has TWO steps (`save-off`,
then `save-all flush`). If the second fails, autosave is already SWITCHED OFF; if the exception
escapes before the `try`, the `finally` with the `post_hook` (`save-on`) NEVER runs. No incident
is recorded either: that hangs on `scheduled_backup` alone and needs a `result` that does not
exist in this case.

What would remain is exactly the state the same file calls silent data loss: a failed `save-on`
leaves autosave off for good, and nobody finds out — only without the message built for it. All
call paths are affected.

Second: `prune_backups` must not sit behind `create_backup` in the same `try` of the weekly run.
If packing fails on ENOSPC, nothing would ever be pruned; `prune_backups` has no second caller.
The state would be stably wrong: from then on every mandatory backup fails too, and mod apply,
restore and version change are blocked.
"""
import os

import pytest

from app.services import backup as bk


def _world(tmp_path):
    d = tmp_path / "Saves"
    d.mkdir(parents=True, exist_ok=True)
    (d / "world.bin").write_text("x", encoding="utf-8")
    return str(tmp_path)


def test_a_failed_lead_up_still_runs_the_follow_up(tmp_path):
    """save-off went through, save-all flush raises — save-on MUST run."""
    base = _world(tmp_path)
    ran: list[str] = []

    def _pre():
        ran.append("save-off")
        raise RuntimeError("save-all flush: timeout")

    def _post():
        ran.append("save-on")

    with pytest.raises(RuntimeError):
        bk.create_backup("test", data_dir=base, world_subdirs=("Saves",),
                         pre_hook=_pre, post_hook=_post)

    assert ran == ["save-off", "save-on"], (
        "autosave stays off for good: `save-on` did not run. "
        f"Only ran: {ran}"
    )


def test_a_failed_lead_up_still_creates_no_archive(tmp_path):
    """Cross-check: the intent of the lead-up stays — a torso is not a backup."""
    base = _world(tmp_path)

    def _pre():
        raise RuntimeError("save-all flush: timeout")

    with pytest.raises(RuntimeError):
        bk.create_backup("test", data_dir=base, world_subdirs=("Saves",),
                         pre_hook=_pre, post_hook=lambda: None)

    assert bk.list_backups(data_dir=base) == [], "an archive was created"


def test_a_failed_lead_up_records_an_incident(tmp_path, monkeypatch, mem_engine):
    """The operator sees the dashboard, not `docker logs`."""
    from types import SimpleNamespace

    base = _world(tmp_path)
    incidents: list[tuple] = []
    monkeypatch.setattr(bk, "_incident", lambda srv, kind, detail: incidents.append((kind, detail)))

    def _pre():
        raise RuntimeError("save-all flush: timeout")

    srv = SimpleNamespace(id=1, key="mc", container_name="minecraft")
    with pytest.raises(RuntimeError):
        bk.create_backup("test", data_dir=base, world_subdirs=("Saves",),
                         pre_hook=_pre, post_hook=lambda: None, server=srv)

    assert incidents and incidents[0][0] == "backup_quiesce_failed", (
        f"no incident recorded: {incidents}"
    )


def test_without_a_server_no_incident_but_the_error_is_raised(tmp_path):
    """Cross-check: `server` is optional — without it nothing else changes."""
    base = _world(tmp_path)

    def _pre():
        raise RuntimeError("broken")

    with pytest.raises(RuntimeError):
        bk.create_backup("test", data_dir=base, world_subdirs=("Saves",),
                         pre_hook=_pre, post_hook=lambda: None)


def test_the_normal_case_stays_unchanged(tmp_path):
    """Cross-check: both hooks run, the archive is created."""
    base = _world(tmp_path)
    ran: list[str] = []
    result = bk.create_backup(
        "test", data_dir=base, world_subdirs=("Saves",),
        pre_hook=lambda: ran.append("save-off"),
        post_hook=lambda: ran.append("save-on"),
    )
    assert ran == ["save-off", "save-on"]
    assert os.path.exists(os.path.join(bk.backup_dir(base), result["name"]))


# --- pruning does not depend on packing ------------------------------------------------------

def test_pruning_runs_even_after_failed_packing(tmp_path, monkeypatch):
    """ENOSPC while packing must not suspend the retention limit for good.

    `prune_backups` has no second caller. Behind `create_backup` in the same `try`, the state after
    the first ENOSPC would be stably wrong: from then on every mandatory backup fails too, and mod
    apply, restore and version change are blocked. The way out of the full disk would sit behind
    the condition that the full disk prevents.
    """
    import asyncio
    from types import SimpleNamespace

    base = _world(tmp_path)
    srv = SimpleNamespace(id=1, key="mc", container_name="minecraft")
    spec = SimpleNamespace(data_dir=base, world_subdirs=("Saves",), pre_hook=None, post_hook=None)
    drv = SimpleNamespace(backup_spec=lambda s: spec)

    monkeypatch.setattr(bk.servers_registry, "list_enabled_with_driver", lambda: [(srv, drv)])
    monkeypatch.setattr(bk, "_incident", lambda *a, **k: None)

    def _disk_full(*a, **k):
        raise OSError("ENOSPC: no space left on device")
    monkeypatch.setattr(bk, "create_backup", _disk_full)

    pruned: list[str] = []
    monkeypatch.setattr(bk, "prune_backups",
                        lambda *a, **k: pruned.append("ran") or 0)

    asyncio.run(bk.scheduled_backup())

    assert pruned == ["ran"], (
        "no pruning after the failed packing — the disk stays full, and from now on every "
        "mandatory backup fails too"
    )
