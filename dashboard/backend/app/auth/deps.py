"""FastAPI dependencies for authentication and RBAC."""
from __future__ import annotations

from collections.abc import Callable

import jwt
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import OAuth2PasswordBearer
from sqlmodel import Session

from ..db import get_session
from ..errors import err
from ..models import GameServer, Role, User, UserStatus
from ..services import presence
from ..services import servers as servers_registry
from ..services.drivers import GameDriver, get_driver
from .perm import has_permission
from .security import consume_ws_ticket, decode_token, token_version_of

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/auth/login")


def get_current_user(
    request: Request,
    token: str = Depends(oauth2_scheme),
    session: Session = Depends(get_session),
) -> User:
    cred_exc = HTTPException(
        status.HTTP_401_UNAUTHORIZED,
        detail=err("auth.token_invalid", "Invalid or expired token"),
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = decode_token(token)
        if payload.get("type") != "access":
            raise cred_exc
        user_id = int(payload["sub"])
        token_version = token_version_of(payload)
    except (jwt.PyJWTError, KeyError, ValueError, TypeError):
        raise cred_exc

    user = session.get(User, user_id)
    if user is None or user.status != UserStatus.active:
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail=err("auth.account_inactive", "Account is not active"))
    if token_version != user.token_version:
        # Revoked: issued before a password change, a role/status change or "sign out everywhere".
        raise cred_exc
    _enforce_pw_change(user, request)
    presence.remember(user.id)
    return user


# Routes an account with a pending mandatory password change MUST still reach, otherwise it
# could not change the password and would be locked out. Deliberately short and explicit: `me`
# for the identity (the frontend redirects afterwards), `change-password` for the purpose itself.
# `login` and `refresh` do not depend on `get_current_user` at all.
_PW_CHANGE_ALLOWED = frozenset({"/api/auth/me", "/api/auth/change-password"})


def _enforce_pw_change(user: User, request: Request | None) -> None:
    """Enforce `must_change_pw` at the API, not only in the browser.

    A temporary password (anyone with `can_manage_users` can issue one) must not work as a
    regular credential: without this check its access token would be accepted by every route the
    account has rights for, and a refresh would extend that indefinitely.
    """
    if not getattr(user, "must_change_pw", False):
        return
    path = request.url.path if request is not None else ""
    if path in _PW_CHANGE_ALLOWED:
        return
    raise HTTPException(
        status.HTTP_403_FORBIDDEN,
        detail=err("auth.password_change_required", "Password change required — set a new password first."),
    )


def require_admin(user: User = Depends(get_current_user)) -> User:
    if user.role != Role.admin:
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail=err("auth.admin_required", "Admin rights required"))
    return user


def require_flag(flag: str) -> Callable[..., User]:
    """Dependency factory: grants access if the right is effective (admin / role / per user)."""

    def checker(user: User = Depends(get_current_user)) -> User:
        if has_permission(user, flag):
            return user
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            detail=err("auth.permission_missing", "Missing permission: {flag}", flag=flag),
        )

    return checker


def _ws_user(user_id: int, token_version: int, session: Session) -> User | None:
    """The account behind a WebSocket, if it may (still) use one — the WebSocket counterpart of
    the checks in `get_current_user`."""
    user = session.get(User, user_id)
    if user is None or user.status != UserStatus.active:
        return None
    if token_version != user.token_version:
        return None
    if getattr(user, "must_change_pw", False):
        # There is no allow-list here because there is nothing to allow: without this line
        # `/ws/console`, `/ws/logs` and `/ws/cross-chat` — i.e. the RCON console — would stay
        # open for an account with a temporary password.
        return None
    presence.remember(user.id)
    return user


def user_from_ws_ticket(ticket: str, session: Session) -> User | None:
    """Authenticate a WebSocket handshake with the ticket from `POST /api/auth/ws-ticket`.

    The ticket is single-use: a second connection with the same ticket (replayed from a log, for
    example) is refused even within its lifetime.
    """
    try:
        payload = decode_token(ticket)
        if payload.get("type") != "ws":
            return None
        user_id = int(payload["sub"])
        token_version = token_version_of(payload)
    except (jwt.PyJWTError, KeyError, ValueError, TypeError):
        return None
    if not consume_ws_ticket(payload):
        return None
    return _ws_user(user_id, token_version, session)


def ws_user_recheck(user: User, session: Session) -> User | None:
    """Re-validate the account of an OPEN WebSocket; `user` is the row from the handshake.

    The ticket is long expired by then, so the connection is bound to the account's state
    instead: it ends as soon as the account is disabled, must change its password or has its
    tokens revoked (`token_version` differs from the one the socket was opened with). Rights are
    read from the returned, current row.
    """
    return _ws_user(user.id, user.token_version, session)


def get_server(key: str, _user: User = Depends(get_current_user)) -> GameServer:
    """Path parameter `key` → enabled registry row; unknown or disabled → 404.

    Disabled servers are operationally invisible (the fan-out skips them); an admin enables them
    first via PUT /api/servers/{key}.

    **`_user` is not decoration, it is the order.** FastAPI resolves dependencies in signature
    order and the first HTTPException wins. If this function ran before authentication, an
    unauthenticated caller would get an oracle: unknown key → 404, existing key with the wrong
    game type → 400 including the game type, game type without driver → 501, and only otherwise
    401 — enough to enumerate server keys and game types without any access.

    This is the one place to fix that centrally: `get_driver_dep` and `_pz_only`
    (routers/config.py) both build on `get_server` and inherit the check. The parameter is
    deliberately unused — resolving it IS the effect."""
    srv = servers_registry.get_by_key(key)
    if srv is None or not srv.enabled:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, detail=err("server.not_found", "Server '{key}' not found", key=key)
        )
    return srv


def get_driver_dep(server: GameServer = Depends(get_server)) -> GameDriver:
    """Driver for the resolved server row; a game type without a driver → 501."""
    try:
        return get_driver(server)
    except NotImplementedError:
        raise HTTPException(
            status.HTTP_501_NOT_IMPLEMENTED,
            detail=err(
                "server.game_type_unsupported", "Game type '{game_type}' is not supported yet",
                game_type=server.game_type.value,
            ),
        )
