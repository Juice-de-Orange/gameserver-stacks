"""Long-running operations: start, observe, finish honestly.

Background: applying mods and changing versions take up to 15 minutes, while a proxy in front of
the dashboard may abort the HTTP request after ~100 s. The browser would show an error while the
server kept working correctly.
"""
import asyncio
from types import SimpleNamespace

from fastapi import HTTPException

from app.services import operations


def _srv():
    return SimpleNamespace(id=1, key="pz-1")


def _run(coro):
    return asyncio.run(coro)


async def _drain(op_id):
    """Wait for the background task (in the test instead of polling)."""
    task = operations._tasks.get(op_id)
    if task:
        await task


def test_a_finished_operation_carries_its_result(monkeypatch, mem_engine):
    monkeypatch.setattr(operations, "engine", mem_engine)
    srv = _srv()

    async def work(progress):
        progress.step("working…", current=1, total=2)
        return {"summary": "+1 Mod", "backup": "bk.tar.gz"}

    async def scenario():
        op = operations.start(srv, "mods.apply", work)
        assert op["running"] is True          # returns at once, not only after the work
        await _drain(op["id"])
        return operations.get(op["id"], srv)

    done = _run(scenario())
    assert done["status"] == "ok"
    assert done["result"]["backup"] == "bk.tar.gz"
    assert done["running"] is False
    assert done["finished_at"]


def test_a_failing_operation_keeps_the_plain_text_reason(monkeypatch, mem_engine):
    monkeypatch.setattr(operations, "engine", mem_engine)
    srv = _srv()

    async def work(progress):
        raise HTTPException(409, detail={"error": "map without provider", "blockers": []})

    async def scenario():
        op = operations.start(srv, "mods.apply", work)
        await _drain(op["id"])
        return operations.get(op["id"], srv)

    done = _run(scenario())
    assert done["status"] == "failed"
    assert "map without provider" in done["detail"]


def test_the_server_is_off_warning_reaches_the_reason(monkeypatch, mem_engine):
    """`lifecycle.ensure_restart` puts "the server is now OFF" into `restart_warning`. The panel
    shows only the reason text, so the warning must be part of it — not a field nobody reads."""
    monkeypatch.setattr(operations, "engine", mem_engine)
    srv = _srv()
    warning = "WARNING: the server was running before and is now OFF — the restart failed (x)."

    async def work(progress):
        raise HTTPException(502, detail={"error": "mandatory backup failed",
                                         "restart_warning": warning})

    async def scenario():
        op = operations.start(srv, "version.change", work)
        await _drain(op["id"])
        return operations.get(op["id"], srv)

    done = _run(scenario())
    assert "mandatory backup failed" in done["detail"] and warning in done["detail"]


def test_progress_is_visible_while_the_work_runs(monkeypatch, mem_engine):
    monkeypatch.setattr(operations, "engine", mem_engine)
    srv = _srv()
    gate = asyncio.Event()

    async def work(progress):
        progress.step("Creating mandatory backup…", current=3, total=10)
        await gate.wait()
        return {}

    async def scenario():
        op = operations.start(srv, "mods.apply", work)
        await asyncio.sleep(0)          # give the task one round
        mid = operations.get(op["id"], srv)
        gate.set()
        await _drain(op["id"])
        return mid

    mid = _run(scenario())
    assert mid["step"].startswith("Creating mandatory backup")
    assert mid["percent"] == 30.0


def test_latest_and_running_let_the_ui_reattach(monkeypatch, mem_engine):
    monkeypatch.setattr(operations, "engine", mem_engine)
    srv = _srv()

    async def work(progress):
        return {"summary": "done"}

    async def scenario():
        op = operations.start(srv, "version.change", work)
        await _drain(op["id"])

    _run(scenario())
    assert operations.latest(srv, "version.change")["status"] == "ok"
    assert operations.running(srv, "version.change") is None
    assert operations.latest(srv, "mods.apply") is None      # other kind, no match


def test_operations_of_other_servers_are_not_readable(monkeypatch, mem_engine):
    monkeypatch.setattr(operations, "engine", mem_engine)

    async def work(progress):
        return {}

    async def scenario():
        op = operations.start(_srv(), "mods.apply", work)
        await _drain(op["id"])
        return op["id"]

    op_id = _run(scenario())
    assert operations.get(op_id, SimpleNamespace(id=2, key="mc")) is None


def test_stale_operations_are_closed_after_a_restart(monkeypatch, mem_engine):
    """An operation whose task did not survive the restart must not stay "running" forever."""
    monkeypatch.setattr(operations, "engine", mem_engine)
    from sqlmodel import Session

    from app.models.operation import Operation

    with Session(mem_engine) as s:
        row = Operation(server_id=1, kind="mods.apply", status="running")
        s.add(row)
        s.commit()
        s.refresh(row)
        op_id = row.id

    _run(operations.cancel_stale())
    done = operations.get(op_id, _srv())
    assert done["status"] == "failed"
    assert "was restarted" in done["detail"]
