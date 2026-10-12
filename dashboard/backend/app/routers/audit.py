"""Audit log view (admin)."""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlmodel import Session, select

from ..auth.deps import require_admin
from ..db import get_session
from ..models import AuditLog, User

router = APIRouter(prefix="/api/audit", tags=["audit"])


@router.get("")
def list_audit(limit: int = 200, session: Session = Depends(get_session), _: User = Depends(require_admin)):
    limit = max(1, min(limit, 1000))  # a negative value would otherwise cause a 500 on Postgres
    rows = session.exec(select(AuditLog).order_by(AuditLog.id.desc()).limit(limit)).all()
    return [
        {
            "id": r.id,
            "username": r.username,
            "action": r.action,
            "target": r.target,
            "payload": r.payload,
            "created_at": r.created_at,
        }
        for r in rows
    ]
