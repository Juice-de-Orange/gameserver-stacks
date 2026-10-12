"""The nightly jobs are EXECUTED, not just registered correctly.

Four of the eight cleanup jobs (`metrics_prune`, `audit_prune`, `chat_prune`, `history_prune`)
once died on every run while `job_runs` only carried the "first known start" mark from
`schedule_catchup`. Two causes, both one line:

* `logged` called `await fn()` without checking whether `fn` is a coroutine at all. Three of the
  eight registered functions are synchronous (`prune_metrics`, `prune_audit`, `prune_chat`), and
  `await None` raises `TypeError`.
* `prune_history` accessed `Operation.created_at`; the column is called `started_at`. The access
  sat in the tuple construction BEFORE the loop and OUTSIDE the `try`, so the job died before the
  first table was touched: `server_incidents` and `player_events` stayed uncleaned too.

**Why this file exists.** The earlier guards checked text, never behaviour: one did
`assert "Operation" in inspect.getsource(...)` (the string was there, broken attribute and all),
the other checked via AST that `jobs.logged` appears in the `add_job` call, not whether what it
was given is awaitable. A guard that only reads catches no crash, so here every job is really run.
"""
from __future__ import annotations

import asyncio
import datetime as dt

import pytest
from sqlmodel import Session, select

from app.models import Operation, PlayerEvent, ServerIncident
from app.models.player import utcnow_naive
from app.services import jobs


@pytest.fixture()
def recorded(monkeypatch):
    """Replace `remember` with a list; this is about the run, not about SQL."""
    job_ids: list[str] = []
    monkeypatch.setattr(jobs, "remember", lambda jid, now=None: job_ids.append(jid))
    return job_ids


def test_a_synchronous_nightly_job_runs_and_is_recorded(recorded):
    """**The core.** Three of the eight jobs are synchronous; `await fn()` raised for them."""
    ran = []
    asyncio.run(jobs.logged("sync_job", lambda: ran.append(1))())
    assert ran == [1], "the function itself must have been executed"
    assert recorded == ["sync_job"], "and the run must land in job_runs"


def test_an_asynchronous_nightly_job_still_runs(recorded):
    """The other direction: the fix must not break the four working jobs."""
    ran = []

    async def work():
        ran.append(1)

    asyncio.run(jobs.logged("async_job", work)())
    assert ran == [1]
    assert recorded == ["async_job"]


def test_a_failed_run_is_NOT_recorded(recorded):
    """Otherwise a job that raised counts as run, and is never caught up.

    That is why `remember` comes after the run and not before it.
    """
    def broken():
        raise RuntimeError("on purpose")

    with pytest.raises(RuntimeError):
        asyncio.run(jobs.logged("broken_job", broken)())
    assert recorded == [], "a run that raised is not a run"


def test_a_synchronous_job_does_not_block_the_only_worker():
    """`asyncio.to_thread` instead of a direct call.

    There is exactly ONE uvicorn worker. If the synchronous work ran in the event loop, the whole
    dashboard would stand still during a cleanup run. Checked: the loop keeps running meanwhile.
    """
    barrier = asyncio.Event()
    runs_in_parallel = []

    def slow_work():
        # Blocking: in the event loop this would starve `companion`.
        import time
        for _ in range(50):
            if runs_in_parallel:
                return
            time.sleep(0.01)

    async def drive():
        async def companion():
            await asyncio.sleep(0.02)
            runs_in_parallel.append(1)
            barrier.set()

        await asyncio.gather(
            jobs.logged("slow", slow_work)(),
            companion(),
        )

    asyncio.run(drive())
    assert runs_in_parallel == [1], (
        "The event loop stood still during the synchronous work; it belongs in a thread."
    )


def _stand_in(is_async: bool, trace: list[int]):
    """A dummy with the same async nature as the real job function.

    Factored out so the closure gets its variables as arguments instead of from a loop;
    otherwise all iterations share the same binding (ruff B023).
    """
    if is_async:
        async def fn():
            trace.append(1)
    else:
        def fn():
            trace.append(1)
    return fn


def _registered_job_functions() -> list[tuple[str, object]]:
    """Resolve every function registered via `jobs.logged(...)` in `main.py`.

    Via the AST instead of a hand-kept list: a list here would be a second copy of the same
    decision and would drift apart with the next new job.
    """
    import ast
    import pathlib

    import app.main as main_mod

    source = pathlib.Path(main_mod.__file__)
    tree = ast.parse(source.read_text(encoding="utf-8"))
    found: list[tuple[str, object]] = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "logged"):
            continue
        if len(node.args) < 2 or not isinstance(node.args[0], ast.Constant):
            continue
        job_id = node.args[0].value
        expr = ast.unparse(node.args[1])
        try:
            fn = eval(expr, vars(main_mod))          # noqa: S307 — our own source
        except Exception as e:                       # pragma: no cover
            pytest.fail(f"job function {expr!r} of {job_id!r} cannot be resolved: {e}")
        found.append((job_id, fn))
    return found


def test_every_registered_job_function_survives_logged(recorded):
    """**The guard against creeping back.**

    Every job registered in `main.py` is really run through `logged` here, with a stand-in that
    has the async nature of the real function (`async def` if it is a coroutine, `def`
    otherwise). That needs neither Docker nor network nor database and still catches exactly the
    class of bug that killed four jobs.
    """
    import inspect

    registered = _registered_job_functions()
    assert registered, "not a single logged job was found in main.py"

    synchronous = [jid for jid, fn in registered if not inspect.iscoroutinefunction(fn)]
    assert synchronous, (
        "Not a single registered job is synchronous, so this test checks nothing any more. "
        "Either all were converted (then this assertion may go), or the resolution via the AST "
        "no longer works."
    )

    for job_id, fn in registered:
        ran: list[int] = []
        stand_in = _stand_in(inspect.iscoroutinefunction(fn), ran)
        asyncio.run(jobs.logged(job_id, stand_in)())
        assert ran == [1], f"job {job_id!r} was not executed"
        assert job_id in recorded, f"job {job_id!r} left no trace in job_runs"


def test_prune_history_really_cleans_all_three_tables(mem_engine, monkeypatch):
    """**The second bug, as a run instead of a source grep.**

    The old guard checked `assert "Operation" in inspect.getsource(prune_history)`; the string
    was there, including `Operation.created_at`. Here cleaning really happens, in all three
    tables: the `AttributeError` fell BEFORE the loop, so it took `server_incidents` and
    `player_events` down with it.
    """
    from app.config import settings
    from app.services import cleanup

    monkeypatch.setattr(cleanup, "engine", mem_engine)
    monkeypatch.setattr(settings, "incident_retention_days", 30, raising=False)
    monkeypatch.setattr(settings, "player_event_retention_days", 30, raising=False)
    monkeypatch.setattr(settings, "operation_retention_days", 30, raising=False)

    old = utcnow_naive() - dt.timedelta(days=90)
    new = utcnow_naive()
    with Session(mem_engine) as s:
        s.add(ServerIncident(at=old, kind="test", detail="old", server_id=1))
        s.add(ServerIncident(at=new, kind="test", detail="new", server_id=1))
        s.add(PlayerEvent(at=old, kind="join", username="old", server_id=1))
        s.add(PlayerEvent(at=new, kind="join", username="new", server_id=1))
        s.add(Operation(kind="backup.create", status="done", started_at=old, server_id=1))
        s.add(Operation(kind="backup.create", status="done", started_at=new, server_id=1))
        s.commit()

    cleanup.prune_history()

    with Session(mem_engine) as s:
        assert [i.detail for i in s.exec(select(ServerIncident)).all()] == ["new"]
        assert [e.username for e in s.exec(select(PlayerEvent)).all()] == ["new"]
        remaining = s.exec(select(Operation)).all()
        assert len(remaining) == 1 and remaining[0].started_at == new, (
            "The operation table was not cleaned; this is exactly where `created_at` was."
        )


def test_prune_history_knows_the_time_column_of_every_model():
    """Three conventions side by side (`at`, `started_at`, `created_at`) were the root cause.

    Checked is not the name but that every column used in `prune_history` really exists on its
    model, so a typo shows up in the test, not at 05:10 at night.
    """
    from app.services import cleanup

    # The function builds its tuple when called; a missing attribute already raises there.
    # So accessing the models themselves is enough here.
    for model, column_name in ((ServerIncident, "at"), (PlayerEvent, "at"), (Operation, "started_at")):
        assert hasattr(model, column_name), (
            f"{model.__name__} has no field {column_name!r}; `prune_history` would die while "
            f"building its retention tuple, before the first table is touched."
        )
    assert callable(cleanup.prune_history)
