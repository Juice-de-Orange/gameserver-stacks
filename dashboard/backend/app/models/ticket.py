"""Mod-request tickets + comments."""
from __future__ import annotations

import datetime as dt
import enum

from sqlalchemy import String
from sqlmodel import Field, SQLModel


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


class TicketStatus(str, enum.Enum):
    open = "open"
    in_review = "in_review"
    approved = "approved"
    rejected = "rejected"
    done = "done"


class TicketCategory(str, enum.Enum):
    mod_request = "mod_request"     # mod request (self-service with admin approval)
    bug_report = "bug_report"
    player_report = "player_report"
    question = "question"
    general = "general"


class Ticket(SQLModel, table=True):
    __tablename__ = "tickets"

    id: int | None = Field(default=None, primary_key=True)
    author_id: int = Field(index=True)
    author_name: str = ""
    title: str
    workshop_url: str = ""
    body: str = ""
    status: TicketStatus = Field(default=TicketStatus.open, index=True)
    assignee_id: int | None = None
    # --- category + mod-request fields (DB columns via db._COLUMN_MIGRATIONS) ---
    # `sa_type=String` is intentional: the migration creates the column as VARCHAR (db.py), while
    # `create_all` would turn it into the native type `ticketcategory` on a FRESH database. That
    # would give a fresh database a constraint a grown one does not have — "works in tests,
    # breaks in production". The VARCHAR side wins because the other direction would need a
    # type change with a table rewrite in `init_db()`: that runs before everything else, and a
    # single unknown value in the column would prevent the app from starting.
    # Validation still happens — `TicketCategory` stays the Python/Pydantic type (schemas.py)
    # and every request passes through it. Only the database keeps the result as text.
    category: TicketCategory = Field(default=TicketCategory.general, index=True, sa_type=String)
    workshop_id: str = ""            # numeric ID parsed from the workshop link
    mod_id: str = ""                 # one or more mod IDs (;-separated), optional
    applied_at: dt.datetime | None = None  # set once it has been added to the mod list
    server_id: int | None = Field(default=None, index=True)  # multi-game: affected server
    created_at: dt.datetime = Field(default_factory=_now)
    updated_at: dt.datetime = Field(default_factory=_now)


class TicketComment(SQLModel, table=True):
    __tablename__ = "ticket_comments"

    id: int | None = Field(default=None, primary_key=True)
    ticket_id: int = Field(index=True)
    author_id: int
    author_name: str = ""
    body: str
    created_at: dt.datetime = Field(default_factory=_now)
