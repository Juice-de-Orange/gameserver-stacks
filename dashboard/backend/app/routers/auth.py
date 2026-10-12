"""Auth endpoints: registration, login, token refresh, profile, change password."""
from __future__ import annotations

import datetime as dt

import jwt
from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy import func
from sqlmodel import Session, select

from ..auth import ratelimit
from ..auth.deps import get_current_user
from ..auth.security import (
    WS_TICKET_TTL_SEC,
    decode_token,
    hash_password,
    invalidate_tokens,
    make_access_token,
    make_refresh_token,
    make_ws_ticket,
    token_version_of,
    verify_password,
)
from ..db import get_session
from ..errors import err
from ..models import Role, User, UserStatus
from ..schemas import AccessOut, PasswordChangeIn, RefreshIn, RegisterIn, TokenOut, UserOut
from ..services.audit import record

router = APIRouter(prefix="/api/auth", tags=["auth"])


# How many pending registrations may be open at the same time. The cap sits BEFORE the argon2
# hash, and that is the point: otherwise every call costs 64 MiB of memory and three time
# rounds (RFC_9106_LOW_MEMORY), unauthenticated and reachable from the internet. `register` is
# synchronous, so FastAPI runs it in the anyio thread pool — up to 40 concurrent hashes of
# 64 MiB each while the same threads block every other synchronous endpoint. Meanwhile the
# `users` table fills up and drowns the admin approval list.
MAX_PENDING = 25


@router.post("/register", status_code=status.HTTP_201_CREATED)
def register(request: Request, body: RegisterIn, session: Session = Depends(get_session)) -> dict:
    # Same throttle as for the login: this is the second publicly reachable write path, and
    # the more expensive one.
    keys = (f"reg-ip:{ratelimit.client_ip(request)}",)
    if (wait := ratelimit.retry_after(keys)):
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            detail=err("auth.too_many_registrations", "Too many registrations — please try again later."),
            headers={"Retry-After": str(wait)},
        )
    # Every registration counts, not just a failed one: the cost arises with the attempt, not
    # with the failure.
    ratelimit.record_failure(keys)

    pending = session.exec(
        select(func.count()).select_from(User).where(User.status == UserStatus.pending)
    ).one()
    if pending >= MAX_PENDING:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=err(
                "auth.registration_queue_full",
                "Too many registrations are already waiting for approval. Please try again later.",
            ),
        )

    if session.exec(select(User).where(User.username == body.username)).first():
        raise HTTPException(
            status.HTTP_409_CONFLICT, detail=err("auth.username_taken", "Username is already taken"),
        )
    user = User(
        display_name=body.display_name,
        username=body.username,
        pw_hash=hash_password(body.password),
        role=Role.member,
        status=UserStatus.pending,
    )
    session.add(user)
    session.commit()
    return {"message": "Registration received — waiting for approval by an admin."}


def _throttle_keys(request: Request, username: str) -> tuple[str, str]:
    """Keys for the login throttle — with a truncated username.

    `OAuth2PasswordRequestForm` declares `username: str = Form()` **without** `max_length`, and
    neither Caddy nor uvicorn limits a field; unlike the multipart parser, Starlette's urlencoded
    parser has no cap. The untruncated value would end up as a dict key in the throttle and stay
    there. `schemas.RegisterIn` limits the name to 32 characters, but that only covers
    registration, not login.
    """
    return (f"ip:{ratelimit.client_ip(request)}", f"user:{username.lower()[:64]}")


@router.post("/login", response_model=TokenOut)
def login(
    request: Request,
    form: OAuth2PasswordRequestForm = Depends(),
    session: Session = Depends(get_session),
) -> TokenOut:
    # Brute-force throttle: count per IP AND per username (in-memory, since single worker).
    keys = _throttle_keys(request, form.username)
    wait = ratelimit.retry_after(keys)
    if wait:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            detail=err("auth.too_many_attempts", "Too many failed attempts — please try again later."),
            headers={"Retry-After": str(wait)},
        )
    user = session.exec(select(User).where(User.username == form.username)).first()
    if not user or not verify_password(form.password, user.pw_hash):
        ratelimit.record_failure(keys)
        record(
            session, user, "auth.login_failed", form.username,
            "wrong password" if user else "unknown username",
        )
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED, detail=err("auth.login_failed", "Wrong username or password")
        )
    if user.status == UserStatus.pending:
        record(session, user, "auth.login_denied", form.username, "account pending")
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            detail=err("auth.account_pending", "Account has not been approved yet"),
        )
    if user.status == UserStatus.disabled:
        record(session, user, "auth.login_denied", form.username, "account disabled")
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail=err("auth.account_disabled", "Account disabled"))
    ratelimit.reset(keys)
    user.last_login = dt.datetime.now(dt.timezone.utc)
    session.add(user)
    session.commit()
    return TokenOut(
        access_token=make_access_token(user.id, user.token_version),
        refresh_token=make_refresh_token(user.id, user.token_version),
        must_change_pw=user.must_change_pw,
    )


@router.post("/refresh", response_model=AccessOut)
def refresh(body: RefreshIn, session: Session = Depends(get_session)) -> AccessOut:
    try:
        payload = decode_token(body.refresh_token)
        if payload.get("type") != "refresh":
            raise ValueError("not a refresh token")
        user_id = int(payload["sub"])
        token_version = token_version_of(payload)
    except (jwt.PyJWTError, KeyError, ValueError, TypeError):
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED, detail=err("auth.refresh_token_invalid", "Invalid refresh token")
        )
    user = session.get(User, user_id)
    if not user or user.status != UserStatus.active:
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail=err("auth.account_inactive", "Account is not active"))
    if token_version != user.token_version:
        # Revoked (password change, role/status change, "sign out everywhere").
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED, detail=err("auth.refresh_token_invalid", "Invalid refresh token")
        )
    return AccessOut(access_token=make_access_token(user_id, user.token_version))


@router.get("/me", response_model=UserOut)
def me(user: User = Depends(get_current_user)) -> UserOut:
    return UserOut.from_user(user)


@router.post("/change-password")
def change_password(
    body: PasswordChangeIn,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
) -> dict:
    if not verify_password(body.old_password, user.pw_hash):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, detail=err("auth.old_password_wrong", "Old password is wrong")
        )
    user.pw_hash = hash_password(body.new_password)
    user.must_change_pw = False
    # Every token issued under the old password stops working — including the one this request
    # came with. The response carries a fresh pair so that this session, and only this one,
    # continues.
    invalidate_tokens(user)
    session.add(user)
    session.commit()
    return {
        "message": "Password changed",
        "access_token": make_access_token(user.id, user.token_version),
        "refresh_token": make_refresh_token(user.id, user.token_version),
        "token_type": "bearer",
    }


@router.post("/logout-all")
def logout_all(
    user: User = Depends(get_current_user), session: Session = Depends(get_session)
) -> dict:
    """Sign out everywhere: revoke every token of the calling account, this session included."""
    invalidate_tokens(user)
    session.add(user)
    session.commit()
    record(session, user, "auth.logout_all", user.username)
    return {"ok": True}


@router.post("/ws-ticket")
def ws_ticket(user: User = Depends(get_current_user)) -> dict:
    """Ticket for opening ONE WebSocket (`?ticket=`), requested right before connecting.

    Browsers cannot send an `Authorization` header with a WebSocket, so a credential has to go
    into the URL — and URLs end up in proxy and access logs. The ticket keeps the access token
    out of them: it expires after WS_TICKET_TTL_SEC, works once and is not accepted by the HTTP
    API.
    """
    return {"ticket": make_ws_ticket(user.id, user.token_version), "expires_in": WS_TICKET_TTL_SEC}
