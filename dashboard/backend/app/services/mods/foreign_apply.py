"""Activate a file-based mod selection (Minecraft / Vintage Story).

The little sibling of `apply.py`: there are no world cells and no maps here, but the same
promise holds: either the change takes effect and the server comes up, or afterwards
everything is as it was before.

What makes the flow simpler than for PZ: **nothing in the world is touched**. The way back is
therefore simply moving the files back.
"""
from __future__ import annotations

import asyncio
import logging

from fastapi import HTTPException, status as http

from ...errors import err
from .. import lifecycle
from ..docker_client import docker
from .. import watchdog
from .. import world_lock
from ..drivers import get_driver
from ..version_manager import verify_boot
from . import foreign

log = logging.getLogger("uvicorn.error")

SUPPRESS_MINUTES = 15
BOOT_TIMEOUT = 900.0



async def apply(server, enabled: set[str], *, progress=None) -> dict:
    # The world lock covers ALL operations that touch world files, so that e.g. a version change
    # cannot run into the middle of this one.
    if (holder := world_lock.held_by(server.key)):
        raise HTTPException(
            http.HTTP_409_CONFLICT,
            detail=err("mods.world_operation_running",
                       "This server already has an operation running: {operation}.",
                       operation=world_lock.OPERATION_LABELS.get(holder, holder)))
    async with world_lock.hold(server.key, "mods.files"):
        try:
            return await _apply_locked(server, enabled, progress)
        finally:
            # As on the workshop path: the report is stale afterwards, even on failure.
            from .. import mod_check

            mod_check.discard(server.key)


async def _apply_locked(server, enabled: set[str], progress) -> dict:
    step = progress.step if progress is not None else (lambda *a, **k: None)
    driver = get_driver(server)

    # An unreadable mod folder aborts here, BEFORE stopping and backing up. With a silently
    # shorter list `check()` would find no blockers, and the operation would run stop, mandatory
    # backup and restart, move nothing and report success.
    try:
        present = foreign.scan(server)
    except OSError as e:
        raise HTTPException(
            http.HTTP_409_CONFLICT,
            detail=err("mods.folder_unreadable",
                       "The mod folder cannot be read ({error}) — nothing was touched.",
                       error=str(e)),
        ) from e
    before = {m.filename for m in present if m.enabled}
    if before == enabled:
        raise HTTPException(http.HTTP_400_BAD_REQUEST,
                            detail=err("mods.selection_unchanged",
                                       "The selection already matches the running server."))

    # Preflight again: something may have changed between display and click.
    step("Checking the selection…")
    result = await foreign.check(server, enabled)
    if result.blockers:
        rejected = err("mods.selection_blocked",
                        "The selection was not applied — it would damage the server.")
        raise HTTPException(http.HTTP_409_CONFLICT, detail={
            **rejected,
            "error": rejected["message"],
            "blockers": [vars(f) for f in result.blockers],
        })

    # Stop, confirm the stop, THEN back up; the announcement and the world save are part of the
    # game-appropriate shutdown (`lifecycle`). MC and VS have real quiesce hooks, so a backup
    # before the stop would do less damage than for PZ, but the order is kept identical on every
    # path so it is not copied the wrong way round.
    step("Stopping the server and backing it up…")
    was_running, backup = await lifecycle.stop_and_snapshot(
        server, driver, "pre-modfiles",
        announce_text="Server is about to restart — a mod change is being applied.",
        suppress_minutes=SUPPRESS_MINUTES,
    )

    step("Moving the mod files…")
    moved = await asyncio.to_thread(foreign.set_enabled, server, enabled)

    # Actual versus requested. The summary must be built from the files actually moved, not from
    # the REQUESTED selection: otherwise a failed `_move` would only be a missing list entry,
    # the operation would end green and the crashing plugin would still sit in `mods/`. The UI
    # shows the user only `summary`, not the lists next to it.
    if moved["failed"]:
        not_moved = ", ".join(moved["failed"])
        log.error("mods(files)[%s]: could not be moved: %s", server.key, not_moved)
        # **Roll back unconditionally**, neither only when the server was running nor only when
        # files were moved. Tied to `was_running`, a stopped server (the normal case when
        # swapping mods) would get no way back at all, and `foreign.scan` would read the
        # half-moved state as the current selection from then on. Tied to "files were moved",
        # the case where ALL moves fail (`enabled` and `disabled` empty, `failed` full) would
        # leave the server DOWN after the confirmed stop, with the watchdog silenced for
        # `suppress_minutes`.
        #
        # With nothing moved, `_rollback` is a no-op plus start, so running it unconditionally
        # costs nothing and closes both holes.
        files_restored, running = await _rollback(server, before, driver, start=was_running)
        note = _rollback_text(files_restored, running, was_running)
        # `rollback` is the finished English report of the way back (it starts with a blank).
        raise HTTPException(
            http.HTTP_502_BAD_GATEWAY,
            detail=err("mods.files_not_moved",
                       "These mod files could not be moved: {files}. The selection was NOT "
                       "applied completely.{rollback}",
                       files=not_moved, rollback=note),
        )

    if not was_running:
        # No boot proof possible: the server was not running before and is not started just for
        # that. The message says so instead of pretending safety.
        return {"status": "ok", "backup": backup["name"], **moved, "started": False,
                "summary": _summary(moved) + " — server was stopped, no boot proof"}

    step("Server is starting — the boot is being verified via RCON…")
    try:
        await docker.action(server.container_name, "start")
        if not await verify_boot(server, driver, timeout=BOOT_TIMEOUT):
            raise RuntimeError("the server did not come up with the new mod selection")
    except Exception as e:
        log.warning("mods(files)[%s]: apply failed → rollback (%s)", server.key, e)
        files_restored, running = await _rollback(server, before, driver, start=was_running)
        note = _rollback_text(files_restored, running, was_running)
        raise HTTPException(
            http.HTTP_502_BAD_GATEWAY,
            detail=err("mods.change_failed", "Mod change failed: {error}.{rollback}",
                       error=str(e), rollback=note))

    return {"status": "ok", "backup": backup["name"], **moved, "started": True,
            "summary": _summary(moved)}


def _rollback_text(files_restored: bool, running: bool, was_started: bool) -> str:
    """What the way back really achieved: both statements, not one.

    Files and server state are two questions: a file can be left behind while the server is
    running, and vice versa.
    """
    if files_restored and (running or not was_started):
        return (" The previous selection is back in place."
                + (" The server is running." if was_started else " The server was stopped "
                   "before and stays stopped."))
    parts = []
    if not files_restored:
        parts.append("files could not be moved back")
    if was_started and not running:
        parts.append("the server did NOT come back up")
    return " WARNING: rollback incomplete — " + ", ".join(parts) + ". Please check manually."


def _summary(moved: dict) -> str:
    """Built from the ACTUAL result, not from the request."""
    parts = []
    if moved["enabled"]:
        parts.append(f"{len(moved['enabled'])} enabled")
    if moved["disabled"]:
        parts.append(f"{len(moved['disabled'])} disabled")
    return ", ".join(parts) or "nothing changed"


async def _rollback(server, before: set[str], driver, *, start: bool) -> tuple[bool, bool]:
    """Move the old selection back, and bring the server up only if it was running before.

    Returns `(files_restored, running)`.

    * *Files always go back.* A file that cannot be moved must not end the rollback before
      `docker start`: the container would stay down with the watchdog silenced by
      `suppress_minutes`. A failure only sets `files_restored=False` (the PZ twin
      `apply._rollback` also starts with `complete=False`).
    * *`start` decouples the rollback from the start wish.* The caller must be able to roll
      back files on a stopped server (the normal case when swapping mods) without starting it;
      otherwise the half-moved state would be read by `foreign.scan` as the "current
      selection".
    * *Two values instead of one.* "The files are back in place" and "the server is running"
      are different statements, and the operator needs both.
    """
    _, stopped = await lifecycle.stop_and_confirm(server)
    if not stopped:
        log.error("mods(files)[%s]: container not stopped — rollback aborted", server.key)
        return False, False

    try:
        restored = await asyncio.to_thread(foreign.set_enabled, server, before)
        files_restored = not restored["failed"]
    except OSError as e:
        # `set_enabled` calls `foreign.scan` internally, which raises on an unreadable mod
        # folder. In the ROLLBACK this error must not escape: it would replace the 502 and its
        # rollback report, and the operator would see a bare traceback instead of the state the
        # server is in. So record it as "files not back" and carry on; the start below matters
        # more.
        log.error("mods(files)[%s]: the rollback could not read the files: %s", server.key, e)
        restored = {"failed": ["(mod folder not readable)"]}
        files_restored = False
    if not files_restored:
        # The rollback is incomplete; the caller must not report it as "running again". It
        # carries on regardless: a server that stays down is worse than one that runs with one
        # file too many.
        log.error("mods(files)[%s]: rollback incomplete, could not be moved: %s",
                  server.key, ", ".join(restored["failed"]))

    if not start:
        return files_restored, False

    try:
        await docker.action(server.container_name, "start")
    except Exception as e:
        log.error("mods(files)[%s]: start after rollback failed: %s", server.key, e)
        return files_restored, False
    watchdog.suppress(server.key, SUPPRESS_MINUTES)
    return files_restored, await verify_boot(server, driver, timeout=BOOT_TIMEOUT)
