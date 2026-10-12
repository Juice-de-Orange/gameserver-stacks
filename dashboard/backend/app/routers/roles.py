"""Manage the role permission matrix (admin only). Retroactive for all users of the role."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlmodel import Session, select

from ..auth import perm
from ..auth.deps import require_admin
from ..db import get_session
from ..errors import err
from ..models import PERMISSION_FLAGS, Role, RolePermission, User
from ..services.audit import record

router = APIRouter(prefix="/api/roles", tags=["roles"])


class MatrixIn(BaseModel):
    matrix: dict[str, dict[str, bool]]


@router.get("/matrix")
def get_matrix(_: User = Depends(require_admin)) -> dict:
    return {
        "roles": [r.value for r in Role],
        "flags": list(PERMISSION_FLAGS),
        "matrix": perm.matrix_as_dict(),  # admin row = all True (bypass), read-only
    }


@router.put("/matrix")
def put_matrix(
    body: MatrixIn,
    session: Session = Depends(get_session),
    user: User = Depends(require_admin),
) -> dict:
    # Validate + ignore the admin row (admin bypasses everything anyway).
    cleaned: dict[Role, dict[str, bool]] = {}
    for role_name, flags in body.matrix.items():
        try:
            role = Role(role_name)
        except ValueError:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                detail=err("roles.unknown_role", "Unknown role: {role}", role=role_name),
            )
        if role == Role.admin:
            continue
        cleaned[role] = {f: bool(v) for f, v in flags.items() if f in PERMISSION_FLAGS}

    # Upsert: update existing rows, create missing ones (no delete needed).
    existing = session.exec(
        select(RolePermission).where(RolePermission.role.in_(list(cleaned.keys())))
    ).all()
    by_key = {(rp.role, rp.flag): rp for rp in existing}
    for role, flags in cleaned.items():
        for flag in PERMISSION_FLAGS:
            enabled = flags.get(flag, False)
            rp = by_key.get((role, flag))
            if rp is None:
                session.add(RolePermission(role=role, flag=flag, enabled=enabled))
            else:
                rp.enabled = enabled
                session.add(rp)
    session.commit()

    perm.load_matrix()  # refresh the cache
    record(session, user, "roles.matrix", "role_permissions", ",".join(r.value for r in cleaned))
    return {"ok": True, "matrix": perm.matrix_as_dict()}
