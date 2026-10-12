"""The ONE way to stop or restart a game server.

Why this file exists (measured on a running system, not assumed):

The PZ server image starts the JVM via `su … ./start-server.sh` under a shell without `trap`
and without `exec`. PID 1 in the container is therefore a Bash **without a SIGTERM handler**
(`/proc/1/status` shows `SigCgt=0x10002`, i.e. only SIGINT and SIGCHLD). For PID 1 the Linux
kernel drops signals without a handler: the SIGTERM from `docker stop` **never** reaches the
game. dockerd then logs `Container failed to exit within 2m0s of signal 15 - using the force`,
and the server logs show no shutdown at all. **Every such stop throws the world back to the
last autosave**; a longer grace period only postpones the SIGKILL.

The way out is to end the game through its own console (`driver.graceful_shutdown`). So that
this does not collide with the restart policy, the **order is decisive**:

    1. `docker stop` is issued FIRST (as a concurrent task).
    2. Only then do `save`/`quit` go out via RCON.

Docker marks the container as *manually stopped* at the beginning of `ContainerStop`. If the
game process then exits by itself, `unless-stopped` does not kick in and the container stays
down. The other way round (first `quit`, then `docker stop`) Docker would bring the server
straight back up.

Verified result: exit code **0** after 22 s instead of SIGKILL after 120 s, a complete save
block in the log (`Saving players` … `Shutdown handling finished`), `RestartCount=0`, and the
container does not come back by itself.

All stop/restart paths go through this module; `tests/test_stop_paths.py` enforces that.
"""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
import contextlib
import logging
from typing import TYPE_CHECKING

from ..errors import err
from .backup import create_backup
from .docker_client import docker
from .drivers import get_driver, stop_grace

if TYPE_CHECKING:
    from ..models.server import GameServer
    from .drivers import GameDriver

log = logging.getLogger("uvicorn.error")

# So that Docker has reliably recorded the container as "manually stopped" before the game
# process exits through our `quit`. Without this head start `unless-stopped` could pull the
# server straight back up. Two seconds are plenty (the HTTP request stays in the same Docker
# network).
_STOP_MARK_DELAY = 2.0

# Process-local lock per server_key (single-worker invariant, like `version_manager._swap_lock`):
# prevents idle auto-stop and an admin click from stopping at the same time.
_locks: dict[str, asyncio.Lock] = {}


def _lock(server_key: str) -> asyncio.Lock:
    lock = _locks.get(server_key)
    if lock is None:
        lock = asyncio.Lock()
        _locks[server_key] = lock
    return lock


def _driver(server: GameServer) -> GameDriver | None:
    try:
        return get_driver(server)
    except NotImplementedError:
        return None  # unknown game type → Docker path only


def _suppress(server_key: str, minutes: int) -> tuple[float, float] | None:
    """Mute the watchdog (imported locally to avoid a cycle). Returns (previous, ours).

    The return value exists for the abort case: if the operation does not happen after all,
    the mute is reset to exactly the previous value, not to zero — but only while it is still
    ours. `suppress` itself cannot do that; it deliberately only extends (`max(...)`) so that two
    operations do not pull the net out from under each other.
    """
    try:
        from .watchdog import _state, suppress

        st = _state(server_key)
        before = st.suppress_until
        suppress(server_key, minutes)
        return before, st.suppress_until
    except Exception:
        # Do not swallow silently: if this fails, the watchdog may touch a server that is being
        # stopped on purpose, and that should be visible in the logs.
        log.warning("lifecycle[%s]: watchdog could not be muted", server_key, exc_info=True)
        return None


def _revert_silence(server_key: str, muted: tuple[float, float] | None) -> None:
    """Take back our own mute if the operation did not happen at all."""
    if muted is None:
        return
    previous, ours = muted
    try:
        from .watchdog import _state

        st = _state(server_key)
        # Only while the value is still ours: if someone else extended the mute in the meantime,
        # it is theirs and stays (same rule as `watchdog.silence`).
        if st.suppress_until == ours:
            st.suppress_until = previous
    except Exception:
        log.warning("lifecycle[%s]: mute could not be taken back", server_key,
                    exc_info=True)


# Background stops that exceeded their time budget. The reference keeps the garbage collector
# from collecting the task before Docker is done.
_detached: set[asyncio.Task] = set()


def _detach(server_key: str, task: asyncio.Task) -> None:
    _detached.add(task)

    def _done(t: asyncio.Task) -> None:
        _detached.discard(t)
        exc = t.exception() if not t.cancelled() else None
        if exc is not None:
            log.warning("lifecycle[%s]: background stop failed: %s", server_key, exc)

    task.add_done_callback(_done)


@contextlib.asynccontextmanager
async def ensure_restart(server, was_running: bool, *, what: str):
    """If the block fails, a previously running server comes back, and audibly so.

    **Why it exists.** Between a confirmed stop and the protected transaction block of an
    operation there are unprotected steps (creating directories, copying config, DB inserts).
    If one of them raises, the exception would leave the function raw: no rollback, no start,
    no history row. And because the watchdog is muted for the operation, autoheal does not
    bring the server back either. It would simply stay off, with a bare OS or DB error in the
    operation panel.

    The rule must sit in one place that cannot be forgotten, the same answer as for
    `stop_and_confirm`, the world lock and the backup pack lock.

    **The restart is not silent when it fails.** Otherwise the server would be off, the
    watchdog muted, and the only message would be the original error, which knows nothing
    about the restart.

    The rule is "back to the previous state", not "always start": a server that was already
    stopped before stays stopped.
    """
    try:
        yield
    except BaseException as original:
        if was_running:
            try:
                await docker.action(server.container_name, "start")
                log.warning("%s: %s failed — server started again", server.key, what)
            except Exception as e:
                log.error("%s: %s failed AND the server could not be started "
                          "again (%s) — it is now off.", server.key, what, e)
                _incident(server, "restart_failed",
                          f"{what} failed; the restart failed as well: {e}")
                _append_note(original, e)
        raise


def detail_text(detail) -> str:
    """The English sentence of an `HTTPException.detail`, for a log line or an incident.

    A coded text (`app/errors.py`) is a dict; written into an f-string it would show up as
    its `repr`.
    """
    if isinstance(detail, dict):
        return str(detail.get("message") or detail.get("error") or detail)
    return str(detail)


def _append_note(error: BaseException, start_error: Exception) -> None:
    """Write the failed restart **into the propagating exception**.

    **Why here and not at the caller.** The outcome of the restart is only known at this point,
    and it is exactly what the message would otherwise lack: the caller raises "nothing was
    changed" while the start failed, the server is OFF, the watchdog is muted via
    `suppress_minutes`, and the operator reads that nothing happened.

    Reminding every caller would mean yet another copy. So the one who knows writes it: via
    `stop_and_snapshot`, `restore.py`, `version_manager.py` and `mods/foreign_apply.py` inherit
    the honest message without a line of their own.

    The log alone is not enough: the operator sees the operation panel, not `docker logs`.
    """
    note = (f"WARNING: the server was running before and is now OFF — the restart "
               f"failed ({start_error}). Manual intervention is needed.")
    detail = getattr(error, "detail", None)
    if isinstance(detail, str):
        error.detail = f"{detail} | {note}"
    elif isinstance(detail, dict):
        # Do not write into `message`: the UI shows structured messages field by field, and an
        # appended sentence would disappear in one line there.
        detail["restart_warning"] = note
        if "code" in detail:
            # A coded text is translated by its code, so the field above would never be shown
            # for it. The detail becomes a coded text of its own that carries the original
            # one — as `cause` for a client that wants to translate it, as text for the rest.
            cause = {k: detail.get(k) for k in ("code", "message", "params")}
            detail.update(err(
                "lifecycle.restart_after_failure_failed",
                "{cause} | WARNING: the server was running before and is now OFF — the restart "
                "failed ({error}). Manual intervention is needed.",
                cause=str(cause["message"]), error=str(start_error),
            ))
            detail["cause"] = cause
    elif error.args and isinstance(error.args[0], str):
        error.args = (f"{error.args[0]} | {note}",) + error.args[1:]


def _incident(server, kind: str, detail: str) -> None:
    """Record an incident, same pattern as `restarts._incident` and `backup._incident`."""
    from sqlmodel import Session

    from ..db import engine
    from ..models import ServerIncident

    try:
        with Session(engine) as s:
            s.add(ServerIncident(kind=kind, detail=detail[:500], server_id=server.id))
            s.commit()
    except Exception:
        log.warning("lifecycle: incident could not be saved", exc_info=False)


async def stop_and_snapshot(
    server,
    driver,
    label: str,
    *,
    announce_text: str | None = None,
    suppress_minutes: int = 20,
) -> tuple[bool, dict]:
    """Stop, confirm the stop, THEN back up. Returns `(was_running, backup)`.

    **The order is the whole point.** "Back up, then stop" yields a hot copy of a world that is
    being written for Project Zomboid: `pz.backup_spec` has no quiesce hooks (`pre_hook` and
    `post_hook` are the base defaults `None`), so the `tarfile` run would capture half-written
    `map/*.bin` and a `players.db` in the middle of a write. And exactly that archive is then
    the only way back, for restore, version change and mod apply.

    This is one shared helper, like `stop_and_confirm`, because hand-copied versions of such a
    sequence drift apart, and one inverted copy is enough.

    If the backup fails, a previously running server is started again: the caller has not
    touched anything then, and a failed backup must not leave a server down.

    Raises `HTTPException` (409 unconfirmed stop, 502 backup); the caller passes it through.
    """
    from fastapi import HTTPException, status as http

    # The mute lives in `stop_and_confirm`: it belongs at the choke point that ALL confirmed
    # stops pass through, not at each caller individually (see there).
    was_running, stopped = await stop_and_confirm(
        server, announce_text=announce_text, suppress_minutes=suppress_minutes,
    )
    if not stopped:
        raise HTTPException(
            http.HTTP_409_CONFLICT,
            detail=err("lifecycle.stop_not_confirmed",
                       "The server is still running — nothing was changed. Writing over a "
                       "running world would damage it."),
        )

    spec = driver.backup_spec(server)
    # The restart goes through the choke point that attaches the notice to the exception
    # itself; otherwise the 502 below would claim "nothing changed" even when the start failed
    # and the server is off.
    async with ensure_restart(server, was_running, what="mandatory backup"):
        try:
            # **No quiesce hooks: the stop was CONFIRMED above.** A hook that is meant to make
            # the server save from outside can only fail against a stopped container, and
            # would record `backup_quiesce_failed` incidents about a container that no longer
            # exists (e.g. "RCON unreachable: name resolution failure").
            #
            # None is needed here anyway: the purpose of quiescing is to briefly calm a
            # WRITING world, and a stopped one does not write. The `post_hook` (MC's
            # `save-on`) would be doubly pointless, since autosave is on again by itself after
            # the next start.
            backup = await asyncio.to_thread(
                create_backup, label, data_dir=spec.data_dir, world_subdirs=spec.world_subdirs,
                config_subdirs=spec.config_subdirs,
                pre_hook=None, post_hook=None, server=server,
            )
        except Exception as e:
            raise HTTPException(
                http.HTTP_502_BAD_GATEWAY,
                detail=err("lifecycle.mandatory_backup_failed",
                           "Mandatory backup failed — nothing was changed: {error}",
                           error=str(e)),
            ) from e
    return was_running, backup


def _no_world_operation(server, what: str) -> None:
    """No start/restart while a world-writing operation is running.

    **Delegation, not a copy.** The rule lives in `world_lock`, where the config editor's write
    routes use it as well; only the call remains here. Four callers pass through this point:
    `routers/server.perform_action`, the scheduled restart, the watchdog autoheal and the
    manual start.
    """
    from . import world_lock

    world_lock.refuse_during_world_operation(server.key, what)


async def start_server(server: GameServer) -> None:
    """Start a server, under the same lock as stop and restart.

    Without the lock a start would run into a stop that is still in progress, and that is not
    a theoretical case:

    * The idle auto-stop holds the lock for the full stop (up to 120 s for PZ). If someone
      clicks "Start" in that window, `docker start` clears the "manually stopped" mark and
      re-arms `unless-stopped`, and the subsequent `quit` from `graceful_shutdown` then kills
      the freshly booted server. The user gets `{"ok": true}`.
    * `perform_action("stop")` uses `wait_timeout=75`. If it runs past that, `_detach`
      decouples the `docker stop` and the lock is released, but the SIGKILL is still pending.

    The remaining gap from the second case is deliberately left open: waiting for the detached
    stop here would leave the start button hanging for minutes. It is named, not solved.
    """
    _no_world_operation(server, "Start")
    async with _lock(server.key):
        await docker.action(server.container_name, "start")
        _suppress(server.key, 5)  # do not mistake the boot for a hang


async def is_running(server: GameServer, *, when_in_doubt: bool = True) -> bool:
    """Is this server's container running?

    Public because outside this module the same question must be answered with the same
    **failure direction**. Whoever rebuilds it will sooner or later choose the opposite sign.

    **But these are two different questions asked of the same function**, and they need
    opposite failure directions:

    * *"Do I still have to stop it?"* → `when_in_doubt=True`. An unanswered question is not a "no"
      here: if it is unknown whether the server is running, stop it regularly instead of
      writing over a possibly running world.
    * *"May I restart it?"* → `when_in_doubt=False`. The same answer would be fail-open here:
      `restart_server` has `docker start` in its `finally` and would bring up a deliberately
      stopped server just because the socket proxy did not answer at that moment.
    """
    return await _is_running(server.container_name, when_in_doubt=when_in_doubt)


def running_or_restarting(st: dict) -> bool:
    """Is this container in someone else's hands: running OR being restarted by Docker?

    **The choke point for the question "may I write into the world now".**

    Measured against a real Docker daemon (container in a crash loop with
    `--restart unless-stopped`, 300 samples): during the restart backoff Docker reports
    `Running=true` together with `Restarting=true`; `Running` is NEVER `false`. moby sets both
    explicitly in `State.SetRestarting()` ("we should consider the container running when it
    is restarting"). So reading `running` alone already gives the right answer there.

    **Why the function exists anyway.** It is not a fix for an observed bug but the right
    QUESTION: "is the container in someone else's hands" is exactly `running or restarting`,
    and the Docker API nowhere promises that one implies the other. It is strictly more
    cautious than `running` alone and costs nothing at runtime.

    `tests/test_no_direct_running_in_the_world_core.py` pins down that the question is answered
    in ONE place.
    """
    return bool(st.get("running") or st.get("restarting"))


async def _is_running(name: str, *, when_in_doubt: bool = True) -> bool:
    try:
        return running_or_restarting(await docker.state(name))
    except Exception:
        return when_in_doubt  # see `is_running`: the direction belongs to the caller


def _can_shut_itself_down(drv: GameDriver) -> bool:
    """Does this driver override `graceful_shutdown` at all?

    Minecraft, Reforger, Avorion, Valheim and Vintage Story (and any future driver without a
    console shutdown) inherit the base default. Those need neither the head start nor a console
    attempt, and no "falling back" warning either: the signal path is the clean stop there.
    """
    from .drivers.base import GameDriver as _Base

    own = getattr(type(drv), "graceful_shutdown", None)
    return own is not None and own is not _Base.graceful_shutdown


async def _stop_locked(server: GameServer, announce_text: str | None, wait_timeout: float | None) -> bool:
    """The actual stop. The caller MUST hold the lock for `server.key`."""
    name = server.container_name
    grace = stop_grace(server)
    _suppress(server.key, 5)
    if not await _is_running(name):
        # Already down. The stop is sent anyway because it is cheap and cleanly closes any
        # residual state, but it does NOT mark the container as manually stopped: Docker
        # answers 304 before it even enters `containerStop`. Whoever must be sure that nothing
        # is running checks `docker.state` afterwards (as restore and the mod rollbacks do).
        await docker.action(name, "stop", stop_timeout=grace)
        return False

    drv = _driver(server)
    stop_task = asyncio.create_task(docker.action(name, "stop", stop_timeout=grace))
    try:
        # The announcement runs AFTER the stop call: it is a convenience, but with a dead RCON
        # it costs the full timeout; placed before, it would delay the stop by up to 16 s.
        if drv is not None and announce_text:
            try:
                await drv.announce(server, announce_text)
            except Exception:
                pass

        if drv is not None and _can_shut_itself_down(drv):
            await asyncio.sleep(_STOP_MARK_DELAY)
            # If the stop has ALREADY failed (proxy 404, container gone, permissions), Docker
            # never marked the container as manually stopped. A `quit` would now end the game
            # process and `unless-stopped` would pull it straight back up, worse than doing
            # nothing. In that case only the error reported by the `await` below remains.
            if stop_task.done() and stop_task.exception() is not None:
                log.warning(
                    "lifecycle[%s]: Docker did not accept the stop — no console shutdown",
                    server.key,
                )
            else:
                # Capped at the grace period: a hanging RCON must not hold the lock longer than
                # the signal path needs anyway. Without a cap a single wedged server could
                # freeze the watchdog job process-wide (`max_instances=1`).
                budget = max(5.0, grace - _STOP_MARK_DELAY - 10)
                try:
                    if await asyncio.wait_for(drv.graceful_shutdown(server), timeout=budget):
                        log.info("lifecycle[%s]: graceful in-game shutdown triggered", server.key)
                    else:
                        # The silent failure belongs in the log too. Without it, a server that
                        # is killed hard after the grace period (e.g. a stop shortly after
                        # boot, RCON not yet reliable) gives no hint that the console never
                        # cooperated.
                        log.warning(
                            "lifecycle[%s]: the game could not be shut down through the console — "
                            "falling back to the signal path (grace period %ss)", server.key, grace,
                        )
                except Exception:
                    log.warning(
                        "lifecycle[%s]: graceful in-game shutdown failed — falling back "
                        "to the signal path", server.key, exc_info=True,
                    )
    finally:
        if wait_timeout is None:
            await stop_task  # waits until the container is really down
        else:
            # Callers with a time budget (an HTTP request behind a reverse proxy with a ~100 s
            # cap): after it expires we return and the stop finishes in the background.
            try:
                await asyncio.wait_for(asyncio.shield(stop_task), timeout=wait_timeout)
            except asyncio.TimeoutError:
                _detach(server.key, stop_task)
                log.warning(
                    "lifecycle[%s]: stop takes longer than %.0f s — continuing in the background",
                    server.key, wait_timeout,
                )
    return True


async def run_state(server: GameServer) -> bool | None:
    """Is it running? With `None` for **cannot be determined**.

    `is_running` answers the same question with a failure direction, which is right for most
    callers. This one is for callers that must tell the three cases apart (running / not
    running / unknown), e.g. the scheduled restart: "socket proxy does not answer" must not be
    booked as "nothing to do" without an incident, even though not starting is the right
    DIRECTION.
    """
    try:
        return running_or_restarting(await docker.state(server.container_name))
    except Exception:
        return None


async def stop_server(
    server: GameServer, *, announce_text: str | None = None, wait_timeout: float | None = None,
    precondition: "Callable[[], Awaitable[bool]] | None" = None,
) -> bool:
    """Shut a game server down the game's own way. Returns whether it was running before.

    Errors of the game console are swallowed; the `docker stop` runs in any case and is the
    fallback. Only an error from Docker itself is passed up (the routers turn it into a 502).

    `wait_timeout` caps ONLY the wait for the end; the stop itself continues in the background
    afterwards. That is meant for callers inside an HTTP request (a reverse proxy may cut off
    at ~100 s). Internal callers that touch files afterwards (restore, mod apply, version
    change) omit it and really wait until nothing is running.
    """
    async with _lock(server.key):
        # **The lock serialises; it does not revalidate.**
        #
        # Whoever arrives here may have waited minutes: `stop_grace` is 120 s for Project
        # Zomboid, 90 s for Minecraft, and `restart_server` holds the same lock across stop AND
        # start. An idle stop that waited behind a scheduled restart would otherwise stop the
        # server that restart has just brought up: its decision ("nobody here for 20 days")
        # dates from before the wait, and neither run state nor player count nor
        # `last_active_at` would be read again.
        #
        # The precondition is the opportunity to repeat the decision INSIDE the lock. It is
        # deliberately optional: a human pressing "Stop" wants to stop.
        if precondition is not None and not await precondition():
            log.info("lifecycle[%s]: stop cancelled — the situation changed while waiting "
                     "for the lock", server.key)
            return False
        return await _stop_locked(server, announce_text, wait_timeout)


async def stop_and_confirm(
    server: GameServer, *, announce_text: str | None = None, suppress_minutes: int | None = None,
    precondition: "Callable[[], Awaitable[bool]] | None" = None,
) -> tuple[bool, bool]:
    """Stop **and confirm the stop**. Returns `(was_running, really_stopped_now)`.

    For every caller that touches world files afterwards: restore, mod apply, rollback, version
    change. Writing over a running world damages it; `stop_server` alone is not sufficient
    proof, because it also returns when Docker did not accept the stop at all.

    **Why this is a separate helper:** hand-copied versions of these lines drift, and a single
    copy with the sign inverted (`except Exception: running = False`) in a rollback path, i.e.
    where the stop has just proven unreliable, pushes world cells back into a possibly running
    game. Both calls go through the same socket proxy: if it is gone, both fail.

    The confirmation is the authority, not the stop call: an error from `stop_server` is logged
    but not passed on. If the container was already down, all is well, and the query afterwards
    decides that. If it cannot be answered, the server counts as **running**. An unanswered
    question is not a "no" here.

    **`suppress_minutes` belongs here, not with the caller.** Whoever stops must mute the
    watchdog beforehand (during the graceful shutdown the container is still running while RCON
    is already dead, which the watchdog reads as a hang), and must take the mute back if the
    stop could NOT be confirmed: then nothing happened, the server keeps running, possibly
    hanging, and depriving it of autoheal, `rcon_hang` and `boot_crash_loop` for 15-20 minutes
    hits it in exactly the situation where it needs them.

    The pair sits at the choke point all confirmed stops pass through, so no caller can
    implement only half of it. The mute is reset to the PREVIOUS value, not to zero, and only
    while it is still ours: if someone else muted for longer (a scheduled restart, a second
    operation), before or during the stop, the mute is theirs.
    `tests/test_silence_does_not_survive_an_abort.py` pins that down.

    `precondition` is passed to `stop_server`, which asks it INSIDE the lock (see there). With
    one, `was_running` is what `stop_server` found inside the lock: `False` when the
    precondition said no or someone else stopped the server while this call waited, so the
    caller does not start a server it did not stop.
    """
    our_silence = _suppress(server.key, suppress_minutes) if suppress_minutes else None
    name = server.container_name
    was_running = await _is_running(name)
    if was_running:
        try:
            ran = await stop_server(server, announce_text=announce_text, precondition=precondition)
            if precondition is not None:
                was_running = ran
        except Exception:
            log.error("lifecycle[%s]: stop failed", server.key, exc_info=True)

    try:
        # Same question as above, hence the same answer: a container in restart backoff is
        # NOT stopped.
        still_running = running_or_restarting(await docker.state(name))
    except Exception:
        log.error(
            "lifecycle[%s]: stop cannot be confirmed — the server counts as RUNNING, nothing is "
            "written", server.key, exc_info=True,
        )
        still_running = True
    if still_running:
        # Nothing happened, so leave no mute behind either (see docstring).
        _revert_silence(server.key, our_silence)
    return was_running, not still_running


async def restart_server(
    server: GameServer, *, announce_text: str | None = None,
    precondition: "Callable[[], Awaitable[bool]] | None" = None,
) -> None:
    """Stop + start instead of `docker restart`.

    `docker restart` sends the same ineffective SIGTERM and would kill PZ hard again.
    Stop-then-start takes the clean path above instead; `docker start` clears the "manually
    stopped" mark again.

    The start is in the `finally`: unlike `docker restart` these are two steps, and an error in
    the stop must not leave a server down. `docker start` on a running container is a no-op
    (304), so that case is harmless.

    The lock is held across BOTH steps. Otherwise a stop arriving during the restart could get
    the lock in the gap and report "ok", and the restart would undo it right afterwards with
    its `start`.
    """
    _no_world_operation(server, "Restart")
    async with _lock(server.key):
        # The same re-check as for the stop, for the same reason and in the opposite direction:
        # a scheduled restart that waited out the idle stop's lock (up to two minutes) would
        # otherwise bring the just deliberately stopped server back up via the `docker start`
        # in the `finally`.
        if precondition is not None and not await precondition():
            log.info("lifecycle[%s]: restart cancelled — the situation changed while "
                     "waiting for the lock", server.key)
            return
        try:
            await _stop_locked(server, announce_text, None)
        finally:
            await docker.action(server.container_name, "start")
            _suppress(server.key, 10)  # do not mistake the boot for a hang
