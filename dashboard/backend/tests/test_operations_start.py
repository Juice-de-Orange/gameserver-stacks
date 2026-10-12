"""Background operations need a running event loop — otherwise they never start.

A synchronous `def` endpoint runs in FastAPI's thread pool, where there is no event loop;
`asyncio.create_task` raises `RuntimeError: no running event loop` there.

The consequence would be worse than a failure if `start()` created the database row BEFORE the
task: the endpoint ends with HTTP 500 — and the row stays "running" forever. Every further attempt
then falls into the 409 guard ("… is already running for this server") until the backend
restarts. Affected would be exactly the paths protected by mandatory backup and rollback: mod
apply, mod files apply, backup restore and version change.
"""
import asyncio
import inspect
import pathlib

import pytest
from sqlmodel import Session, select

from app.models.operation import Operation
from app.models.server import GameServer, GameType
from app.services import operations


def _srv(sid=1):
    s = GameServer(key="pz-1", game_type=GameType.pz, container_name="pz-1")
    s.id = sid
    return s


async def _work(progress):
    return {"summary": "done"}


def test_without_event_loop_no_row_is_created(monkeypatch, mem_engine):
    """The core: it fails LOUDLY and leaves no operation that stays "running" forever."""
    monkeypatch.setattr(operations, "engine", mem_engine)

    with pytest.raises(RuntimeError) as ei:
        operations.start(_srv(), "mods.apply", _work)

    assert "SYNCHRONOUS endpoint" in str(ei.value)
    assert "async def" in str(ei.value)

    with Session(mem_engine) as s:
        assert s.exec(select(Operation)).all() == [], "a row was left behind — exactly the bug"


def test_with_event_loop_the_operation_really_runs(monkeypatch, mem_engine):
    monkeypatch.setattr(operations, "engine", mem_engine)
    ran: list[str] = []

    async def work(progress):
        ran.append("yes")
        return {"summary": "done"}

    async def main():
        op = operations.start(_srv(), "mods.apply", work)
        for _ in range(50):                      # give the task time without a fixed sleep
            await asyncio.sleep(0.01)
            if not operations._tasks.get(op["id"]):
                break
        return op

    op = asyncio.run(main())
    assert ran == ["yes"]
    with Session(mem_engine) as s:
        row = s.get(Operation, op["id"])
        assert row.status == "ok", f"operation ended as {row.status}: {row.detail}"


def test_every_caller_of_operations_start_is_async():
    """The guard rail. It counts the callers so that it cannot silently grasp at nothing.

    Writing a new operation endpoint as `def` instead of `async def` breaks here — not only when
    someone presses "Apply" in the dashboard and the server says 500.
    """
    from app.routers import mod_config, server_ops, servers, versions

    found = []
    covered = 0
    for module in (server_ops, mod_config, versions, servers):
        for name in dir(module):
            fn = getattr(module, name, None)
            if not callable(fn) or not hasattr(fn, "__code__"):
                continue
            try:
                source = inspect.getsource(fn)
            except (OSError, TypeError):
                continue
            if "operations.start(" not in source:
                continue
            found.append(f"{module.__name__.split('.')[-1]}.{name}")
            covered += source.count("operations.start(")
            assert inspect.iscoroutinefunction(fn), (
                f"{module.__name__}.{name} calls operations.start but is synchronous — "
                "the background operation would never start"
            )

    # Every call site in the app must sit in one of the functions checked above — counted over
    # the source text, so a caller in a module this test does not import fails here instead of
    # going unchecked, and a collection that finds nothing cannot pass.
    app_dir = pathlib.Path(operations.__file__).resolve().parent.parent
    total = sum(
        path.read_text(encoding="utf-8").count("operations.start(")
        for path in app_dir.rglob("*.py")
        if path.resolve() != pathlib.Path(operations.__file__).resolve()
    )
    assert total, "no call site of operations.start found — does the collection still work?"
    assert covered == total, (
        f"{total} call sites in the app, only {covered} checked ({found}) — a caller outside "
        "server_ops / mod_config / versions? Add its module above."
    )
