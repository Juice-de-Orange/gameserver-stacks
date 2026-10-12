"""Audit log: who ran which critical action / console command."""
from __future__ import annotations

import datetime as dt

from sqlmodel import Field, SQLModel


class AuditLog(SQLModel, table=True):
    __tablename__ = "audit_log"

    id: int | None = Field(default=None, primary_key=True)
    user_id: int | None = Field(default=None, index=True)
    username: str = ""
    action: str = Field(index=True)
    target: str = ""
    payload: str = ""
    server_id: int | None = Field(default=None, index=True)  # multi-game: originating server
    created_at: dt.datetime = Field(
        default_factory=lambda: dt.datetime.now(dt.timezone.utc), index=True
    )
