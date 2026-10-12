"""Installation-wide settings from the UI (`install_settings`), over their environment variable.

So far one key: `public_host`, the address players connect to (Settings → Network). The
environment variable (`PUBLIC_HOST`) is the starting value; a value saved in the UI wins, and
clearing it there falls back to the variable again. A server's own `extra.public_host` wins over
both (`drivers/base.public_host`).

The values are kept in memory (`load` at start, `put` on save) because `public_host` is read for
every server on every fleet poll. That is correct with exactly one API worker, which is the
deployment (`backend/Dockerfile`), the same assumption the login throttle makes.
"""
from __future__ import annotations

import ipaddress
import logging
import re

from fastapi import HTTPException, status
from sqlmodel import Session, select

from ..config import settings
from ..errors import err
from ..models.install_setting import InstallSetting
from ..models.player import utcnow_naive

log = logging.getLogger("uvicorn.error")

PUBLIC_HOST = "public_host"

_cache: dict[str, str] = {}

# A DNS name: labels of letters, digits, '-' (and '_', which some dynamic-DNS names carry).
_HOST_RE = re.compile(
    r"^(?=.{1,253}$)[A-Za-z0-9_](?:[A-Za-z0-9_-]{0,61}[A-Za-z0-9_])?"
    r"(?:\.[A-Za-z0-9_](?:[A-Za-z0-9_-]{0,61}[A-Za-z0-9_])?)*\.?$"
)


def load(eng=None) -> None:
    """Read every stored value into memory (at start)."""
    from .. import db

    with Session(eng or db.engine) as s:
        rows = s.exec(select(InstallSetting)).all()
    _cache.clear()
    _cache.update({r.key: r.value for r in rows if r.value})


def stored(key: str) -> str:
    """The value saved in the UI, `""` if there is none."""
    return _cache.get(key, "")


def put(session: Session, key: str, value: str) -> None:
    """Save (or with `""` remove) a value and commit."""
    row = session.get(InstallSetting, key)
    if value:
        row = row or InstallSetting(key=key)
        row.value = value
        row.updated_at = utcnow_naive()
        session.add(row)
    elif row is not None:
        session.delete(row)
    session.commit()
    if value:
        _cache[key] = value
    else:
        _cache.pop(key, None)


def normalize_public_host(value: str | None) -> str:
    """A host name or an IP address, without scheme, port, path or spaces — or `""`."""
    raw = (value or "").strip()
    if not raw:
        return ""
    try:
        return str(ipaddress.ip_address(raw.strip("[]")))
    except ValueError:
        pass
    if _HOST_RE.fullmatch(raw):
        return raw.rstrip(".").lower()
    raise HTTPException(
        status.HTTP_400_BAD_REQUEST,
        detail=err(
            "settings.public_host_invalid",
            "'{value}' is not a host name or IP address. Enter the address alone, e.g. "
            "play.example.com or 203.0.113.10 — no http://, no port, no path.",
            value=raw[:64],
        ),
    )


def public_host() -> str:
    """The installation's public address: saved in the UI, otherwise `PUBLIC_HOST`, otherwise `""`."""
    return stored(PUBLIC_HOST) or settings.public_host or ""


def public_host_state() -> dict:
    """What Settings → Network shows: the effective value and where it comes from."""
    own = stored(PUBLIC_HOST)
    env = settings.public_host or ""
    return {
        "public_host": own or env,
        "stored": own,
        "env": env,
        "source": "stored" if own else ("env" if env else "none"),
    }
