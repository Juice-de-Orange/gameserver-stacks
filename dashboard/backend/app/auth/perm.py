"""Effective rights = admin bypass OR per-user flag OR role default.

The role matrix (3 roles × N flags) is tiny and kept as an in-memory cache:
`get_current_user` loads the user per request anyway, so this adds NO database
cost to the auth hot path. The cache is refreshed at start and after every
write to the matrix via load_matrix().

Note: with more than one worker process the cache would have to be refreshed
per process (console-api runs with one worker — see the Dockerfile CMD).
"""
from __future__ import annotations

from sqlmodel import Session, select

from ..db import engine
from ..models import PERMISSION_FLAGS, Role, RolePermission, User

# {Role: {flag: enabled}}
_MATRIX: dict[Role, dict[str, bool]] = {}


def load_matrix() -> None:
    """Load the role matrix from the database into the cache (idempotent)."""
    global _MATRIX
    matrix: dict[Role, dict[str, bool]] = {r: {f: False for f in PERMISSION_FLAGS} for r in Role}
    with Session(engine) as session:
        for row in session.exec(select(RolePermission)).all():
            if row.role in matrix and row.flag in PERMISSION_FLAGS:
                matrix[row.role][row.flag] = row.enabled
    _MATRIX = matrix


def role_allows(role: Role, flag: str) -> bool:
    return _MATRIX.get(role, {}).get(flag, False)


def has_permission(user: User, flag: str) -> bool:
    """Whether the user effectively holds the flag."""
    return (
        user.role == Role.admin              # admin bypasses everything
        or getattr(user, flag, False)        # per-user grant (additive)
        or role_allows(user.role, flag)      # role default (retroactive)
    )


def effective_flags(user: User) -> dict[str, bool]:
    """All effective flags — for the frontend (gating) and the admin UI."""
    return {f: has_permission(user, f) for f in PERMISSION_FLAGS}


def matrix_as_dict() -> dict[str, dict[str, bool]]:
    """Current matrix for the GET endpoint (admin row = all True because of the bypass)."""
    out: dict[str, dict[str, bool]] = {}
    for role in Role:
        if role == Role.admin:
            out[role.value] = {f: True for f in PERMISSION_FLAGS}
        else:
            out[role.value] = {f: role_allows(role, f) for f in PERMISSION_FLAGS}
    return out
