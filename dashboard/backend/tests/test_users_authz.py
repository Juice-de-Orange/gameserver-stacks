"""Privilege escalation via can_manage_users.

A non-admin with the flag can_manage_users must not promote themselves or others to admin,
reset admin passwords or disable admins. The guards bind these privileged operations to
require_admin.
"""
import pytest
from fastapi import HTTPException
from sqlmodel import Session

from app.models import Role, User, UserStatus
from app.routers import users as users_router
from app.schemas import ApproveIn, UserUpdateIn


def _mk(session, username, role, status=UserStatus.active, **flags):
    u = User(display_name=username, username=username, pw_hash="x", role=role, status=status)
    for k, v in flags.items():
        setattr(u, k, v)
    session.add(u)
    session.commit()
    session.refresh(u)
    return u


def test_non_admin_cannot_self_promote(mem_engine):
    with Session(mem_engine) as s:
        manager = _mk(s, "manager", Role.member, can_manage_users=True)
        with pytest.raises(HTTPException) as ei:
            users_router.update_user(manager.id, UserUpdateIn(role=Role.admin), s, manager)
        assert ei.value.status_code == 403


def test_non_admin_cannot_promote_other(mem_engine):
    with Session(mem_engine) as s:
        manager = _mk(s, "manager", Role.member, can_manage_users=True)
        victim = _mk(s, "victim", Role.member)
        with pytest.raises(HTTPException) as ei:
            users_router.update_user(victim.id, UserUpdateIn(role=Role.admin), s, manager)
        assert ei.value.status_code == 403


def test_non_admin_cannot_grant_flags(mem_engine):
    with Session(mem_engine) as s:
        manager = _mk(s, "manager", Role.member, can_manage_users=True)
        victim = _mk(s, "victim", Role.member)
        with pytest.raises(HTTPException) as ei:
            users_router.update_user(victim.id, UserUpdateIn(flags={"can_console_full": True}), s, manager)
        assert ei.value.status_code == 403


def test_non_admin_cannot_touch_admin_account(mem_engine):
    with Session(mem_engine) as s:
        manager = _mk(s, "manager", Role.member, can_manage_users=True)
        admin = _mk(s, "admin", Role.admin)
        # reset-password on an admin → 403
        with pytest.raises(HTTPException) as ei:
            from app.schemas import ResetPasswordIn
            users_router.reset_password(admin.id, ResetPasswordIn(), s, manager)
        assert ei.value.status_code == 403
        # disabling an admin → 403
        with pytest.raises(HTTPException) as ei:
            users_router.disable(admin.id, s, manager)
        assert ei.value.status_code == 403


def test_non_admin_can_still_approve_member(mem_engine):
    """The delegated use case remains: a non-admin approves a member."""
    with Session(mem_engine) as s:
        manager = _mk(s, "manager", Role.member, can_manage_users=True)
        pending = _mk(s, "pending", Role.member, status=UserStatus.pending)
        out = users_router.approve(pending.id, ApproveIn(role=Role.member), s, manager)
        assert out.status == UserStatus.active
        assert out.role == Role.member


def test_admin_can_promote(mem_engine):
    with Session(mem_engine) as s:
        admin = _mk(s, "admin", Role.admin)
        victim = _mk(s, "victim", Role.member)
        out = users_router.update_user(victim.id, UserUpdateIn(role=Role.admin), s, admin)
        assert out.role == Role.admin


def test_disabled_admin_deletable_even_as_sole_active_admin(mem_engine):
    """The last-admin protection checks the status: a DISABLED admin can be deleted even when only
    one active admin exists (deleting it locks nobody out)."""
    with Session(mem_engine) as s:
        admin = _mk(s, "admin", Role.admin)  # only active admin
        old = _mk(s, "old", Role.admin, status=UserStatus.disabled)
        assert users_router.delete_user(old.id, s, admin) == {"ok": True}


def test_cannot_disable_last_active_admin(mem_engine):
    with Session(mem_engine) as s:
        admin = _mk(s, "admin", Role.admin)  # only active admin
        with pytest.raises(HTTPException) as ei:
            users_router.disable(admin.id, s, admin)
        assert ei.value.status_code == 400


def test_non_admin_cannot_self_promote_to_trusted(mem_engine):
    """Not only the ADMIN role is locked — `trusted` must be too.

    `_guard_target` only blocks other admin accounts; the own account passes it. Without this a
    non-admin with `can_manage_users` could promote themselves to `trusted` via PATCH on their own
    ID. That is not cosmetic: with the default matrix `trusted` brings `can_console_safe` — RCON
    verbs on EVERY game server — plus `can_view_logs` and `can_view_player_stats`. And the matrix
    is admin-editable, so what trusted can do is not static.

    Via `POST /{id}/approve` (ApproveIn.role) the same user could also approve arbitrary pending
    self-registrations as trusted.
    """
    with Session(mem_engine) as s:
        manager = _mk(s, "manager", Role.member, can_manage_users=True)
        with pytest.raises(HTTPException) as ei:
            users_router.update_user(manager.id, UserUpdateIn(role=Role.trusted), s, manager)
        assert ei.value.status_code == 403

        # The cross-check: an admin still may.
        admin = _mk(s, "boss", Role.admin)
        users_router.update_user(manager.id, UserUpdateIn(role=Role.trusted), s, admin)
        assert s.get(User, manager.id).role == Role.trusted


def test_a_non_admin_cannot_approve_anyone_as_trusted(mem_engine):
    """The same gap via the approval path."""
    with Session(mem_engine) as s:
        manager = _mk(s, "manager2", Role.member, can_manage_users=True)
        waiting = _mk(s, "newcomer", Role.member, status=UserStatus.pending)
        with pytest.raises(HTTPException) as ei:
            users_router.approve(waiting.id, ApproveIn(role=Role.trusted), s, manager)
        assert ei.value.status_code == 403
