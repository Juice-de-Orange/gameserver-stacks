"""User model incl. roles, status and granular permission flags."""
from __future__ import annotations

import datetime as dt
import enum

from sqlmodel import Field, SQLModel


class Role(str, enum.Enum):
    admin = "admin"
    trusted = "trusted"
    member = "member"


class UserStatus(str, enum.Enum):
    pending = "pending"   # registered, waiting for admin approval
    active = "active"
    disabled = "disabled"


# Permission flags. Admins bypass the flags (role == admin -> everything allowed).
PERMISSION_FLAGS: tuple[str, ...] = (
    "can_view_status",
    "can_restart",
    "can_console_safe",
    "can_console_full",
    "can_edit_sandbox",
    "can_manage_mods",
    "can_manage_users",
    "can_view_logs",
    "can_manage_backups",
    "can_manage_tickets",
    "can_view_player_stats",
    "can_use_actions",
    "can_moderate",
    # --- multi-game: global flags, admin-only by default ---
    "can_manage_resources",   # RAM/CPU allocation + budget
    "can_manage_versions",    # version change + world rollback
    "can_manage_cross_server",  # configure cross-server features
)


class User(SQLModel, table=True):
    __tablename__ = "users"

    id: int | None = Field(default=None, primary_key=True)
    display_name: str
    username: str = Field(index=True, unique=True)
    # **Nullable on purpose.** Postgres allows any number of NULLs in a UNIQUE index; with
    # `NOT NULL DEFAULT ''` existing accounts would collide when the index is created (they
    # would all share the same empty value). `None` explicitly means "no address on file yet" —
    # an account without an address cannot use passwordless sign-in, and that should be visible
    # rather than guessed.
    email: str | None = Field(default=None, index=True, unique=True)
    pw_hash: str
    role: Role = Field(default=Role.member)
    status: UserStatus = Field(default=UserStatus.pending)
    must_change_pw: bool = Field(default=False)
    # Carried by every token as the `tv` claim and compared on each request. Raising it revokes
    # all tokens issued before (`auth.security.invalidate_tokens`).
    token_version: int = Field(default=0)

    # --- permission flags (dangerous ones default to False; admin bypasses all) ---
    can_view_status: bool = True
    can_restart: bool = False
    can_console_safe: bool = False
    can_console_full: bool = False        # default admin-only
    can_edit_sandbox: bool = False
    can_manage_mods: bool = False         # default admin-only
    can_manage_users: bool = False        # default admin-only
    can_view_logs: bool = False
    can_manage_backups: bool = False
    can_manage_tickets: bool = False
    can_view_player_stats: bool = False
    can_use_actions: bool = False         # game-master/RCON actions, default admin-only
    can_moderate: bool = False            # player moderation (kick/ban/access), default admin-only
    # --- multi-game: global flags, default admin-only ---
    can_manage_resources: bool = False    # RAM/CPU allocation + shared budget
    can_manage_versions: bool = False     # version change + world rollback
    can_manage_cross_server: bool = False  # configure cross-server features

    created_at: dt.datetime = Field(default_factory=lambda: dt.datetime.now(dt.timezone.utc))
    # `last_login` is the last SIGN-IN, `last_seen` the last request of any kind. Keeping the
    # two apart is the whole point: `last_login` is ONLY written on an interactive login and
    # `/api/auth/refresh` does not touch it — with a refresh token valid for days it would show
    # "5 days ago" for someone who is active right now.
    last_login: dt.datetime | None = Field(default=None)
    # Naive UTC (`utcnow_naive`), like all later timestamps — otherwise Postgres converts
    # against the session time zone when writing to a `TIMESTAMP` column.
    last_seen: dt.datetime | None = Field(default=None)
