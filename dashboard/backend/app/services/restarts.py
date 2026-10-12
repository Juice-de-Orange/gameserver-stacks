"""Scheduled server restarts with broadcast warnings (driven by APScheduler).

reload_jobs() is called at startup and after every schedule change: it deletes all
'restart_*' jobs and registers them again from the DB. Each schedule produces one restart
job (save -> optional backup -> restart) plus one broadcast job per warning minute.
"""
from __future__ import annotations

import asyncio
import logging

from fastapi import HTTPException
from sqlmodel import Session, select

from ..db import engine
from ..models import RestartSchedule
from ..scheduler import scheduler
from ..services import lifecycle
from ..services import servers as servers_registry
from ..services import backup
from ..services.backup import create_backup
from ..services.drivers import get_driver
from ..services.mods import state as mod_state
from ..services.rcon import RconError
from ..services import world_lock
from ..services.watchdog import (
    BOOT_WINDOW_MIN,
    silence as watchdog_silence,
    suppress as watchdog_suppress,
)

log = logging.getLogger("uvicorn.error")


def _incident(server, kind: str, detail: str) -> None:
    """Record an incident — a rejected restart must not vanish into the log only."""
    from ..models import ServerIncident

    try:
        with Session(engine) as s:
            s.add(ServerIncident(kind=kind, detail=detail[:500], server_id=server.id))
            s.commit()
    except Exception:
        log.warning("incident could not be saved", exc_info=False)

_VALID_DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
# Previous day per weekday (mon→sun, tue→mon, …) — for warnings that wrap past midnight.
_PREV_DAY = {d: _VALID_DAYS[i - 1] for i, d in enumerate(_VALID_DAYS)}

# Tight misfire grace for the warning (the scheduler default is deliberately generous, see
# `app/scheduler.py`): an announcement "restart in 15 minutes" that fires an hour late is a
# false statement. The restart itself may and should be caught up.
WARN_GRACE_S = 60


def _dow(days: str) -> str:
    """Schedule days -> APScheduler day_of_week ('*' for daily, otherwise CSV)."""
    days = (days or "").strip().lower()
    if not days or days == "daily":
        return "*"
    chosen = [d for d in (x.strip() for x in days.split(",")) if d in _VALID_DAYS]
    return ",".join(chosen) if chosen else "*"


def days_valid(days: str) -> bool:
    """True if `days` is 'daily'/empty OR contains only known weekday abbreviations.

    Prevents a typo ('mo,mi,fr') from silently degrading to '*' (DAILY) in the service, which
    would restart the server on days nobody chose — the routers reject invalid days with 400.
    """
    d = (days or "").strip().lower()
    if not d or d == "daily":
        return True
    toks = [x.strip() for x in d.split(",") if x.strip()]
    return bool(toks) and all(t in _VALID_DAYS for t in toks)


def can_announce(server) -> tuple[bool, str]:
    """Does a warning reach anyone at all for this game?

    `drv.announce()` is a deliberate no-op for Valheim, Avorion and Arma Reforger — there is no
    way to tell all players something there. Warning jobs for such a game would fire into the
    void: nobody gets a warning, and nobody learns that none was sent.

    The answer comes from `capabilities()`, not from a list of game names, so a new game answers
    the question by itself.
    """
    try:
        drv = get_driver(server)
    except NotImplementedError:
        return False, "There is no driver for this game."
    if "announce" in drv.capabilities():
        return True, ""
    return False, (
        "This game cannot send an announcement to all players — a warning would reach "
        "nobody. The restart itself still runs as scheduled."
    )


async def _broadcast(minutes: int, sid: int | None = None) -> None:
    """Warning broadcast to the schedule's target server (no schedule/no server → nothing)."""
    server_id = None
    if sid is not None:
        with Session(engine) as session:
            sched = session.get(RestartSchedule, sid)
        server_id = sched.server_id if sched else None
    server = servers_registry.resolve_server(server_id)
    # Same condition as in the restart itself (`_do_restart` checks `enabled` too). `enabled`
    # is an ordinary PATCH field, and changing it neither stops the container nor calls
    # `reload_jobs()` — the cron jobs stay registered until the next backend start. Without this
    # check, players on a server that is disabled in the registry but still running would get a
    # warning every night for a restart that never comes.
    if server is None or (server.id is not None and not server.enabled):
        return
    # Second safeguard: `reload_jobs` creates no warning job for a game that cannot announce.
    # Should one survive from an older registration anyway, it ends here with a log line
    # instead of silence.
    possible, reason = can_announce(server)
    if not possible:
        log.info("warning for %s skipped: %s", server.key, reason)
        return
    try:
        drv = get_driver(server)
    except NotImplementedError:
        return
    try:
        # The game's own command (PZ servermsg / MC say / VS announce) via the driver, not a
        # hard-coded PZ servermsg, which would do nothing on other games.
        await drv.announce(server, f"Server restart in {minutes} min!")
    except RconError:
        pass


async def _do_restart(sid: int) -> None:
    with Session(engine) as session:
        sched = session.get(RestartSchedule, sid)
    server = servers_registry.resolve_server(sched.server_id if sched else None)
    if server is None or (server.id is not None and not server.enabled):
        log.warning("scheduled restart %s: target server missing/disabled — skipped", sid)
        return
    # A "restart" of a STOPPED server is not a restart but a start nobody asked for.
    # `restart_server` has `docker start` in its `finally` — right if the server was running,
    # wrong if someone (or the idle auto-stop) left it down on purpose. Otherwise the two
    # mechanisms work against each other: the schedule starts an idle server every night and the
    # idle auto-stop stops it again a little later (`idle_autostop` deliberately does not reset
    # its activity clock for a scheduled restart).
    #
    # A stopped server is normal operation here, not a failure, so no incident is recorded.
    #
    # The run state is tri-state on purpose. The question here is not "do I have to stop?" but
    # "may I touch it?". Treating an unanswered query as "running" is right for stopping and
    # fail-open here: it would start a deliberately stopped server just because the socket
    # proxy did not answer.
    state = await lifecycle.run_state(server)
    if state is None:
        # "We do not know" is not "nothing to do". Not restarting is the right direction, but
        # it needs an incident like the other abort paths: otherwise the nightly maintenance
        # restart is skipped silently, and the schedule page just shows the next date because
        # `next_restart()` only reads the scheduler jobs.
        log.warning("scheduled restart %s: run state of %s cannot be queried — no restart",
                    sid, server.key)
        _incident(server, "restart_skipped",
                  "Scheduled restart cancelled: the run state of the container could not be "
                  "queried (Docker proxy). Nothing was touched.")
        return
    if not state:
        log.info("scheduled restart %s: %s is not running — nothing to do", sid, server.key)
        return
    # Mod gate: a scheduled restart must not activate a broken mod configuration. Without this
    # check the preflight could be bypassed — the server would run until the next nightly
    # restart and then fail to come up, with nobody connecting that to the mod change.
    blockers = await mod_state.guard_boot(server)
    if blockers:
        log.error("scheduled restart %s rejected: %s", sid, mod_state.guard_message(blockers))
        _incident(server, "restart_blocked", mod_state.guard_message(blockers))
        return
    try:
        drv = get_driver(server)
    except NotImplementedError:
        drv = None  # without a driver: Docker restart only, no announcement/backup/save
    if drv is not None:
        try:
            await drv.announce(server, "Server is restarting now…")
        except RconError:
            pass
    # Save first, THEN back up: the backup should contain the current game state, not the one
    # from the last autosave.
    if drv is not None:
        try:
            await drv.save(server)  # the game's own world save (PZ save / MC save-all flush / VS no-op)
        except RconError:
            pass
    # A game that cannot be told to save (Valheim) gets its mandatory backup while the restart
    # has it stopped (`backup.restart_with_backup_while_stopped`, below). Packing it here would
    # copy a world that is being written: the last autosave at best.
    backup_while_stopped = False
    if sched and sched.backup_first and drv is not None:
        spec = drv.backup_spec(server)
        backup_while_stopped = spec.stop_for_consistency
    if sched and sched.backup_first and drv is not None and not backup_while_stopped:
        try:
            # Two guards around the archiving:
            #
            # 1. `world_lock`: packing a multi-GB world takes minutes. Without the lock a mod
            #    apply, a version change or a restore could start in the middle and move files
            #    around under the running `tarfile` — and the archive would still be listed as
            #    a regular `pre-restart` backup. `scheduled_backup` protects itself the same way.
            # 2. `watchdog.silence`: with PZ the `save` right before blocks the game thread and
            #    RCON stops answering; after three 60 s rounds an armed watchdog would autoheal
            #    and restart the container in the middle of the tar run. It is a block with an
            #    end, not `suppress()`: silent while packing, armed again as soon as it is over
            #    — also on failure, because a failed backup must not mute the watchdog.
            async with world_lock.hold(server.key, "restart.backup"):
                with watchdog_silence(server.key, BOOT_WINDOW_MIN):
                    await asyncio.to_thread(
                        create_backup, "pre-restart",
                        data_dir=spec.data_dir, world_subdirs=spec.world_subdirs,
                        config_subdirs=spec.config_subdirs,
                        pre_hook=spec.pre_hook, post_hook=spec.post_hook, server=server,
                    )
        except HTTPException as e:
            # 409 from the world lock: another world-writing operation is running. That is a
            # REJECTION, not a failure — and not something the operator should read as an
            # incident in the morning. The restart is skipped this time; the running operation
            # brings its own boot.
            if e.status_code != 409:
                raise
            log.info("scheduled restart %s: skipped — %s", sid, lifecycle.detail_text(e.detail))
            return
        except Exception as e:
            # A failed mandatory backup gets an incident, the cause in the log, and a `return`:
            # the operator ticked "create a backup before the restart", so the restart must not
            # run without one. If the server then failed to come up there would be no backup AND
            # no trace of why. Same rule as the mod gate above: a scheduled restart must not
            # activate a state whose way back is known to be missing.
            log.warning("scheduled restart: mandatory backup failed", exc_info=True)
            _incident(server, "backup_failed",
                      f"Backup before the scheduled restart failed: {e}")
            return
    # Check the state AGAIN right before the restart. Between the check above and here lie the
    # announcement, the world save and the mandatory backup — minutes for a large world. The
    # hourly idle auto-stop can fall into that window; `restart_server` has `docker start` in
    # its `finally` and would immediately bring the deliberately stopped server back up.
    # Unknown again means "do not touch". The gap that remains after this check (waiting for
    # `lifecycle._lock`) is closed by the `precondition` passed to `restart_server` below.
    state = await lifecycle.run_state(server)
    if state is not True:
        log.info("scheduled restart %s: %s was stopped in the meantime (or the run state "
                 "cannot be queried) — no restart", sid, server.key)
        if state is None:
            _incident(server, "restart_skipped",
                      "Scheduled restart cancelled: the run state could not be queried before "
                      "the restart.")
        return
    # Suppress only HERE: a `watchdog_suppress` before the early returns above would mute the
    # watchdog although no restart happened — in exactly the situation (mandatory backup failed,
    # game possibly hanging) where it is needed.
    # The duration comes from the watchdog, not from a number at this spot: it has to cover a
    # boot that `verify_boot` allows up to 900 s, while the watchdog ticks every 60 s and acts
    # after three dead answers.
    watchdog_suppress(server.key, BOOT_WINDOW_MIN)
    try:
        # Stop-then-start instead of `docker restart`: a Docker restart sends the same SIGTERM
        # that never reaches PZ — the server would be killed hard (see lifecycle.py).
        # The precondition is passed INTO `restart_server` because that function first takes
        # `lifecycle._lock`, which the idle auto-stop holds for its ENTIRE stop (120 s grace for
        # PZ, 90 s for Minecraft), and `docker start` sits unconditionally in its `finally`.
        # The lock serialises, it does not re-validate — so the check must run inside it.
        if backup_while_stopped:
            await backup.restart_with_backup_while_stopped(
                server, drv, "pre-restart",
                precondition=lambda: _still_running_and_wanted(server, sid),
            )
        else:
            await lifecycle.restart_server(
                server,
                precondition=lambda: _still_running_and_wanted(server, sid),
            )
    except HTTPException as e:
        # Same distinction as above, and the timing can produce this case by itself: an
        # auto-update job shortly before the scheduled restart holds `world_lock` across stop,
        # mandatory backup, image swap and a boot check of up to 900 s. This job then sees a
        # running container and is correctly rejected by the lock — a skip, not a failure.
        if e.status_code != 409:
            raise
        log.info("scheduled restart %s: skipped — %s", sid, lifecycle.detail_text(e.detail))
    except Exception as e:
        # Record it visibly, not only in the log: stop and start are two steps, and an error in
        # between can leave the server down. The watchdog deliberately does not act on a STOPPED
        # container — without this incident nobody would notice.
        log.warning("scheduled restart: restart failed", exc_info=True)
        _incident(server, "restart_failed", f"Scheduled restart failed: {e}")


async def _still_running_and_wanted(server, sid) -> bool:
    """Asked INSIDE `lifecycle._lock`, immediately before stop and start."""
    state = await lifecycle.run_state(server)
    if state is not True:
        log.info("scheduled restart %s: %s is no longer running (situation changed while waiting "
                 "for the lock) — no restart", sid, server.key)
        return False
    return True


def _clear_jobs() -> None:
    for job in scheduler.get_jobs():
        if job.id and job.id.startswith("restart_"):
            scheduler.remove_job(job.id)


def reload_jobs() -> None:
    """Rebuild all restart jobs from the DB."""
    _clear_jobs()
    with Session(engine) as session:
        schedules = session.exec(select(RestartSchedule).where(RestartSchedule.enabled == True)).all()  # noqa: E712
    for sched in schedules:
        try:
            hour, minute = (int(x) for x in sched.time_hhmm.split(":"))
        except (ValueError, AttributeError):
            continue
        dow = _dow(sched.days)
        scheduler.add_job(
            _do_restart, "cron", hour=hour, minute=minute, day_of_week=dow,
            id=f"restart_{sched.id}", kwargs={"sid": sched.id}, replace_existing=True, max_instances=1,
        )
        # No warning jobs for a game that cannot announce anything: a job that can do nothing
        # is not a safeguard but a promise that is not kept.
        target = servers_registry.resolve_server(sched.server_id)
        if target is None:
            continue
        possible, reason = can_announce(target)
        if not possible and str(sched.warn_minutes).strip():
            log.info("schedule %s (%s): warning not possible — %s", sched.id, target.key, reason)

        base = hour * 60 + minute
        warnings = (
            (int(x) for x in str(sched.warn_minutes).split(",") if x.strip().isdigit())
            if possible else ()
        )
        for w in warnings:
            wm_total = (base - w) % (24 * 60)
            # If the warning wraps past midnight (e.g. restart Mon 00:05, warning 23:55), it
            # must fire on the PREVIOUS day — otherwise it would come almost 24 h AFTER the
            # restart. 'daily' ('*') needs no shift.
            warn_dow = dow
            if base - w < 0 and dow != "*":
                warn_dow = ",".join(_PREV_DAY[d] for d in dow.split(","))
            scheduler.add_job(
                _broadcast, "cron", hour=wm_total // 60, minute=wm_total % 60, day_of_week=warn_dow,
                id=f"restart_{sched.id}_warn_{w}", kwargs={"minutes": w, "sid": sched.id},
                replace_existing=True, max_instances=1,
                # Explicitly overrides the generous scheduler default (1 h): a warning that
                # arrives after the restart is worse than none. The restart itself (above) may
                # be caught up.
                misfire_grace_time=WARN_GRACE_S,
            )


def _job_sid(job_id: str) -> int | None:
    """'restart_5' → 5 (warning jobs are filtered out beforehand)."""
    try:
        return int(job_id.split("_")[1])
    except (IndexError, ValueError):
        return None


def next_restart(server_id: int | None = None) -> str | None:
    """ISO timestamp of the next scheduled restart (from the restart_* jobs, excluding warnings).

    Without `server_id`: across ALL restart jobs. With `server_id`: only the restarts whose
    schedule belongs to that server.
    """
    jobs = [
        job for job in scheduler.get_jobs()
        if job.id and job.id.startswith("restart_") and "_warn_" not in job.id and job.next_run_time
    ]
    if server_id is not None:
        with Session(engine) as session:
            allowed = {
                s.id
                for s in session.exec(
                    select(RestartSchedule).where(RestartSchedule.enabled == True)  # noqa: E712
                ).all()
                if s.server_id == server_id
            }
        jobs = [job for job in jobs if _job_sid(job.id) in allowed]
    times = [job.next_run_time for job in jobs]
    return min(times).isoformat() if times else None
