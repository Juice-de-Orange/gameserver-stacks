"""User management (admin / can_manage_users): approval, roles/flags, password reset."""
from __future__ import annotations

import secrets
import string

from fastapi import APIRouter, Depends, HTTPException, status
from sqlmodel import Session, select

from ..auth.deps import require_flag
from ..auth.security import hash_password, invalidate_tokens
from ..db import get_session
from ..errors import err
from ..models import PERMISSION_FLAGS, Role, User, UserStatus
from ..schemas import ApproveIn, ResetPasswordIn, ResetPasswordOut, UserOut, UserUpdateIn
from ..services.audit import record

router = APIRouter(prefix="/api/users", tags=["users"])

# User management: admin or can_manage_users
_manage = require_flag("can_manage_users")


def _apply_flags(user: User, flags: dict[str, bool] | None) -> None:
    for key, value in (flags or {}).items():
        if key in PERMISSION_FLAGS:
            setattr(user, key, bool(value))


def _guard_target(me: User, target: User) -> None:
    """Privilege-escalation guard: non-admins with `can_manage_users` must NOT touch an admin
    account (otherwise they could reset admin passwords or lock admins out). Admins may do
    anything."""
    if me.role != Role.admin and target.role == Role.admin:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            detail=err("users.admin_accounts_admin_only", "Only admins may manage admin accounts."),
        )


def _guard_role_and_flags(me: User, new_role: Role | None, flags: dict[str, bool] | None) -> None:
    """Only admins may grant the admin role and set permission flags — otherwise
    `can_manage_users` would transitively be full admin (self-promotion, flag grant)."""
    if me.role == Role.admin:
        return
    # ANY role other than `member`, not just `admin`. Checking only `new_role == Role.admin`
    # would let `trusted` through, and `_guard_target` only blocks OTHER admin accounts, not
    # one's own — a non-admin with `can_manage_users` could promote themselves to `trusted`.
    #
    # That is not cosmetic: by the default matrix `trusted` comes with `can_console_safe`, i.e.
    # RCON verbs on EVERY game server, plus `can_view_logs` and `can_view_player_stats`. And
    # the matrix can be changed by admins — what `trusted` can do is not static.
    #
    # `member` stays allowed because `approve()` has to pass with its default `Role.member`:
    # approving someone is exactly the job of `can_manage_users`.
    if new_role is not None and new_role != Role.member:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            detail=err("users.roles_admin_only", "Only admins may assign roles."),
        )
    if flags:  # non-empty flags dict
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            detail=err("users.flags_admin_only", "Only admins may set permission flags."),
        )


def _get_or_404(session: Session, user_id: int) -> User:
    user = session.get(User, user_id)
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=err("users.not_found", "User not found"))
    return user


def _gen_password(length: int = 14) -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


@router.get("", response_model=list[UserOut])
def list_users(
    status_filter: UserStatus | None = None,
    session: Session = Depends(get_session),
    _: User = Depends(_manage),
) -> list[UserOut]:
    query = select(User).order_by(User.id)
    if status_filter is not None:
        query = query.where(User.status == status_filter)
    return [UserOut.from_user(u) for u in session.exec(query).all()]


@router.post("/{user_id}/approve", response_model=UserOut)
def approve(
    user_id: int,
    body: ApproveIn,
    session: Session = Depends(get_session),
    me: User = Depends(_manage),
) -> UserOut:
    user = _get_or_404(session, user_id)
    _guard_target(me, user)
    _guard_role_and_flags(me, body.role, body.flags)
    user.status = UserStatus.active
    user.role = body.role
    _apply_flags(user, body.flags)
    invalidate_tokens(user)  # status/role change
    session.add(user)
    session.commit()
    session.refresh(user)
    record(session, me, "user.approve", user.username, f"role={body.role.value}")
    return UserOut.from_user(user)


@router.post("/{user_id}/disable", response_model=UserOut)
def disable(user_id: int, session: Session = Depends(get_session), me: User = Depends(_manage)) -> UserOut:
    user = _get_or_404(session, user_id)
    _guard_target(me, user)
    # Do not lock out the last active admin.
    if user.role == Role.admin and user.status == UserStatus.active:
        active_admins = session.exec(
            select(User).where(User.role == Role.admin, User.status == UserStatus.active)
        ).all()
        if len(active_admins) <= 1:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                detail=err("users.last_admin_disable", "The last active admin cannot be disabled."),
            )
    user.status = UserStatus.disabled
    invalidate_tokens(user)  # tokens issued before must not work again after a later enable
    session.add(user)
    session.commit()
    session.refresh(user)
    record(session, me, "user.disable", user.username)
    return UserOut.from_user(user)


@router.post("/{user_id}/enable", response_model=UserOut)
def enable(user_id: int, session: Session = Depends(get_session), me: User = Depends(_manage)) -> UserOut:
    user = _get_or_404(session, user_id)
    _guard_target(me, user)
    user.status = UserStatus.active
    invalidate_tokens(user)  # status change
    session.add(user)
    session.commit()
    session.refresh(user)
    record(session, me, "user.enable", user.username)
    return UserOut.from_user(user)


@router.patch("/{user_id}", response_model=UserOut)
def update_user(
    user_id: int,
    body: UserUpdateIn,
    session: Session = Depends(get_session),
    me: User = Depends(_manage),
) -> UserOut:
    user = _get_or_404(session, user_id)
    _guard_target(me, user)
    _guard_role_and_flags(me, body.role, body.flags)
    if body.role is not None:
        if body.role != user.role:
            invalidate_tokens(user)  # role change
        user.role = body.role
    _apply_flags(user, body.flags)
    session.add(user)
    session.commit()
    session.refresh(user)
    changed = ",".join(k for k in ("role", "flags") if getattr(body, k) is not None)
    record(session, me, "user.update", user.username, changed)
    return UserOut.from_user(user)


@router.post("/{user_id}/reset-password", response_model=ResetPasswordOut)
def reset_password(
    user_id: int,
    body: ResetPasswordIn,
    session: Session = Depends(get_session),
    me: User = Depends(_manage),
) -> ResetPasswordOut:
    """Sets a temporary password (generated or given) and shows it to the admin once."""
    user = _get_or_404(session, user_id)
    _guard_target(me, user)  # non-admins must not reset admin passwords (takeover protection)
    temp = body.new_password or _gen_password()
    user.pw_hash = hash_password(temp)
    user.must_change_pw = True
    invalidate_tokens(user)  # sessions opened with the old password end here
    session.add(user)
    session.commit()
    record(session, me, "user.reset_password", user.username)  # do NOT log the temp password itself
    return ResetPasswordOut(temp_password=temp)


@router.delete("/{user_id}")
def delete_user(user_id: int, session: Session = Depends(get_session), me: User = Depends(_manage)) -> dict:
    """Permanently delete a registered user (e.g. test accounts). Guards: not yourself, not the
    last active admin, admin accounts only by admins."""
    user = _get_or_404(session, user_id)
    _guard_target(me, user)
    if user.id == me.id:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, detail=err("users.self_delete", "You cannot delete yourself.")
        )
    # Only block if the account being deleted is ITSELF an active admin (a disabled admin does
    # not count as active → deleting it locks nobody out).
    if user.role == Role.admin and user.status == UserStatus.active:
        active_admins = session.exec(
            select(User).where(User.role == Role.admin, User.status == UserStatus.active)
        ).all()
        if len(active_admins) <= 1:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                detail=err("users.last_admin_delete", "The last active admin cannot be deleted."),
            )
    uname = user.username
    session.delete(user)
    session.commit()
    record(session, me, "user.delete", uname, "")
    return {"ok": True}
