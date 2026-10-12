"""Server incidents (watchdog): hangs, auto-restarts, recovery (new table → create_all)."""
from __future__ import annotations

import datetime as dt

from sqlmodel import Field, SQLModel

from .player import utcnow_naive


class ServerIncident(SQLModel, table=True):
    __tablename__ = "server_incidents"

    id: int | None = Field(default=None, primary_key=True)
    at: dt.datetime = Field(default_factory=utcnow_naive, index=True)
    kind: str = Field(index=True)  # "rcon_hang" | "auto_restart" | "recovered" | "restart_failed" | "idle_autostop"
    detail: str = ""
    server_id: int | None = Field(default=None, index=True)  # multi-game: originating server
