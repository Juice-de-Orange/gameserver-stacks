"""Idle auto-stop (multi-game): stops servers after a long time without players.

ONE hourly scheduler job (`max_instances=1`) over all enabled servers. The timer is long
(default ~20 days), so hourly sampling suffices: `last_active_at` is set whenever players are
seen; if the idle time exceeds the effective timeout AND a LIVE query confirms 0 players, the
server is stopped.

**Safety:** RCON unreachable means "unknown" (not "empty") → do NOT stop (a hanging server is
the watchdog's business, not the idle stop's). On the very first sighting `last_active_at` is
only set (grace), the server is not stopped right away.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import logging
from typing import TYPE_CHECKING

from sqlmodel import Session

from ..db import engine
from ..models import ServerIncident
from ..models.player import utcnow_naive
from ..models.server import GameServer
from ..services import backup, lifecycle
from ..services import resource_budget as budget_svc
from ..services import servers as servers_registry
from ..services.docker_client import docker, start_time as docker_start_time

if TYPE_CHECKING:
    from ..services.drivers import GameDriver

log = logging.getLogger("uvicorn.error")

# How long a freshly started server is spared from the idle stop. Sized for its purpose: start
# the server, start the game, join. The job runs hourly, so the window is effectively
# 60–120 min. Generous enough for joining, too short to defeat the idle stop.
START_GRACE_MIN = 60

# How often the round runs. Defined here and not in `main.py` because the number is needed
# twice: the scheduler uses it, and the display must be able to say "at the earliest": a
# server whose deadline expires at 03:05 is only stopped by the run at 04:00.
JOB_INTERVAL_MIN = 60


@dataclasses.dataclass(frozen=True)
class IdleStatus:
    """What the idle stop currently intends for this server.

    **Why this type exists:** if the stop rule lived only in `_check_server`, anyone wanting to
    show the countdown would have to rebuild the rule in the frontend, i.e. a second version of
    the same decision that diverges at the first special case and makes the display lie.

    Hence: ONE function answers the question, `_check_server` acts on it, and the API shows the
    same result. `tests/test_idle_status_is_one_source.py` pins that down.
    """

    active: bool
    """Is the idle stop enabled AND armed for this server at all?"""
    timeout_min: int
    """The effective timeout (server value, otherwise the global default). 0 = none."""
    reason: str
    """`off` | `no_timeout` | `not_running` | `never_seen` | `grace_period` | `counting` | `due`"""
    last_active_at: dt.datetime | None
    earliest_stop_at: dt.datetime | None
    """Earliest possible stop, **without** the job interval. The actual stop is up to
    `JOB_INTERVAL_MIN` later, and does not happen at all if RCON is reachable with players."""
    remaining_min: int | None


def idle_status(
    server: GameServer,
    *,
    running: bool,
    started_at: dt.datetime | None,
    default_timeout: int,
    now: dt.datetime,
) -> IdleStatus:
    """The stop rule as a pure function: no Docker, no RCON, no database.

    The order of the checks is significant: the job takes its decision through this function,
    so nothing may shift.
    """
    timeout_min = server.idle_timeout_minutes if server.idle_timeout_minutes is not None else default_timeout
    timeout_min = int(timeout_min or 0)

    def status(reason: str, *, active: bool, earliest: dt.datetime | None = None) -> IdleStatus:
        rest = None if earliest is None else max(int((earliest - now).total_seconds() // 60), 0)
        return IdleStatus(
            active=active, timeout_min=timeout_min, reason=reason,
            last_active_at=server.last_active_at, earliest_stop_at=earliest, remaining_min=rest,
        )

    if not server.idle_autostop_enabled:
        return status("off", active=False)
    if timeout_min <= 0:
        return status("no_timeout", active=False)
    if not running:
        return status("not_running", active=True)
    if server.last_active_at is None:
        # First sighting: the round only sets the clock. There is no deadline yet.
        return status("never_seen", active=True)

    due = server.last_active_at + dt.timedelta(minutes=timeout_min)
    grace_until = None if started_at is None else started_at + dt.timedelta(minutes=START_GRACE_MIN)
    # The grace period pushes the earliest possible stop back, but it does not cancel the
    # deadline, hence the maximum of both and not "either or".
    earliest = due if grace_until is None else max(due, grace_until)

    if grace_until is not None and now < grace_until:
        return status("grace_period", active=True, earliest=earliest)
    if now < due:
        return status("counting", active=True, earliest=earliest)
    return status("due", active=True, earliest=earliest)


def _touch(server_id: int | None) -> None:
    if server_id is None:
        return
    with Session(engine) as s:
        row = s.get(GameServer, server_id)
        if row:
            row.last_active_at = utcnow_naive()
            s.add(row)
            s.commit()


def _incident(server_id: int | None, detail: str) -> None:
    try:
        with Session(engine) as s:
            s.add(ServerIncident(kind="idle_autostop", detail=detail, server_id=server_id))
            s.commit()
    except Exception:
        log.warning("idle_autostop: incident could not be saved", exc_info=False)


async def check_once() -> None:
    """ONE job: idle round over all enabled servers."""
    default_timeout = budget_svc.get_budget().default_idle_timeout_minutes
    for server, drv in servers_registry.list_enabled_with_driver():
        try:
            await _check_server(server, drv, default_timeout)
        except Exception:
            log.warning("idle_autostop[%s]: round failed", server.key, exc_info=False)


async def _check_server(server: GameServer, drv: GameDriver, default_timeout: int) -> None:
    st = await docker.state(server.container_name)

    # The decision is made in `idle_status`, the same function the UI feeds its countdown
    # from. This code only states what FOLLOWS from which result.
    status = idle_status(
        server,
        running=bool(st.get("running")),
        started_at=docker_start_time(st.get("started_at")),
        default_timeout=default_timeout,
        now=utcnow_naive(),
    )

    if status.reason == "never_seen":
        _touch(server.id)  # first sighting -> start the clock, do not stop right away (grace)
        return
    if status.reason != "due":
        # off | no_timeout | not_running | grace_period | counting -> nothing to do.
        #
        # The grace period protects the automatic starts (autoheal, restart after a host
        # reboot), which do not set the clock and would otherwise be stopped again within the
        # hour.
        #
        # The clock is deliberately NOT written back here: `started_at` comes from Docker and
        # does not say WHO started the server; the nightly restart schedule renews it every day
        # and the clock would never get past 24 h. The clock is set by the one entitled to:
        # `routers/server.py::perform_action` on every start/restart WITH a user.
        return

    # Deadline expired -> check LIVE. RCON unreachable = unknown -> do NOT stop.
    try:
        players = await drv.players(server)
    except Exception:
        return
    if players.get("count", 0) > 0:
        _touch(server.id)  # active after all
        return

    # **No stop while this folder is being archived.**
    #
    # `scheduled_backup` asks the world lock before each server and skips it, so that no
    # "regularly reported archive of an intermediate state" is produced. This is the opposite
    # direction: `_stop_locked` shuts the game down through its own console, and PZ then writes
    # the COMPLETE world while `tarfile` is reading exactly those files.
    spec = drv.backup_spec(server)
    if backup.is_packing(spec.data_dir):
        log.info("idle_autostop[%s]: a backup is running for this server — not stopped",
                 server.key)
        return

    idle_min = int((utcnow_naive() - server.last_active_at).total_seconds() / 60)

    async def _still_due() -> bool:
        """Repeat the decision INSIDE the lock.

        Between the `docker.state` at the top and this point there are two `await` points where
        other jobs get to run: the RCON query runs until its timeout on a dead server, and
        `check_once` iterates sequentially, so the snapshot for the last server can be a minute
        old. On top of that comes waiting for `lifecycle._lock` itself, which a scheduled
        restart holds across stop AND start.
        If another job starts the server in that window, the first `idle_status` still saw
        the OLD `started_at` with the grace period in the past, and the freshly started server
        would be stopped seconds after its start. `START_GRACE_MIN` exists to prevent exactly
        that.
        """
        fresh = await docker.state(server.container_name)
        new_status = idle_status(
            server,
            running=bool(fresh.get("running")),
            started_at=docker_start_time(fresh.get("started_at")),
            default_timeout=default_timeout,
            now=utcnow_naive(),
        )
        if new_status.reason != "due":
            log.info("idle_autostop[%s]: stop cancelled — the situation is now %r",
                     server.key, new_status.reason)
            return False
        return True

    if not await lifecycle.stop_server(server, precondition=_still_due):
        return
    _incident(
        server.id,
        f"Idle auto-stop after {idle_min} min without players (timeout {status.timeout_min} min).",
    )
    log.info("idle_autostop[%s]: stopped after %d min idle", server.key, idle_min)
