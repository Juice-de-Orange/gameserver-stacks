"""Server registry API (multi-game): list + admin CRUD + server-scoped views.

The scoped views (/status, /overview, /history, /incidents, start/stop/restart) call the core
helpers in `routers/server.py` with the resolved registry row. The lifecycle of a row: create
(`POST`), create its container (`POST /{key}/container`), edit (`PUT /{key}`), switch off
(`PUT {enabled: false}`, stops the container), remove (`POST /{key}/remove`, only a switched-off
row; telemetry goes, records stay — `services/provisioning.py`).
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlmodel import Session, select

from ..auth.deps import get_server, require_admin, require_flag
from ..config import settings
from ..db import get_session
from ..errors import detail_of, err
from ..models import MetricSample, Role, ServerIncident, User
from ..models.player import utcnow_naive
from ..models.server import ConsoleKind, GameServer, GameType, ManagementMode
from ..services import game_settings, install_settings, operations, ports, provisioning, restarts
from ..services import secrets as server_secrets
from ..services import servers as servers_registry
from ..services.adoption import adoption_preflight
from ..services.audit import record
from ..services.container_spec import ContainerSpecError, _check_data_dir
from ..services.docker_client import docker
from ..services.drivers import get_driver, running_version
from ..services.drivers.base import MASKED, mask_if_secret
from ..services.fleet import clear_cache as fleet_clear_cache
from ..services.fleet import fleet_status
from ..services.server_presets import preset_list
from .server import _bucket, overview_payload, perform_action, status_payload

router = APIRouter(prefix="/api/servers", tags=["servers"])

_view = require_flag("can_view_status")
_restart = require_flag("can_restart")

_KEY_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,31}$")
# Docker's own rule for container names (`[a-zA-Z0-9][a-zA-Z0-9_.-]+`), capped at 128 characters.
# The name is also the host name the console connects to on the game network.
_CONTAINER_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{1,127}$")
# The NAME of an environment variable (`rcon_password_env`, `extra.*_env`). A password typed
# into the name field would be stored as a name; the pattern turns most of those mistakes away.
_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
# The first step of a new server without the browser (`cli._create_container`, the same service
# as the "Create container" button), as the operator types it on the host: `console-api` is the
# API service in docker-compose.yml, `app.cli` the module.
CREATE_CONTAINER_COMMAND = "docker compose exec console-api python -m app.cli create-container {key}"
_IMMUTABLE = {"key", "game_type", "management_mode"}
_NULLABLE = {"heap_max_mb", "heap_min_mb", "idle_timeout_minutes"}  # the only columns that may be NULL
_COMPOSE_EDITABLE = {
    "display_name", "sort_order", "enabled", "idle_autostop_enabled", "idle_timeout_minutes",
    "bridge_in", "bridge_out", "presence_visible", "announce_target", "extra",
}


# ------------------------------------------------------------------ schemas / serialisers

class ServerCreate(BaseModel):
    key: str
    game_type: GameType
    display_name: str = ""
    sort_order: int = 0
    enabled: bool = False  # new rows default to OFF (the fan-out ignores them until the admin enables them)
    container_name: str = ""
    data_dir: str = ""
    image_repo: str = ""
    image_tag: str = ""
    network: str = Field(default_factory=lambda: settings.game_network)
    console_kind: ConsoleKind = ConsoleKind.source_rcon
    rcon_host: str = ""
    rcon_port: int = 0
    # Write-only: stored encrypted (`services/secrets.py`), never in the row, never returned.
    rcon_password: str = ""
    rcon_password_env: str = ""
    game_ports: dict[str, str] = {}
    # Host address the game ports are published on (`services/ports.py`); empty = 0.0.0.0.
    bind_address: str = ports.ANY
    game_version: str = ""
    server_type: str = ""
    ram_mb: int = 0
    cpu_cores: float = 0.0
    heap_max_mb: int | None = None
    heap_min_mb: int | None = None
    idle_autostop_enabled: bool = True
    idle_timeout_minutes: int | None = None
    bridge_in: bool = True
    bridge_out: bool = True
    presence_visible: bool = True
    announce_target: bool = True
    extra: dict = {}
    # Minecraft only: the admin accepted the Minecraft EULA in person. Recorded in
    # `extra.eula_accepted`; without it neither the row nor its container is created
    # (`services/provisioning.check_static`).
    eula_accepted: bool = False
    # management_mode is NOT accepted: rows created through the API are always "dashboard".


class ServerUpdate(BaseModel):
    display_name: str | None = None
    sort_order: int | None = None
    enabled: bool | None = None
    container_name: str | None = None
    data_dir: str | None = None
    image_repo: str | None = None
    image_tag: str | None = None
    network: str | None = None
    console_kind: ConsoleKind | None = None
    rcon_host: str | None = None
    rcon_port: int | None = None
    rcon_password: str | None = None
    rcon_password_env: str | None = None
    game_ports: dict[str, str] | None = None
    bind_address: str | None = None
    game_version: str | None = None
    server_type: str | None = None
    ram_mb: int | None = None
    cpu_cores: float | None = None
    heap_max_mb: int | None = None
    heap_min_mb: int | None = None
    idle_autostop_enabled: bool | None = None
    idle_timeout_minutes: int | None = None
    bridge_in: bool | None = None
    bridge_out: bool | None = None
    presence_visible: bool | None = None
    announce_target: bool | None = None
    extra: dict | None = None


def _extra_restored(new: dict, old: dict) -> dict:
    """Replace masked values with the old ones — the inverse of `_extra_masked`."""
    out = {}
    for k, v in new.items():
        if isinstance(v, dict):
            out[k] = _extra_restored(v, old.get(k) if isinstance(old.get(k), dict) else {})
        elif v == MASKED and k in old:
            out[k] = old[k]
        else:
            out[k] = v
    return out


def _extra_masked(srv: GameServer) -> dict:
    """`extra` with masked secrets — recursive.

    `GET /api/servers` is gated by `can_view_status`, which by the default matrix EVERY active
    user has (model default plus role default for `member`). But by driver design `extra` can
    carry secrets: `pz.container_env` reads `extra.get("password")` as clear text, and
    MC/VS/Reforger pass `extra.env` through as container env unchecked — arbitrary secret names
    and values.

    The helper is the same one that masks `config_fields` (`drivers/base.mask_if_secret`): a
    secret has to be masked in BOTH read directions.
    """
    def _value(k, v):
        if isinstance(v, dict):
            return _rec(v)
        # Only mask strings: numbers and booleans are not secrets, and stringifying them would
        # silently change the type (`{"a": 1}` → `{"a": "1"}`), which would then end up in the
        # registry on write-back.
        if not isinstance(v, str):
            return v
        # `*_env` is a NAME, not a value: the driver resolves `admin_password_env` against the
        # environment. It is masked all the same (see `server_public`) and restored on save by
        # `_extra_restored`, so the row stays usable.
        if str(k).endswith("_env"):
            return _name_masked(v)
        return mask_if_secret(str(k), v)

    def _rec(d):
        return {k: _value(k, v) for k, v in d.items()}

    return _rec(json.loads(srv.extra or "{}"))


def _name_masked(name: str) -> str:
    return MASKED if name else ""


def server_public(srv: GameServer) -> dict:
    """Public view of a registry row — WITHOUT any secret.

    `GET /api/servers` is open to every signed-in user. The console password is never in it, and
    since A2 neither are the NAMES of the variables that hold secrets (`rcon_password_env`,
    `extra.*_env`): they tell a member where to look and add nothing a member needs. Admins get
    the name and the "set / not set" state from `GET /{key}/setup` (`services/secrets.status`).
    A masked name sent back on save is restored, like the masked `extra` values.
    """
    # The driver properties the UI needs come from ONE query. Without them the frontend would
    # have to rebuild a per-game list by hand (`versioned_by_image_tag`, `config_reload`).
    try:
        drv = get_driver(srv)
        caps = sorted(drv.capabilities())
        version_source = drv.version_source.value
        config_reload = drv.config_reload.value
    except NotImplementedError:
        caps, version_source, config_reload = [], "", ""
    return {
        "id": srv.id,
        "key": srv.key,
        "game_type": srv.game_type,
        "display_name": srv.display_name,
        "sort_order": srv.sort_order,
        "enabled": srv.enabled,
        "management_mode": srv.management_mode,
        "container_name": srv.container_name,
        "data_dir": srv.data_dir,
        "image_repo": srv.image_repo,
        "image_tag": srv.image_tag,
        "network": srv.network,
        "console_kind": srv.console_kind,
        "rcon_host": srv.rcon_host,
        "rcon_port": srv.rcon_port,
        "rcon_password_env": _name_masked(srv.rcon_password_env),
        "game_ports": json.loads(srv.game_ports or "{}"),
        "bind_address": srv.bind_address,
        # **Not `srv.game_version` directly.** For games whose version is in the image tag the
        # column is a second source of truth that can drift apart from the tag.
        # `running_version` asks the source that applies to the respective game.
        "game_version": running_version(srv),
        "server_type": srv.server_type,
        "ram_mb": srv.ram_mb,
        "cpu_cores": srv.cpu_cores,
        "heap_max_mb": srv.heap_max_mb,
        "heap_min_mb": srv.heap_min_mb,
        "idle_autostop_enabled": srv.idle_autostop_enabled,
        "idle_timeout_minutes": srv.idle_timeout_minutes,
        # When someone was last seen — needed to compute the time left until the idle stop.
        "last_active_at": srv.last_active_at,
        "bridge_in": srv.bridge_in,
        "bridge_out": srv.bridge_out,
        "presence_visible": srv.presence_visible,
        "announce_target": srv.announce_target,
        "extra": _extra_masked(srv),
        "capabilities": caps,
        # Where the version comes from: "image_tag" (a change downloads an image) | "env" | "runtime".
        "version_source": version_source,
        # What makes a config change take effect: "live" | "restart" | "recreate".
        "config_reload": config_reload,
        "created_at": srv.created_at,
        "updated_at": srv.updated_at,
    }


def _validate_key(key: str) -> None:
    if not _KEY_RE.fullmatch(key):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail=err(
                "server.key_invalid",
                "Invalid server key (a-z, 0-9, '-', 2–32 characters, no leading hyphen)",
            ),
        )


def _validate_container_name(name: str) -> None:
    """Checked on create and save: an empty or invalid name only failed later, at "Create
    container", with Docker's bare redirect error instead of a reason."""
    if not _CONTAINER_NAME_RE.fullmatch(name):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail=err(
                "server.container_name_invalid",
                "Invalid container name (letters, digits, '_', '.', '-', 2–128 characters, starting "
                "with a letter or digit)",
            ),
        )


def _validate_env_names(rcon_password_env: str | None, extra: dict | None) -> None:
    """`rcon_password_env` and the `*_env` keys of `extra` must look like variable names.

    The value is deliberately NOT part of the error: if it is a password typed into the wrong
    field, the response must not carry it back.
    """
    names = [rcon_password_env or ""]
    names += [v for k, v in (extra or {}).items() if str(k).endswith("_env") and isinstance(v, str)]
    # A masked name is the one GET delivered, sent back unchanged; `_extra_restored` puts the
    # real name back before anything is stored.
    if any(n and n != MASKED and not _ENV_NAME_RE.fullmatch(n) for n in names):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail=err(
                "server.secret_env_name_invalid",
                "An env variable name may contain only letters, digits and '_' and must not start "
                "with a digit. Enter the NAME of the variable, not the password.",
            ),
        )


def _normalize_public_host(extra: dict | None) -> None:
    """`extra.public_host` is a host name or IP address alone (`install_settings`); empty → removed.

    Reforger writes it into its `config.json` as `publicAddress`, and the join page shows it, so
    "http://…" or a port in it would be wrong in both places.
    """
    if not isinstance(extra, dict) or "public_host" not in extra:
        return
    value = install_settings.normalize_public_host(str(extra.get("public_host") or ""))
    if value:
        extra["public_host"] = value
    else:
        extra.pop("public_host")


def _validate_data_dir(data_dir: str) -> None:
    """Uses exactly the container_spec hardening (settings.allowed_data_roots, no `..`) — DRY."""
    if not data_dir:
        return  # empty is allowed (set later on adoption)
    try:
        _check_data_dir(data_dir)
    except ContainerSpecError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=detail_of(e))


def _check_version_required(srv: GameServer) -> None:
    """Games whose image does not start without a version need `game_version` to be set.

    See `GameDriver.version_required`: without this check the error only shows at the next
    recreate — as a restart loop the fallback does not get out of.
    """
    try:
        driver = get_driver(srv)
    except NotImplementedError:
        return
    if not driver.version_required:
        return
    version = running_version(srv) or ""
    if not version.strip():
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail=err(
                "server.version_required",
                "This game needs a version to be set (game_version) — the image has no default version.",
            ),
        )
    if not driver.version_valid(version):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail=err(
                "server.version_invalid",
                "'{version}' is not a valid version for this game (e.g. 1.22.7).",
                version=version,
            ),
        )


def _check_update(srv: GameServer, fields: dict) -> None:
    bad = _IMMUTABLE & set(fields)
    if bad:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail=err(
                "server.fields_immutable", "Fields that cannot be changed: {fields}",
                fields=", ".join(sorted(bad)),
            ),
        )
    # Allow an explicit JSON null only for truly nullable columns — otherwise 400 instead of 500
    # (IntegrityError on NOT NULL) or a game_ports/extra stored as the string "null".
    bad_null = sorted(k for k, v in fields.items() if v is None and k not in _NULLABLE)
    if bad_null:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail=err(
                "server.fields_not_nullable", "Field(s) must not be null: {fields}",
                fields=", ".join(bad_null),
            ),
        )
    if srv.management_mode == ManagementMode.compose:
        illegal = set(fields) - _COMPOSE_EDITABLE
        if illegal:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                detail=err(
                    "server.compose_managed_fields",
                    "Compose-managed row: field(s) {fields} cannot be changed here",
                    fields=", ".join(sorted(illegal)),
                ),
            )


# ------------------------------------------------------------------ registry CRUD

@router.get("")
def list_servers(session: Session = Depends(get_session), _: User = Depends(_view)) -> list[dict]:
    """All registry rows (incl. disabled ones — metadata only, no secrets). No live status."""
    rows = session.exec(select(GameServer).order_by(GameServer.sort_order, GameServer.id)).all()
    return [server_public(s) for s in rows]


@router.get("/fleet")
async def fleet(_: User = Depends(_view)) -> dict:
    """All enabled servers with live figures plus the host's load — in ONE response.

    **This route must come BEFORE `GET /{key}`, and that is not a matter of style.** For
    equally good matches Starlette takes the route registered FIRST; if it were below, `/{key}`
    would answer it and report "server not found" for a key named `fleet`.
    `tests/test_route_dispatch.py` checks this against the real matcher.

    What it contains and what the fields mean: `services/fleet.py`.
    """
    return await fleet_status()


@router.get("/presets")
def presets(_: User = Depends(require_admin), session: Session = Depends(get_session)) -> dict:
    """Starting values for the "Add server" form, one per ACTIVE game (`services/server_presets.py`).

    Like `/fleet`, this route has to be registered BEFORE `GET /{key}`. The key pattern and the
    data root are sent along so that the form validates with the same values as `create_server`.
    """
    return {
        "key_pattern": _KEY_RE.pattern,
        "data_root": settings.data_root,
        "network": settings.game_network,
        "presets": preset_list(game_settings.active_games(session)),
    }


def _require_active_game(game_type: GameType, session: Session) -> None:
    """A server of an inactive game is neither created nor switched on (`services/game_settings.py`)."""
    if not game_settings.is_active(game_type, session):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail=err(
                "games.inactive",
                "Game '{game}' is not active on this installation. Activate it under Settings → Games first.",
                game=GameType(game_type).value,
            ),
        )


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_server(
    body: ServerCreate,
    session: Session = Depends(get_session),
    user: User = Depends(require_admin),
) -> dict:
    _validate_key(body.key)
    _validate_container_name(body.container_name)
    _validate_data_dir(body.data_dir)
    _validate_env_names(body.rcon_password_env, body.extra)
    _normalize_public_host(body.extra)
    # A join password typed into the form (Valheim SERVER_PASS) goes into encrypted storage,
    # never into the row (`services/secrets.py`).
    literals = server_secrets.split_literals(body.game_type, body.extra)
    _require_active_game(body.game_type, session)
    if session.exec(select(GameServer).where(GameServer.key == body.key)).first():
        raise HTTPException(
            status.HTTP_409_CONFLICT, detail=err("server.key_exists", "Server key already exists")
        )
    data = body.model_dump()
    secret = data.pop("rcon_password") or ""
    eula_accepted = data.pop("eula_accepted")
    if body.game_type == GameType.minecraft:
        # Server-side, not only the checkbox of the form: the driver creates the container with
        # EULA=TRUE, so whoever calls the API directly has to accept it just the same.
        if not eula_accepted:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                detail=err(
                    "server.eula_required",
                    "A Minecraft server can only be created after accepting the Minecraft EULA "
                    "(https://www.minecraft.net/en-us/eula): send eula_accepted=true.",
                ),
            )
        data["extra"] = {**data["extra"], "eula_accepted": True}
    data["game_version"] = (data.get("game_version") or "").strip()
    data["bind_address"] = ports.normalize_bind_address(data.get("bind_address"))
    data["game_ports"] = json.dumps(data["game_ports"])
    data["extra"] = json.dumps(data["extra"])
    data["management_mode"] = ManagementMode.dashboard
    srv = GameServer(**data)
    _check_version_required(srv)
    # Port format and conflicts with other servers and running containers (`services/ports.py`).
    await ports.check(srv, session)
    # A server that cannot announce is not announced to either: otherwise the announce fan-out
    # counts into the void and the cross-server bridge sends to a server that cannot use it.
    #
    # The CAPABILITY is asked, not the game type — it is in `capabilities()` already, and
    # `cross_server/features.py` and `bridge.py` use the same gate. Arma Reforger and Valheim
    # fall under it (no way to reach all players), Factorio does not — there `game.print`
    # really reaches every player.
    try:
        can_announce = "announce" in get_driver(srv).capabilities()
    except NotImplementedError:
        can_announce = False
    if not can_announce:
        srv.bridge_in = srv.bridge_out = srv.announce_target = False
    session.add(srv)
    session.commit()
    session.refresh(srv)
    if srv.id is not None and (secret or any(literals.values())):
        if secret:
            server_secrets.store(session, srv.id, secret)
        server_secrets.store_found(session, srv.id, literals)
        session.commit()
    # Auto-seed: create the configuration file + directories if missing — otherwise the first
    # config/mod write would hit an empty or partial file. Best effort; file ownership and
    # permissions for the container are not adjusted here.
    #
    # The CAPABILITY is asked, not the game type: `hasattr` is the same seam through which
    # `server_ops` offers `set_mods`/`set_scenario`. Reforger seeds its `config.json`,
    # Factorio `server-settings.json` **and `rconpw`** — without the latter the image generates
    # a random password at first start and the dashboard would never reach the console again.
    seed = getattr(get_driver(srv), "ensure_config", None)
    if callable(seed):
        try:
            seed(srv, servers_registry.resolve_secret(srv))
        except Exception as e:  # noqa: BLE001 — non-fatal, creation must not fail because of it
            servers_registry.log.warning("config seed failed for %s: %s", srv.key, e)
    record(session, user, "servers.create", srv.key, server_id=srv.id)
    return server_public(srv)


async def validate_update(srv: GameServer, fields: dict, session: Session) -> None:
    """Every check of a change to a registry row, in one place — before anything is written.

    The edit page (`frontend/src/pages/EditServer.tsx`) sends its changes here; checks that need
    the merged row (the version) run after the fields are applied (`_check_version_required`).
    The ports are checked on the row as it WILL be (`services/ports.py`): format, other servers,
    running containers other than its own.
    """
    _check_update(srv, fields)
    if fields.get("enabled") and not srv.enabled:
        _require_active_game(srv.game_type, session)
    if "container_name" in fields:
        _validate_container_name(fields["container_name"])
    if "data_dir" in fields:
        _validate_data_dir(fields["data_dir"])
    if "bind_address" in fields:
        fields["bind_address"] = ports.normalize_bind_address(fields["bind_address"])
    _validate_env_names(fields.get("rcon_password_env"), fields.get("extra"))
    _normalize_public_host(fields.get("extra"))
    if {"game_ports", "bind_address", "container_name"} & set(fields):
        merged = {k: fields[k] for k in ("game_ports", "bind_address", "container_name") if k in fields}
        if "game_ports" in merged:
            ports.check_format(merged["game_ports"])
            merged["game_ports"] = json.dumps(merged["game_ports"])
        candidate = GameServer(**{**srv.model_dump(), **merged})
        await ports.check(candidate, session, exclude={srv.container_name})


async def _stop_for_disable(srv: GameServer) -> None:
    """Switching a server off stops its container — before the row says "off".

    Before A2 `enabled=False` only took the row out of the fan-out: the container went on
    running under `unless-stopped`, used its RAM and kept players on a server the dashboard no
    longer watched (no watchdog, no backups, no restarts). Now the container is stopped first,
    through `lifecycle` (the game's own clean stop, as with the Stop button). If Docker cannot
    stop it, the row stays switched on and the caller gets the reason: a row that says "off"
    over a running game would be the same lie as before.
    """
    from ..services import lifecycle, world_lock

    world_lock.refuse_during_world_operation(srv.key, "Switching off")
    if not srv.container_name:
        return
    try:
        state = await docker.state(srv.container_name)
        if state.get("exists") and (state.get("running") or state.get("status") == "restarting"):
            await lifecycle.stop_server(
                srv, announce_text="The server is being switched off.", wait_timeout=75.0
            )
    except Exception as e:  # noqa: BLE001 — every failure: stays on, with the reason
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            detail=err(
                "server.disable_stop_failed",
                "The container could not be stopped ({reason}) — the server stays switched on.",
                reason=str(e)[:300],
            ),
        ) from None


def _sync_driver_config(srv: GameServer, new_secret: str | None = None) -> None:
    """Carry a changed port, address or console password into the game's own file, where the
    driver has such a seam (`sync_config_from_registry`: Minecraft's server.properties, Reforger's
    config.json): there the container env alone does not reach the game after its first start.

    `new_secret` is a password stored in THIS request's (uncommitted) transaction, which
    `resolve_secret` would not see yet; an environment variable named by the row still wins.
    """
    try:
        sync = getattr(get_driver(srv), "sync_config_from_registry", None)
    except NotImplementedError:
        return
    if not callable(sync) or not srv.data_dir:
        return
    env_value = os.environ.get(srv.rcon_password_env) if srv.rcon_password_env else None
    if new_secret is None or env_value:
        secret = servers_registry.resolve_secret(srv)
    else:
        secret = new_secret
    try:
        sync(srv, secret)
    except Exception as e:  # noqa: BLE001 — nothing is saved if the file cannot follow
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=err(
                "server.config_sync_failed",
                "The game's configuration file could not be updated ({reason}) — nothing was saved.",
                reason=str(e)[:300],
            ),
        ) from None


@router.put("/{key}")
async def update_server(
    key: str,
    body: ServerUpdate,
    session: Session = Depends(get_session),
    user: User = Depends(require_admin),
) -> dict:
    # Own fetch (NOT get_server): disabled rows must stay editable too (re-enabling).
    srv = session.exec(select(GameServer).where(GameServer.key == key)).first()
    if srv is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, detail=err("server.not_found", "Server '{key}' not found", key=key)
        )
    fields = body.model_dump(exclude_unset=True)
    # What GET delivered masked comes back unchanged: a masked name is no new name.
    if fields.get("rcon_password_env") == MASKED:
        fields.pop("rcon_password_env")
    if not fields:
        return server_public(srv)
    await validate_update(srv, fields, session)
    if fields.get("enabled") is False and srv.enabled:
        await _stop_for_disable(srv)
    for k, v in fields.items():
        if k == "rcon_password":
            # Write-only and encrypted, never into the row (`services/secrets.py`).
            server_secrets.store(session, srv.id, v or "")
        elif k == "game_ports":
            srv.game_ports = json.dumps(v)
        elif k == "game_version" and isinstance(v, str):
            setattr(srv, k, v.strip())  # trimmed: the image does not accept " 1.22.7"
        elif k == "extra":
            # Reversible: what was DELIVERED masked must not be written BACK masked —
            # otherwise an admin save would write the dots into the registry as the real value
            # (same principle as `ini_editor.restore_env_managed`).
            old = json.loads(srv.extra or "{}")
            restored = _extra_restored(v, old)
            # A join password in it goes into encrypted storage, not into the row. A masked
            # value is one GET delivered, sent back unchanged: no new password.
            found = server_secrets.split_literals(srv.game_type, restored)
            server_secrets.store_found(session, srv.id, {k: p for k, p in found.items() if p != MASKED})
            srv.extra = json.dumps(restored)
        else:
            setattr(srv, k, v)
    if "game_version" in fields:
        _check_version_required(srv)
    # `extra` too: the public address goes into Reforger's config.json (`publicAddress`).
    if {"game_ports", "rcon_port", "rcon_password", "rcon_password_env", "extra"} & set(fields):
        _sync_driver_config(srv, fields.get("rcon_password"))
    srv.updated_at = utcnow_naive()
    session.add(srv)
    session.commit()
    session.refresh(srv)
    record(session, user, "servers.update", srv.key, payload=",".join(sorted(fields)), server_id=srv.id)
    # **Whoever flips `enabled` has to reload the jobs.** Without a reload the schedule's cron
    # jobs would stay registered until the next backend start: players of a switched-off server
    # would get a warning night after night for a restart that `_do_restart` then correctly
    # skips, and recurring announcements would go on forever.
    if "enabled" in fields:
        from ..services import broadcasts as broadcast_svc

        restarts.reload_jobs()
        broadcast_svc.reload_jobs()
        fleet_clear_cache()
    return server_public(srv)


@router.get("/{key}/setup")
async def setup_state(
    key: str,
    session: Session = Depends(get_session),
    _: User = Depends(require_admin),
) -> dict:
    """What a new server still needs before it can run. Admin only.

    Own fetch, NOT `get_server`: a new row is created with `enabled=False`, and `get_server`
    answers 404 for exactly those. The container state is asked of Docker here and not guessed
    by the frontend; `exists=None` means Docker could not be asked.

    `secret` says whether the console password is set and where it comes from
    (`services/secrets.status`), never its value. A row without one would get a container with an
    empty console password.
    """
    srv = session.exec(select(GameServer).where(GameServer.key == key)).first()
    if srv is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, detail=err("server.not_found", "Server '{key}' not found", key=key)
        )
    container: dict = {"exists": False, "running": False, "status": "missing"}
    if srv.container_name:
        try:
            state = await docker.state(srv.container_name)
            container = {
                "exists": bool(state.get("exists")),
                "running": bool(state.get("running")),
                "status": state.get("status") or "",
            }
        except Exception as e:  # noqa: BLE001 — "unknown" is an answer here, not a crash
            container = {"exists": None, "running": None, "status": "", "error": str(e)}
    secret = server_secrets.status(srv, session=session)
    dashboard_managed = srv.management_mode == ManagementMode.dashboard
    # Every further secret slot of this game (Project Zomboid: the in-game admin password).
    secrets_by_slot = {
        slot: server_secrets.status(srv, slot, session=session)
        for slot in secret_slots(srv) if slot != server_secrets.RCON
    }
    return {
        # Minecraft whose EULA acceptance is not recorded yet (a row from before A2): the page
        # asks for it before it offers to create the container.
        "eula_required": provisioning.eula_missing(srv),
        "key": srv.key,
        "enabled": srv.enabled,
        "management_mode": srv.management_mode,
        "container_name": srv.container_name,
        "container": container,
        # Compose rows get their container from Compose, never from this command.
        "create_command": CREATE_CONTAINER_COMMAND.format(key=srv.key) if dashboard_managed else "",
        "secret": secret,
        "secrets": secrets_by_slot,
    }


class ProvisionIn(BaseModel):
    # Replace a STOPPED container (the edit page of a switched-off server). A running one is
    # refused; an enabled server is recreated through the version path instead.
    replace: bool = False
    # Minecraft: record the EULA acceptance on a row that has none yet (created before A2).
    eula_accepted: bool = False


@router.post("/{key}/container")
async def create_container(
    key: str,
    body: ProvisionIn,
    session: Session = Depends(get_session),
    user: User = Depends(require_admin),
) -> dict:
    """"Create container": image pull with progress, data directories, configuration, container.

    A **background operation** (`services/operations.py`): the first pull of a game image takes
    minutes, longer than a request survives behind a typical proxy. The answer is the operation;
    `GET /{key}/provision` reports its progress. Everything that can be refused without Docker is
    refused here, synchronously, so the form gets a 400 instead of a failed operation.
    Admin only; switched-off rows are served (a new server is switched off).
    """
    srv = _row_or_404(key, session)
    if body.eula_accepted and srv.game_type == GameType.minecraft and provisioning.eula_missing(srv):
        extra = json.loads(srv.extra or "{}")
        extra["eula_accepted"] = True
        srv.extra = json.dumps(extra)
        srv.updated_at = utcnow_naive()
        session.add(srv)
        session.commit()
        session.refresh(srv)
        record(session, user, "servers.eula_accepted", srv.key, server_id=srv.id)
    provisioning.check_static(srv)
    if srv.enabled and body.replace:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail=err(
                "provision.replace_enabled",
                "'{key}' is switched on: its container is recreated through the version path "
                "(with backup and boot check), not replaced here.",
                key=srv.key,
            ),
        )
    if operations.running(srv, provisioning.KIND):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail=err(
                "server.operation_running",
                "Another operation is already running for this server: {operation}.",
                operation="creating the container",
            ),
        )

    # Plain values for the background task: the ORM row belongs to this request's session.
    server_key, replace = srv.key, body.replace

    async def work(progress):
        return await provisioning.provision(server_key, replace=replace, progress=progress)

    op = operations.start(srv, provisioning.KIND, work)
    record(session, user, "servers.provision", srv.key,
           f"replace={body.replace} (operation {op['id']})", server_id=srv.id)
    return op


@router.get("/{key}/provision")
def provision_state(
    key: str,
    id: int | None = None,
    session: Session = Depends(get_session),
    _: User = Depends(require_admin),
) -> dict:
    """The "Create container" operation — by id, or the latest one (`{}` if there is none).

    Own route rather than `…/operations/{id}` (`routers/mod_config.py`): that one resolves the
    server with `get_server`, which answers 404 for the switched-off row this page is about.
    """
    srv = _row_or_404(key, session)
    if id is not None:
        op = operations.get(id, srv)
        if op is None or op.get("kind") != provisioning.KIND:
            raise HTTPException(
                status.HTTP_404_NOT_FOUND, detail=err("operation.not_found", "Operation not found")
            )
        return op
    return operations.latest(srv, provisioning.KIND) or {}


class RemoveIn(BaseModel):
    # The server key, typed by the admin: the confirmation that this is the server meant.
    confirm_key: str = Field(default="", max_length=64)
    # False: the data directory stays on disk (the world can be adopted again with the same
    # data_dir). True: it is deleted, which cannot be undone.
    delete_data: bool = False


@router.post("/{key}/remove")
async def remove_server(
    key: str,
    body: RemoveIn,
    session: Session = Depends(get_session),
    user: User = Depends(require_admin),
) -> dict:
    """Remove a switched-off server: container, on request its data, then its registry row.

    Admin only, confirmed by typing the key. Irreversible — the audit entry (a record, it stays)
    says what was removed. See `services/provisioning.remove_server` for the order and the
    refusals.
    """
    srv = _row_or_404(key, session)
    if body.confirm_key.strip() != srv.key:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail=err("server.remove_confirm",
                       "Type the server key '{key}' to confirm the removal.", key=srv.key),
        )
    sid = srv.id
    session.commit()  # end this session's transaction: the service deletes the row in its own
    result = await provisioning.remove_server(srv.key, delete_data=body.delete_data)
    record(session, user, "servers.remove", result["key"],
           payload="data deleted" if result["data_deleted"] else "data kept", server_id=sid)
    fleet_clear_cache()
    return result


class SecretIn(BaseModel):
    # Bounded like every other free text: a console password is short, and Reforger caps it at 32.
    value: str = Field(default="", max_length=256)
    # Which secret: `rcon` (console), `admin` (Project Zomboid's in-game admin password).
    # The join password is edited with the game's configuration, where players' values live.
    slot: str = server_secrets.RCON


def secret_slots(srv: GameServer) -> list[str]:
    """The secrets a server has besides the join password: the console password (unless the game
    has no console) and Project Zomboid's in-game admin password."""
    slots = [] if srv.console_kind == ConsoleKind.none else [server_secrets.RCON]
    if srv.game_type == GameType.pz:
        slots.append(server_secrets.ADMIN)
    return slots


def _row_or_404(key: str, session: Session) -> GameServer:
    """Own fetch, NOT `get_server`: these routes serve switched-off rows too."""
    srv = session.exec(select(GameServer).where(GameServer.key == key)).first()
    if srv is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, detail=err("server.not_found", "Server '{key}' not found", key=key)
        )
    return srv


@router.put("/{key}/secret")
def set_secret(
    key: str,
    body: SecretIn,
    session: Session = Depends(get_session),
    user: User = Depends(require_admin),
) -> dict:
    """Store the console password, encrypted. An empty value removes the stored one. Admin only.

    The answer is the same `secret` state as in `GET /{key}/setup` — never the value. An
    environment variable named by the row still wins over the stored value; the state says so
    (`source: env`), so nobody wonders why the new password has no effect.

    The container receives the password when it is created; an existing one has to be recreated
    for Project Zomboid and Vintage Story. Minecraft and Arma Reforger read it from a file, which
    is written right here (`_sync_driver_config`) and takes effect at the next start; Factorio
    and Avorion keep it in a file in their data directory, which wins.
    """
    srv = _row_or_404(key, session)
    if srv.management_mode == ManagementMode.compose:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail=err(
                "server.compose_managed_fields",
                "Compose-managed row: field(s) {fields} cannot be changed here",
                fields="rcon_password",
            ),
        )
    if body.slot not in secret_slots(srv):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail=err("server.secret_slot_invalid",
                       "'{slot}' is not a password this server has.", slot=body.slot[:16]),
        )
    server_secrets.store(session, srv.id, body.value, body.slot)
    if body.slot == server_secrets.RCON:
        _sync_driver_config(srv, body.value)
    srv.updated_at = utcnow_naive()
    session.add(srv)
    session.commit()
    session.refresh(srv)
    what = "set" if body.value else "cleared"
    if body.slot != server_secrets.RCON:
        what += f" ({body.slot})"
    record(session, user, "servers.secret", srv.key, payload=what, server_id=srv.id)
    return server_secrets.status(srv, body.slot, session=session)


# ------------------------------------------------------------------ server-scoped views

@router.get("/{key}")
def get_one(server: GameServer = Depends(get_server), _: User = Depends(_view)) -> dict:
    return server_public(server)


@router.get("/{key}/status")
async def scoped_status(server: GameServer = Depends(get_server), _: User = Depends(_view)) -> dict:
    return await status_payload(server)


@router.get("/{key}/overview")
async def scoped_overview(
    server: GameServer = Depends(get_server),
    session: Session = Depends(get_session),
    _: User = Depends(_view),
) -> dict:
    return await overview_payload(server, session)


@router.get("/{key}/history")
def scoped_history(
    hours: int = 24,
    server: GameServer = Depends(get_server),
    session: Session = Depends(get_session),
    _: User = Depends(_view),
) -> list[dict]:
    hours = max(1, min(hours, 24 * 14))
    cutoff = utcnow_naive() - dt.timedelta(hours=hours)
    rows = session.exec(
        select(MetricSample)
        .where(MetricSample.at >= cutoff, servers_registry.scope_clause(MetricSample.server_id, server))
        .order_by(MetricSample.at)
    ).all()
    return _bucket(list(rows), 240)


@router.get("/{key}/incidents")
def scoped_incidents(
    limit: int = 20,
    server: GameServer = Depends(get_server),
    session: Session = Depends(get_session),
    _: User = Depends(_view),
) -> list[dict]:
    limit = max(1, min(limit, 200))  # a negative value would otherwise cause a 500 on Postgres (cf. scoped_history)
    rows = session.exec(
        select(ServerIncident)
        .where(servers_registry.scope_clause(ServerIncident.server_id, server))
        .order_by(ServerIncident.id.desc())
        .limit(limit)
    ).all()
    return [{"at": r.at, "kind": r.kind, "detail": r.detail} for r in rows]


@router.get("/{key}/adopt/preflight")
async def scoped_adopt_preflight(
    server: GameServer = Depends(get_server),
    _: User = Depends(require_admin),
) -> dict:
    """Read-only diff: running (Compose) container vs. planned recreate payload.

    Admin only. Proves BEFORE the cutover (compose→dashboard) that the adoption reproduces the
    container faithfully (world binds/ports/network/secrets). Executes nothing. `ok=False` ⇒
    there are critical differences to reconcile before the flip (e.g. a missing join PASSWORD)."""
    return await adoption_preflight(server)


# --- Lifecycle: three explicit routes instead of one catch-all ---
#
# A catch-all `@router.post("/{key}/{action}")` would be a silent bug: `main.py` mounts this
# router BEFORE `server_ops`, `schedules` and `broadcasts`, and for equally good matches
# Starlette takes the route registered first. The catch-all would swallow every POST route with
# exactly one segment after `{key}`:
#
#   POST /api/servers/<key>/backups    → perform_action("backups")    → HTTP 400 (invalid action)
#   POST /api/servers/<key>/schedules  → same
#   POST /api/servers/<key>/broadcasts → same
#
# The GET variants would keep working (the catch-all is POST-only), so the lists look normal
# and only creating fails — and on top the wrong permission applies (`can_restart` instead of
# `can_manage_backups`).
#
# Deliberately NOT solved by reordering the registration: that would be a silent dependency on
# the order in `main.py` that comes back with the next refactoring. Three named routes cannot
# shadow anything by construction. `tests/test_route_dispatch.py` checks it against the real
# Starlette dispatch — for ALL routes, not just these three.

async def _lifecycle(
    action: str, force: bool, server: GameServer, session: Session, user: User
) -> dict:
    if force and user.role != Role.admin:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            detail=err("server.budget_override_admin_only", "Budget override is for admins only"),
        )
    result = await perform_action(server, action, session, user, force=force)
    # The fleet response is cached for a few seconds. Right after a click that is the wrong
    # answer — someone is watching and expects something to happen.
    fleet_clear_cache()
    return result


@router.post("/{key}/start")
async def scoped_start(
    force: bool = False,   # deliberately override the budget block (admin only)
    server: GameServer = Depends(get_server),
    session: Session = Depends(get_session),
    user: User = Depends(_restart),
) -> dict:
    return await _lifecycle("start", force, server, session, user)


@router.post("/{key}/stop")
async def scoped_stop(
    server: GameServer = Depends(get_server),
    session: Session = Depends(get_session),
    user: User = Depends(_restart),
) -> dict:
    return await _lifecycle("stop", False, server, session, user)


@router.post("/{key}/restart")
async def scoped_restart(
    server: GameServer = Depends(get_server),
    session: Session = Depends(get_session),
    user: User = Depends(_restart),
) -> dict:
    return await _lifecycle("restart", False, server, session, user)
