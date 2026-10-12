"""Manage scheduled announcements (broadcasts) — CRUD. Re-registers the APScheduler jobs on
every change. Lives in the configuration tab → gated by `can_edit_sandbox`.
"""
from __future__ import annotations

import re

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlmodel import Session, select

from ..auth.deps import get_server, require_flag
from ..db import get_session
from ..errors import err
from ..models import BroadcastSchedule, GameServer, User
from ..services import broadcasts
from ..services import restarts as restarts_svc
from ..services.audit import record
from ..services.servers import scope_clause


_manage = require_flag("can_edit_sandbox")
_HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


class BroadcastIn(BaseModel):
    message: str
    interval_minutes: int | None = None
    time_hhmm: str | None = None
    days: str = "daily"
    enabled: bool = True


def _validate(body: BroadcastIn) -> None:
    msg = (body.message or "").strip()
    if not (1 <= len(msg) <= 200):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail=err("broadcast.message_length", "Message must be 1–200 characters long"),
        )
    has_interval = bool(body.interval_minutes and body.interval_minutes > 0)
    has_time = bool(body.time_hhmm and _HHMM.match(body.time_hhmm.strip()))
    if not has_interval and not has_time:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail=err(
                "broadcast.interval_or_time_required",
                "Give either an interval (minutes ≥ 1) or a time of day HH:MM",
            ),
        )
    # `days` only applies to time-of-day mode; invalid abbreviations would silently fall back to DAILY in the service.
    if has_time and not restarts_svc.days_valid(body.days):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail=err(
                "schedule.days_invalid",
                "Days: 'daily' or abbreviations mon,tue,wed,thu,fri,sat,sun (comma-separated)",
            ),
        )


def _normalize(b: BroadcastSchedule) -> None:
    if not (b.time_hhmm and str(b.time_hhmm).strip()):
        b.time_hhmm = None
    if not b.interval_minutes:
        b.interval_minutes = None


# --- Server-scoped routes ------------------------------------------------------
scoped_router = APIRouter(prefix="/api/servers", tags=["broadcasts"])


def _owns(b: BroadcastSchedule, server: GameServer) -> bool:
    return b.server_id == server.id


@scoped_router.get("/{key}/broadcasts")
def list_broadcasts_scoped(
    server: GameServer = Depends(get_server),
    session: Session = Depends(get_session),
    _: User = Depends(_manage),
) -> dict:
    rows = session.exec(
        select(BroadcastSchedule)
        .where(scope_clause(BroadcastSchedule.server_id, server))
        .order_by(BroadcastSchedule.id)
    ).all()
    return {"broadcasts": rows}


@scoped_router.post("/{key}/broadcasts", status_code=status.HTTP_201_CREATED)
def create_broadcast_scoped(
    body: BroadcastIn,
    server: GameServer = Depends(get_server),
    session: Session = Depends(get_session),
    user: User = Depends(_manage),
):
    _validate(body)
    b = BroadcastSchedule(**body.model_dump(), server_id=server.id)
    _normalize(b)
    session.add(b)
    session.commit()
    session.refresh(b)
    broadcasts.reload_jobs()
    record(session, user, "broadcast.create", server.key, b.message[:80])
    return b


@scoped_router.put("/{key}/broadcasts/{bid}")
def update_broadcast_scoped(
    bid: int,
    body: BroadcastIn,
    server: GameServer = Depends(get_server),
    session: Session = Depends(get_session),
    user: User = Depends(_manage),
):
    _validate(body)
    b = session.get(BroadcastSchedule, bid)
    if not b or not _owns(b, server):
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, detail=err("broadcast.not_found", "Announcement not found")
        )
    for k, v in body.model_dump().items():
        setattr(b, k, v)
    _normalize(b)
    session.add(b)
    session.commit()
    session.refresh(b)
    broadcasts.reload_jobs()
    record(session, user, "broadcast.update", server.key, str(bid))
    return b


@scoped_router.delete("/{key}/broadcasts/{bid}")
def delete_broadcast_scoped(
    bid: int,
    server: GameServer = Depends(get_server),
    session: Session = Depends(get_session),
    user: User = Depends(_manage),
) -> dict:
    b = session.get(BroadcastSchedule, bid)
    if not b or not _owns(b, server):
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, detail=err("broadcast.not_found", "Announcement not found")
        )
    session.delete(b)
    session.commit()
    broadcasts.reload_jobs()
    record(session, user, "broadcast.delete", server.key, str(bid))
    return {"ok": True}
