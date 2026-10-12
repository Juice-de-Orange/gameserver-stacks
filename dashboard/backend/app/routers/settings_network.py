"""Settings → Network: the installation's public address (`services/install_settings.py`).

Admin only, in both directions: the value is not secret (the join page shows it to every
player), but the page also says which environment variable it comes from.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlmodel import Session

from ..auth.deps import require_admin
from ..db import get_session
from ..models import User
from ..services import install_settings
from ..services.audit import record
from ..services.fleet import clear_cache as fleet_clear_cache

router = APIRouter(prefix="/api/settings/network", tags=["settings"])


class NetworkUpdate(BaseModel):
    # Empty: fall back to PUBLIC_HOST from the environment.
    public_host: str = Field(default="", max_length=255)


@router.get("")
def get_network(_: User = Depends(require_admin)) -> dict:
    return install_settings.public_host_state()


@router.put("")
def put_network(
    body: NetworkUpdate,
    session: Session = Depends(get_session),
    user: User = Depends(require_admin),
) -> dict:
    value = install_settings.normalize_public_host(body.public_host)
    install_settings.put(session, install_settings.PUBLIC_HOST, value)
    record(session, user, "settings.public_host", value or "(PUBLIC_HOST)")
    fleet_clear_cache()  # the fleet shows the connect address
    return install_settings.public_host_state()
