"""Start and observe long-running operations.

An operation is started and keeps running in the background even if the browser loses its
connection. The caller gets an ID back immediately and polls the progress.

Deliberately simple: one database row plus one `asyncio` task. No queue, no worker process — the
backend runs as **one** worker anyway (single-worker invariant), and the locks in
`mods/apply.py` and `version_manager.py` already prevent concurrent operations of the same kind.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import Awaitable, Callable

from fastapi import HTTPException
from sqlmodel import Session, select

from ..db import engine
from ..errors import note
from ..models.operation import FAILED, OK, RUNNING, Operation
from ..models.player import utcnow_naive

log = logging.getLogger("uvicorn.error")

# Hold references to running tasks so the garbage collector does not collect them.
_tasks: dict[int, asyncio.Task] = {}


class Progress:
    """Progress reporter handed to the actual work.

    Robust on purpose: a failure to write the progress must never abort the operation itself —
    the display is incidental, the work is not.
    """

    def __init__(self, op_id: int) -> None:
        self.op_id = op_id

    def step(self, text: str, *, current: int = 0, total: int = 0) -> None:
        try:
            with Session(engine) as s:
                row = s.get(Operation, self.op_id)
                if row is None or row.status != RUNNING:
                    return
                row.step = text[:200]
                if total:
                    row.current, row.total = current, total
                s.add(row)
                s.commit()
        except Exception:
            log.debug("operations: progress could not be saved", exc_info=False)


RESULT_MAX = 4000


def _result_json(result: dict | None) -> str:
    """The result as JSON — **valid** JSON, even when it is too long.

    Slicing the string (`json.dumps(...)[:4000]`) would cut in the middle of a string or array
    and leave the operation row permanently unreadable for `json.loads`, although the operation
    itself succeeded. The limit is reachable: the mod-file change returns three unbounded file
    lists.

    So the content is shortened, not the string, and the result itself says that it was
    shortened.
    """
    text = json.dumps(result or {}, ensure_ascii=False)
    if len(text) <= RESULT_MAX:
        return text
    brief = {
        "truncated": True,
        "note": note("operation.result_truncated",
                     "The result was {length} characters long and does not fit into the operation row.",
                     length=len(text)),
    }
    # Keep what can be kept cheaply: the short fields the UI depends on.
    for key in ("summary", "error", "blockers", "name", "restored", "safety_backup"):
        value = (result or {}).get(key)
        if value is None:
            continue
        candidate = {**brief, key: value}
        if len(json.dumps(candidate, ensure_ascii=False)) <= RESULT_MAX:
            brief = candidate
    return json.dumps(brief, ensure_ascii=False)


def _serialize(row: Operation) -> dict:
    try:
        result = json.loads(row.result or "{}")
        if not isinstance(result, dict):
            result = {"value": result}
    except (ValueError, TypeError):
        # Safety net below `_result_json`: a row written by an older version may already be
        # broken in the database. It must not turn the request into a 500 — the operation's
        # outcome is known, only its attachment is lost.
        log.warning("operations[%s]: result JSON cannot be read", row.id)
        result = {"unreadable": True}
    return {
        "id": row.id,
        "kind": row.kind,
        "status": row.status,
        "step": row.step,
        "current": row.current,
        "total": row.total,
        "percent": row.percent,
        "detail": row.detail,
        "result": result,
        "started_at": row.started_at.isoformat(),
        "finished_at": row.finished_at.isoformat() if row.finished_at else None,
        "running": row.status == RUNNING,
    }


def _finish(op_id: int, status: str, *, detail: str = "", result: dict | None = None) -> None:
    with Session(engine) as s:
        row = s.get(Operation, op_id)
        if row is None:
            return
        row.status = status
        row.detail = detail[:1000]
        row.result = _result_json(result)
        row.finished_at = utcnow_naive()
        s.add(row)
        s.commit()


def start(server, kind: str, work: Callable[[Progress], Awaitable[dict]]) -> dict:
    """Create an operation and start it in the background. Returns the operation's ID immediately.

    **The caller MUST be an `async def` endpoint.** FastAPI runs synchronous endpoints in a
    thread pool where no event loop is running — `asyncio.create_task` further down would raise
    `RuntimeError: no running event loop`.

    The check comes **before** the database row is created, and that is the point: otherwise the
    row would be created, `create_task` would raise, the endpoint would end in HTTP 500 — and the
    row would stay "running" forever, so every further attempt would hit the 409 guard until the
    backend restarts.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        raise RuntimeError(
            f"operations.start('{kind}') was called from a SYNCHRONOUS endpoint. "
            "The endpoint must be `async def` — otherwise there is no event loop in which "
            "the background operation could run."
        ) from None

    with Session(engine) as s:
        row = Operation(server_id=server.id, kind=kind, step="preparing")
        s.add(row)
        s.commit()
        s.refresh(row)
        op_id = row.id
        snapshot = _serialize(row)

    async def run() -> None:
        # Two separate `try` blocks: the inner one catches errors of the WORK, the outer one only
        # those of the bookkeeping. With `_finish(OK, …)` in the same `try` as the work, a failed
        # write (DB hiccup) would report a SUCCESSFUL operation as failed, with the DB error as
        # the "reason" — and if that second write failed too, the exception would escape the
        # task and leave the row "running" forever.
        try:
            try:
                result = await work(Progress(op_id))
            except HTTPException as e:
                # The work functions report errors as HTTPException — the plain text is what
                # counts here. The structured form (`{"error": …, "blockers": [...]}`) comes from
                # the preflight and is rendered by the frontend; it must not be boiled down to
                # one sentence, or the operator learns THAT it failed but not WHY (the stable
                # blocker codes exist for exactly that).
                detail = e.detail
                payload = detail if isinstance(detail, dict) else None
                if isinstance(detail, dict):
                    # A coded text (`app/errors.py`) carries its English sentence in `message`.
                    detail = (detail.get("error") or detail.get("message")
                              or json.dumps(detail, ensure_ascii=False))
                    # "The server is now OFF" (`lifecycle.ensure_restart`) travels as a field of
                    # its own; the panel shows only this text, so it has to be in it.
                    warning = payload.get("restart_warning")
                    if isinstance(warning, str) and warning and warning not in str(detail):
                        detail = f"{detail} | {warning}"
                # Log the EXPECTED failures too: they are what one looks for when someone asks
                # "why did that not work".
                log.warning("operations[%s/%s]: rejected — %s", kind, op_id, detail)
                _finish(op_id, FAILED, detail=str(detail), result=payload)
                return
            except Exception as e:
                log.exception("operations[%s/%s]: failed", kind, op_id)
                _finish(op_id, FAILED, detail=str(e))
                return

            # From here on the work is DONE. Anything that fails now is bookkeeping.
            if not isinstance(result, dict):
                result = {"value": result}
            _finish(op_id, OK, detail=_final_text(result, kind), result=result)
        except Exception:
            # The bookkeeping itself failed. Say so, but do NOT report it as a failure of the
            # work — `cancel_stale` closes the row on the next start.
            log.exception("operations[%s/%s]: completion could not be written", kind, op_id)
        finally:
            _tasks.pop(op_id, None)

    _tasks[op_id] = asyncio.create_task(run())
    return snapshot


def get(op_id: int, server=None) -> dict | None:
    with Session(engine) as s:
        row = s.get(Operation, op_id)
        if row is None or (server is not None and row.server_id != server.id):
            return None
        return _serialize(row)


def latest(server, kind: str | None = None) -> dict | None:
    """Newest operation of a server — lets the UI reattach after a page reload."""
    with Session(engine) as s:
        query = select(Operation).where(Operation.server_id == server.id)
        if kind:
            query = query.where(Operation.kind == kind)
        row = s.exec(query.order_by(Operation.id.desc())).first()
        return _serialize(row) if row else None


KIND_LABELS = {
    "mods.apply": "The mod change was applied.",
    "mods.files": "The mod files were applied.",
    "backup.create": "The backup was created.",
    "backup.restore": "The backup was restored.",
    "version.change": "The version change is complete.",
    "version.recreate": "The container was recreated with the saved settings.",
}


def _final_text(result: dict, kind: str) -> str:
    """The final text — never empty.

    If the work function returns no summary, an empty detail would leave the UI showing the last
    "running" line, and the operation would look as if it never finished.

    A summary with a value in it is coded (`errors.note`): the detail gets its English sentence,
    and the frontend translates `result.summary` by its code.
    """
    summary = result.get("summary")
    if isinstance(summary, dict):
        summary = summary.get("message")
    return str(summary or KIND_LABELS.get(kind) or "Done.")


def running(server, kind: str) -> dict | None:
    """The running operation of this kind — not the newest one.

    `latest(server, kind)` plus a check for `running` is not equivalent: after a double click
    the newest operation is the rejected one (the first still runs, the second fails at once
    with 409 and gets the higher ID). The UI would then show the failure while the real
    operation is running, and the 409 guard, which relies on `running()`, would let the next
    operation in.
    """
    with Session(engine) as s:
        query = (select(Operation)
                 .where(Operation.server_id == server.id)
                 .where(Operation.kind == kind)
                 .where(Operation.status == RUNNING))
        row = s.exec(query.order_by(Operation.id.desc())).first()
        return _serialize(row) if row else None


_RESTART_TEXT = ("The backend was restarted while this operation was running — "
                  "its outcome is unknown. Please check the server state.")


async def cancel_stale() -> None:
    """On startup: close operations that did not survive a backend restart.

    Otherwise an operation would stay "running" forever and the UI would wait for a result that
    never comes.

    **There are three ledgers, not one.** Besides the `operations` row, every world-writing
    operation keeps a second record: `version_manager._record_pending` creates
    `VersionChange(status="pending")` and `mods/apply._record` creates
    `ModChange(status="pending")` — and both are closed only in their own success or rollback
    branch. If the process dies in between (e.g. a deploy during a version change), neither
    branch ever runs, so they are closed here as well. That row is the one that says whether the
    rollback ran (`ok`/`rolled_back`/`rolled_back_partial`/`failed`), so it must not stay
    "pending".

    The status set here is **`unknown`**, not `failed`: "failed" would be a statement about the
    outcome, which is exactly what is not known. Both history views render an unrecognised status
    verbatim in a neutral tone, so the extra value is safe there.
    """
    from ..models.mod_set import ModChange
    from ..models.version_change import VersionChange

    with contextlib.suppress(Exception):
        with Session(engine) as s:
            for row in s.exec(select(Operation).where(Operation.status == RUNNING)).all():
                if row.id in _tasks:
                    continue
                row.status = FAILED
                row.detail = _RESTART_TEXT
                row.finished_at = utcnow_naive()
                s.add(row)
            # Nothing runs at startup, so EVERY open row here is stale. `_tasks` is empty at this
            # point — the check above is still right, it protects a later call.
            for model in (VersionChange, ModChange):
                for line in s.exec(select(model).where(model.status == "pending")).all():
                    line.status = "unknown"
                    line.detail = _RESTART_TEXT
                    s.add(line)
            s.commit()
