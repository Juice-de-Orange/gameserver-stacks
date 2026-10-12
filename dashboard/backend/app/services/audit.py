"""Audit helpers."""
from __future__ import annotations

import datetime as dt
import logging

from sqlalchemy import delete as sa_delete
from sqlmodel import Session

from ..config import settings
from ..db import engine
from ..models import AuditLog, User

log = logging.getLogger("uvicorn.error")


def record(
    session: Session,
    user: User | None,
    action: str,
    target: str = "",
    payload: str = "",
    *,
    server_id: int | None = None,
) -> None:
    session.add(
        AuditLog(
            user_id=user.id if user else None,
            username=user.username if user else "system",
            action=action,
            # `target` must be truncated like `payload`: for failed logins it holds the
            # username from the form (`routers/auth.py`), the only unauthenticated `record()`
            # caller in the backend, and the column is a VARCHAR without a length.
            target=target[:200],
            payload=payload[:1000],
            server_id=server_id,
        )
    )
    session.commit()


def prune_audit() -> None:
    """Remove old audit rows, same pattern as `metrics.prune_metrics`.

    The retention period is deliberately much longer than for metrics (default one year
    instead of 30 days): the audit log is security evidence, not display garnish. Whoever wants
    to trace a console or admin action often does so months later.
    """
    # Timezone-aware because `AuditLog.created_at` is too, as the only model in the project
    # (the others use `utcnow_naive`). Both sides must match, otherwise the prune compares
    # incompatible values and deletes too much or nothing.
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=settings.audit_retention_days)
    try:
        with Session(engine) as session:
            session.execute(sa_delete(AuditLog).where(AuditLog.created_at < cutoff))
            session.commit()
    except Exception:
        log.warning("audit: prune failed", exc_info=False)
