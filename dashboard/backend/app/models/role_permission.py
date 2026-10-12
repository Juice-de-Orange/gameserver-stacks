"""Role permission matrix: one default permission per (role, flag).

Configurable by admins and retroactive — a user's effective permissions are
admin bypass OR per-user flag OR role default (see auth/perm.py).
New table → covered by create_all, no ALTER needed.
"""
from __future__ import annotations

from sqlmodel import Field, SQLModel

from .user import Role


class RolePermission(SQLModel, table=True):
    __tablename__ = "role_permissions"

    id: int | None = Field(default=None, primary_key=True)
    role: Role = Field(index=True)        # stored as varchar (enum .value)
    flag: str = Field(index=True)         # one of PERMISSION_FLAGS
    enabled: bool = False
