"""Manage scheduled restarts (CRUD). Re-registers the APScheduler jobs on every change."""
from __future__ import annotations

import re

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlmodel import Session, select

from ..auth.deps import get_server, require_flag
from ..db import get_session
from ..errors import err
from ..models import GameServer, RestartSchedule, User
from ..scheduler import scheduler
from ..services import restarts
from ..services.audit import record
from ..services.servers import scope_clause


_manage = require_flag("can_restart")

_HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


class ScheduleIn(BaseModel):
    time_hhmm: str = "05:00"
    days: str = "daily"
    warn_minutes: str = "5,1"
    backup_first: bool = False
    enabled: bool = True


def _validate(body: ScheduleIn) -> None:
    if not _HHMM.match(body.time_hhmm.strip()):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail=err("schedule.time_invalid", "Time must be HH:MM (00:00–23:59)"),
        )
    for w in (x.strip() for x in str(body.warn_minutes).split(",") if x.strip()):
        if not w.isdigit() or not (1 <= int(w) <= 1439):
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                detail=err(
                    "schedule.warn_minutes_invalid",
                    "Warning minutes: only numbers 1–1439, comma-separated",
                ),
            )
    if not restarts.days_valid(body.days):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail=err(
                "schedule.days_invalid",
                "Days: 'daily' or abbreviations mon,tue,wed,thu,fri,sat,sun (comma-separated)",
            ),
        )


# --- Server-scoped routes ------------------------------------------------------
# Per `{key}`: new rows get the server_id, the list is filtered through `scope_clause`.
scoped_router = APIRouter(prefix="/api/servers", tags=["schedules"])


def _owns(sched: RestartSchedule, server: GameServer) -> bool:
    return sched.server_id == server.id


@scoped_router.get("/{key}/schedules")
def list_schedules_scoped(
    server: GameServer = Depends(get_server),
    session: Session = Depends(get_session),
    _: User = Depends(_manage),
) -> dict:
    rows = session.exec(
        select(RestartSchedule)
        .where(scope_clause(RestartSchedule.server_id, server))
        .order_by(RestartSchedule.time_hhmm)
    ).all()
    # **Whether a warning can arrive at all belongs in the response.** Valheim, Avorion and
    # Arma Reforger have no way to tell all players something; without this the form would
    # offer a lead time and the warning jobs would fire into the void. The UI can only say so
    # if it is told.
    warn_possible, warn_reason = restarts.can_announce(server)
    return {
        "schedules": rows,
        "next_restart": restarts.next_restart(server_id=server.id),
        "warn_possible": warn_possible,
        "warn_reason": warn_reason,
        # **The zone in which the entered time of day applies.** With a UTC default and a
        # different local time the form would otherwise be off by the UTC offset.
        "timezone": str(scheduler.timezone),
    }


@scoped_router.post("/{key}/schedules", status_code=status.HTTP_201_CREATED)
def create_schedule_scoped(
    body: ScheduleIn,
    server: GameServer = Depends(get_server),
    session: Session = Depends(get_session),
    user: User = Depends(_manage),
):
    _validate(body)
    sched = RestartSchedule(**body.model_dump(), server_id=server.id)
    session.add(sched)
    session.commit()
    session.refresh(sched)
    restarts.reload_jobs()
    record(session, user, "schedule.create", server.key, f"{sched.time_hhmm} {sched.days}")
    return sched


@scoped_router.put("/{key}/schedules/{sched_id}")
def update_schedule_scoped(
    sched_id: int,
    body: ScheduleIn,
    server: GameServer = Depends(get_server),
    session: Session = Depends(get_session),
    user: User = Depends(_manage),
):
    _validate(body)
    sched = session.get(RestartSchedule, sched_id)
    if not sched or not _owns(sched, server):
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, detail=err("schedule.not_found", "Schedule not found")
        )
    for k, v in body.model_dump().items():
        setattr(sched, k, v)
    session.add(sched)
    session.commit()
    session.refresh(sched)
    restarts.reload_jobs()
    record(session, user, "schedule.update", server.key, f"{sched.time_hhmm} {sched.days} enabled={sched.enabled}")
    return sched


@scoped_router.delete("/{key}/schedules/{sched_id}")
def delete_schedule_scoped(
    sched_id: int,
    server: GameServer = Depends(get_server),
    session: Session = Depends(get_session),
    user: User = Depends(_manage),
) -> dict:
    sched = session.get(RestartSchedule, sched_id)
    if not sched or not _owns(sched, server):
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, detail=err("schedule.not_found", "Schedule not found")
        )
    session.delete(sched)
    session.commit()
    restarts.reload_jobs()
    record(session, user, "schedule.delete", server.key, str(sched_id))
    return {"ok": True}
