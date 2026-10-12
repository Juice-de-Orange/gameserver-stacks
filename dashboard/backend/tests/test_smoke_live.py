"""Assertions against the RUNNING dashboard.

Run them from `dashboard/backend` against your own installation — there is no default host:

    python -m pytest -m smoke --base-url=https://dashboard.example.com

(or set `GSS_SMOKE_BASE_URL`; an admin token goes into `--admin-token` / `GSS_SMOKE_ADMIN_TOKEN`).
Without a URL every test here skips itself.

**The core checks need no credentials.** That is not thrift but the condition for running them
automatically. Passing a token (`--admin-token`) adds the cases that need a signed-in session.

Deliberately NOT in `test_smoke.py`: that is the hermetic import test that runs in the default
run. This file needs a network and is excluded via `pytest.ini`.
"""

import httpx
import pytest

pytestmark = pytest.mark.smoke

TIMEOUT = 15.0


# ------------------------------------------------------------------ 1. Health WITH database

def test_health_reports_a_reachable_database(base_url):
    """A green status code alone proves nothing here.

    `main.py::health()` **always** returns 200 and `"status": "ok"` — even with a dead database;
    only the `db` field flips to `false`. A deploy check that only looks at the status code would
    wave an API without a database through as green.
    """
    r = httpx.get(f"{base_url}/api/health", timeout=TIMEOUT)
    assert r.status_code == 200, f"health answers with {r.status_code}"
    data = r.json()
    assert data.get("status") == "ok", data
    assert data.get("db") is True, f"database not reachable: {data}"


# ------------------------------------------------------------------ 2. Scoped route without a token

@pytest.mark.parametrize(
    "path",
    [
        "/api/servers/pz-1/logs",
        "/api/servers/pz-1/backups",
        "/api/servers/pz-1/schedules",
    ],
)
def test_a_scoped_route_without_a_token_is_401(base_url, path):
    """Not 404, not 400, not 501 — **401**.

    If `Depends(get_server)` runs before the auth dependency, unauthenticated callers get an
    oracle: unknown key → 404, existing key with the wrong game type → 400 naming the game type.
    That lets anyone enumerate server keys and game types without any access.
    """
    r = httpx.get(f"{base_url}{path}", timeout=TIMEOUT, follow_redirects=True)
    assert r.status_code == 401, f"{path} answers with {r.status_code} instead of 401"


def test_an_invented_server_is_indistinguishable_from_a_real_one(base_url):
    """The actual property: the answer does not reveal whether the server exists."""
    real = httpx.get(f"{base_url}/api/servers/pz-1/logs", timeout=TIMEOUT)
    invented = httpx.get(f"{base_url}/api/servers/doesnotexist-xyz/logs", timeout=TIMEOUT)
    assert real.status_code == invented.status_code == 401


# ------------------------------------------------------------------ 3. /ws/logs handshake

def _ws_close_code(url: str, token: str = "") -> int:
    """Connect and read the close code from the frame.

    The handshake is ALWAYS accepted (`routers/logs.py` calls `ws.accept()` first) — the
    rejection follows as a close frame. A test that waits for an exception on connect therefore
    checks nothing.
    """
    from websockets.exceptions import ConnectionClosed
    from websockets.sync.client import connect as ws_connect

    target = url
    if token:
        # WebSockets take a single-use ticket, not the access token (`POST /api/auth/ws-ticket`).
        base = url.split("/ws/")[0].replace("wss://", "https://").replace("ws://", "http://")
        r = httpx.post(f"{base}/api/auth/ws-ticket", timeout=TIMEOUT,
                       headers={"Authorization": f"Bearer {token}"})
        r.raise_for_status()
        target = url + ("&" if "?" in url else "?") + f"ticket={r.json()['ticket']}"
    with ws_connect(target, open_timeout=TIMEOUT, close_timeout=5) as ws:
        try:
            ws.recv(timeout=TIMEOUT)
        except ConnectionClosed as e:
            return e.rcvd.code if e.rcvd else 0
        except TimeoutError:
            return 0              # connected and silent — that is the success case
        return 0                  # data arrived → connected


def _ws_base(base_url: str) -> str:
    return base_url.replace("https://", "wss://").replace("http://", "ws://")


def test_ws_logs_rejects_without_a_token_with_4401(base_url):
    """Proves two things at once: the WebSocket path is reachable (the reverse proxy really
    passes `/ws` through) AND it lets nobody without access at the server logs.

    4401 rather than 4400: the token check comes BEFORE the server check — here too the answer
    reveals nothing about whether a server exists.
    """
    code = _ws_close_code(f"{_ws_base(base_url)}/ws/logs?server=pz-1")
    assert code == 4401, f"close code {code} instead of 4401"


def test_ws_logs_with_a_token_requires_a_server(base_url, admin_token):
    """Valid token but no `?server=` → 4400. There is no silent fallback to a default server;
    a client that forgets the parameter must fail loudly, not show an empty log dock."""
    if not admin_token:
        pytest.skip("--admin-token missing")
    code = _ws_close_code(f"{_ws_base(base_url)}/ws/logs", admin_token)
    assert code == 4400, f"close code {code} instead of 4400"


def test_ws_logs_connects_with_a_token_and_a_server(base_url, admin_token):
    if not admin_token:
        pytest.skip("--admin-token missing")
    code = _ws_close_code(f"{_ws_base(base_url)}/ws/logs?server=pz-1", admin_token)
    assert code == 0, f"connection rejected with close code {code}"


# ------------------------------------------------------------------ Bonus without credentials

def test_the_dropped_global_routes_are_really_gone(base_url):
    """Removed without replacement. The import smoke test checks this against the code — this
    one against what is actually served."""
    for path in ("/api/config", "/api/backups", "/api/moderation", "/api/logs"):
        r = httpx.get(f"{base_url}{path}", timeout=TIMEOUT)
        assert r.status_code == 404, f"{path} is still alive ({r.status_code})"


def test_the_ui_is_served(base_url):
    """The reverse proxy serves the built frontend — otherwise the deploy only half arrived."""
    r = httpx.get(f"{base_url}/", timeout=TIMEOUT)
    assert r.status_code == 200
    assert "<div id=\"root\"" in r.text or "<script" in r.text, r.text[:200]


# (Token masking does not belong here — it can be checked without a network and lives in
#  `tests/test_auth_order.py`, where it runs in the default run instead of only on deploy.)
