"""Shared APScheduler instance (avoids circular imports through main)."""
import logging
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from .config import settings

log = logging.getLogger("uvicorn.error")


def _zone():
    """The time zone in which EVERY scheduled time of day applies — explicit, not accidental.

    Without `timezone=` APScheduler uses `tzlocal.get_localzone()`. A container usually has no
    zone set, which yields UTC — a restart entered as "05:00" would then silently run at 05:00
    UTC instead of local time.

    An unknown zone falls back to UTC and says so loudly: a scheduler that aborts at startup
    because of a typo in `.env` would take every nightly job down with it.
    """
    name = (settings.scheduler_tz or "UTC").strip() or "UTC"
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        log.error("scheduler: time zone %r not found — falling back to UTC. Scheduled times of day "
                  "therefore run in UTC, not in local time.", name)
        return ZoneInfo("UTC")

# Without an explicit value `misfire_grace_time` is **one second** (`apscheduler/schedulers/base.py`:
# `asint(job_defaults.get("misfire_grace_time", 1))`). A run that starts later than that is
# dropped — for the weekly backup that would mean a week without a backup, visible only as a
# WARNING in the middle of the night. The loop missing that second is the normal case, not the
# edge case: all jobs are coroutines in the ONE uvicorn loop (hard constraint), next to
# `bridge_fanout` (12 s), `bridge_ingest` (25 s), stats/metrics/watchdog (60 s each) and every
# HTTP request. A one-hour catch-up window is right for everything whose value does not depend on
# the time of day; where it does (announcements), the job explicitly overrides it with its own
# tight deadline — `tests/test_scheduler_jobs.py` enforces both.
#
# `coalesce=True`: otherwise a caught-up interval job fires its entire backlog at once. It has
# been the default since APScheduler 3.x anyway — it is spelled out here because the grace time
# above is what makes it matter.
scheduler = AsyncIOScheduler(
    timezone=_zone(),
    job_defaults={"misfire_grace_time": 3600, "coalesce": True},
)
