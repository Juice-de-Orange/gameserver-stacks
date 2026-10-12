"""Inputs nobody had capped.

Common denominator: a number or a text from a request travels unchecked into memory, the
database or the file system. None of them is a hole through which someone gets in — all are ways
in which an authenticated mistake (or a persistent attacker) loads the backend, which runs as
**one** worker.
"""
import datetime as dt

import pytest
from sqlmodel import Session, select

from app.models import AuditLog
from app.services import audit


# ------------------------------------------------------------------ audit

def test_target_is_truncated(mem_engine):
    """`payload` was always truncated, `target` was not — and for failed logins it holds the
    username from the form. `/api/auth/login` is the only unauthenticated `record()` caller in
    the backend, and the column is a VARCHAR without a length."""
    with Session(mem_engine) as s:
        audit.record(s, None, "auth.login_failed", "x" * 5000, "y" * 5000)
        line = s.exec(select(AuditLog)).first()
        assert len(line.target) == 200
        assert len(line.payload) == 1000
        assert line.username == "system"     # unknown user → no user_id


def test_audit_has_a_retention_rule(mem_engine, monkeypatch):
    """`audit_log` was the only growing table without a prune — `metric_samples` and the
    backups always had one."""
    monkeypatch.setattr("app.services.audit.engine", mem_engine)

    with Session(mem_engine) as s:
        s.add(AuditLog(
            username="admin", action="console", target="pz-1",
            created_at=dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=400),
        ))
        s.add(AuditLog(username="admin", action="console", target="pz-1"))
        s.commit()

    audit.prune_audit()

    with Session(mem_engine) as s:
        remaining = s.exec(select(AuditLog)).all()
        assert len(remaining) == 1, "the old row was not cleaned up"


def test_the_audit_retention_is_much_longer_than_the_metrics_retention():
    """Deliberate, not arbitrary: the audit is a security record. Whoever wants to trace a console
    or admin action often does so only months later."""
    from app.config import settings

    assert settings.audit_retention_days >= 365
    assert settings.audit_retention_days > settings.metrics_retention_days


# ------------------------------------------------------------------ `tail`

@pytest.mark.parametrize(
    "value,expected",
    [(999_999_999, 5000), (-1, 1), (0, 1), (300, 300), (5000, 5000), (5001, 5000)],
)
def test_tail_is_clamped(value, expected, monkeypatch):
    """Every list route in the backend clamps its size; `tail` must too.

    `?tail=999999999` would make Docker deliver the COMPLETE log file; it lands entirely in the
    memory of the single-worker process, and `_demux` builds a list of strings from it on top.
    """
    import asyncio
    import types

    from app.routers import server_ops

    seen: list[int] = []

    class _Docker:
        async def logs(self, name, tail=None):
            seen.append(tail)
            return ""

    monkeypatch.setattr(server_ops, "docker", _Docker())
    srv = types.SimpleNamespace(container_name="pz-1", key="pz-1")

    asyncio.run(server_ops.get_logs(tail=value, server=srv, _=None))
    assert seen == [expected]
