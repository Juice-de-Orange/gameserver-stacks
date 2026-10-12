"""Game master: curated RCON actions (spawning/events/weather/players).

Validates every parameter by type, checks online players, audits every run.
Gated by the flag can_use_actions (admin-only by default).
"""
from __future__ import annotations

import re

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlmodel import Session

from ..auth.deps import get_driver_dep, get_server, require_flag
from ..db import get_session
from ..errors import detail_of, err
from ..models import User
from ..models.server import GameServer
from ..services import action_catalog as cat
from ..services.audit import record
from ..services.drivers import GameDriver
from ..services.rcon import RconError


_act = require_flag("can_use_actions")

_ITEM_RE = re.compile(r"^\w+\.[\w.]+$")
_COORDS_RE = re.compile(r"^-?\d+,-?\d+(,-?\d+)?$")
# Identifier-like values (perks, vehicle IDs) that go into the command unquoted.
_TOKEN_RE = re.compile(r"^[A-Za-z0-9_.+-]+$")
# Minecraft resource ID (optionally namespaced): e.g. minecraft:diamond, zombie, chest.
_MC_ID_RE = re.compile(r"^([a-z0-9_]+:)?[a-z0-9_/.]+$")


def _bad(detail: dict) -> HTTPException:
    return HTTPException(status.HTTP_400_BAD_REQUEST, detail=detail)


def _validate(spec: cat.ActionSpec, raw_params: dict, online: list[str]) -> dict[str, str]:
    built: dict[str, str] = {}
    for p in spec.params:
        raw = raw_params.get(p.name, "")
        raw = raw.strip() if isinstance(raw, str) else raw
        if raw in ("", None):
            if p.required:
                raise _bad(err("action.param_missing", "Missing parameter: {param}", param=p.label or p.name))
            built[p.name] = p.default
            continue
        value = str(raw)
        if p.kind == "player":
            if value not in online:
                raise _bad(err("action.player_not_online", "Player '{player}' is not online", player=value))
            built[p.name] = value
        elif p.kind == "item":
            if not _ITEM_RE.match(value):
                raise _bad(err("action.item_format", "Item format: Module.Item (e.g. Base.Axe)"))
            built[p.name] = value
        elif p.kind == "int":
            try:
                built[p.name] = str(int(float(value)))
            except ValueError:
                raise _bad(err(
                    "action.integer_expected", "'{param}' expects a whole number", param=p.label or p.name
                ))
        elif p.kind == "float":
            try:
                built[p.name] = str(float(value))
            except ValueError:
                raise _bad(err(
                    "action.number_expected", "'{param}' expects a number", param=p.label or p.name
                ))
        elif p.kind == "coords":
            if not _COORDS_RE.match(value):
                raise _bad(err("action.coords_format", "Coordinate format: x,y or x,y,z"))
            built[p.name] = value
        elif p.kind == "token":  # unquoted identifier (perk, vehicle ID)
            if not _TOKEN_RE.match(value):
                raise _bad(err(
                    "action.token_invalid", "'{param}': only letters, digits, . _ + - allowed",
                    param=p.label or p.name,
                ))
            built[p.name] = value
        elif p.kind in ("mc_item", "mc_entity"):  # Minecraft resource ID (namespaced)
            if not _MC_ID_RE.match(value):
                raise _bad(err(
                    "action.mc_id_invalid", "'{param}': invalid ID (e.g. minecraft:diamond)",
                    param=p.label or p.name,
                ))
            built[p.name] = value
        elif p.kind == "enum":  # only a value from the given choices
            if p.choices and value not in p.choices:
                raise _bad(err(
                    "action.choice_invalid", "'{param}': invalid choice", param=p.label or p.name
                ))
            built[p.name] = value
        elif p.kind == "toggle":
            built[p.name] = "true" if value.lower() in ("true", "1", "on", "yes") else "false"
        else:  # str (quoted) — defuse quotes/control characters/semicolons
            built[p.name] = re.sub(r"[\x00-\x1f\x7f;]", " ", value.replace('"', "'"))
    return built


class RunIn(BaseModel):
    action_id: str
    params: dict[str, str] = {}



# --- Server-scoped routes ------------------------------------------------------
# Each server gets the catalogue of its game (`catalog_for(game_type)`) and runs commands
# through `driver.console`. Every game thereby gets the same safety guarantees (whitelisted
# catalogue, typed validation, online-player check, audit).
scoped_router = APIRouter(prefix="/api/servers", tags=["actions"])


async def _driver_online_names(driver: GameDriver, server: GameServer) -> list[str]:
    try:
        res = await driver.players(server)
    except RconError:
        return []
    return list(res.get("names", []))


@scoped_router.get("/{key}/actions/catalog")
async def get_catalog_scoped(
    server: GameServer = Depends(get_server),
    _: User = Depends(_act),
) -> dict:
    return {"actions": cat.serialize_for(server.game_type)}


@scoped_router.post("/{key}/actions/run")
async def run_action_scoped(
    body: RunIn,
    server: GameServer = Depends(get_server),
    driver: GameDriver = Depends(get_driver_dep),
    session: Session = Depends(get_session),
    user: User = Depends(_act),
) -> dict:
    spec = cat.by_id_for(server.game_type, body.action_id)
    if spec is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=err("action.unknown", "Unknown action"))

    needs_players = spec.needs_online_player or any(p.kind == "player" for p in spec.params)
    online = await _driver_online_names(driver, server) if needs_players else []
    if spec.needs_online_player and not online:
        raise _bad(err("action.no_player_online", "No player online"))

    built = _validate(spec, body.params, online)
    command = spec.template.format(**built)
    try:
        output = await driver.console(server, command)
    except RconError as e:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, detail=detail_of(e))
    record(session, user, "action.run", f"action:{spec.id}", f"[{server.key}] {command}"[:1000])
    return {"ok": True, "command": command, "output": output}
