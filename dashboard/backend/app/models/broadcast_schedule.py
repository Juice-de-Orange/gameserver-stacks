"""Scheduled server announcements (recurring servermsg). New table → covered by create_all.

Either `interval_minutes` (every N minutes) OR `time_hhmm` + `days` (cron).
"""
from __future__ import annotations

import datetime as dt

from sqlmodel import Field, SQLModel


class BroadcastSchedule(SQLModel, table=True):
    __tablename__ = "broadcast_schedules"

    id: int | None = Field(default=None, primary_key=True)
    message: str = ""
    interval_minutes: int | None = None   # set → every N minutes; otherwise time_hhmm+days
    time_hhmm: str | None = None          # "HH:MM" for a daily/cron announcement
    days: str = "daily"                   # daily or CSV mon,tue,wed,thu,fri,sat,sun
    enabled: bool = True
    server_id: int | None = Field(default=None, index=True)  # multi-game: target server
    target_all: bool = False              # True → one announcement to ALL running servers
    created_at: dt.datetime = Field(default_factory=lambda: dt.datetime.now(dt.timezone.utc))
