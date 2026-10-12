"""Whoever stops a server and then fails brings it back up, and loudly.

Between a confirmed stop and the protected `try` with the full rollback, `_apply_locked` runs
`_new_quarantine` (`os.makedirs`), `_stash_config` (`shutil.copy2`) and `_record` (DB insert).
If one of them raises without a safety net, the exception leaves the function raw: no rollback,
no start, no `ModChange` row, and `watchdog.suppress(..., 20)` keeps autoheal away for 20
minutes. The server simply stays off.

The backup error path right before it already brings a previously running server back up; a
failed start there must not be swallowed with `except Exception: pass` and no log either. The
version change has the same gap between the mandatory backup and its transaction block.

So the rule lives in one place: `lifecycle.ensure_restart`.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from app.services import lifecycle


def _srv():
    return SimpleNamespace(id=1, key="pz-1", container_name="pz-1")


def test_an_error_after_the_stop_brings_the_server_back_up(monkeypatch):
    actions: list[str] = []

    class _Dk:
        async def action(self, name, act, **kw):
            actions.append(act)
    monkeypatch.setattr(lifecycle, "docker", _Dk())

    async def scenario():
        async with lifecycle.ensure_restart(_srv(), True, what="mod change"):
            raise OSError("ENOSPC while creating the quarantine")

    with pytest.raises(OSError):
        asyncio.run(scenario())
    assert actions == ["start"], "the server stayed down after the error"


def test_a_previously_stopped_server_stays_stopped(monkeypatch):
    """Cross-check: the rule is "back to the previous state", not "always start"."""
    actions: list[str] = []

    class _Dk:
        async def action(self, name, act, **kw):
            actions.append(act)
    monkeypatch.setattr(lifecycle, "docker", _Dk())

    async def scenario():
        async with lifecycle.ensure_restart(_srv(), False, what="mod change"):
            raise OSError("whatever")

    with pytest.raises(OSError):
        asyncio.run(scenario())
    assert actions == [], "a previously stopped server was started"


def test_without_an_error_nothing_is_touched(monkeypatch):
    actions: list[str] = []

    class _Dk:
        async def action(self, name, act, **kw):
            actions.append(act)
    monkeypatch.setattr(lifecycle, "docker", _Dk())

    async def scenario():
        async with lifecycle.ensure_restart(_srv(), True, what="mod change"):
            return "done"

    # The body's result comes through untouched: no exception, nothing swallowed or replaced.
    assert asyncio.run(scenario()) == "done"
    assert actions == []


def test_a_failed_restart_is_not_swallowed(monkeypatch, caplog):
    """No `except Exception: pass` without a log.

    The server is off then, the watchdog silent for 20 minutes, and the only message would be
    the original error, which knows nothing about the restart.
    """
    import logging

    class _Dk:
        async def action(self, name, act, **kw):
            raise RuntimeError("socket proxy not responding")
    monkeypatch.setattr(lifecycle, "docker", _Dk())
    incidents: list[tuple] = []
    monkeypatch.setattr(lifecycle, "_incident", lambda *a, **k: incidents.append(a), raising=False)

    async def scenario():
        async with lifecycle.ensure_restart(_srv(), True, what="mod change"):
            raise OSError("the actual error")

    with caplog.at_level(logging.ERROR, logger="uvicorn.error"):
        with pytest.raises(OSError):
            asyncio.run(scenario())

    assert any("pz-1" in r.getMessage() for r in caplog.records), (
        "the failed restart is in no log"
    )
