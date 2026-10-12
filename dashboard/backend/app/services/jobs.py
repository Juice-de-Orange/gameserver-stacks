"""Nightly runs that survive a backend restart.

**The problem.** The nightly jobs live in the `MemoryJobStore`; every process start registers
them anew, and `add_job` computes the next run from that moment. If the scheduled minute falls
into a window in which the process was not running, there is no job that could misfire:
`misfire_grace_time` only protects runs that a RUNNING scheduler picks up late. A restart
across the scheduled minute would drop the weekly run without replacement and silently: no log
line, no incident, no archive.

**The answer is a memory, not a longer grace period.** Every run writes its time into
`job_runs`; at start-up it is checked whether the last run is further back than the job's
interval allows. If so, ONE catch-up run is scheduled shortly after start.

**Two limits, both deliberate:**

* **Nothing is caught up the very first time.** Without a row in the table it is unknown
  whether the job ever ran, and "unknown" must not become "overdue" here: otherwise the first
  start with this table would immediately trigger a full backup of every server. Instead the
  start time is recorded, and the clock counts from the next regular run.
* **Catch-up happens ONCE, not for the whole backlog.** Two missed weekly runs yield one
  backup, not two; the older one would only contain today's world state anyway.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import inspect
import logging

from sqlmodel import Session

from ..db import engine
from ..models import JobRun
from ..models.player import utcnow_naive

log = logging.getLogger("uvicorn.error")

# How long after start-up the catch-up run is scheduled. Not immediately: seeding, registry
# reconciliation and `cancel_stale` run at start-up, and a weekly run archives gigabytes, which
# does not belong in the same second.
CATCHUP_DELAY_S = 180


def remember(job_id: str, *, now: dt.datetime | None = None) -> None:
    """Record this run as done. Errors here must not take down a job."""
    try:
        with Session(engine) as session:
            line = session.get(JobRun, job_id)
            if line is None:
                line = JobRun(job_id=job_id)
            line.last_run_at = now or utcnow_naive()
            session.add(line)
            session.commit()
    except Exception:
        log.warning("jobs: run %r could not be recorded", job_id, exc_info=False)


def last_run(job_id: str) -> dt.datetime | None:
    try:
        with Session(engine) as session:
            line = session.get(JobRun, job_id)
            return line.last_run_at if line else None
    except Exception:
        log.warning("jobs: last run of %r cannot be read", job_id, exc_info=False)
        return None


def logged(job_id: str, fn):
    """The same job, but it leaves a trace.

    The run is recorded AFTER it finishes and only without an exception: a failed weekly run is
    not a weekly run, and it should be caught up on the next start.

    **`fn` may be synchronous or asynchronous.** Several registered job functions are ordinary
    `def`s (`prune_metrics`, `prune_audit`, `prune_chat`), and a blank `await fn()` would raise
    `TypeError` on `await None`. Such a failure hides its own trace: because `remember` comes
    after the run, `job_runs` stays empty and `schedule_catchup` permanently takes the job for
    "first start" instead of "overdue" (see there).

    **Synchronous work runs in a thread, not in the event loop.** There is exactly ONE uvicorn
    worker; a prune run inside the loop would freeze the whole dashboard for its duration, the
    same reason `backup.py` uses `to_thread`.
    """
    async def _run():
        if inspect.iscoroutinefunction(fn):
            await fn()
        else:
            await asyncio.to_thread(fn)
        remember(job_id)

    _run.__name__ = f"{job_id}__logged"
    return _run


def schedule_catchup(scheduler, job_id: str, fn, *, max_gap_h: float) -> bool:
    """If this job is overdue, schedule a one-off catch-up run. Returns whether it did."""
    last = last_run(job_id)
    if last is None:
        # First known start: the clock begins now (see module docstring).
        remember(job_id)
        return False

    missed_h = (utcnow_naive() - last).total_seconds() / 3600.0
    if missed_h <= max_gap_h:
        return False

    when = dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=CATCHUP_DELAY_S)
    scheduler.add_job(
        logged(job_id, fn), "date", run_date=when, id=f"{job_id}__catchup",
        replace_existing=True, misfire_grace_time=3600,
    )
    log.warning(
        "jobs: %r is overdue (last run %.1f h ago, allowed are %.1f) — catching up in %d s. "
        "The cause is almost always a backend restart across the scheduled minute; the job did "
        "not exist during that time and so could not misfire either.",
        job_id, missed_h, max_gap_h, CATCHUP_DELAY_S,
    )
    return True
