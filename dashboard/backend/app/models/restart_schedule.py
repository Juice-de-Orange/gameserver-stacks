"""Scheduled server restarts with broadcast warnings.

New table → covered by create_all.
"""
from __future__ import annotations

import datetime as dt

from sqlmodel import Field, SQLModel


class RestartSchedule(SQLModel, table=True):
    __tablename__ = "restart_schedules"

    id: int | None = Field(default=None, primary_key=True)
    time_hhmm: str = "05:00"           # time of day HH:MM (in the scheduler time zone)
    days: str = "daily"               # "daily" or CSV of mon,tue,wed,thu,fri,sat,sun
    warn_minutes: str = "5,1"         # CSV of warning minutes (broadcasts before the restart)
    backup_first: bool = False        # take a backup before the restart
    enabled: bool = True
    server_id: int | None = Field(default=None, index=True)  # multi-game: for which server
    created_at: dt.datetime = Field(default_factory=lambda: dt.datetime.now(dt.timezone.utc))
