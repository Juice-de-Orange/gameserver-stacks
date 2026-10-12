"""Restore a backup — safeguarded like a version change.

A restore overwrites world data, so it follows the same sequence as `mods/apply.py` and
`version_manager.py`:

- **a safety backup first** — picking the wrong archive must not lose the current state.
- **replace, not overlay** — `backup.extract_backup` swaps each archived folder for its archived
  version; nothing created since the archive survives in it.
- **a confirmed stop** — not just `docker stop`, but a check that the container really is down.
- **boot verification** — after the start, check that the server comes back.
- **`watchdog.suppress`** — a long boot after the restore must not count as a hang.
"""
from __future__ import annotations

import asyncio
import logging
import os

from fastapi import HTTPException, status as http

from ..errors import err, note

from . import backup as bk
from . import lifecycle
from .mods import state as mod_state
from . import world_lock
from .docker_client import docker
from .version_manager import verify_boot

log = logging.getLogger("uvicorn.error")

SUPPRESS_MINUTES = 20
BOOT_TIMEOUT = 900.0


async def restore(server, driver, name: str, *, subdirs: tuple[str, ...] | None = None,
                  progress=None) -> dict:
    """Restore a backup. Raises HTTPException with a plain-text reason; the server is never left
    silently broken.

    The world lock is TAKEN here, not just queried — same shape as in `mods/apply.py` and
    `version_manager.py`. Checking `held_by()` alone is not enough: a mod apply starting a second
    later would see the lock free and run **over the same world at the same time**, both with a
    mandatory backup and an assumed way back, both on the same files.
    `tests/test_world_lock_complete.py` pins this down.

    The cheap checks (name, archive exists, parts contained) are deliberately INSIDE the lock:
    nobody must get in between "the archive contains what you want" and the stop.
    """
    if (holder := world_lock.held_by(server.key)):
        raise HTTPException(
            http.HTTP_409_CONFLICT,
            detail=err("world_lock.busy_please_wait",
                       "Already running for this server: {operation} — please wait.",
                       operation=world_lock.OPERATION_LABELS.get(holder, holder)),
        )
    async with world_lock.hold(server.key, "backup.restore"):
        return await _restore_locked(server, driver, name, subdirs=subdirs, progress=progress)


async def _restore_locked(server, driver, name: str, *, subdirs: tuple[str, ...] | None = None,
                          progress=None) -> dict:
    step = progress.step if progress is not None else (lambda *a, **k: None)

    if not bk.valid_backup_name(name):
        raise HTTPException(http.HTTP_400_BAD_REQUEST, detail=err("restore.backup_name_invalid", "Invalid backup name"))
    spec = driver.backup_spec(server)
    if not os.path.exists(bk.backup_path(name, data_dir=spec.data_dir)):
        raise HTTPException(http.HTTP_404_NOT_FOUND, detail=err("restore.backup_not_found", "Backup not found"))

    # 0. Can the archive be restored at all? Cheap, and must come BEFORE the stop.
    #
    # `extract_backup` checks this itself — but only further down, when the server is already
    # stopped and the safety archive is written. Its `except` branch then raises 500 with "the
    # server stays stopped" and the restart is never reached (e.g. a partial restore asking for
    # a folder the archive of this game does not contain).
    #
    # The check applies to BOTH the partial and the full restore, each with its own rule: the
    # full restore overwrites the WHOLE world and needs it most.
    await asyncio.to_thread(
        bk.check_restorable, name,
        data_dir=spec.data_dir, world_subdirs=spec.world_subdirs, subdirs=subdirs,
    )

    # 1. Stop, CONFIRM the stop, THEN back up — in that order.
    #
    # PZ has no quiesce hooks (`pz.backup_spec` leaves `pre_hook`/`post_hook` at the base
    # defaults), so a safety backup taken before the stop would be a hot copy of a world being
    # written — and that archive is the way back once `extract_backup` has overwritten the
    # current state. It would also leave an archive behind when the stop fails (409).
    step("Stopping the server and backing up the current state…")
    was_running, safety = await lifecycle.stop_and_snapshot(
        server, driver, "pre-restore", suppress_minutes=SUPPRESS_MINUTES,
    )

    # 3. Restore
    step("Restoring the backup…")
    try:
        left = await asyncio.to_thread(bk.extract_backup, name, data_dir=spec.data_dir, subdirs=subdirs)
    except Exception as e:
        raise HTTPException(
            http.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=err("restore.extract_failed",
                       "Restore failed ({error}). The server stays stopped, the data may be "
                       "incomplete — the backup “{safety}” holds the previous state.",
                       error=str(e), safety=safety["name"]),
        ) from e

    # The world is restored; a replaced version that could not be deleted (`extract_backup`)
    # only costs space — but a whole world's worth, in a place only this note names. Files a
    # game wrote as root are deleted by the cleanup helper first.
    if left:
        from . import provisioning

        left = await provisioning.remove_leftovers_as_root(server, left)
    left_note = note(
        "restore.old_copy_left",
        "The replaced version could not be deleted and still takes up space: {paths}. "
        "Remove it by hand.",
        paths=", ".join(left),
    ) if left else None

    # 4. Start and verify the boot
    if not was_running:
        stays_stopped = "The server was stopped before and stays stopped."
        return {"ok": True, "restored": name, "safety_backup": safety["name"], "started": False,
                "note": [stays_stopped, left_note] if left_note else stays_stopped}

    # The boot gate — here, where it matters most. `extract_backup` has just written `Server/`
    # back, which contains the `<name>.ini` with `Mods=`, `WorkshopItems=` and `Map=`. If the
    # archive is older than the last mod change, the restored `.ini` may list a map whose
    # download has been quarantined in the meantime.
    #
    # `verify_boot` does NOT catch that: it only proves that RCON answers, and a map without a
    # provider crashes clients on JOIN. The server would come up, the response would say "backup
    # restored", and every player would be thrown out while connecting.
    #
    # The server stays stopped in that case: a running server that kills every join is worse
    # than a stopped one with a message that says what to do — the same trade-off as in
    # `routers/server.perform_action`.
    if (blockers := await mod_state.guard_boot(server)):
        raise HTTPException(
            http.HTTP_409_CONFLICT,
            detail={
                **err("restore.boot_blocked",
                      "{reason} The backup “{name}” has been restored, the server stays "
                      "stopped. The previous state is available as “{safety}”.",
                      reason=mod_state.guard_message(blockers), name=name, safety=safety["name"]),
                "blockers": [vars(b) for b in blockers],
            },
        )

    step("Server is starting — verifying the boot…")
    # The start belongs in the same protected block as the boot check. `docker.action` is an
    # httpx POST with `raise_for_status()` — a ConnectError or a 500 from the socket proxy would
    # otherwise escape raw and take the whole restore message with it, including the NAME of
    # the safety archive, which is exactly what is needed in that situation.
    try:
        await docker.action(server.container_name, "start")
        came_up = await verify_boot(server, driver, timeout=BOOT_TIMEOUT)
    except Exception as e:
        log.error("%s: start after the restore failed: %s", server.key, e)
        came_up = False
    if not came_up:
        # The way back is EXECUTED here, not just announced, as in
        # `version_manager._rollback_swap` and `mods/apply._rollback`. `verify_boot` returns
        # `False` among other things when the restart counter rises, i.e. in a crash loop. The
        # containers run `unless-stopped` and the watchdog deliberately does not act on
        # `not running`, so without this rollback the server would keep flapping indefinitely
        # after the world lock is released.
        step("Boot failed — restoring the previous state…")
        steps: list[str] = []
        try:
            _, stopped = await lifecycle.stop_and_confirm(server)
            if not stopped:
                steps.append("the container could not be stopped with confirmation")
            else:
                steps.append("container stopped")
                await asyncio.to_thread(bk.extract_backup, safety["name"], data_dir=spec.data_dir)
                steps.append("previous state restored")
                if was_running:
                    await docker.action(server.container_name, "start")
                    steps.append("server started again")
        except Exception as e:  # noqa: BLE001 — the reason belongs in the message, not only in the log
            log.error("%s: way back after the failed restore incomplete: %s", server.key, e)
            steps.append(f"ABORTED: {e}")

        raise HTTPException(
            http.HTTP_502_BAD_GATEWAY,
            detail=err("restore.boot_failed",
                       "The server did not come up after restoring “{name}”. Way back: {steps}. "
                       "The backup of the previous state is called “{safety}”.",
                       name=name, steps="; ".join(steps), safety=safety["name"]),
        )
    return {"ok": True, "restored": name, "safety_backup": safety["name"], "started": True,
            "summary": note("restore.done", "Backup {name} restored", name=name),
            **({"note": left_note} if left_note else {})}
