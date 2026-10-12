"""Token model: fail-closed JWT secret, revocation by token version, WebSocket tickets.

Everything here runs against the real app and an in-memory SQLite database (`mem_engine`), without
the lifespan — the routes are exercised through HTTP, not through their source text.
"""
import asyncio
import logging

import pytest
from sqlmodel import Session

from app import config, log_filters, main
from app.auth import deps, security
from app.auth.security import hash_password
from app.db import get_session
from app.models import Role, User, UserStatus

STRONG_SECRET = "s" * 40


@pytest.fixture(autouse=True)
def _strong_secret(monkeypatch):
    """Sign with a secret of sufficient length — the default is short and PyJWT warns about it."""
    monkeypatch.setattr(config.settings, "jwt_secret", STRONG_SECRET)
    security._used_ws_tickets.clear()
    yield
    security._used_ws_tickets.clear()


@pytest.fixture()
def client(api_client, mem_engine, monkeypatch):
    def _session():
        with Session(mem_engine) as s:
            yield s

    main.app.dependency_overrides[get_session] = _session
    # `record()` and the login throttle are not under test; keep them out of the way.
    from app.auth import ratelimit
    ratelimit._failures.clear()
    return api_client


def _add_user(engine, name="alice", password="correct-horse", role=Role.member, **fields) -> int:
    with Session(engine) as s:
        u = User(display_name=name, username=name, pw_hash=hash_password(password), role=role,
                 status=UserStatus.active, **fields)
        s.add(u)
        s.commit()
        return u.id


def _login(client, name="alice", password="correct-horse") -> dict:
    r = client.post("/api/auth/login", data={"username": name, "password": password})
    assert r.status_code == 200, r.text
    return r.json()


def _bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# ------------------------------------------------------------------ JWT secret (start-up)

@pytest.mark.parametrize(
    "secret",
    [config.DEFAULT_JWT_SECRET, "", "short", "x" * (config.JWT_SECRET_MIN_LENGTH - 1)],
)
def test_a_weak_jwt_secret_is_named_as_a_problem(secret):
    problem = config.jwt_secret_problem(secret)
    assert problem and "JWT_SECRET" in problem and "openssl rand -hex 32" in problem


def test_a_strong_jwt_secret_is_accepted():
    assert config.jwt_secret_problem("x" * config.JWT_SECRET_MIN_LENGTH) is None


@pytest.mark.parametrize("seed_password", ["", "some-seed-password"])
def test_the_api_refuses_to_start_with_the_default_secret(monkeypatch, seed_password):
    """Unconditionally — in particular with an empty SEED_ADMIN_PASSWORD, which is the normal
    state after the first start and used to switch the check off."""
    monkeypatch.setattr(config.settings, "jwt_secret", config.DEFAULT_JWT_SECRET)
    monkeypatch.setattr(config.settings, "seed_admin_password", seed_password)
    reached = []
    monkeypatch.setattr(main, "init_db", lambda: reached.append("init_db"))

    async def _start():
        async with main.lifespan(main.app):
            reached.append("running")

    with pytest.raises(RuntimeError, match="JWT_SECRET"):
        asyncio.run(_start())
    assert reached == [], "the start went past the secret check"


# ------------------------------------------------------------------ revocation

def test_tokens_carry_the_token_version(client, mem_engine):
    uid = _add_user(mem_engine)
    tokens = _login(client)
    for name in ("access_token", "refresh_token"):
        payload = security.decode_token(tokens[name])
        assert payload["sub"] == str(uid) and payload["tv"] == 0


def test_a_token_without_the_claim_counts_as_version_zero():
    """Tokens issued before the claim existed stay valid until the account's version is raised."""
    assert security.token_version_of({"sub": "1"}) == 0
    assert security.token_version_of({"sub": "1", "tv": 3}) == 3


def test_a_password_change_revokes_every_other_session(client, mem_engine):
    _add_user(mem_engine)
    other = _login(client)      # e.g. a second browser, or a stolen token
    mine = _login(client)

    r = client.post("/api/auth/change-password", headers=_bearer(mine["access_token"]),
                    json={"old_password": "correct-horse", "new_password": "a-new-password"})
    assert r.status_code == 200, r.text
    fresh = r.json()

    # Everything issued before is dead: access AND refresh, of both sessions.
    for old in (other, mine):
        assert client.get("/api/auth/me", headers=_bearer(old["access_token"])).status_code == 401
        assert client.post("/api/auth/refresh",
                           json={"refresh_token": old["refresh_token"]}).status_code == 401
    # The session that changed the password continues with the pair from the response.
    assert client.get("/api/auth/me", headers=_bearer(fresh["access_token"])).status_code == 200
    r = client.post("/api/auth/refresh", json={"refresh_token": fresh["refresh_token"]})
    assert r.status_code == 200
    assert client.get("/api/auth/me", headers=_bearer(r.json()["access_token"])).status_code == 200


def test_sign_out_everywhere_revokes_all_tokens(client, mem_engine):
    _add_user(mem_engine)
    a, b = _login(client), _login(client)
    assert client.post("/api/auth/logout-all", headers=_bearer(a["access_token"])).status_code == 200
    for old in (a, b):
        assert client.get("/api/auth/me", headers=_bearer(old["access_token"])).status_code == 401
        assert client.post("/api/auth/refresh",
                           json={"refresh_token": old["refresh_token"]}).status_code == 401
    # A new login works and is valid.
    assert client.get("/api/auth/me", headers=_bearer(_login(client)["access_token"])).status_code == 200


def _admin_and_target(client, mem_engine):
    _add_user(mem_engine, name="root", role=Role.admin)
    target_id = _add_user(mem_engine, name="bob")
    return _login(client, "root"), target_id, _login(client, "bob")


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("patch", "/api/users/{id}", {"role": "trusted"}),
        ("post", "/api/users/{id}/reset-password", {}),
        ("post", "/api/users/{id}/approve", {"role": "member"}),
    ],
)
def test_role_and_password_changes_by_an_admin_revoke_the_targets_tokens(
    client, mem_engine, method, path, body
):
    admin, target_id, target = _admin_and_target(client, mem_engine)
    r = getattr(client, method)(path.format(id=target_id), json=body,
                                headers=_bearer(admin["access_token"]))
    assert r.status_code == 200, r.text
    assert client.get("/api/auth/me", headers=_bearer(target["access_token"])).status_code == 401
    assert client.post("/api/auth/refresh",
                       json={"refresh_token": target["refresh_token"]}).status_code == 401
    # The admin's own session is untouched.
    assert client.get("/api/auth/me", headers=_bearer(admin["access_token"])).status_code == 200


def test_tokens_from_before_a_disable_do_not_come_back_with_the_enable(client, mem_engine):
    admin, target_id, target = _admin_and_target(client, mem_engine)
    hdr = _bearer(admin["access_token"])
    assert client.post(f"/api/users/{target_id}/disable", headers=hdr).status_code == 200
    assert client.get("/api/auth/me", headers=_bearer(target["access_token"])).status_code == 403
    assert client.post(f"/api/users/{target_id}/enable", headers=hdr).status_code == 200
    assert client.get("/api/auth/me", headers=_bearer(target["access_token"])).status_code == 401
    assert client.post("/api/auth/refresh",
                       json={"refresh_token": target["refresh_token"]}).status_code == 401


def test_a_flag_change_without_role_change_keeps_the_session(client, mem_engine):
    """Flags are read from the database on every request; only role and status changes revoke."""
    admin, target_id, target = _admin_and_target(client, mem_engine)
    r = client.patch(f"/api/users/{target_id}", json={"flags": {"can_view_logs": True}},
                     headers=_bearer(admin["access_token"]))
    assert r.status_code == 200, r.text
    assert client.get("/api/auth/me", headers=_bearer(target["access_token"])).status_code == 200


# ------------------------------------------------------------------ WebSocket tickets

def _ticket(client, tokens) -> str:
    r = client.post("/api/auth/ws-ticket", headers=_bearer(tokens["access_token"]))
    assert r.status_code == 200, r.text
    body = r.json()
    assert 0 < body["expires_in"] <= 60
    return body["ticket"]


def test_a_ws_ticket_needs_a_session(client):
    assert client.post("/api/auth/ws-ticket").status_code == 401


def test_a_ws_ticket_is_short_lived_bound_to_the_user_and_single_use(client, mem_engine):
    uid = _add_user(mem_engine)
    ticket = _ticket(client, _login(client))
    payload = security.decode_token(ticket)
    assert payload["type"] == "ws" and payload["sub"] == str(uid)
    assert payload["exp"] - payload["iat"] <= 60

    with Session(mem_engine) as s:
        user = deps.user_from_ws_ticket(ticket, s)
        assert user is not None and user.id == uid
        assert deps.user_from_ws_ticket(ticket, s) is None, "a ticket must work only once"


def test_a_ws_ticket_is_not_an_api_token_and_an_api_token_is_not_a_ticket(client, mem_engine):
    _add_user(mem_engine)
    tokens = _login(client)
    ticket = _ticket(client, tokens)
    assert client.get("/api/auth/me", headers=_bearer(ticket)).status_code == 401
    assert client.post("/api/auth/refresh", json={"refresh_token": ticket}).status_code == 401
    with Session(mem_engine) as s:
        assert deps.user_from_ws_ticket(tokens["access_token"], s) is None
        assert deps.user_from_ws_ticket(tokens["refresh_token"], s) is None
        assert deps.user_from_ws_ticket("", s) is None


def test_an_expired_ws_ticket_is_refused(mem_engine, monkeypatch):
    uid = _add_user(mem_engine)
    monkeypatch.setattr(security, "WS_TICKET_TTL_SEC", -5)
    with Session(mem_engine) as s:
        assert deps.user_from_ws_ticket(security.make_ws_ticket(uid, 0), s) is None


def test_an_open_websocket_ends_when_the_account_changes(mem_engine):
    """`ws_user_recheck` is what the socket loops call per command/tick: the ticket has expired by
    then, so the connection hangs on the account's state."""
    uid = _add_user(mem_engine)
    with Session(mem_engine) as s:
        opened_as = deps.user_from_ws_ticket(security.make_ws_ticket(uid, 0), s)
    assert opened_as is not None

    def _recheck():
        with Session(mem_engine) as s:
            return deps.ws_user_recheck(opened_as, s)

    def _change(**fields):
        with Session(mem_engine) as s:
            u = s.get(User, uid)
            for k, v in fields.items():
                setattr(u, k, v)
            s.add(u)
            s.commit()

    assert _recheck() is not None
    _change(must_change_pw=True)
    assert _recheck() is None
    _change(must_change_pw=False)
    assert _recheck() is not None
    _change(status=UserStatus.disabled)
    assert _recheck() is None
    _change(status=UserStatus.active, token_version=1)   # tokens revoked
    assert _recheck() is None


@pytest.mark.parametrize("path", ["/ws/logs?server=pz-1", "/ws/console?server=pz-1", "/ws/cross-chat"])
def test_websockets_refuse_an_access_token_in_the_url(client, mem_engine, path):
    """The old `?token=<access token>` no longer opens anything — neither under its old name nor
    as `ticket`."""
    from starlette.websockets import WebSocketDisconnect

    _add_user(mem_engine, role=Role.admin)
    access = _login(client)["access_token"]
    sep = "&" if "?" in path else "?"
    for query in (f"token={access}", f"ticket={access}", ""):
        with client.websocket_connect(f"{path}{sep}{query}") as ws:
            with pytest.raises(WebSocketDisconnect) as closed:
                ws.receive_text()
        assert closed.value.code == 4401


def test_tickets_are_masked_in_the_logs():
    line = 'GET /ws/console?server=mc&ticket=eyJhbGciOiJIUzI1NiJ9.abc.def HTTP/1.1'
    rec = logging.LogRecord("uvicorn.access", logging.INFO, __file__, 1, line, None, None)
    log_filters.MaskTokensFilter().filter(rec)
    assert "ticket=***" in rec.msg and "eyJ" not in rec.msg
