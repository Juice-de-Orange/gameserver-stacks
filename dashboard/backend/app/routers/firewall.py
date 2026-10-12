"""Settings → Network → Firewall: preview and apply the helper's rules (`services/firewall.py`).

Admin only: applying changes the host's firewall. `GET` never changes anything — it returns what
should be open and the helper's plan for it; `POST /apply` applies exactly the plan the admin saw
(its `plan_id`), or answers 409 if the rules or the servers changed in between.
"""
from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlmodel import Session

from ..auth.deps import require_admin
from ..db import get_session
from ..models import User
from ..services import firewall
from ..services.audit import record

router = APIRouter(prefix="/api/settings/firewall", tags=["settings"])


class ApplyBody(BaseModel):
    plan_id: str = Field(min_length=1, max_length=64)


@router.get("")
async def get_firewall(session: Session = Depends(get_session), _: User = Depends(require_admin)) -> dict:
    """`status`: `off` (no GSS_FIREWALL_TOKEN), `ready` (with the plan) or `error` (with the
    reason). `desired` is always there: what the helper would open."""
    desired = firewall.desired(session)
    if not firewall.enabled():
        return {"status": "off", "desired": desired, "plan": None, "error": None}
    try:
        plan = await firewall.preview(session)
    except HTTPException as e:
        return {"status": "error", "desired": desired, "plan": None, "error": e.detail}
    return {"status": "ready", "desired": desired, "plan": plan, "error": None}


@router.post("/apply")
async def apply_firewall(
    body: ApplyBody,
    session: Session = Depends(get_session),
    user: User = Depends(require_admin),
) -> dict:
    plan = await firewall.apply(session, body.plan_id)
    opened = [f"{a['server']} {a['port']}/{a['proto']}" for a in plan.get("allows", [])]
    record(session, user, "firewall.apply", f"{len(opened)} port(s), {plan.get('applied', 0)} change(s)",
           json.dumps({"allows": opened, "sources": plan.get("desired", {}).get("sources", [])}))
    return {"status": "ready", "desired": firewall.desired(session), "plan": plan, "error": None}
