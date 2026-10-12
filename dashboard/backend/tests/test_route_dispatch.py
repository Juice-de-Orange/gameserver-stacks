"""Which route answers which path — checked against the real Starlette dispatch.

`test_smoke.py` checks which paths are *registered*, `test_auth_order.py` the dependency trees —
neither says who actually answers a real request.

A catch-all `POST /api/servers/{key}/{action}` in `routers/servers.py`, mounted in `main.py`
**before** `server_ops`, `schedules` and `broadcasts`, swallows every POST route with exactly one
segment after `{key}`, because Starlette takes the **first registered** route among equally good
matches:

    POST /api/servers/pz-1/backups    → scoped_action → perform_action("backups") → HTTP 400
    POST /api/servers/pz-1/schedules  → same
    POST /api/servers/pz-1/broadcasts → same

Creating a backup, a restart schedule or a broadcast would answer *400 invalid action*, while the
GET variants keep working (the catch-all is POST-only), so the lists look normal. The fix is not
reordering the registration — an order dependency that silently comes back with the next
refactor — but explicit routes instead of the catch-all. This test pins down both.
"""
import re

import pytest
from starlette.routing import Match

from app.main import app
from tests.route_walk import flat_routes


def _winner(method: str, path: str):
    """The route Starlette really takes for this request (first full match)."""
    scope = {
        "type": "http", "method": method, "path": path, "headers": [],
        "query_string": b"", "root_path": "", "path_params": {},
    }
    for route in flat_routes(app):
        if route.matches(scope)[0] == Match.FULL:
            return getattr(route, "name", "?")
    return None


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("/api/servers/pz-1/backups", "create_backup"),
        ("/api/servers/pz-1/schedules", "create_schedule_scoped"),
        ("/api/servers/pz-1/broadcasts", "create_broadcast_scoped"),
    ],
)
def test_the_create_routes_are_not_shadowed(path, expected):
    """These three must not answer 400 instead of doing what they are for."""
    assert _winner("POST", path) == expected


@pytest.mark.parametrize("action", ["start", "stop", "restart"])
def test_the_lifecycle_actions_still_work(action):
    assert _winner("POST", f"/api/servers/pz-1/{action}") == f"scoped_{action}"


def test_no_registered_route_is_shadowed_by_another():
    """The guard rail that keeps a shadowed route from coming back.

    For EVERY registered route a concrete sample path is built and the dispatch is asked who
    answers it. Mounting a new catch-all (`/{anything}`) before more specific routes breaks here —
    not only when someone presses a button in the dashboard.
    """
    sample = {"key": "pz-1", "name": "20260101-000000-weekly.tar.gz", "sid": "1",
                "op_id": "1", "user_id": "1", "ticket_id": "1", "id": "1", "modId": "1",
                "action": "start", "slot": "a", "preset_id": "p", "username": "admin"}
    shadowed = []
    for route in flat_routes(app):
        path = getattr(route, "path", "")
        name = getattr(route, "name", "")
        methods = getattr(route, "methods", None) or set()
        if not path.startswith("/api/") or not methods:
            continue
        placeholders = re.findall(r"\{([a-zA-Z_]+)[^}]*\}", path)
        if any(p not in sample for p in placeholders):
            continue  # a placeholder we have no sample for — not checkable, not a problem
        concrete = path
        for p in placeholders:
            concrete = re.sub(r"\{" + p + r"[^}]*\}", sample[p], concrete)
        for method in sorted(methods - {"HEAD", "OPTIONS"}):
            actual = _winner(method, concrete)
            if actual != name:
                shadowed.append(f"{method} {path} ({name}) is answered by '{actual}'")

    assert not shadowed, "Shadowed routes:\n  " + "\n  ".join(shadowed)
