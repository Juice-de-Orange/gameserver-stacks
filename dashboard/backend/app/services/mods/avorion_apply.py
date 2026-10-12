"""Activate Avorion's workshop list.

The little sibling of `foreign_apply.py`, with the same promise: either the change takes
effect and the server comes up, or afterwards everything is as it was before.

The flow is simpler than for the file-based games because there is exactly ONE file
(`modconfig.lua`): the way back is writing its old content back, and that cannot fail halfway.
What is NOT simpler: what the server makes of the list is decided by Steam at the next start.
The boot proof is therefore no formality here but the only place that shows whether the IDs
could really be loaded.
"""
from __future__ import annotations

import asyncio
import logging
import os

from fastapi import HTTPException, status as http

from ...errors import err, note
from .. import lifecycle, watchdog, world_lock
from ..docker_client import docker
from ..drivers import get_driver
from ..version_manager import verify_boot
from . import preflight_avorion

log = logging.getLogger("uvicorn.error")

SUPPRESS_MINUTES = 15
BOOT_TIMEOUT = 900.0

_HEADER = (
    "-- Written by the dashboard (Stacks Console). Manual changes are kept as long as the list\n"
    "-- below is not set again through the dashboard.\n"
)


_SETTING_RE = {
    "modLocation": __import__("re").compile(r'^\s*modLocation\s*=\s*(?P<value>.+?)\s*$', __import__("re").M),
    "forceEnabling": __import__("re").compile(r'^\s*forceEnabling\s*=\s*(?P<value>.+?)\s*$', __import__("re").M),
}


def _carry_over_settings(before: str | None) -> dict[str, str]:
    """Carry `modLocation` and `forceEnabling` over from the existing file.

    The file header promises that manual changes survive. `build_modconfig` generates the WHOLE
    file from a fixed template, so without this a manually set `forceEnabling = true` or a
    custom `modLocation` would be reset to the default on every mod apply. Nothing would catch
    that: `_rollback` only runs when the boot proof fails, and a reset `forceEnabling` does not
    prevent the start.
    """
    out: dict[str, str] = {}
    for name, pattern in _SETTING_RE.items():
        m = pattern.search(before or "")
        if m:
            out[name] = m.group("value")
    return out


def build_modconfig(ids: list[str], allowed: list[str] | None = None, *,
                   before: str | None = None) -> str:
    """Generate the file Avorion expects.

    Deliberately in exactly the form shown by the documentation and the hosting guides, with
    no formatting of our own, so the file looks familiar to someone opening it by hand.
    """
    # **The digit check lives HERE, in the shared generator**, so the preflight endpoint and the
    # apply endpoint both get it. Without it this function would write
    # `{workshopid = "<arbitrary text>"}`, `preflight_avorion._ID_RE` (digits only) would find
    # nothing, and `check()` would return an EMPTY, i.e. green, result before the Steam query:
    # "checked, no blockers" for a selection of which not a single line was checked.
    #
    # The typical mistake is named in the error message: the copied workshop URL or the title
    # instead of the ID.
    for i in list(ids) + list(allowed or []):
        if not str(i).strip().isdigit():
            raise ValueError(
                f"{i!r} is not a workshop ID. An ID consists of digits only — the number from "
                f"the address bar of the workshop page, not the title and not the whole URL."
            )
    keep = _carry_over_settings(before)
    lines = [
        _HEADER,
        f'modLocation = {keep.get("modLocation", chr(34) + chr(34))}\n',
        f'forceEnabling = {keep.get("forceEnabling", "false")}\n',
        "mods = {\n",
    ]
    lines += [f'    {{workshopid = "{i}"}},\n' for i in ids]
    lines.append("}\n")
    lines.append("allowed = {\n")
    lines += [f'    {{workshopid = "{i}"}},\n' for i in (allowed or [])]
    lines.append("}\n")
    return "".join(lines)


def _path(server) -> str:
    from ..drivers.avorion import AvorionDriver

    return os.path.join(AvorionDriver()._galaxy(server), "modconfig.lua")


def _read_raw(server) -> str | None:
    """The current file content; `None` if there is no file yet."""
    try:
        with open(_path(server), encoding="utf-8", errors="replace") as f:
            return f.read()
    except FileNotFoundError:
        return None


def _write(server, content: str) -> None:
    path = _path(server)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".incoming"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(content)
    os.replace(tmp, path)      # atomic: never expose half a file


async def apply(server, ids: list[str], allowed: list[str] | None = None, *, progress=None) -> dict:
    if (holder := world_lock.held_by(server.key)):
        raise HTTPException(
            http.HTTP_409_CONFLICT,
            detail=err("mods.world_operation_running",
                       "This server already has an operation running: {operation}.",
                       operation=world_lock.OPERATION_LABELS.get(holder, holder)))
    async with world_lock.hold(server.key, "mods.files"):
        return await _apply_locked(server, ids, allowed, progress)


async def _apply_locked(server, ids: list[str], allowed, progress) -> dict:
    step = progress.step if progress is not None else (lambda *a, **k: None)
    driver = get_driver(server)

    raw_before = await asyncio.to_thread(_read_raw, server)
    # **Compare both lists.** Comparing only the mods would reject a change that affects only
    # the `allowed` list as "already matches the running server", and that is exactly the change
    # the preflight asks for: `AV_NOT_ALLOWED` warns when mods are missing from `allowed`.
    ids_before, allowed_before = (
        preflight_avorion.ids_and_allowed(raw_before)[:2] if raw_before else ([], [])
    )
    if ids_before == ids and allowed_before == list(allowed or []):
        raise HTTPException(http.HTTP_400_BAD_REQUEST,
                            detail=err("mods.list_unchanged",
                                       "The list already matches the running server."))

    new = build_modconfig(ids, allowed, before=raw_before)

    # Preflight against the NEW state, which has to be on disk for that. It is therefore written
    # to a copy, not to the real file: a blocker must leave nothing behind.
    step("Checking the workshop list…")
    result = await _check_draft(server, new)
    if result.blockers:
        rejected = err("mods.list_blocked",
                        "The list was not applied — it would damage the server.")
        raise HTTPException(http.HTTP_409_CONFLICT, detail={
            **rejected,
            "error": rejected["message"],
            "blockers": [vars(f) for f in result.blockers],
        })

    step("Stopping the server and backing it up…")
    was_running, backup = await lifecycle.stop_and_snapshot(
        server, driver, "pre-avorionmods",
        announce_text="Server is about to restart — a mod change is being applied.",
        suppress_minutes=SUPPRESS_MINUTES,
    )

    step("Writing modconfig.lua…")
    try:
        await asyncio.to_thread(_write, server, new)
    except OSError as e:
        # Nothing was touched except perhaps an `.incoming` file, but the server is stopped and
        # the watchdog is silenced. Both must be undone before the message goes out.
        running = await _restart(server, driver, start=was_running)
        # **"Not started because it is not supposed to run" is not a failure.** `_restart`
        # returns `False` immediately for `start=False`; reading that as the server state
        # would raise an alarm about a state that never occurred, which is the normal case when
        # swapping mods on a stopped server. `foreign_apply._rollback_text` makes the same
        # distinction.
        if not was_running:
            detail = err("mods.modconfig_write_failed_stays_stopped",
                         "modconfig.lua could not be written ({error}). Nothing was changed. "
                         "The server was stopped before and stays stopped.",
                         error=str(e))
        elif running:
            detail = err("mods.modconfig_write_failed_running_again",
                         "modconfig.lua could not be written ({error}). Nothing was changed. "
                         "The server is running again.",
                         error=str(e))
        else:
            detail = err("mods.modconfig_write_failed_server_down",
                         "modconfig.lua could not be written ({error}). Nothing was changed. "
                         "WARNING: the server did NOT come back up.",
                         error=str(e))
        raise HTTPException(http.HTTP_502_BAD_GATEWAY, detail=detail) from e

    if not was_running:
        return {"status": "ok", "backup": backup["name"], "ids": ids, "started": False,
                "summary": note("mods.avorion_entered_no_boot_proof",
                                "{mods} mod(s) entered — server was stopped, no boot proof",
                                mods=len(ids))}

    step("Server is starting — the boot is being verified…")
    try:
        await docker.action(server.container_name, "start")
        if not await verify_boot(server, driver, timeout=BOOT_TIMEOUT):
            raise RuntimeError("the server did not come up with the new mod list")
    except Exception as e:
        log.warning("mods(avorion)[%s]: apply failed → rollback (%s)", server.key, e)
        restored, running, stopped = await _rollback(server, raw_before, driver, start=was_running)
        # `rollback` is the finished English report of the way back (it starts with a blank).
        raise HTTPException(
            http.HTTP_502_BAD_GATEWAY,
            detail=err("mods.change_failed", "Mod change failed: {error}.{rollback}",
                       error=str(e),
                       rollback=_rollback_text(restored, running, stopped, was_running,
                                               backup.get("name", ""))))

    return {"status": "ok", "backup": backup["name"], "ids": ids, "started": True,
            "summary": note("mods.avorion_active", "{mods} mod(s) active", mods=len(ids))}


async def _check_draft(server, content: str):
    """Check the draft without touching the real file.

    `preflight_avorion.check` reads from disk, but the draft only exists in memory. Instead of
    giving the preflight a second path, it gets a server whose galaxy path points at a
    throwaway directory. That way EXACTLY the same code runs that later checks the live state.
    """
    import tempfile
    import types

    def _put_in_place(tmp: str) -> None:
        """File work bundled, so it runs in the thread pool, not in the event loop.

        With ONE uvicorn worker every blocking call halts the whole API, including for the
        seconds a slow file system needs.
        """
        os.makedirs(os.path.join(tmp, "galaxy"), exist_ok=True)
        with open(os.path.join(tmp, "galaxy", "modconfig.lua"), "w", encoding="utf-8") as f:
            f.write(content)

    with tempfile.TemporaryDirectory() as tmp:
        await asyncio.to_thread(_put_in_place, tmp)
        dummy = types.SimpleNamespace(
            key=server.key, game_type=server.game_type, data_dir=tmp,
            container_name=server.container_name,
        )
        return await preflight_avorion.check(dummy)


async def _restart(server, driver, *, start: bool) -> bool:
    if not start:
        return False
    try:
        await docker.action(server.container_name, "start")
    except Exception as e:
        log.error("mods(avorion)[%s]: start failed: %s", server.key, e)
        return False
    watchdog.suppress(server.key, SUPPRESS_MINUTES)
    return await verify_boot(server, driver, timeout=BOOT_TIMEOUT)


async def _rollback(server, raw_before: str | None, driver, *, start: bool
                    ) -> tuple[bool, bool, bool]:
    """Write the old file content back, then start. Returns `(file_restored, running, stopped)`.

    Three values, not two (extending `foreign_apply._rollback`): "the file is back in place",
    "the server is running" and "the rollback started at all" are three different statements,
    and the operator needs all of them.

    `stop_and_confirm` returns `stopped=False` precisely when the container is RUNNING or its
    state cannot be queried. In that case nothing was written back: the file still carries the
    NEW list and the server is up with an unproven mod list. The caller must be able to say
    that instead of claiming that the write-back failed and the server is down.
    """
    _, stopped = await lifecycle.stop_and_confirm(server)
    if not stopped:
        log.error("mods(avorion)[%s]: container not stopped — rollback aborted", server.key)
        return False, False, False
    file_restored = True
    try:
        if raw_before is None:
            # There was no file before, so the way back is to REMOVE it again. Leaving it would
            # leave behind a modded setup that never existed.
            await asyncio.to_thread(os.remove, _path(server))
        else:
            await asyncio.to_thread(_write, server, raw_before)
    except OSError as e:
        log.error("mods(avorion)[%s]: modconfig.lua could not be written back: %s", server.key, e)
        file_restored = False
    return file_restored, await _restart(server, driver, start=start), True


def _rollback_text(file_restored: bool, running: bool, stopped: bool, was_started: bool,
                   backup: str = "") -> str:
    """What the way back really achieved, and what the operator has to do now.

    Four inputs, because they are four different questions: is the file back in place, is the
    server running, did the rollback start at all, and was the server supposed to run at all.
    """
    if not stopped:
        return (
            " WARNING: the rollback never started — the container could not be confirmed as "
            "stopped (it is running, restarting, or its state cannot be queried). modconfig.lua "
            "therefore still carries the NEW list, and the server may be running with it "
            "without the boot having been proven."
            + (f" The way back is the backup {backup!r} or correcting modconfig.lua by hand on "
               f"the host." if backup else
               " The way back is the backup 'pre-avorionmods' or correcting modconfig.lua by "
               "hand on the host.")
        )
    if file_restored and (running or not was_started):
        return (" The previous list is back in place."
                + (" The server is running." if was_started else
                   " The server was stopped before and stays stopped."))
    parts = []
    if not file_restored:
        parts.append("modconfig.lua could not be written back")
    if was_started and not running:
        parts.append("the server did NOT come back up")
    return " WARNING: rollback incomplete — " + ", ".join(parts) + ". Please check manually."
