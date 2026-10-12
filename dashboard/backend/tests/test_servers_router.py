"""Registry API helpers and deps (no database; the HTTP paths are covered elsewhere)."""
import asyncio

import pytest
from fastapi import HTTPException

from app.auth import deps
from app.models.server import GameServer, GameType, ManagementMode
from app.routers import server as server_router
from app.routers import servers as servers_router


def _srv(**over):
    base = dict(key="pz-x", game_type=GameType.pz, enabled=True)
    base.update(over)
    return GameServer(**base)


# ------------------------------------------------------------------ Deps

def test_get_server_404_for_missing_or_disabled(monkeypatch):
    monkeypatch.setattr(deps.servers_registry, "get_by_key", lambda k: None)
    with pytest.raises(HTTPException) as ei:
        deps.get_server("nope")
    assert ei.value.status_code == 404

    monkeypatch.setattr(deps.servers_registry, "get_by_key", lambda k: _srv(enabled=False))
    with pytest.raises(HTTPException) as ei:
        deps.get_server("dis")
    assert ei.value.status_code == 404


def test_get_server_returns_enabled_row(monkeypatch):
    row = _srv(key="pz-1")
    monkeypatch.setattr(deps.servers_registry, "get_by_key", lambda k: row)
    assert deps.get_server("pz-1") is row


def test_get_driver_dep_resolves_minecraft():
    # MC has a driver → no 501, get_driver_dep returns it.
    from app.services.drivers.minecraft import MinecraftDriver

    drv = deps.get_driver_dep(_srv(key="mc", game_type=GameType.minecraft))
    assert isinstance(drv, MinecraftDriver)


# ------------------------------------------------------------------ server_public

def test_server_public_hides_password_and_lists_caps():
    srv = _srv(rcon_password="SECRET", game_ports='{"game":"16261/udp"}', extra='{"a":1}')
    srv.id = 1
    out = servers_router.server_public(srv)
    assert "rcon_password" not in out              # never deliver the secret
    assert out["rcon_password_env"] == ""          # an env name is not a secret → stays in
    assert out["game_ports"] == {"game": "16261/udp"}
    assert out["extra"] == {"a": 1}
    assert "console" in out["capabilities"]        # PZ has capabilities

    mc = _srv(key="mc", game_type=GameType.minecraft)
    assert "console" in servers_router.server_public(mc)["capabilities"]  # MC has capabilities


# ------------------------------------------------------------------ validation

@pytest.mark.parametrize("key", ["pz-1", "mc1", "a-b-c", "x9"])
def test_validate_key_ok(key):
    servers_router._validate_key(key)


@pytest.mark.parametrize("key", ["", "A", "-x", "x", "UPPER", "x_y", "toolong" * 10, "sp ace"])
def test_validate_key_bad(key):
    with pytest.raises(HTTPException):
        servers_router._validate_key(key)


@pytest.mark.parametrize("d", ["", "/srv/gameservers/mc", "/srv/gameservers/pz-test", "/srv/gameservers/minecraft/data"])
def test_validate_data_dir_ok(d):
    servers_router._validate_data_dir(d)  # /srv/gameservers is allowed (MC/VS adoption)


@pytest.mark.parametrize("d", ["/etc", "/srv/gameserversx", "/srv/gameservers/../etc", "relative/dir", "/srv/gameserversx"])
def test_validate_data_dir_bad(d):
    with pytest.raises(HTTPException):
        servers_router._validate_data_dir(d)


def test_check_update_immutable_and_compose_guard():
    dash = _srv(management_mode=ManagementMode.dashboard)
    with pytest.raises(HTTPException):
        servers_router._check_update(dash, {"key": "x"})            # immutable
    with pytest.raises(HTTPException):
        servers_router._check_update(dash, {"game_type": GameType.minecraft})
    servers_router._check_update(dash, {"container_name": "c", "ram_mb": 2048})  # dashboard: ok

    comp = _srv(management_mode=ManagementMode.compose)
    with pytest.raises(HTTPException):
        servers_router._check_update(comp, {"container_name": "c"})  # compose: forbidden
    servers_router._check_update(comp, {"display_name": "New", "enabled": True})  # compose: allowed


def test_check_update_rejects_bad_null():
    dash = _srv(management_mode=ManagementMode.dashboard)
    with pytest.raises(HTTPException):
        servers_router._check_update(dash, {"display_name": None})   # not nullable → 400 instead of 500
    with pytest.raises(HTTPException):
        servers_router._check_update(dash, {"game_ports": None})     # otherwise stored as the string "null"
    servers_router._check_update(dash, {"heap_max_mb": None})            # really nullable → ok
    servers_router._check_update(dash, {"idle_timeout_minutes": None})   # really nullable → ok


# ------------------------------------------------------------------ perform_action

def test_perform_action_rejects_unknown_action():
    with pytest.raises(HTTPException) as ei:
        asyncio.run(server_router.perform_action(_srv(), "kill", None, None))
    assert ei.value.status_code == 400


def test_extra_is_delivered_masked_and_written_back_unchanged():
    """`GET /api/servers` hangs on `can_view_status` — EVERY active user has that.

    But `extra` carries secrets by driver design: `pz.container_env` reads
    `extra.get("password")` as plain text, and MC/VS/Reforger pass `extra.env` through to the
    container env unseen.

    The second half matters just as much: delivering masked without reversing the mask on save
    would write the dots into the registry as the real value.
    """
    import json

    from app.models.server import GameServer, GameType
    from app.routers.servers import MASKED, _extra_masked, _extra_restored

    srv = GameServer(key="x", game_type=GameType.pz, extra=json.dumps({
        "servername": "servertest",
        "password": "top-secret",
        "admin_password_env": "PZ_ADMIN_PW",
        "env": {"SOME_TOKEN": "abc123", "HARMLESS": "yes"},
    }))

    visible = _extra_masked(srv)
    raw = json.dumps(visible)
    assert "top-secret" not in raw and "abc123" not in raw
    assert visible["servername"] == "servertest"          # structure stays readable
    # Since A2 the NAMES of secret variables are masked too: members need none of them.
    assert visible["admin_password_env"] == MASKED
    assert visible["env"]["HARMLESS"] == "yes"

    # Round trip: sent back unchanged -> the real values are back in.
    old = json.loads(srv.extra)
    restored = _extra_restored(visible, old)
    assert restored == old, f"Saving would have written the mask into the registry: {restored}"

    # And a real change goes through.
    changed = dict(visible, password="new-secret")
    assert _extra_restored(changed, old)["password"] == "new-secret"
