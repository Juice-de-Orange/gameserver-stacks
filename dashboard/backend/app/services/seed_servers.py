"""Seed the registry singletons (resource budget, cross-server settings).

Idempotent and additive. Runs in the lifespan after `seed_role_permissions()`. Game servers are
NOT seeded: they are added through the UI / `POST /api/servers`.
"""
from __future__ import annotations

from sqlmodel import Session, select

from ..db import engine
from ..models.cross_server import CrossServerSettings
from ..models.resource_budget import ResourceBudget


def seed_servers() -> None:
    with Session(engine) as session:
        # Singletons (idempotent)
        if not session.exec(select(ResourceBudget)).first():
            session.add(ResourceBudget())            # total=0 → advisory only until an admin sets the limit
        if not session.exec(select(CrossServerSettings)).first():
            session.add(CrossServerSettings())       # chat bridge OFF by default
        session.commit()
