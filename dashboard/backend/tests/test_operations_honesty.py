"""A background operation must report honestly — even when its own bookkeeping jams.

Everything here sits at the same two functions: `_finish` writes the result, `_serialize` reads
it. The common theme: **the operation itself ran correctly, only what the operator learns about it
would be wrong.** That is the most expensive kind of bug in this project — it looks like an
operator error.
"""
from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace

from fastapi import HTTPException

from app.services import operations


def _srv():
    return SimpleNamespace(id=1, key="pz-1")


async def _drain(op_id):
    task = operations._tasks.get(op_id)
    if task:
        await task


def test_a_too_long_result_stays_readable(monkeypatch, mem_engine):
    """`[:4000]` on finished JSON cuts in the middle of a string — and makes the row unreadable.

    `_serialize` serves `get()`, `latest()` and `running()`. A cut-off row would mean HTTP 500 on
    EVERY further fetch of the operation, permanently — although the operation itself succeeded.
    The limit is reachable through the mod file change, whose result carries three unbounded file
    lists.
    """
    monkeypatch.setattr(operations, "engine", mem_engine)
    srv = _srv()

    async def work(progress):
        return {"summary": "done", "files": ["a-very-long-mod-file-%03d.jar" % i for i in range(300)]}

    async def scenario():
        op = operations.start(srv, "mods.files", work)
        await _drain(op["id"])
        return operations.get(op["id"], srv)   # must NOT raise

    result = asyncio.run(scenario())
    assert result["status"] == operations.OK
    assert isinstance(result["result"], dict), "the result is no longer readable JSON"
    # Truncating is allowed — but visibly, not secretly.
    assert result["result"].get("truncated") is True or "files" in result["result"]


def test_a_structured_error_keeps_its_blockers(monkeypatch, mem_engine):
    """The preflight reports `{"error": …, "blockers": [...]}` — the operator needs the WHY.

    `mods/apply.py` and `mods/foreign_apply.py` raise the structured form; the frontend renders
    exactly that (`ModFindings.tsx`) via stable codes (`MAP_WITHOUT_PROVIDER` …). If only the plain
    text and an empty `result` survived the way through `run()`, WHICH map blocks would be lost.
    """
    monkeypatch.setattr(operations, "engine", mem_engine)
    srv = _srv()

    async def work(progress):
        raise HTTPException(409, detail={
            "error": "The configuration was not applied.",
            "blockers": [{"code": "MAP_WITHOUT_PROVIDER", "mod": "Riverside"}],
        })

    async def scenario():
        op = operations.start(srv, "mods.apply", work)
        await _drain(op["id"])
        return operations.get(op["id"], srv)

    result = asyncio.run(scenario())
    assert result["status"] == operations.FAILED
    assert "not applied" in result["detail"]
    assert result["result"].get("blockers"), "the blocker list got lost on the way"
    assert result["result"]["blockers"][0]["code"] == "MAP_WITHOUT_PROVIDER"


def test_a_success_without_summary_still_gets_a_text(monkeypatch, mem_engine):
    """Otherwise the UI keeps showing the last "running" line — as if nothing had happened."""
    monkeypatch.setattr(operations, "engine", mem_engine)
    srv = _srv()

    async def work(progress):
        progress.step("Backing up world…")
        return {"ok": True}          # no `summary`

    async def scenario():
        op = operations.start(srv, "backup.create", work)
        await _drain(op["id"])
        return operations.get(op["id"], srv)

    result = asyncio.run(scenario())
    assert result["status"] == operations.OK
    assert result["detail"].strip(), "finished, but without a word about it"
    assert "Backing up world" not in result["detail"], "that is the progress, not the result"


def test_the_running_operation_is_not_hidden_by_the_newest(monkeypatch, mem_engine):
    """`running()` must not ask for the NEWEST operation — after a double click that is the rejected one.

    Otherwise, while the real version change is still running, `running()` reports "nothing
    running". The page shows the failure permanently, and the 409 guard that relies on `running()`
    lets the next operation through — right into the running one.
    """
    monkeypatch.setattr(operations, "engine", mem_engine)
    srv = _srv()
    keep_running = asyncio.Event()

    async def long_work(progress):
        await keep_running.wait()
        return {"summary": "done"}

    async def fails(progress):
        raise HTTPException(409, detail="A version change is already running for this server.")

    async def scenario():
        first = operations.start(srv, "version.change", long_work)
        second = operations.start(srv, "version.change", fails)
        await _drain(second["id"])                     # the second one is finished (failed)
        running_op = operations.running(srv, "version.change")
        keep_running.set()
        await _drain(first["id"])
        return first["id"], running_op

    first_id, running_op = asyncio.run(scenario())
    assert running_op is not None, "the running operation was invisible because a newer one sat next to it"
    assert running_op["id"] == first_id


def test_a_failing_finish_does_not_turn_a_success_into_a_failure(monkeypatch, mem_engine):
    """The bookkeeping must not rewrite the result.

    If `_finish(OK, …)` sits inside the same `try` that catches the errors of the WORK, a failing
    write (DB hiccup, connection gone) runs the `except` branch and reports the successful
    operation as failed — with the DB error as "reason". If that second write fails too, the
    exception escapes the task: the row stays "running" forever and the 409 guard blocks until
    the next backend restart.
    """
    monkeypatch.setattr(operations, "engine", mem_engine)
    srv = _srv()
    calls: list[str] = []
    real = operations._finish

    def broken(op_id, status, **kw):
        calls.append(status)
        if status == operations.OK:
            raise RuntimeError("database briefly gone")
        return real(op_id, status, **kw)

    monkeypatch.setattr(operations, "_finish", broken)

    async def work(progress):
        return {"summary": "it worked"}

    async def scenario():
        op = operations.start(srv, "backup.create", work)
        await _drain(op["id"])

    asyncio.run(scenario())
    assert calls == [operations.OK], (
        f"a success was turned into a failure: {calls}"
    )


def test_a_handled_failure_is_in_the_log(monkeypatch, mem_engine, caplog):
    """The EXPECTED failures must be logged too.

    The `except Exception` branch logs with `log.exception`; the `except HTTPException` branch must
    log as well. A blocked mod apply, a rejected restore, a 409 from the world lock — exactly the
    cases one looks for in the log when an operator asks "why did that not work" — must leave a
    line there.
    """
    monkeypatch.setattr(operations, "engine", mem_engine)
    srv = _srv()

    async def work(progress):
        raise HTTPException(409, detail="A restore is already running for this server.")

    async def scenario():
        op = operations.start(srv, "mods.apply", work)
        await _drain(op["id"])

    with caplog.at_level(logging.WARNING, logger="uvicorn.error"):
        asyncio.run(scenario())

    assert any("mods.apply" in r.getMessage() for r in caplog.records), (
        "the handled failure is in no log"
    )


def test_a_restart_midway_heals_ALL_three_ledgers(monkeypatch, mem_engine):
    """`cancel_stale` must close not only the `operations` row but all three tables.

    Every world-writing operation keeps a second ledger: `version_manager._record_pending` creates
    `VersionChange(status="pending")`, `mods/apply._record` creates `ModChange(status="pending")`.
    Both are closed only in their own success or rollback branch. If the process dies in between —
    a deploy during a version change — none of these branches ever runs, and nothing cleans up.

    That is particularly nasty because THIS row says whether the rollback ran (`ok` /
    `rolled_back` / `rolled_back_partial` / `failed`). It would stay "pending" forever, i.e.
    running — while the operation row next to it honestly reports that the outcome is unknown.
    """
    monkeypatch.setattr(operations, "engine", mem_engine)
    from sqlmodel import Session, select

    from app.models.mod_set import ModChange
    from app.models.operation import Operation
    from app.models.version_change import VersionChange

    with Session(mem_engine) as s:
        s.add(Operation(server_id=1, kind="version.change", status=operations.RUNNING))
        s.add(VersionChange(server_id=1, status="pending", backup_name="pre-x.tar.gz"))
        s.add(ModChange(server_id=1, status="pending", backup_name="pre-y.tar.gz"))
        s.commit()

    asyncio.run(operations.cancel_stale())

    with Session(mem_engine) as s:
        op = s.exec(select(Operation)).first()
        vc = s.exec(select(VersionChange)).first()
        mc = s.exec(select(ModChange)).first()

    assert op.status == operations.FAILED
    assert vc.status != "pending", "the version history still says \"pending\""
    assert mc.status != "pending", "the mod history still says \"pending\""
    for line in (vc, mc):
        assert "unknown" in line.detail.lower(), (
            "the entry claims a result nobody knows"
        )
