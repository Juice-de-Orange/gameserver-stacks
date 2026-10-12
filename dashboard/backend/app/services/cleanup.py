"""Retention rules for the job-written tables `server_incidents`, `player_events`, `operations`.

Periodic jobs write into all three, and nothing else ever deletes from them:

* **`server_incidents`**: six modules write into it, and during a boot loop the watchdog opens
  a new incident per cycle.
* **`player_events`**: the event parser runs every 30 seconds over each enabled server and adds
  one row per death, join, leave and level-up, with four indexes.
* **`operations`**: one row per backup, restore, mod apply and version change, with `detail` up
  to 1000 and `result` up to 4000 characters. `cancel_stale()` only flips the status at start.

**The retention periods are deliberately long.** Growth is small; what matters is having a
RULE, not the free space. And `player_events` feeds the leaderboard and the death map: a short
period would not be tidying up but data loss. `player_sessions` is therefore NOT pruned at all
(see below).
"""
from __future__ import annotations

import datetime as dt
import logging

from sqlalchemy import delete as sa_delete
from sqlmodel import Session

from ..config import settings
from ..db import engine
from ..models import Operation, PlayerEvent, ServerIncident
from ..models.player import utcnow_naive

log = logging.getLogger("uvicorn.error")


def prune_history() -> None:
    """ONE scheduler job for the three tables. Errors stay inside the job.

    **`player_sessions` is missing here on purpose.** It is not a log table but a stock table:
    playtime, leaderboard and heatmap come from it. Pruning it would change those numbers, which
    is an operator decision, not housekeeping. Its growth is also bound to the number of play
    sessions, not to a job interval.
    """
    # CAUTION: the time column is NOT named the same everywhere. `ServerIncident` and
    # `PlayerEvent` have `at`, `Operation` has `started_at` (`models/operation.py`), and
    # `AuditLog` (not pruned here) is the only model with `created_at`. This tuple is built
    # BEFORE the loop and OUTSIDE the `try`, so a wrong attribute name raises `AttributeError`
    # and kills the job for all THREE tables before the first one is touched.
    retentions = (
        (ServerIncident, ServerIncident.at, settings.incident_retention_days, "server_incidents"),
        (PlayerEvent, PlayerEvent.at, settings.player_event_retention_days, "player_events"),
        (Operation, Operation.started_at, settings.operation_retention_days, "operations"),
    )
    for model, column, days, name in retentions:
        if not days or days <= 0:
            continue          # 0 = explicitly keep forever
        cutoff = utcnow_naive() - dt.timedelta(days=int(days))
        try:
            with Session(engine) as session:
                result = session.execute(sa_delete(model).where(column < cutoff))
                session.commit()
            if result.rowcount:
                log.info("cleanup: removed %d row(s) from %s (older than %d days)",
                         result.rowcount, name, int(days))
        except Exception:
            log.warning("cleanup: %s could not be pruned", name, exc_info=False)
