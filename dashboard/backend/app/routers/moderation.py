"""Player moderation: kick/ban/unban/access level + whitelist — via RCON, audited.

Gated by can_moderate. Names/reasons are sanitised (no quote/newline injection into the RCON
string). The whitelist view reads the PZ SQLite DB read-only.
"""
from __future__ import annotations

import re
from dataclasses import asdict

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlmodel import Session

from ..auth.deps import get_driver_dep, get_server, require_flag
from ..db import get_session
from ..errors import detail_of, err
from ..models import User
from ..models.server import GameServer
from ..services.audit import record
from ..services.drivers import GameDriver
from ..services.rcon import RconError


_mod = require_flag("can_moderate")


def _clean(s: str) -> str:
    """Strip quotes/control characters/semicolons (values go into the RCON command quoted)."""
    return re.sub(r'[\x00-\x1f\x7f";]', "", s or "").strip()


class UserIn(BaseModel):
    username: str
    reason: str = ""
    ip: bool = False


class AccessIn(BaseModel):
    username: str
    level: str


# --- Server-scoped routes ------------------------------------------------------
# Each server uses the driver of its game; `moderation_spec` states which actions the game
# supports (VS e.g. only kick/ban) → the frontend only shows the supported controls and the
# router rejects unsupported actions with 400.
scoped_router = APIRouter(prefix="/api/servers", tags=["moderation"])


async def _srun(session: Session, user: User, server: GameServer, action: str, detail: str, coro) -> dict:
    try:
        out = await coro
    except NotImplementedError:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, detail=err("moderation.unsupported", "Not supported for this game")
        )
    except RconError as e:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, detail=detail_of(e))
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=detail_of(e))
    record(session, user, f"moderation.{action}", server.key, detail[:1000])
    return {"ok": True, "output": out}


def _need(u: str) -> str:
    v = _clean(u)
    if not v:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, detail=err("moderation.username_missing", "Username is missing")
        )
    return v


@scoped_router.get("/{key}/moderation/spec")
def moderation_spec_scoped(
    server: GameServer = Depends(get_server),
    driver: GameDriver = Depends(get_driver_dep),
    _: User = Depends(_mod),
) -> dict:
    return asdict(driver.moderation_spec(server))


@scoped_router.post("/{key}/moderation/kick")
async def kick_scoped(body: UserIn, server: GameServer = Depends(get_server), driver: GameDriver = Depends(get_driver_dep), session: Session = Depends(get_session), user: User = Depends(_mod)) -> dict:
    if not driver.moderation_spec(server).kick:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, detail=err("moderation.kick_unsupported", "Kick is not supported for this game")
        )
    u = _need(body.username)
    return await _srun(session, user, server, "kick", u, driver.kick(server, body.username, body.reason))


@scoped_router.post("/{key}/moderation/ban")
async def ban_scoped(body: UserIn, server: GameServer = Depends(get_server), driver: GameDriver = Depends(get_driver_dep), session: Session = Depends(get_session), user: User = Depends(_mod)) -> dict:
    spec = driver.moderation_spec(server)
    if not spec.ban:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, detail=err("moderation.ban_unsupported", "Ban is not supported for this game")
        )
    if body.ip and not spec.ban_ip:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, detail=err("moderation.ip_ban_unsupported", "IP ban is not supported for this game")
        )
    u = _need(body.username)
    return await _srun(session, user, server, "ban", u, driver.ban(server, body.username, body.reason, ip=body.ip))


@scoped_router.post("/{key}/moderation/unban")
async def unban_scoped(body: UserIn, server: GameServer = Depends(get_server), driver: GameDriver = Depends(get_driver_dep), session: Session = Depends(get_session), user: User = Depends(_mod)) -> dict:
    if not driver.moderation_spec(server).unban:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, detail=err("moderation.unban_unsupported", "Unban is not supported for this game")
        )
    u = _need(body.username)
    return await _srun(session, user, server, "unban", u, driver.unban(server, body.username))


@scoped_router.post("/{key}/moderation/access")
async def access_scoped(body: AccessIn, server: GameServer = Depends(get_server), driver: GameDriver = Depends(get_driver_dep), session: Session = Depends(get_session), user: User = Depends(_mod)) -> dict:
    spec = driver.moderation_spec(server)
    level = body.level.strip().lower()
    if not spec.access_levels:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, detail=err("moderation.access_levels_unsupported", "This game has no access levels")
        )
    if level not in spec.access_levels:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail=err(
                "moderation.level_invalid", "Invalid level (allowed: {levels})",
                levels=", ".join(spec.access_levels),
            ),
        )
    u = _need(body.username)
    return await _srun(session, user, server, "access", f"{u} → {level}", driver.set_access(server, body.username, level))


@scoped_router.post("/{key}/moderation/whitelist/add")
async def whitelist_add_scoped(body: UserIn, server: GameServer = Depends(get_server), driver: GameDriver = Depends(get_driver_dep), session: Session = Depends(get_session), user: User = Depends(_mod)) -> dict:
    if not driver.moderation_spec(server).whitelist:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, detail=err("moderation.whitelist_unsupported", "This game has no whitelist")
        )
    u = _need(body.username)
    return await _srun(session, user, server, "whitelist_add", u, driver.whitelist_add(server, body.username))


@scoped_router.post("/{key}/moderation/whitelist/remove")
async def whitelist_remove_scoped(body: UserIn, server: GameServer = Depends(get_server), driver: GameDriver = Depends(get_driver_dep), session: Session = Depends(get_session), user: User = Depends(_mod)) -> dict:
    if not driver.moderation_spec(server).whitelist:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, detail=err("moderation.whitelist_unsupported", "This game has no whitelist")
        )
    u = _need(body.username)
    return await _srun(session, user, server, "whitelist_remove", u, driver.whitelist_remove(server, body.username))


@scoped_router.get("/{key}/moderation/whitelist")
def whitelist_scoped(
    server: GameServer = Depends(get_server),
    driver: GameDriver = Depends(get_driver_dep),
    _: User = Depends(_mod),
) -> dict:
    return driver.whitelist_list(server)
