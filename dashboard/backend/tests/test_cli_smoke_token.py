"""`app.cli smoke-token` — the session for acceptance checks, without a password involved.

Two smoke assertions need a signed-in session (`/ws/logs` with a token: without `?server=` →
4400, with `?server=` → the connection holds). Signing in with the seed admin password from the
live `.env` has three drawbacks:

1. **The `.env` drifts.** A credentials file that nobody uses goes stale unnoticed, and the
   sign-in then fails.
2. **Every failed attempt counts towards the login throttle** (`auth/ratelimit.py`): eight per
   five-minute window lock out exactly the account the operator needs right now.
3. **Every deploy would leave a sign-in** in the audit — noise in exactly the trail one reads
   when something has happened.

The command issues the token with `make_access_token()` instead, the same function the login
uses. It moves no trust boundary: whoever can run it already has shell or Docker access to the
host and with it `JWT_SECRET`.

**The output is a contract:** stdout carries the token and nothing else, because the deploy
script reads it straight into a variable. Everything explanatory goes to stderr.
"""
from __future__ import annotations

import pytest
from sqlmodel import Session

from app import cli
from app.auth.security import decode_token
from app.models import Role, User


@pytest.fixture()
def db(monkeypatch, mem_engine):
    import app.db
    monkeypatch.setattr(app.db, "engine", mem_engine)
    monkeypatch.setattr(cli, "engine", mem_engine, raising=False)
    return mem_engine


def test_stdout_carries_the_token_and_nothing_else(db, capsys, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "seed_admin_username", "admin", raising=False)
    with Session(db) as s:
        s.add(User(display_name="Admin", username="admin", pw_hash="x", role=Role.admin))
        s.commit()

    assert cli.main(["smoke-token"]) == 0
    out = capsys.readouterr()
    lines = [ln for ln in out.out.splitlines() if ln.strip()]
    assert len(lines) == 1, f"stdout must carry EXACTLY the token, was: {out.out!r}"

    claims = decode_token(lines[0])
    assert claims["type"] == "access"
    with Session(db) as s:
        admin_id = s.exec(__import__("sqlmodel").select(User)).first().id
    assert claims["sub"] == str(admin_id)


def test_another_account_can_be_chosen(db, capsys):
    with Session(db) as s:
        s.add(User(display_name="Admin", username="admin", pw_hash="x", role=Role.admin))
        s.add(User(display_name="Second", username="second", pw_hash="x", role=Role.member))
        s.commit()

    assert cli.main(["smoke-token", "second"]) == 0
    token = capsys.readouterr().out.strip()
    with Session(db) as s:
        second = s.exec(
            __import__("sqlmodel").select(User).where(User.username == "second")
        ).first()
    assert decode_token(token)["sub"] == str(second.id)


def test_an_unknown_account_fails_loudly_and_keeps_stdout_empty(db, capsys):
    """An empty token must not look like a valid one — otherwise the skip is silent again."""
    assert cli.main(["smoke-token", "nosuchaccount"]) != 0
    out = capsys.readouterr()
    assert out.out.strip() == "", "stdout must stay empty, otherwise the deploy script reads garbage as a token"
    assert "nosuchaccount" in out.err


def test_the_command_is_listed_in_the_help(capsys):
    """A command the help does not mention gets reinvented next time."""
    assert cli.main([]) == 2
    assert "smoke-token" in capsys.readouterr().out


@pytest.mark.parametrize("secret", ["dev-insecure-change-me", "short"])
def test_a_weak_secret_issues_no_token(db, capsys, monkeypatch, secret):
    """The API refuses to start with the built-in default or a short `JWT_SECRET`
    (`config.jwt_secret_problem`). The CLI signs with the same secret, so it refuses too —
    a token minted with a public secret is one anybody could have minted."""
    from app.config import settings
    monkeypatch.setattr(settings, "jwt_secret", secret)
    with Session(db) as s:
        s.add(User(display_name="Admin", username="admin", pw_hash="x", role=Role.admin))
        s.commit()

    assert cli.main(["smoke-token", "admin"]) != 0
    out = capsys.readouterr()
    assert out.out.strip() == "", "stdout must stay empty, otherwise a caller reads garbage as a token"
    assert "JWT_SECRET" in out.err
