"""Scheduled server announcements (recurring `servermsg`). Mirrors services/restarts.py.

reload_jobs() removes all 'bcast_*' jobs and rebuilds them from the DB: per entry either an
interval job (every N minutes) OR a cron job (time of day + days). Messages are sanitised (no
quote, control-character or semicolon injection into the RCON string).

Multi-game: the job only knows the `bid`; message and target are read from the DB at send time
(edits take effect immediately, and CRUD calls `reload_jobs()` anyway). `target_all` fans out
to all running servers; otherwise the announcement goes to the target server (no target server
→ nothing).
"""
from __future__ import annotations

import logging
import re

from sqlmodel import Session, select

from ..db import engine
from ..models import BroadcastSchedule
from ..scheduler import scheduler
from ..services import servers as servers_registry
from ..services.docker_client import docker
from ..services.drivers import get_driver
from ..services.rcon import RconError
# Same day logic AND the same capability check as for the restarts.
from ..services.restarts import _dow, can_announce

log = logging.getLogger("uvicorn.error")

# An announcement explicitly overrides the generous scheduler default (1 h): its value dies with
# the delay. "Restart in 15 minutes" fired an hour late is no longer a late warning but a false
# statement, so better none at all.
BCAST_GRACE_S = 60


def _clean(s: str) -> str:
    return re.sub(r'[\x00-\x1f\x7f";]', "", s or "").strip()


async def _send(bid: int) -> None:
    with Session(engine) as session:
        b = session.get(BroadcastSchedule, bid)
    if not b or not b.enabled:
        return
    msg = _clean(b.message)
    if not msg:
        return

    if b.target_all:
        # Shared announcement to ALL running servers.
        for server, drv in servers_registry.list_enabled_with_driver():
            try:
                state = await docker.state(server.container_name)
                if not state.get("running"):
                    continue
                await drv.announce(server, msg)  # game-specific command (PZ servermsg / MC say / VS /announce)
            except Exception:
                continue  # swallow per server, never abort the loop
        return

    server = servers_registry.resolve_server(b.server_id)
    # **Check `enabled`**, as the `target_all` branch above does via
    # `list_enabled_with_driver()`. This branch resolves via `get_by_id()`, a bare
    # `Session.get()` that returns the row regardless of `enabled`. `enabled=False` is the
    # DELETE gesture here and does not touch the container, so on a server that counts as
    # deleted but still runs, players would keep getting the announcement. The sibling job
    # `restarts._do_restart` checks the same thing.
    if server is None or (server.id is not None and not server.enabled):
        return
    try:
        drv = get_driver(server)
    except NotImplementedError:
        return
    # **Whether an announcement arrives at all is up to the GAME.** `ValheimDriver.announce` and
    # `ReforgerDriver.announce` are deliberate no-ops: no error, no exception, no log. Without
    # this check the job would record every run as done while the message reached nobody.
    possible, reason = can_announce(server)
    if not possible:
        log.info("broadcasts[%s]: announcement not possible — %s", server.key, reason)
        return
    try:
        await drv.announce(server, msg)  # game-specific command instead of a hard-coded PZ servermsg
    except RconError:
        pass


def _clear_jobs() -> None:
    for job in scheduler.get_jobs():
        if job.id and job.id.startswith("bcast_"):
            scheduler.remove_job(job.id)


def reload_jobs() -> None:
    """Rebuild all announcement jobs from the DB.

    **The countdown survives this.** In APScheduler an interval job without `next_run_time`
    starts a FULL interval after registration, and registration happens more than once:
    `_clear_jobs()` removes ALL `bcast_*` jobs, and this is called on every create, update and
    delete of ANY announcement (the routers reload globally, not per server) and on every
    backend start. Without preserving `next_run_time`, editing a completely different
    announcement on a different server would reset the countdown of an hourly announcement to
    zero, and on a day with several changes it would never fire.

    Cron jobs are not affected: their next run follows from the time of day.
    """
    # Before clearing, remember when the jobs would have run next.
    before = {
        job.id: job.next_run_time
        for job in scheduler.get_jobs()
        if job.id and job.id.startswith("bcast_") and job.next_run_time
    }
    _clear_jobs()
    with Session(engine) as session:
        rows = session.exec(select(BroadcastSchedule).where(BroadcastSchedule.enabled == True)).all()  # noqa: E712
    for b in rows:
        jid = f"bcast_{b.id}"
        if b.interval_minutes and b.interval_minutes > 0:
            next_run = before.get(jid)
            scheduler.add_job(
                _send, "interval", minutes=int(b.interval_minutes), id=jid,
                kwargs={"bid": b.id}, replace_existing=True, max_instances=1,
                misfire_grace_time=BCAST_GRACE_S,
                # Keep the part of the interval that has already elapsed (see docstring).
                **({"next_run_time": next_run} if next_run else {}),
            )
        elif b.time_hhmm:
            try:
                hour, minute = (int(x) for x in b.time_hhmm.split(":"))
            except (ValueError, AttributeError):
                continue
            scheduler.add_job(
                _send, "cron", hour=hour, minute=minute, day_of_week=_dow(b.days), id=jid,
                kwargs={"bid": b.id}, replace_existing=True, max_instances=1,
                misfire_grace_time=BCAST_GRACE_S,
            )
