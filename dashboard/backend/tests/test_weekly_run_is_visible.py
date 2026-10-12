"""A failed weekly backup becomes visible — not just logged.

The loop body of `scheduled_backup` must not just catch every error and do inconsequential things:
a `log.warning` **without traceback** and a `results.append({... "error"})` — `results` is the
return value of an APScheduler cron job that nobody ever receives. It records an `_incident(...)`.

The helper for that is in the same file and is used for the MILDER case: `backup_quiesce_failed`
— there the archive exists and is usable according to `QuiesceWarning`. The harder case, no
archive at all, needs an incident just as much (the same omission was fixed in `restarts.py`).
For several games the weekly run is the **only** automatic backup — without an incident a failure
shows only when someone needs a rollback.
"""
from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace

from app.services import backup as bk


class _Spec:
    data_dir = "/any"
    world_subdirs = ("Saves",)
    config_subdirs = ()
    pre_hook = None
    post_hook = None
    # Minecraft can save and is therefore not stopped for the backup.
    # The field is set here explicitly and not read as `getattr(..., False)` at the call site:
    # a default there would silently turn a typo in the field name into "do not stop" — the
    # fail-open direction. A dummy that does not follow the contract should stand out.
    stop_for_consistency = False


class _Drv:
    def backup_spec(self, server):
        return _Spec()


def test_a_failed_weekly_backup_records_an_incident(monkeypatch, caplog):
    srv = SimpleNamespace(id=7, key="mc", container_name="minecraft")
    monkeypatch.setattr(bk.servers_registry, "list_enabled_with_driver", lambda: [(srv, _Drv())])

    def _packing_fails(*a, **k):
        raise RuntimeError("save-all flush ran into the time limit")
    monkeypatch.setattr(bk, "create_backup", _packing_fails)

    incidents: list[tuple] = []
    monkeypatch.setattr(bk, "_incident", lambda s, kind, detail: incidents.append((kind, detail)))

    with caplog.at_level(logging.ERROR, logger="uvicorn.error"):
        asyncio.run(bk.scheduled_backup())

    assert incidents, "the failure of the ONLY automatic backup was visible nowhere"
    kind, detail = incidents[0]
    assert kind == "backup_failed"
    assert "save-all flush" in detail, "the reason is missing from the incident"


def test_the_failure_is_logged_with_its_cause(monkeypatch, caplog):
    """`exc_info=False` swallowed the cause — the same fault as fixed in restarts.py."""
    srv = SimpleNamespace(id=7, key="mc", container_name="minecraft")
    monkeypatch.setattr(bk.servers_registry, "list_enabled_with_driver", lambda: [(srv, _Drv())])
    monkeypatch.setattr(bk, "_incident", lambda *a, **k: None)

    def _packing_fails(*a, **k):
        raise RuntimeError("disk full")
    monkeypatch.setattr(bk, "create_backup", _packing_fails)

    with caplog.at_level(logging.ERROR, logger="uvicorn.error"):
        asyncio.run(bk.scheduled_backup())

    mc_records = [r for r in caplog.records if "mc" in r.getMessage()]
    assert mc_records, "no error log for the failed server"
    assert any(r.exc_info for r in mc_records), "the cause (traceback) is missing"


def test_one_failure_does_not_stop_the_run(monkeypatch):
    """Cross-check: the remaining servers must still be backed up."""
    a = SimpleNamespace(id=1, key="mc", container_name="minecraft")
    b = SimpleNamespace(id=2, key="vs", container_name="vintagestory")
    monkeypatch.setattr(bk.servers_registry, "list_enabled_with_driver",
                        lambda: [(a, _Drv()), (b, _Drv())])
    monkeypatch.setattr(bk, "_incident", lambda *a, **k: None)
    monkeypatch.setattr(bk, "prune_backups", lambda *a, **k: 0)

    packed: list[str] = []

    def _pack(label, *, data_dir=None, **k):
        if not packed:
            packed.append("mc-attempt")
            raise RuntimeError("disk full")
        packed.append("vs")
        return {"name": "x.tar.gz", "size": 100}
    monkeypatch.setattr(bk, "create_backup", _pack)

    asyncio.run(bk.scheduled_backup())
    assert "vs" in packed, "the second server was skipped after the first one failed"
