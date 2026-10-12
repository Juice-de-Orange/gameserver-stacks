"""Pydantic request/response schemas."""
from __future__ import annotations

import datetime as dt

from pydantic import BaseModel, ConfigDict, Field, field_serializer

from .models import PERMISSION_FLAGS, Role, TicketCategory, TicketStatus, User, UserStatus


# --- Auth ---
class RegisterIn(BaseModel):
    display_name: str = Field(min_length=1, max_length=64)
    username: str = Field(min_length=3, max_length=32, pattern=r"^[A-Za-z0-9_.-]+$")
    password: str = Field(min_length=8, max_length=128)


class TokenOut(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    must_change_pw: bool = False


class RefreshIn(BaseModel):
    refresh_token: str


class AccessOut(BaseModel):
    access_token: str
    token_type: str = "bearer"


class PasswordChangeIn(BaseModel):
    old_password: str
    new_password: str = Field(min_length=8, max_length=128)


# --- Users (Admin) ---
class UserOut(BaseModel):
    id: int
    display_name: str
    username: str
    role: Role
    status: UserStatus
    must_change_pw: bool
    flags: dict[str, bool]            # raw per-user overrides (for the admin UI)
    effective_flags: dict[str, bool]  # effective (admin/role/override merged) — for gating
    created_at: dt.datetime | None = None
    last_login: dt.datetime | None = None   # last SIGN-IN
    last_seen: dt.datetime | None = None    # last request of any kind

    # The columns are `TIMESTAMP` without a zone, so they come back from the DB NAIVE and
    # Pydantic would serialise them without a zone suffix. `new Date("2026-09-22T16:00:00")` is
    # read as LOCAL time in the browser — off by the viewer's UTC offset wherever the value is
    # shown.
    #
    # **This fixes this one schema, NOT the whole class of bug.** Other call sites in the
    # frontend still append the `Z` by hand (`fmtRelative(..., needsZ)`) or not at all, and many
    # responses are not Pydantic schemas but plain dicts from `services/fleet.py` and its
    # neighbours. The proper fix would be ONE serialiser for the whole backend, after which
    # `needsZ` could go away entirely. That is still open.
    @field_serializer("created_at", "last_login", "last_seen")
    def _timestamp_as_utc(self, value: dt.datetime | None) -> str | None:
        if value is None:
            return None
        if value.tzinfo is None:
            value = value.replace(tzinfo=dt.timezone.utc)
        return value.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z")

    @classmethod
    def from_user(cls, u: User) -> "UserOut":
        from .auth.perm import effective_flags  # lazy: avoids import cycles at module load
        from .services import presence

        # The later of the two values wins: `users.last_seen` is only written in batches once
        # per minute (`services/presence.py`), while the in-memory note is current. Without
        # this the user list would show someone who is active right now as up to a minute stale.
        seen = max(
            (w for w in (presence.naive_utc(u.last_seen),
                         presence.latest_seen(u.id)) if w is not None),
            default=None,
        )

        return cls(
            id=u.id,
            display_name=u.display_name,
            username=u.username,
            role=u.role,
            status=u.status,
            must_change_pw=u.must_change_pw,
            flags={f: getattr(u, f) for f in PERMISSION_FLAGS},
            effective_flags=effective_flags(u),
            created_at=u.created_at,
            last_login=u.last_login,
            last_seen=seen,
        )


class ApproveIn(BaseModel):
    role: Role = Role.member
    flags: dict[str, bool] | None = None


class UserUpdateIn(BaseModel):
    role: Role | None = None
    flags: dict[str, bool] | None = None


class ResetPasswordIn(BaseModel):
    new_password: str | None = Field(default=None, min_length=8, max_length=128)


class ResetPasswordOut(BaseModel):
    temp_password: str


# --- Tickets (read schemas; bare SQLModel tables would otherwise serialise to {}) ---
class CommentRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    ticket_id: int
    author_id: int
    author_name: str
    body: str
    created_at: dt.datetime


class TicketRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    author_id: int
    author_name: str
    title: str
    workshop_url: str
    body: str
    status: TicketStatus
    category: TicketCategory
    workshop_id: str
    mod_id: str
    applied_at: dt.datetime | None
    assignee_id: int | None
    created_at: dt.datetime
    updated_at: dt.datetime


class TicketDetail(BaseModel):
    ticket: TicketRead
    comments: list[CommentRead]
