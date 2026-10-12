"""Reachability of one server: checklist, check from inside, check from outside.

Admin only: the checklist shows the host's bind address and firewall commands, and the outside
check sends the public address to third-party services (`services/reachability/outside.py`),
which is recorded in the audit log.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlmodel import Session

from ..auth.deps import get_server, require_admin
from ..config import settings
from ..db import get_session
from ..errors import err
from ..models import User
from ..models.server import GameServer
from ..services.audit import record
from ..services.reachability import checklist, inside, outside

router = APIRouter(prefix="/api/servers/{key}/reachability", tags=["reachability"])


@router.get("")
def get_reachability(
    _: User = Depends(require_admin),
    server: GameServer = Depends(get_server),
) -> dict:
    return {**checklist.checklist(server), "outside": outside.disclosure(server)}


@router.post("/inside")
async def check_inside(
    _: User = Depends(require_admin),
    server: GameServer = Depends(get_server),
) -> dict:
    return await inside.check(server)


@router.post("/outside")
async def check_outside(
    user: User = Depends(require_admin),
    server: GameServer = Depends(get_server),
    session: Session = Depends(get_session),
) -> dict:
    if not settings.reachability_probes:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail=err(
                "reachability.probes_off",
                "The check from outside is switched off. Set GSS_REACHABILITY_PROBES=on in the "
                ".env and restart the dashboard to offer it.",
            ),
        )
    result = await outside.check(server)
    if result["ran"]:
        services = sorted({p["service"] for p in result["ports"] if p["status"] not in ("not_checkable", "service_off")})
        record(session, user, "reachability.outside", server.key, ", ".join(services), server_id=server.id)
    return result
