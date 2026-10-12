"""Auth comes BEFORE server resolution, and tokens do not appear in the log.

Both were silent leaks: nothing was broken, it just gave away more than it should.

1. `Depends(get_server)` stood before the auth dependency in all ~80 scoped routes. FastAPI
   resolves dependencies in signature order and the first HTTPException wins, so unauthenticated
   callers got 404 (unknown key), 400 including the game type (wrong game) or 501 instead of 401
   and could enumerate server keys and game types that way.
2. WebSockets cannot set an `Authorization` header; the JWT hangs on the URL and uvicorn logs the
   full query.
"""
import logging

import pytest
from fastapi.dependencies.utils import get_dependant  # exactly how FastAPI builds the tree

from app import log_filters
from app.auth import deps


def _flatten(dependant, out=None):
    """All dependency callables of an endpoint, recursively."""
    out = [] if out is None else out
    for sub in dependant.dependencies:
        out.append(sub.call)
        _flatten(sub, out)
    return out


def _scoped_routes():
    from app.main import app
    from tests.route_walk import flat_routes

    for r in flat_routes(app):
        dep = getattr(r, "dependant", None)
        if dep is None:
            continue
        calls = _flatten(dep) + [dep.call]
        if deps.get_server in calls or deps.get_driver_dep in calls:
            yield getattr(r, "path", "?"), calls


def test_every_route_with_server_resolution_requires_a_token_first():
    """The guard rail. Whoever builds a new scoped route inherits the check via `get_server`.

    Whoever bypasses it (fetching the server row in the body) must set the auth dependency
    themselves; this test tells them.
    """
    checked = 0
    for path, calls in _scoped_routes():
        assert deps.get_current_user in calls, f"{path}: resolves the server without a token check"
        checked += 1
    # Lower bound against a silent failure of the collection itself (there were 80).
    assert checked >= 70, f"only {checked} scoped routes found; is the collection broken?"


def test_get_server_requires_the_token_check_in_its_OWN_tree():
    """The test above is too weak; this one is the actual guard rail.

    `get_current_user` is in EVERY one of the ~80 dependency trees anyway via `require_flag(...)`,
    quite independently of `get_server`. If the `_user` argument is taken out of
    `deps.get_server` again, the membership check above stays green, and the oracle is back,
    because FastAPI resolves in signature order and `Depends(get_server)` stands BEFORE the
    permission dependency in most signatures.

    A position comparison in the flat list does not help either: `_flatten` appends `get_server`
    BEFORE it descends into its subtree. What counts is the nesting, so that is exactly what we
    ask about.
    """
    own_tree = _flatten(get_dependant(path="/", call=deps.get_server))
    assert deps.get_current_user in own_tree, (
        "deps.get_server resolves the server without requiring a token itself; "
        "the `_user` argument is missing. The 404 wins against the 401 again."
    )


def test_without_a_token_an_invented_server_cannot_be_told_from_a_real_one(
    api_client, monkeypatch
):
    """The same assurance on real behaviour instead of on the dependency tree.

    `test_smoke_live.py` runs only with `-m smoke`, after the deploy against the running instance,
    so the gate before a commit needs its own copy. It needs no Postgres here: without a token the
    request ends in `oauth2_scheme` before `get_session` is pulled in.
    """
    from app.models.server import GameServer, GameType

    real = GameServer(key="pz-1", game_type=GameType.pz, enabled=True)

    for key, line in (("pz-1", real), ("doesnotexist", None)):
        monkeypatch.setattr(deps.servers_registry, "get_by_key", lambda k, z=line: z)
        response = api_client.get(f"/api/servers/{key}/logs")
        assert response.status_code == 401, (
            f"/api/servers/{key}/logs without a token → {response.status_code} instead of 401. "
            "A 404 here gives away which server keys exist."
        )


def test_get_server_stays_directly_callable(monkeypatch):
    """The auth dependency is a default argument; unit tests keep calling the function directly."""
    from app.models.server import GameServer, GameType

    row = GameServer(key="pz-1", game_type=GameType.pz, enabled=True)
    monkeypatch.setattr(deps.servers_registry, "get_by_key", lambda k: row)
    assert deps.get_server("pz-1") is row


# ------------------------------------------------------------------ token masking

@pytest.mark.parametrize(
    "line",
    [
        'GET /ws/logs?server=pz-1&token=eyJhbGciOiJIUzI1NiJ9.abc.def HTTP/1.1',  # gitleaks:allow — synthetic JWT-shaped fixture
        '"WebSocket /ws/console?server=mc&token=eyJ0eXAi.xyz" [accepted]',
        "/api/x?access_token=secret&next=/",
        "/api/x?refresh_token=secret",
    ],
)
def test_tokens_are_masked(line):
    rec = logging.LogRecord("uvicorn.access", logging.INFO, __file__, 1, line, None, None)
    assert log_filters.MaskTokensFilter().filter(rec) is True
    assert "token=***" in rec.msg
    for secret in ("eyJhbGciOiJIUzI1NiJ9.abc.def", "eyJ0eXAi.xyz", "secret"):
        assert secret not in rec.msg


def test_masking_also_applies_to_the_format_arguments():
    """uvicorn logs `'%s - "%s %s"'` with arguments; the query sits there, not in msg."""
    rec = logging.LogRecord(
        "uvicorn.access", logging.INFO, __file__, 1,
        '%s - "%s %s" %d', ("203.0.113.10", "GET", "/ws/logs?token=top-secret", 200), None,
    )
    log_filters.MaskTokensFilter().filter(rec)
    assert rec.args[2] == "/ws/logs?token=***"
    assert rec.args[3] == 200  # non-strings untouched


def test_unrelated_lines_stay_as_they_are():
    rec = logging.LogRecord("uvicorn.error", logging.INFO, __file__, 1, "Application startup complete.", None, None)
    log_filters.MaskTokensFilter().filter(rec)
    assert rec.msg == "Application startup complete."


def test_install_attaches_to_both_loggers_and_is_idempotent():
    """`uvicorn.error` is no accident: the WebSocket `[accepted]` line lands there, not in access."""
    for name in ("uvicorn.access", "uvicorn.error"):
        logger = logging.getLogger(name)
        logger.filters = [f for f in logger.filters if not isinstance(f, log_filters.MaskTokensFilter)]

    log_filters.install()
    log_filters.install()

    for name in ("uvicorn.access", "uvicorn.error"):
        hits = [f for f in logging.getLogger(name).filters if isinstance(f, log_filters.MaskTokensFilter)]
        assert len(hits) == 1, f"{name}: {len(hits)} filters instead of exactly one"
