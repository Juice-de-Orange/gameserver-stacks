"""Curated Project Zomboid config editor: .ini GUI + raw .ini + SandboxVars.

Server-scoped under `/api/servers/{key}/config/...`: every read and write goes to the files and
the console of the server named in the path, never to a fixed default server.

How it differs from the generic `/{key}/config` in `server_ops.py`: there the driver returns the
raw field list (MC `server.properties`, VS `serverconfig.json`, PZ all .ini keys). Here is the
**curated** PZ catalogue — grouped, typed, with help text — plus the two PZ specifics (raw
editor as an escape hatch, SandboxVars). Hence separate sub-paths instead of a collision.

Env-managed keys (Public/Password/RCON/PublicName) stay locked: the image writes them from the
container env at every boot.
"""
from __future__ import annotations

import asyncio
import math
import os
from dataclasses import asdict

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlmodel import Session

from ..auth.deps import get_driver_dep, get_server, require_flag
from ..db import get_session
from ..errors import detail_of, err
from ..models import User
from ..models.server import GameServer, GameType
from ..services import world_lock
from ..services import backup as bk
from ..services import ini_editor as ini
from ..services import sandbox as sb
from ..services.audit import record
from ..services.drivers import GameDriver, get_driver
from ..services.drivers.base import JOIN_PASSWORD

router = APIRouter(prefix="/api/servers", tags=["config"])

# Curated catalogue of editable server.ini keys (B42). Grouped, typed, with help text for the
# frontend. Env-managed keys (Public/Password/RCON/PublicName) are deliberately NOT included —
# they come from the container env.
# type ∈ "bool" | "int" | "str". The raw .ini editor remains as an escape hatch.
GUI_FIELDS: list[dict] = [
    # --- general ---
    {"key": "MaxPlayers", "label": "Max. players", "group": "General", "type": "int", "help": "Maximum number of players at the same time."},
    {"key": "PauseEmpty", "label": "Pause when empty", "group": "General", "type": "bool", "help": "Time is paused while nobody is online."},
    {"key": "Open", "label": "Open (no whitelist)", "group": "General", "type": "bool", "help": "Allow joining without an account created beforehand."},
    {"key": "GlobalChat", "label": "Global chat", "group": "General", "type": "bool", "help": "Enable the server-wide chat."},
    {"key": "PingLimit", "label": "Ping limit (ms)", "group": "General", "type": "int", "help": "Kick above this ping (0 = off). Commonly 400."},
    {"key": "MaxAccountsPerUser", "label": "Accounts per user", "group": "General", "type": "int", "help": "Max. characters/accounts per Steam user (0 = unlimited)."},
    {"key": "PublicDescription", "label": "Server description", "group": "General", "type": "str", "help": "Text in the server browser."},
    {"key": "ServerWelcomeMessage", "label": "Welcome message", "group": "General", "type": "str", "help": "Greeting when joining."},
    # --- PvP ---
    {"key": "PVP", "label": "PvP enabled", "group": "PvP", "type": "bool", "help": "Allow player-versus-player combat."},
    {"key": "SafetySystem", "label": "Safety system", "group": "PvP", "type": "bool", "help": "PvP only after both sides have switched it on (toggle)."},
    {"key": "ShowSafety", "label": "Show safety icon", "group": "PvP", "type": "bool", "help": "Skull icon above players ready for PvP."},
    {"key": "PVPMeleeDamageModifier", "label": "Melee damage ×", "group": "PvP", "type": "int", "help": "Multiplier for melee damage in PvP (default 30)."},
    {"key": "PVPFirearmDamageModifier", "label": "Firearm damage ×", "group": "PvP", "type": "int", "help": "Multiplier for firearm damage in PvP (default 50)."},
    {"key": "PVPMeleeWhileHitReaction", "label": "Melee hit reaction", "group": "PvP", "type": "bool", "help": "Hit reaction in melee combat as well."},
    # --- Safehouse ---
    {"key": "PlayerSafehouse", "label": "Player safehouses", "group": "Safehouse", "type": "bool", "help": "Players may claim safehouses."},
    {"key": "AdminSafehouse", "label": "Admin safehouses", "group": "Safehouse", "type": "bool", "help": "Only admins may create safehouses."},
    {"key": "SafehouseAllowTrepass", "label": "Allow entering", "group": "Safehouse", "type": "bool", "help": "Strangers may enter safehouses."},
    {"key": "SafehouseAllowFire", "label": "Fire in safehouses", "group": "Safehouse", "type": "bool", "help": "Fire can damage safehouses."},
    {"key": "SafehouseAllowLoot", "label": "Allow looting", "group": "Safehouse", "type": "bool", "help": "Strangers may loot in a safehouse."},
    {"key": "SafehouseAllowRespawn", "label": "Respawn in safehouse", "group": "Safehouse", "type": "bool", "help": "Members respawn in the safehouse."},
    {"key": "SafehouseDaySurvivedToClaim", "label": "Days until claim", "group": "Safehouse", "type": "int", "help": "Days survived before a safehouse can be claimed."},
    {"key": "SafeHouseRemovalTime", "label": "Expiry (hrs)", "group": "Safehouse", "type": "int", "help": "Hours of inactivity until a safehouse becomes free."},
    {"key": "DisableSafehouseWhenPlayerConnected", "label": "Protection only offline", "group": "Safehouse", "type": "bool", "help": "Safehouse protection is off while a member is online."},
    # --- world & loot ---
    {"key": "HoursForLootRespawn", "label": "Loot respawn (hrs)", "group": "World & loot", "type": "int", "help": "Hours until loot respawns (0 = off)."},
    {"key": "ConstructionPreventsLootRespawn", "label": "Buildings stop respawn", "group": "World & loot", "type": "bool", "help": "Player buildings prevent loot respawn in the cell."},
    {"key": "MinutesPerPage", "label": "Minutes per page", "group": "World & loot", "type": "int", "help": "Reading time per book page."},
    {"key": "MapRemotePlayerVisibility", "label": "Players on map", "group": "World & loot", "type": "int", "help": "1=off, 2=faction/safehouse, 3=all."},
    # --- security / anti-cheat ---
    {"key": "DoLuaChecksum", "label": "Lua checksum", "group": "Security", "type": "bool", "help": "Check mod files for tampering (kicks players whose files differ)."},
    {"key": "KickFastPlayers", "label": "Speed kick", "group": "Security", "type": "bool", "help": "Kick players with impossible speed (can be a false positive)."},
    {"key": "SteamScoreboard", "label": "Steam scoreboard", "group": "Security", "type": "bool", "help": "Show Steam names/avatars in the player list."},
    # --- convenience ---
    {"key": "AutoCreateUserInWhiteList", "label": "Auto whitelist", "group": "Convenience", "type": "bool", "help": "Add joining players to the whitelist automatically."},
    {"key": "DropOffWhiteListAfterDeath", "label": "Whitelist drop on death", "group": "Convenience", "type": "bool", "help": "Remove the account from the whitelist after death."},
    {"key": "Faction", "label": "Factions", "group": "Convenience", "type": "bool", "help": "Players may found factions."},
    {"key": "FactionDaySurvivedToCreate", "label": "Days until faction", "group": "Convenience", "type": "int", "help": "Days survived before a faction can be founded."},
    {"key": "AllowDestructionBySledgehammer", "label": "Sledgehammer demolition", "group": "Convenience", "type": "bool", "help": "Allow demolishing buildings with a sledgehammer."},
]

_FIELD_BY_KEY = {f["key"]: f for f in GUI_FIELDS}

# Curated set of conveniently editable SandboxVars (only keys present in the file are shown).
SANDBOX_FIELDS: list[dict] = [
    {"path": "Zombies", "label": "Zombie population", "type": "int", "help": "Population level (1 = very many … higher = fewer)."},
    {"path": "Distribution", "label": "Loot distribution", "type": "int", "help": "1 = city-focused (default)."},
    {"path": "DayLength", "label": "Day length", "type": "int", "help": "Day length level (higher = longer)."},
    {"path": "XpMultiplier", "label": "XP factor", "type": "float", "help": "Global XP multiplier (1.0 = normal)."},
    {"path": "ZombieLore.Speed", "label": "Zombie speed", "type": "int", "help": "1 sprinters · 2 fast shamblers · 3 shamblers · 4 slow."},
    {"path": "ZombieLore.Strength", "label": "Zombie strength", "type": "int", "help": "1 strong … 3 weak."},
    {"path": "ZombieLore.Toughness", "label": "Zombie toughness", "type": "int", "help": "1 tough … 3 weak."},
    {"path": "ZombieLore.Cognition", "label": "Zombie intelligence", "type": "int", "help": "1 navigates/opens doors … 3 dull."},
    {"path": "ZombieLore.Memory", "label": "Zombie memory", "type": "int", "help": "How long zombies keep following players."},
]
_SANDBOX_BY_PATH = {f["path"]: f for f in SANDBOX_FIELDS}

_edit = require_flag("can_edit_sandbox")


def _coerce(field: dict, raw: str) -> str:
    """Normalise a value into an .ini string matching the field type."""
    t = field["type"]
    if t == "bool":
        return "true" if str(raw).strip().lower() in ("true", "1", "yes", "on") else "false"
    if t == "int":
        try:
            return str(int(float(str(raw).strip())))
        except ValueError:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                detail=err("config.number_expected", "'{field}' expects a number", field=field["key"]),
            )
    text = str(raw)
    # Line breaks would inject additional `key=value` lines into the .ini (bypassing the
    # ENV_MANAGED/whitelist lock) — the sink guard in ini_editor catches that too, but here
    # the caller gets a meaningful 400 message.
    if "\r" in text or "\n" in text:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail=err(
                "config.line_breaks_not_allowed", "'{field}': line breaks are not allowed",
                field=field["key"],
            ),
        )
    return text


class IniUpdate(BaseModel):
    values: dict[str, str]


class PresetUpdate(BaseModel):
    """Like `IniUpdate`, but with the file state — the quick settings write a FILE.

    `sb.write_values` rewrites the file completely; without the collision check everything that
    got in since loading (from the SandboxVars field right below, from a second tab) would be
    silently lost. `revision` is therefore required, not optional — same as for `put_sandbox`,
    which writes the same file.
    """

    values: dict[str, str]
    revision: str


class RawIn(BaseModel):
    content: str
    # The file state this text is based on (from the GET). Required, not optional: this editor
    # writes the WHOLE file, and a client that may omit the state loses the protection exactly
    # when someone forgets it. To overwrite on purpose, fetch the current state with a GET —
    # one more call, but no silent loss.
    revision: str


def _pz_only(server: GameServer = Depends(get_server)) -> GameServer:
    """This router speaks Project Zomboid: the `.ini` catalogue and SandboxVars only exist there.

    Other games are not silently redirected to some PZ server but cleanly rejected — their
    configuration goes through the generic `/{key}/config`.
    """
    if server.game_type != GameType.pz:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail=err(
                "config.pz_only",
                "The curated .ini editor exists only for Project Zomboid, not for {game}.",
                game=server.game_type.value,
            ),
        )
    return server


def _no_world_operation(server: GameServer = Depends(_pz_only)) -> GameServer:
    """Write dependency: 409 while a world operation is running for this server.

    **Why a `Depends` and not a line in every route.** There are several write routes, and that
    is how this class of bug arises: three remember, one does not. The guard is therefore part
    of the signature — a new write route that uses `_pz_only` instead of this dependency is
    caught by `tests/test_config_routes_ask_the_world_lock.py`.

    The reason: on fallback `mods/apply._restore_config` restores the COMPLETE `.ini` from
    before the operation, and applying mods takes up to 20 minutes. A setting saved in that
    window would be gone without a trace afterwards — despite an audit entry.

    **GET stays open.** Reading does no harm, and the UI should be able to show the current
    settings during an operation.
    """
    world_lock.refuse_during_world_operation(server.key, "Saving the configuration")
    return server


async def _reload(server: GameServer, driver: GameDriver) -> bool:
    """`reloadoptions` on THIS server, through its driver."""
    try:
        await driver.console(server, "reloadoptions")
        return True
    except Exception:
        return False


def _file_missing(path: str) -> HTTPException:
    """404 for a config file that does not exist yet.

    A server whose configuration has not been written yet (never started, not seeded) is a state
    the page can explain — not an internal error.
    """
    return HTTPException(
        status.HTTP_404_NOT_FOUND,
        detail=err(
            "config.file_missing", "Configuration file '{file}' not found",
            file=os.path.basename(path),
        ),
    )


def _check_revision(path: str, revision: str) -> None:
    """Reject if the file has been written by someone else since it was loaded.

    Runs before the mandatory backup and before any write — a conflict should cost nothing.
    """
    try:
        current = ini.file_revision(path)
    except FileNotFoundError:
        raise _file_missing(path) from None
    if current != revision:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail=err(
                "config.file_changed_elsewhere",
                "The file has been changed elsewhere in the meantime — by the “Settings” tab, a "
                "second browser tab, a second admin, a mod apply or a version change. Your text "
                "here is based on the old state and would overwrite that change. Reload the page "
                "and enter the change again.",
            ),
        )


async def _backup(server: GameServer, driver: GameDriver, label: str) -> None:
    """Backup before every write to a file that can prevent the server from starting."""
    spec = driver.backup_spec(server)
    await asyncio.to_thread(
        bk.create_backup,
        label,
        data_dir=spec.data_dir,
        world_subdirs=spec.world_subdirs,
        config_subdirs=spec.config_subdirs,
        pre_hook=spec.pre_hook,
        post_hook=spec.post_hook,
        server=server,
    )


@router.get("/{key}/config/ini")
async def get_config(server: GameServer = Depends(_pz_only), _: User = Depends(_edit)) -> dict:
    path = ini.ini_path(server)
    try:
        data = ini.get_ini_at(path)
    except FileNotFoundError:
        raise _file_missing(path) from None
    fields = [{**f, "value": data.get(f["key"], "")} for f in GUI_FIELDS]
    # The join password comes from the DRIVER, not from the `.ini` — that only holds what the
    # image wrote into it at the last boot, and writing there would have no effect. Appended
    # rather than maintained in `GUI_FIELDS` so that there is exactly one source of truth.
    fields += [asdict(f) for f in get_driver(server).config_fields(server) if f.kind == JOIN_PASSWORD]
    # The complete .ini dict is deliberately NOT returned: it contains `RCONPassword` and
    # `Password` in clear text and would undo the curation of `GUI_FIELDS` (which leaves the
    # secrets out on purpose). The frontend only uses `fields` and `env_managed`.
    return {
        "fields": fields,
        "env_managed": sorted(ini.ENV_MANAGED),
    }


@router.put("/{key}/config/ini")
async def put_config(
    body: IniUpdate,
    server: GameServer = Depends(_no_world_operation),
    driver: GameDriver = Depends(get_driver_dep),
    session: Session = Depends(get_session),
    user: User = Depends(_edit),
) -> dict:
    # The join password is the one env-managed key that MAY arrive here — it does not go into
    # the `.ini`, though, but through the driver into the registry (see `PZDriver`).
    join_password = body.values.pop("Password", None)

    # **Validate FIRST, THEN write.** Each of the checks below leaves the function via `raise`,
    # i.e. also before the audit entry. If `set_config` ran before them, a typo in another key
    # would return 400 "nothing changed" while the join password, including `s.commit()`, was
    # already in the registry — with no audit entry — and the rest of the payload was
    # discarded: a half-applied change.
    blocked = [k for k in body.values if k in ini.ENV_MANAGED]
    if blocked:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail=err(
                "config.env_managed_keys",
                "Change env-managed keys through the Compose env, not here: {keys}",
                keys=", ".join(blocked),
            ),
        )
    unknown = [k for k in body.values if k not in _FIELD_BY_KEY]
    if unknown:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail=err(
                "config.unknown_keys", "Unknown keys (use the raw editor): {keys}",
                keys=", ".join(unknown),
            ),
        )
    coerced = {k: _coerce(_FIELD_BY_KEY[k], v) for k, v in body.values.items()}

    if join_password is not None:
        driver.set_config(server, {"Password": str(join_password)})
    if coerced:
        ini.set_ini_keys_at(ini.ini_path(server), coerced)
    changed = list(coerced) + (["Password"] if join_password is not None else [])
    record(session, user, "config.update", ini.ini_path(server), ",".join(changed), server_id=server.id)
    return {
        "ok": True,
        "reloaded": await _reload(server, driver) if coerced else False,
        # Reported separately on purpose: `reloadoptions` picks up the `.ini` values
        # immediately, the password lives in the container env and only takes effect on recreate.
        "config_reload": "recreate" if join_password is not None else "live",
    }


@router.get("/{key}/config/raw")
async def get_raw(server: GameServer = Depends(_pz_only), _: User = Depends(_edit)) -> dict:
    """The complete .ini — env-managed values masked.

    Unmasked, this would leak `RCONPassword` in clear text on every page load. The gate is
    `can_edit_sandbox` — the permission given individually to someone who maintains the config.
    Reading the RCON password with it would mean the full console of the game server as soon as
    the internal network is reachable.
    """
    path = ini.ini_path(server)
    try:
        return {
            "content": ini.mask_env_managed(ini.read_raw(path)),
            # The state belongs to the file, not to the masked text: the client sends it back
            # when saving so that `put_raw` can tell whether someone else wrote in between.
            "revision": ini.file_revision(path),
        }
    except FileNotFoundError:
        raise _file_missing(path) from None


@router.put("/{key}/config/raw")
async def put_raw(
    body: RawIn,
    server: GameServer = Depends(_no_world_operation),
    driver: GameDriver = Depends(get_driver_dep),
    session: Session = Depends(get_session),
    user: User = Depends(_edit),
) -> dict:
    if not body.content.strip():
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, detail=err("config.file_empty", "The file must not be empty")
        )
    _check_revision(ini.ini_path(server), body.revision)
    await _backup(server, driver, "pre-raw-ini")  # in case the edit prevents the start
    # Restore the masked lines from the file — otherwise saving would write the dots into the
    # .ini as the password. The four values come from the container env anyway; changing them
    # here never has an effect.
    content = ini.restore_env_managed(body.content, ini.read_raw(ini.ini_path(server)))
    ini.write_raw(ini.ini_path(server), content)
    record(session, user, "config.raw", ini.ini_path(server), server_id=server.id)
    return {"ok": True, "reloaded": await _reload(server, driver)}


@router.get("/{key}/config/sandbox")
async def get_sandbox(server: GameServer = Depends(_pz_only), _: User = Depends(_edit)) -> dict:
    path = ini.sandbox_path(server)
    try:
        return {"content": ini.read_raw(path), "revision": ini.file_revision(path)}
    except FileNotFoundError:
        raise _file_missing(path) from None


@router.put("/{key}/config/sandbox")
async def put_sandbox(
    body: RawIn,
    server: GameServer = Depends(_no_world_operation),
    driver: GameDriver = Depends(get_driver_dep),
    session: Session = Depends(get_session),
    user: User = Depends(_edit),
) -> dict:
    if not body.content.strip():
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, detail=err("config.file_empty", "The file must not be empty")
        )
    if body.content.count("{") != body.content.count("}"):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail=err("config.lua_unbalanced_braces", "Lua: unbalanced curly braces"),
        )
    # The quick settings (`put_sandbox_presets`) write the same file — and the two forms sit
    # directly below each other in the same tab.
    _check_revision(ini.sandbox_path(server), body.revision)
    await _backup(server, driver, "pre-raw-sandbox")
    ini.write_raw(ini.sandbox_path(server), body.content)
    record(session, user, "sandbox.update", ini.sandbox_path(server), server_id=server.id)
    return {"ok": True, "reloaded": await _reload(server, driver)}


@router.get("/{key}/config/sandbox-presets")
async def get_sandbox_presets(server: GameServer = Depends(_pz_only), _: User = Depends(_edit)) -> dict:
    path = ini.sandbox_path(server)
    try:
        revision = ini.file_revision(path)
    except FileNotFoundError:
        raise _file_missing(path) from None
    values = sb.read_values(server)
    fields = [{**f, "value": values[f["path"]]} for f in SANDBOX_FIELDS if f["path"] in values]
    return {"fields": fields, "revision": revision}


@router.put("/{key}/config/sandbox-presets")
async def put_sandbox_presets(
    body: PresetUpdate,
    server: GameServer = Depends(_no_world_operation),
    driver: GameDriver = Depends(get_driver_dep),
    session: Session = Depends(get_session),
    user: User = Depends(_edit),
) -> dict:
    updates: dict[str, str] = {}
    for path, raw in body.values.items():
        field = _SANDBOX_BY_PATH.get(path)
        if field is None:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                detail=err("config.unknown_sandbox_var", "Unknown SandboxVar: {path}", path=path),
            )
        try:
            number = float(raw)
        except ValueError:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                detail=err("config.number_expected", "'{field}' expects a number", field=field["label"]),
            ) from None
        # **`inf` and `nan` are valid floats — and undefined global names in Lua, i.e. `nil`.**
        # `float("1e999")` yields `inf`, which would end up literally as `XpMultiplier = inf,`
        # in the file. Catching `ValueError` is not enough; the number has to be finite.
        if not math.isfinite(number):
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                detail=err(
                    "config.number_not_finite",
                    "'{field}': {value} is not a finite number. In the Lua file it would become "
                    "an undefined name, and the server would read `nil`.",
                    field=field["label"], value=repr(raw),
                ),
            )
        updates[path] = str(int(number)) if field["type"] == "int" else str(number)

    # **Collision check and mandatory backup — as in the sibling route that writes the SAME
    # file** (`put_sandbox`); every route writing a file that can prevent the start needs both.
    _check_revision(ini.sandbox_path(server), body.revision)
    await _backup(server, driver, "pre-sandbox-presets")
    try:
        sb.write_values(updates, server)
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=detail_of(e)) from e
    record(session, user, "sandbox.preset", ini.sandbox_path(server), ",".join(updates), server_id=server.id)
    return {"ok": True, "reloaded": await _reload(server, driver)}
