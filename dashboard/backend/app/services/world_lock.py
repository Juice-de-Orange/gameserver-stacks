"""One lock per server across ALL operations that touch world files.

**Why it exists.** Locks per operation kind (and `operations.running(server, <kind>)` checks in
the routers) only protect an operation against its own kind. Two browser tabs are then enough:
start "apply mods" (which waits up to 900 s for the boot check) and, in the second tab, "restore
backup" or "change version". Each endpoint sees its own kind as free. The second takes its
mandatory backup of a half-rearranged world and stops and removes the container whose boot the
first is waiting for — and the first then runs its rollback over the second one's files. All of
these are exactly the paths with a mandatory backup and an assumed way back.

**The lock order is binding: `world_lock` OUTSIDE, `lifecycle` INSIDE.** `asyncio.Lock` is not
reentrant, and every world-writing operation calls `stop_and_confirm` → `lifecycle._lock` in
its middle. Reversing the order builds a deadlock that only shows in production — as a server
that can no longer be stopped.

Process-local, like all locks here (single-worker invariant).
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

from ..errors import err

_locks: dict[str, asyncio.Lock] = {}
_holders: dict[str, str] = {}


def _lock(server_key: str) -> asyncio.Lock:
    lock = _locks.get(server_key)
    if lock is None:
        lock = _locks[server_key] = asyncio.Lock()
    return lock


def refuse_during_world_operation(server_key: str, what: str) -> None:
    """409 while a world-writing operation is running for this server.

    **The rule belongs here, not with the callers**, so that it covers start/restart AND the
    config editor's write routes: on rollback `mods/apply._restore_config` writes back the
    COMPLETE `.ini` from before the operation, and a mod apply can take up to 20 minutes. A
    setting saved in that window would be lost without a trace — with an audit entry for a
    change that no longer exists.

    **The dangerous phase is the one in which the container is RUNNING.**
    `mods/apply._apply_locked` and `version_manager` wait up to 900 s in `verify_boot` after
    their start. A restart in the middle makes `verify_boot` see `running=False` and rolls back
    a HEALTHY change; an `.ini` write in the middle is overwritten by the rollback.

    Callers never hold the lock themselves, so a `held_by` test is safe here.
    """
    from fastapi import HTTPException, status as http

    if (holder := held_by(server_key)):
        raise HTTPException(
            http.HTTP_409_CONFLICT,
            detail=err("world_lock.action_rejected",
                       "{action} rejected: {operation} is currently running for this server. "
                       "Intervening in the middle of its boot check would make it look failed "
                       "and roll back a healthy state.",
                       action=what, operation=OPERATION_LABELS.get(holder, holder)),
        )


def held_by(server_key: str) -> str | None:
    """Which operation currently holds the lock — or `None`.

    Lets the routers answer 409 **before** creating an operation row, naming the blocking
    operation. Reporting "a mod change is already running" when it is really a version change
    sends the operator looking in the wrong place.
    """
    lock = _locks.get(server_key)
    if lock is None or not lock.locked():
        # `_holders` is the second source and is set synchronously; ignoring it because the
        # `Lock` currently says "free" would reopen the hand-over gap described in `hold`.
        return _holders.get(server_key)
    # Fallback when the lock is held but no name is recorded. Without it the caller would run
    # into the held lock and wait there — a clean 409 would become a hanging HTTP request, the
    # worst outcome: the operator sees a spinner instead of an answer.
    return _holders.get(server_key) or "another operation"


@asynccontextmanager
async def hold(server_key: str, operation: str):
    """Hold the world lock for the duration of the block — or abort with 409.

    Example:

        async with world_lock.halten(server.key, "mods.apply"):
            ...   # stop, back up, write, boot

    **This context manager does not wait, it rejects.** Waiting would mean carrying on with
    **stale measurements**: each of these operations gathers its plan (preflight, world file
    list, run state, mandatory backup) BEFORE it reaches the lock. If its turn came ten minutes
    later, it would work against a world another operation has half rearranged in the meantime.
    A rejected operation can be triggered again; one that ran through with a wrong plan cannot.

    Rejecting also closes the **hand-over gap**: `asyncio.Lock.release()` sets `_locked = False`
    and wakes a waiter only via callback — during that time `held_by` would report "free"
    although a world-writing operation is already waiting. Without waiters the gap does not
    exist: there is no `await` between the check here and setting `_holders`, and a single-worker
    loop does not interrupt in between.

    **What is NOT closed**: the lead-in window. `operations.start` creates the operation row and
    schedules the task; until its work function arrives here, the lock is free for `held_by`,
    and a router lets a second operation through. That one fails **here**, though — with 409 and
    without having touched the world — instead of carrying on later with an old plan.
    """
    from fastapi import HTTPException, status as http

    if (running := held_by(server_key)):
        raise HTTPException(
            http.HTTP_409_CONFLICT,
            detail=err("world_lock.operation_not_executed",
                       "Already running for this server: {operation} — the operation was not "
                       "executed. Please wait and trigger it again; its preparation (check, "
                       "file list, run state) dates from before this point and would no "
                       "longer be valid afterwards.",
                       operation=OPERATION_LABELS.get(running, running)),
        )
    lock = _lock(server_key)
    # No `await` between `held_by` and here: after the check above `acquire()` cannot block, and
    # `_holders` is set before the loop lets anyone else run.
    await lock.acquire()
    _holders[server_key] = operation
    try:
        yield
    finally:
        _holders.pop(server_key, None)
        lock.release()


# Plain-text names for the 409 message; without this table the message would show the internal
# operation name (e.g. `version.change`).
OPERATION_LABELS = {
    "mods.apply": "a mod change",
    "mods.files": "a mod file change",
    "version.change": "a version change",
    "version.recreate": "a container recreate",
    "backup.restore": "a restore",
    "reforger.mode": "a save/mode change",
    "quarantine.restore": "a restore from quarantine",
    "quarantine.delete": "a deletion of a quarantine",
    "restart.backup": "the mandatory backup before the scheduled restart",
}
