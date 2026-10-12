"""Bootstrap: seed the first admin user (only if it does not exist yet)."""
from __future__ import annotations

import logging

from sqlmodel import Session, select

from ..auth.security import hash_password
from ..config import settings
from ..db import engine
from ..models import PERMISSION_FLAGS, Role, RolePermission, User, UserStatus

log = logging.getLogger("uvicorn.error")

# Sensible role defaults on first start: a member only sees the status; dangerous
# permissions stay admin-only.
_ROLE_DEFAULTS: dict[Role, set[str]] = {
    Role.member: {"can_view_status"},
    Role.trusted: {
        "can_view_status",
        "can_view_player_stats",
        "can_view_logs",
        "can_console_safe",
    },
    Role.admin: set(PERMISSION_FLAGS),  # all, explicitly (admin bypasses the checks anyway)
}


def seed_admin() -> None:
    if not settings.seed_admin_password:
        return
    with Session(engine) as session:
        if session.exec(select(User).where(User.username == settings.seed_admin_username)).first():
            return
        admin = User(
            display_name=settings.seed_admin_username.capitalize(),
            username=settings.seed_admin_username,
            pw_hash=hash_password(settings.seed_admin_password),
            role=Role.admin,
            status=UserStatus.active,
            must_change_pw=True,
        )
        for flag in PERMISSION_FLAGS:
            setattr(admin, flag, True)
        session.add(admin)
        session.commit()
        log.info("Seed admin '%s' created (must_change_pw).", settings.seed_admin_username)


def seed_role_permissions() -> None:
    """Fill the role matrix — idempotent top-up that adds missing (role, flag) cells.

    This way NEWLY added flags get their default rows on an already seeded DB (admin = all,
    otherwise from _ROLE_DEFAULTS) without changing existing values.
    """
    with Session(engine) as session:
        existing = {(rp.role, rp.flag) for rp in session.exec(select(RolePermission)).all()}
        added = 0
        for role, flags in _ROLE_DEFAULTS.items():
            for flag in PERMISSION_FLAGS:
                if (role, flag) not in existing:
                    session.add(RolePermission(role=role, flag=flag, enabled=flag in flags))
                    added += 1
        if added:
            session.commit()
            log.info("Role matrix: added %d missing cells.", added)
