"""Password hashing (argon2) and JWTs (access, refresh, WebSocket ticket)."""
from __future__ import annotations

import datetime as dt
import time
import uuid

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError, InvalidHashError

from ..config import settings

_ph = PasswordHasher()
ALGO = "HS256"

# Lifetime of a WebSocket ticket. It only has to survive the moment between "the page asked for
# it" and "the socket connected"; it ends up in the URL and therefore in proxy logs.
WS_TICKET_TTL_SEC = 30


def hash_password(pw: str) -> str:
    return _ph.hash(pw)


def verify_password(pw: str, pw_hash: str) -> bool:
    try:
        return _ph.verify(pw_hash, pw)
    except (VerifyMismatchError, InvalidHashError):
        return False


def needs_rehash(pw_hash: str) -> bool:
    try:
        return _ph.check_needs_rehash(pw_hash)
    except InvalidHashError:
        return True


def _make_token(sub: str, ttl: dt.timedelta, token_type: str, token_version: int) -> str:
    now = dt.datetime.now(dt.timezone.utc)
    payload = {
        "sub": sub,
        "type": token_type,
        # The user's `token_version` at the time of issue. Every token is checked against the
        # current value, so raising it (`invalidate_tokens`) revokes all tokens issued before.
        "tv": token_version,
        "iat": now,
        "exp": now + ttl,
        "jti": uuid.uuid4().hex,
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=ALGO)


def make_access_token(user_id: int, token_version: int = 0) -> str:
    return _make_token(
        str(user_id), dt.timedelta(minutes=settings.jwt_access_ttl_min), "access", token_version
    )


def make_refresh_token(user_id: int, token_version: int = 0) -> str:
    return _make_token(
        str(user_id), dt.timedelta(days=settings.jwt_refresh_ttl_days), "refresh", token_version
    )


def make_ws_ticket(user_id: int, token_version: int = 0) -> str:
    """Short-lived, single-use ticket that opens ONE WebSocket (type `ws`).

    A browser cannot set an `Authorization` header on a WebSocket, so the credential has to
    travel in the URL. This ticket is what travels instead of the access token: it is useless
    for the HTTP API, expires after WS_TICKET_TTL_SEC and is consumed by the first connection.
    """
    return _make_token(
        str(user_id), dt.timedelta(seconds=WS_TICKET_TTL_SEC), "ws", token_version
    )


def decode_token(token: str) -> dict:
    return jwt.decode(token, settings.jwt_secret, algorithms=[ALGO])


def token_version_of(payload: dict) -> int:
    """`tv` claim of a decoded token. Tokens issued before the claim existed count as version 0,
    the value every existing account starts with."""
    return int(payload.get("tv", 0))


def invalidate_tokens(user) -> None:
    """Revoke every access token, refresh token and WebSocket ticket of this user.

    Only changes the object; the caller commits. Called on a password change or reset, a role or
    status change and "sign out everywhere".
    """
    user.token_version = (user.token_version or 0) + 1


# jti -> expiry (monotonic clock) of the tickets that were already used. Process-local, which is
# valid because the API runs with exactly one worker (see the Dockerfile CMD) — the same
# assumption as the login throttle. Entries are only added for correctly signed, unexpired
# tickets and live for at most WS_TICKET_TTL_SEC, so the dict stays small.
_used_ws_tickets: dict[str, float] = {}


def consume_ws_ticket(payload: dict) -> bool:
    """Mark a decoded ticket as used. False if it was used before (or carries no `jti`)."""
    now = time.monotonic()
    for jti in [j for j, expires in _used_ws_tickets.items() if expires <= now]:
        _used_ws_tickets.pop(jti, None)
    jti = payload.get("jti")
    if not jti or jti in _used_ws_tickets:
        return False
    _used_ws_tickets[jti] = now + WS_TICKET_TTL_SEC
    return True
