"""Server-scoped operations endpoints: backups · config · logs.

For each `{key}` these endpoints resolve a registry row + driver (`get_server`/
`get_driver_dep`) and call the game-agnostic driver seams
(`backup_spec`/`config_fields`/`set_config`) or `docker.logs` on the respective container. That
makes backups/config/logs work for every game instead of pointing at one fixed server.

For PZ, `PZDriver.backup_spec` returns exactly `<data_dir>/data` + `("Saves","Server","db")`. The PZ
config page uses the curated editor in `routers/config.py`, which is scoped as well
(`/api/servers/{key}/config/ini`). There are no global (unscoped) routers as a safety net.
"""
from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import asdict

from fastapi import APIRouter, Depends, HTTPException, Response, status
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlmodel import Session

from ..auth.deps import get_driver_dep, get_server, require_admin, require_flag
from ..auth.perm import has_permission
from ..db import get_session
from ..errors import detail_of, err, note
from ..models import User
from ..models.server import GameServer, GameType
from ..services import backup as bk
from ..services import ini_editor
from ..services import lifecycle
from ..services import operations
from ..services import world_lock
from ..services.world_lock import OPERATION_LABELS
from ..services import restore as restore_svc
from ..services import mod_catalog
from ..services.container_spec import image_ref
from ..services.audit import record
from ..services.docker_client import docker
from ..services.drivers import ConfigReload, GameDriver

log = logging.getLogger("uvicorn.error")

# Wait budget for the save-game swap. The helper copies an Enfusion `.db`, which takes a while
# with a grown save or a slow bind mount. Above all the helper must not be removed HARD when the
# time is up — SIGKILL in the middle of `cp -a` leaves a partial copy in the active directory.
# So: plenty of time, and after that stop loudly instead of killing it.
SWAP_WAIT_S = 300

router = APIRouter(prefix="/api/servers", tags=["server-ops"])

# The driver codes that mean "this is the world the server is using".
_ACTIVE_WORLD_CODES = frozenset({
    "world.factorio_active_save_not_removable",
    "world.valheim_active_world_not_removable",
})

_backups = require_flag("can_manage_backups")
_config = require_flag("can_edit_sandbox")
_logs = require_flag("can_view_logs")
_mods = require_flag("can_manage_mods")


# ------------------------------------------------------------------ Backups
@router.get("/{key}/backups")
def list_backups(
    server: GameServer = Depends(get_server),
    driver: GameDriver = Depends(get_driver_dep),
    _: User = Depends(_backups),
) -> list[dict]:
    spec = driver.backup_spec(server)
    return bk.list_backups(data_dir=spec.data_dir)


@router.post("/{key}/backups")
async def create_backup(
    server: GameServer = Depends(get_server),
    driver: GameDriver = Depends(get_driver_dep),
    session: Session = Depends(get_session),
    user: User = Depends(_backups),
) -> dict:
    """Manual backup — as a background operation, like restore and version change.

    A world of several GB takes minutes, while a proxy/CDN in front commonly cuts the request
    off after ~100 s: run synchronously, the page would report an error while the archive is
    still being written — and the obvious second click would start a second run over the same
    world.
    """
    # While a world-writing operation is running, an archive would otherwise be taken in the
    # MIDDLE of the world being unpacked — and `create_backup` silently skips missing
    # directories. The result would sit at the top of the list as the "latest backup" and
    # invite restoring a torso.
    if (holder := world_lock.held_by(server.key)):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail=err(
                "backup.operation_running",
                "Another operation is already running for this server: {operation}. "
                "A backup taken meanwhile would capture an intermediate state.",
                operation=OPERATION_LABELS.get(holder, holder),
            ),
        )
    if operations.running(server, "backup.create"):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail=err("backup.already_running", "A backup is already running for this server."),
        )
    spec = driver.backup_spec(server)

    def _pack() -> dict:
        return bk.create_backup(
            "manual",
            data_dir=spec.data_dir,
            world_subdirs=spec.world_subdirs,
            config_subdirs=spec.config_subdirs,
            pre_hook=spec.pre_hook,
            post_hook=spec.post_hook,
            server=server,
        )

    async def work(progress) -> dict:
        if spec.stop_for_consistency:
            # The game cannot be told to save (Valheim): the same stop, archive, start as the
            # weekly backup. Without it the archive held only the last autosave, or nothing
            # before the first one, and was still reported as "created".
            progress.step("Stopping the server for a consistent backup…")
            result = await bk.backup_with_stop(
                server, driver, "manual",
                announce_text="Backup — the server will be down briefly in a moment.",
            )
            if result.get("restart_error"):
                raise HTTPException(
                    status.HTTP_502_BAD_GATEWAY,
                    detail=err(
                        "backup.created_restart_failed",
                        "Backup {name} created, but the server did not start again: {reason}",
                        name=result["name"], reason=result["restart_error"][:300],
                    ),
                )
            if result["stopped"]:
                return {**result, "summary": note(
                    "backup.created_with_stop",
                    "Backup {name} created. The server was stopped for it and started again.",
                    name=result["name"],
                )}
            return {**result, "summary": note("backup.created", "Backup {name} created", name=result["name"])}
        progress.step("Backing up the world…")
        # `to_thread` is MANDATORY here, not a matter of taste: `tarfile` blocks, and the
        # backend has exactly ONE uvicorn worker. Without it the whole API — every response,
        # every WebSocket, every scheduler tick — would stand still while packing.
        result = await asyncio.to_thread(bk.create_backup_exclusive, spec.data_dir, _pack)
        return {**result, "summary": note("backup.created", "Backup {name} created", name=result["name"])}

    op = operations.start(server, "backup.create", work)
    record(session, user, "backup.create", server.key, f"(operation {op['id']})", server_id=server.id)
    return op


@router.get("/{key}/backups/{name}/download")
def download_backup(
    name: str,
    server: GameServer = Depends(get_server),
    driver: GameDriver = Depends(get_driver_dep),
    # `require_admin`, not `_backups`, for a concrete reason: the archive contains
    # `Server/<servername>.ini`, which holds `Password` and `RCONPassword` in CLEAR TEXT. The
    # image writes both there from the container env at every boot, so they end up in every
    # single backup.
    #
    # Whoever downloads the archive can read the RCON password from it — and RCON is the full
    # server console. `can_console_full` is deliberately admin-only; the download would make
    # that flag unnecessary. The restore next to it requires admin for the same reason.
    #
    # The LIST of backups stays with `can_manage_backups`: it only reveals names and sizes.
    _: User = Depends(require_admin),
) -> FileResponse:
    if not bk.valid_backup_name(name):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, detail=err("backup.name_invalid", "Invalid backup name")
        )
    spec = driver.backup_spec(server)
    path = bk.backup_path(name, data_dir=spec.data_dir)
    # Containment: the resolved path must lie inside the backup directory (symlink protection).
    real, base = os.path.realpath(path), os.path.realpath(bk.backup_dir(spec.data_dir))
    if not real.startswith(base + os.sep):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=err("backup.path_invalid", "Invalid path"))
    if not os.path.exists(path):
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, detail=err("backup.not_found", "Backup not found")
        )
    return FileResponse(path, filename=os.path.basename(path), media_type="application/gzip")


@router.get("/{key}/backups/{name}/contents")
def backup_contents(
    name: str,
    server: GameServer = Depends(get_server),
    driver: GameDriver = Depends(get_driver_dep),
    _: User = Depends(require_admin),
) -> dict:
    """What is in the archive — so that "only the configuration" can be restored selectively."""
    if not bk.valid_backup_name(name):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, detail=err("backup.name_invalid", "Invalid backup name")
        )
    spec = driver.backup_spec(server)
    return {"name": name, "parts": bk.backup_contents(name, data_dir=spec.data_dir)}


def _resolve_parts(parts: str | None, spec) -> tuple[str, ...] | None:
    """Translate `parts` from the request into archive folders. Not given: everything.

    `config` is the symbolic word for "only the configuration". Which folders that means only
    the game knows — and for several games the answer is "none". A fixed folder name sent by
    the frontend (PZ's `Server`) would not exist in archives that only contain world folders;
    the restore would inevitably fail — after having stopped the server.
    """
    if (parts or "").strip() == "config":
        if not spec.config_subdirs:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                detail=err(
                    "backup.no_config_part",
                    "For this game the archive contains only world data — a configuration cannot "
                    "be restored from it separately.",
                ),
            )
        return spec.config_subdirs
    return tuple(p.strip() for p in (parts or "").split(",") if p.strip()) or None


@router.post("/{key}/backups/{name}/restore")
async def restore_backup(
    name: str,
    parts: str | None = None,
    server: GameServer = Depends(get_server),
    driver: GameDriver = Depends(get_driver_dep),
    session: Session = Depends(get_session),
    user: User = Depends(require_admin),
) -> dict:
    """DESTRUCTIVE: restores a backup. Runs as a background operation with safeguards.

    `parts` (comma-separated, e.g. `Server`) restores only individual folders — "just repair
    the configuration, keep the world". `parts=config` leaves the choice to the driver. Without
    it the whole archive is restored.

    Safeguarded by `services/restore.py`: mandatory backup beforehand, confirmed stop, boot
    verification afterwards — so a mistake cannot overwrite the current state irretrievably.
    """
    subdirs = _resolve_parts(parts, driver.backup_spec(server))
    # World lock rather than a per-kind lock: `operations.running(server, "backup.restore")`
    # only sees its OWN kind. Two tabs would be enough to run a restore and a version change
    # over the same world at the same time — both with a mandatory backup and an assumed way
    # back, both on the same files.
    if (holder := world_lock.held_by(server.key)):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail=err(
                "server.operation_running",
                "Another operation is already running for this server: {operation}.",
                operation=OPERATION_LABELS.get(holder, holder),
            ),
        )

    async def work(progress):
        return await restore_svc.restore(server, driver, name, subdirs=subdirs, progress=progress)

    op = operations.start(server, "backup.restore", work)
    record(session, user, "backup.restore", server.key,
           f"{name} parts={subdirs or 'all'} (operation {op['id']})", server_id=server.id)
    return op


# ------------------------------------------------------------------ config (generic)
class ConfigUpdate(BaseModel):
    values: dict[str, str]


def _effective_from_fields(driver: GameDriver, server: GameServer, keys) -> dict:
    """What it takes for EXACTLY THIS change to take effect — the most expensive touched field wins.

    A single field can be more expensive than the rest of the editor: Project Zomboid re-reads
    its `.ini` at runtime (`live`), but the join password lives in the container env and needs
    a recreate. Reporting the driver default here would say "done" while the change has not
    arrived anywhere yet.
    """
    rank = {ConfigReload.live.value: 0, ConfigReload.restart.value: 1, ConfigReload.recreate.value: 2}
    kind = driver.config_reload.value
    try:
        own = {f.key: f.reload for f in driver.config_fields(server) if f.reload}
    except Exception:  # noqa: BLE001 — this information must not fail because the config cannot be read
        own = {}
    for k in keys:
        if (field := own.get(k)) and rank.get(field, 0) > rank.get(kind, 0):
            kind = field
    # No prose `note`: the config page turns `config_reload` into its own translated message
    # ("only takes effect once the container is recreated" …), and a second English sentence
    # next to it was rendered nowhere.
    return {"restart_required": kind != ConfigReload.live.value, "config_reload": kind}


def _effective_from(driver: GameDriver) -> dict:
    """What it takes for a config change to take effect — from the driver, not the game type.

    "Does this game have a live reload" is not a yes/no question. Valheim gives a third answer,
    and the most awkward one: the configuration lives in the container env, which can only be
    set at CREATION. A restart would not pick up the change — and would look as if it had.

    `restart_required` is kept as a field (the frontend reads it); `config_reload` additionally
    says what exactly is needed.
    """
    kind = driver.config_reload
    return {"restart_required": kind is not ConfigReload.live, "config_reload": kind.value}


@router.get("/{key}/config")
def get_config(
    server: GameServer = Depends(get_server),
    driver: GameDriver = Depends(get_driver_dep),
    _: User = Depends(_config),
) -> dict:
    """Generic config editor: the driver's editable fields (MC=server.properties,
    VS=serverconfig.json, PZ=all .ini keys). For PZ the frontend uses the curated editor
    (`/api/servers/{key}/config/ini`); this is the generic path for the other games."""
    fields = [asdict(f) for f in driver.config_fields(server)]
    return {"fields": fields, **_effective_from(driver)}


def _no_world_operation(server: GameServer = Depends(get_server)) -> GameServer:
    """Write dependency: 409 while a world operation is running for this server.

    The routes here do not write files in their own body but one level down — through
    `driver.set_config` (for PZ the same `.ini` as `routers/config.py`), `driver.set_mods`,
    `set_scenario`, `patch_config`. A guard that scans route bodies for direct file writes does
    not see them, so they need the dependency explicitly.

    The damage is the same: on fallback `mods/apply._restore_config` restores the complete
    `.ini` from before the operation, and applying mods takes up to 20 minutes. For Reforger
    it hits `config.json` during a mode switch, which holds the lock for up to `SWAP_WAIT_S`
    seconds.
    """
    world_lock.refuse_during_world_operation(server.key, "Saving the configuration")
    return server


@router.put("/{key}/config")
def put_config(
    body: ConfigUpdate,
    server: GameServer = Depends(_no_world_operation),
    driver: GameDriver = Depends(get_driver_dep),
    session: Session = Depends(get_session),
    user: User = Depends(_config),
) -> dict:
    # Env-managed keys (PZ: Public/Password/RCON/PublicName) are read-only — reject cleanly.
    managed = {f.key for f in driver.config_fields(server) if f.env_managed}
    blocked = [k for k in body.values if k in managed]
    if blocked:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail=err(
                "config.values_locked", "These values are locked (env-/system-managed): {keys}",
                keys=", ".join(blocked),
            ),
        )
    # Keys that hand out power rather than set options need the moderation permission. The
    # driver says which ones — for Reforger `game.admins` and `game.passwordAdmin`.
    elevated = driver.elevated_config_keys(server) & set(body.values)
    if elevated and not has_permission(user, "can_moderate"):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            detail=err(
                "config.values_need_moderation",
                "These values grant game-master rights and require the moderation permission: {keys}",
                keys=", ".join(sorted(elevated)),
            ),
        )
    # Mod keys belong to the mod workflow. Letting them through here would bypass preflight,
    # mandatory backup, world quarantine and boot check — and this endpoint is gated by
    # `can_edit_sandbox`, not `can_manage_mods`.
    if server.game_type == GameType.pz:
        mod_keys = [k for k in body.values if k in ini_editor.MOD_MANAGED]
        if mod_keys:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                detail=err(
                    "config.values_mod_managed",
                    "These values belong to mod management and are changed there (preflight, "
                    "mandatory backup, quarantine): {keys}. → Mods page or /api/servers/{server}/mods/*",
                    keys=", ".join(mod_keys), server=server.key,
                ),
            )
    try:
        driver.set_config(server, body.values)
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=detail_of(e)) from e
    record(session, user, "config.update", server.key, ",".join(body.values))
    return {"ok": True, **_effective_from_fields(driver, server, body.values.keys())}


# ------------------------------------------------------------------ Logs
@router.get("/{key}/logs")
async def get_logs(
    tail: int = 300,
    server: GameServer = Depends(get_server),
    _: User = Depends(_logs),
) -> dict:
    # Clamped like every other list route (`stats._clamp`, `audit.py`, `servers.py`).
    # `?tail=999999999` would make Docker deliver the COMPLETE log file — it would land in the
    # memory of the single worker process in full, and the demuxer would build a list of strings
    # from it on top. 5000 matches the fixed value `download_logs` uses further down.
    tail = max(1, min(tail, 5000))
    return {"logs": await docker.logs(server.container_name, tail=tail)}


@router.get("/{key}/logs/download")
async def download_logs(
    server: GameServer = Depends(get_server),
    _: User = Depends(_logs),
) -> Response:
    text = await docker.logs(server.container_name, tail=5000)
    return Response(
        content=text,
        media_type="text/plain",
        headers={"Content-Disposition": f"attachment; filename={server.key}.log"},
    )


# ------------------------------------------------------------------ mods/plugins (list, read-only)
@router.get("/{key}/mods")
def list_mods(
    server: GameServer = Depends(get_server),
    driver: GameDriver = Depends(get_driver_dep),
    _: User = Depends(_mods),
) -> dict:
    """Installed mods/plugins (game-specific structure: PZ workshop map, MC plugins/mods,
    VS mods, Reforger game.mods+scenarioId). Read-only — the full PZ workshop management lives
    in `routers/mod_config.py` (`/api/servers/{key}/mods/*`, draft-based with preflight);
    Reforger writes go through the scoped endpoints below."""
    return driver.list_mods(server)


# ---- Mod writes + curated preset catalogue (Arma Reforger: JSON mods in config.json) ----
# Reforger declares mods as the game.mods array (the server downloads them itself) → hence
# server-scoped write endpoints here. Every change needs a restart (no live reload; the image
# fetches new workshop mods at (re)start).
class ModsReplace(BaseModel):
    mods: list[dict]


class PresetApply(BaseModel):
    preset_id: str


def _require_mod_writer(driver: GameDriver) -> None:
    if not hasattr(driver, "set_mods"):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail=err("mods.management_unavailable", "Mod management is not available for this game."),
        )


def _guard_write(fn):
    """Report driver write errors (e.g. Reforger config not seeded yet) cleanly as a 400."""
    try:
        return fn()
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=detail_of(e))


async def _run_save_swap(server: GameServer, driver: GameDriver, from_mode, to_mode) -> None:
    """A short-lived ROOT helper container moves the save game between the mode slots.
    Needed because the game server writes the saves as root and the API container (uid 1000) may only read them.
    Call ONLY with the server stopped (the `.db` must not be written to)."""
    name = f"reforger-saveswap-{server.key}"
    try:
        await docker.remove(name, force=True, volumes=True)  # possible leftovers of an earlier abort
    except Exception:  # noqa: BLE001
        pass
    # `image_ref` instead of assembling it by hand: the image may be pinned by **digest**. An
    # `f"{repo}:{tag}"` would turn that into `repo:sha256:…` — a reference Docker does not know —
    # and the save/scenario switch would fail with "no such image".
    image = image_ref(server.image_repo, server.image_tag)
    payload = {
        "Image": image,
        "Entrypoint": ["sh", "-c", driver.save_swap_script()],
        "Env": [f"FROM={from_mode or ''}", f"TO={to_mode or ''}"],
        "HostConfig": {
            "Binds": [f"{server.data_dir}:/data"], "NetworkMode": "none", "AutoRemove": False,
            # Root, but only what moving files between slots needs: write into the API-owned
            # directories (DAC_OVERRIDE, FOWNER) and keep the owners on `cp -a` (CHOWN). Same
            # boundary as the cleanup helper (`provisioning.root_helper_spec`).
            "CapDrop": ["ALL"], "CapAdd": ["CHOWN", "DAC_OVERRIDE", "FOWNER"],
            "SecurityOpt": ["no-new-privileges"],
        },
    }
    await docker.create(name, payload)
    await docker.action(name, "start")
    out = ""
    # Generous budget: the helper copies an Enfusion `.db`, which is not a matter of seconds
    # when the save has grown or the bind mount responds slowly.
    still_running = True
    for _ in range(SWAP_WAIT_S):
        if not (await docker.state(name)).get("running"):
            still_running = False
            break
        await asyncio.sleep(1)
    try:
        out = await docker.logs(name, tail=20)
    except Exception:  # noqa: BLE001
        pass
    if still_running:
        # Do NOT remove it hard. `docker.remove(force=True)` sends SIGKILL — in the middle of
        # the `cp -a` of the save that leaves a partial copy in the active directory, and
        # because `SWAP_DONE` is then missing, the caller reports "server stays stopped". On
        # the next attempt `active_mode()` still points at FROM and the partial copy moves into
        # the FROM archive: the last good state is gone. Better to stop loudly.
        raise HTTPException(
            status.HTTP_504_GATEWAY_TIMEOUT,
            detail=err(
                "save.swap_timeout",
                "The save swap has been running for more than {seconds}s. The helper container "
                "'{container}' was NOT aborted so that no half copy is left behind — the server "
                "stays stopped. Please check the state of the container.",
                seconds=SWAP_WAIT_S, container=name,
            ),
        )
    try:
        # With its anonymous volumes: the game image declares several `VOLUME`s, and each
        # helper would otherwise leave one set behind per mode switch.
        await docker.remove(name, force=True, volumes=True)
    except Exception:  # noqa: BLE001
        pass
    if "SWAP_DONE" not in out:
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=err(
                "save.swap_failed", "Save swap failed (the server stays stopped): {output}",
                output=out[-200:],
            ),
        )


async def _switch_mode_with_saves(server, driver, from_mode, to_mode, apply_config) -> None:
    """Switch the mode WITHOUT losing the save: shut the server down the way the game needs →
    store the current save in the from_mode slot + load the to_mode slot → write the config → start the server.

    The stop deliberately goes through `lifecycle`, not a raw `docker stop`: a game that does
    not save on SIGTERM (see `services/lifecycle.py`) would otherwise have a save that is up to
    several minutes old copied by the swap.
    """
    # Stop, CONFIRM the stop, then back up — through the shared helper.
    #
    # A plain `lifecycle.stop_server(server)` also returns when Docker did not accept the stop
    # at all. `_run_save_swap` then moves world files (`.db`/`.save`): had Docker not stopped
    # it, the server would keep writing to `profile/profile/.db` while the root helper runs
    # `mv`/`rm -rf` on it — and the script would still report `SWAP_DONE`. Every writer of
    # world files needs a confirmed stop.
    #
    # The mandatory backup: this path moves saves between directories, and the UI promises
    # "your current save is backed up automatically".
    #
    # The world lock: this path can be triggered from two tabs in parallel like any other
    # world operation.
    async with world_lock.hold(server.key, "reforger.mode"):
        await lifecycle.stop_and_snapshot(server, driver, "pre-mode-swap")
        await _run_save_swap(server, driver, from_mode, to_mode)
        try:
            _guard_write(apply_config)
        except Exception:
            # The save has already been moved but the config has not (or only half: `_apply`
            # writes mods, scenario and the rest one after the other). `active_mode()` reads
            # the config and therefore still points at FROM — on the next click on the same
            # preset the swap would run AGAIN from FROM to TO and overwrite the FROM archive
            # with the TO state. The last good state would then only exist in the
            # pre-mode-swap backup.
            #
            # Plain "config first" does not help: then `active_mode()` = TO, `is_mode_switch`
            # is false, and the next attempt would SILENTLY skip the swap — the FROM save would
            # run under the TO scenario. So: move, and move back on failure.
            log.error("reforger[%s]: config not written after the save swap — moving the save "
                      "back (%s → %s)", server.key, to_mode, from_mode)
            try:
                await _run_save_swap(server, driver, to_mode, from_mode)
            except Exception:
                log.error("reforger[%s]: moving the save back failed — the save of '%s' is in "
                          "the slot, the config points to '%s'. The pre-mode-swap backup is "
                          "the way back.", server.key, from_mode, from_mode)
            try:
                await docker.action(server.container_name, "start")
            except Exception:
                pass
            raise
        await docker.action(server.container_name, "start")


@router.get("/{key}/mods/catalog")
def mods_catalog(
    server: GameServer = Depends(get_server),
    _: User = Depends(_mods),
) -> dict:
    """Curated, dependency-complete mod presets for this game (empty if there are none)."""
    return {"presets": mod_catalog.serialize_for(server.game_type)}


@router.put("/{key}/mods")
def replace_mods(
    body: ModsReplace,
    server: GameServer = Depends(_no_world_operation),
    driver: GameDriver = Depends(get_driver_dep),
    session: Session = Depends(get_session),
    user: User = Depends(_mods),
) -> dict:
    _require_mod_writer(driver)
    _guard_write(lambda: driver.set_mods(server, body.mods))
    record(session, user, "mods.replace", server.key, str(len(body.mods)))
    return {"ok": True, "restart_required": True}


@router.post("/{key}/mods/preset")
async def apply_preset(
    body: PresetApply,
    server: GameServer = Depends(_no_world_operation),
    driver: GameDriver = Depends(get_driver_dep),
    session: Session = Depends(get_session),
    user: User = Depends(_mods),
) -> dict:
    """Apply a preset: append its mods (deduplicated, order preserved) as one contiguous block
    and — for mode presets — set scenarioId + the settings the mode requires.

    On a real MODE switch (a different game-mode preset) the saves are separated as well: the
    current save moves into its mode slot, the target mode's save is loaded (or started fresh)
    — for that the server is briefly stopped/restarted."""
    _require_mod_writer(driver)
    preset = mod_catalog.by_id_for(server.game_type, body.preset_id)
    if preset is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, detail=err("mods.preset_unknown", "Unknown preset.")
        )
    existing = driver.list_mods(server).get("mods", [])
    preset_mods = [{"modId": m.modId, "name": m.name, "version": m.version} for m in preset.mods]
    preset_ids = {m["modId"] for m in preset_mods}
    merged = [m for m in existing if (m or {}).get("modId") not in preset_ids] + preset_mods

    def _apply() -> None:
        driver.set_mods(server, merged)
        if preset.sets_scenario and preset.scenarioId and hasattr(driver, "set_scenario"):
            driver.set_scenario(server, preset.scenarioId)
        if preset.config_patch and hasattr(driver, "patch_config"):
            driver.patch_config(server, preset.config_patch)

    from_mode = driver.active_mode(server) if hasattr(driver, "active_mode") else None
    is_mode_switch = preset.sets_scenario and hasattr(driver, "save_swap_script") and preset.id != from_mode
    if is_mode_switch and from_mode is None and getattr(driver, "has_live_save", None):
        # `preset.id != from_mode` is ALWAYS true with `None` — an unknown source mode would
        # silently slip into the switch path, where the running save has no slot it belongs
        # to. `active_mode()` returns `None` as soon as `game.scenarioId` matches no catalogue
        # preset (freely editable field) or `config.json` is unreadable.
        #
        # Rejected here rather than in the helper container: the server is not stopped yet.
        if driver.has_live_save(server):
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                detail=err(
                    "save.mode_undetermined",
                    "The currently loaded game mode cannot be determined — the scenario setting "
                    "probably holds a value that belongs to no preset. A switch could not put "
                    "the running save into any archive. Please set the scenario to a known "
                    "preset first.",
                ),
            )
    if is_mode_switch:
        await _switch_mode_with_saves(server, driver, from_mode, preset.id, _apply)
        record(session, user, "mods.preset", server.key, f"{preset.id} (mode switch, saves separated)")
        return {"ok": True, "restart_required": False, "mode_switched": True, "scenario_set": True}

    _guard_write(_apply)
    record(session, user, "mods.preset", server.key, preset.id)
    return {"ok": True, "restart_required": True, "scenario_set": preset.sets_scenario}


@router.get("/{key}/saves")
def list_saves(
    server: GameServer = Depends(get_server),
    driver: GameDriver = Depends(get_driver_dep),
    _: User = Depends(_mods),
) -> dict:
    """Mode-separated saves: which mode has a save (size/age) + the active mode."""
    if not hasattr(driver, "save_slots"):
        return {"active_mode": None, "slots": {}}
    return driver.save_slots(server)


@router.get("/{key}/worlds")
def list_worlds(
    server: GameServer = Depends(get_server),
    driver: GameDriver = Depends(get_driver_dep),
    _: User = Depends(_mods),
) -> dict:
    """Which worlds/saves exist for this server, and which one is configured?

    Factorio keeps several `saves/*.zip` side by side, Valheim several
    `worlds_local/<name>.db` — without this they are physically present but unreachable from
    the dashboard, and switching would mean changing the container env by hand.
    """
    try:
        saves = driver.world_saves(server)
    except ValueError as e:
        # Unreadable is not empty — the UI must not say "no worlds" here.
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, detail=detail_of(e)) from e
    return {
        "worlds": saves,
        # Whether a switch is possible at all is decided by the driver — not by a list in the frontend.
        "switchable": type(driver).select_world is not GameDriver.select_world,
        "removable": type(driver).remove_world is not GameDriver.remove_world,
    }


@router.post("/{key}/worlds/{name}/select")
def select_world(
    name: str,
    server: GameServer = Depends(_no_world_operation),
    driver: GameDriver = Depends(get_driver_dep),
    session: Session = Depends(get_session),
    user: User = Depends(_mods),
) -> dict:
    """Make this save the active one.

    **For both games this only takes effect with a RECREATE** — the active world depends on a
    container variable. The returned text says so; the UI shows it unchanged.
    """
    try:
        note = driver.select_world(server, name)
    except NotImplementedError as e:
        raise HTTPException(status.HTTP_501_NOT_IMPLEMENTED, detail=detail_of(e)) from e
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=detail_of(e)) from e
    record(session, user, "world.select", server.key, name, server_id=server.id)
    return {"ok": True, "note": note, "recreate_required": True}


@router.delete("/{key}/worlds/{name}")
def remove_world(
    name: str,
    server: GameServer = Depends(_no_world_operation),
    driver: GameDriver = Depends(get_driver_dep),
    session: Session = Depends(get_session),
    user: User = Depends(_mods),
) -> dict:
    """Put a save away — **moved, never deleted**. The active one is refused."""
    try:
        result = driver.remove_world(server, name)
    except NotImplementedError as e:
        raise HTTPException(status.HTTP_501_NOT_IMPLEMENTED, detail=detail_of(e)) from e
    except ValueError as e:
        # The active world is a conflict with the server's state, not a bad request. Decided by
        # the driver's code, never by the wording of its message.
        code = (status.HTTP_409_CONFLICT if getattr(e, "code", "") in _ACTIVE_WORLD_CODES
                else status.HTTP_400_BAD_REQUEST)
        raise HTTPException(code, detail=detail_of(e)) from e
    record(session, user, "world.remove", server.key, name, server_id=server.id)
    return result


@router.delete("/{key}/mods/{mod_id}")
def remove_mod(
    mod_id: str,
    server: GameServer = Depends(_no_world_operation),
    driver: GameDriver = Depends(get_driver_dep),
    session: Session = Depends(get_session),
    user: User = Depends(_mods),
) -> dict:
    _require_mod_writer(driver)
    remaining = [m for m in driver.list_mods(server).get("mods", []) if (m or {}).get("modId") != mod_id]
    _guard_write(lambda: driver.set_mods(server, remaining))
    record(session, user, "mods.remove", server.key, mod_id)
    return {"ok": True, "restart_required": True}
