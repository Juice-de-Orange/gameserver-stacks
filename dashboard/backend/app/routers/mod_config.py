"""Mod configuration: edit the draft, check it, apply it — server-scoped.

The key property: **editing does not change the running server.** All write routes here only
touch the draft in the database. Only `POST …/mods/apply` transfers it — and only if the
preflight reports no blockers.

Paths deliberately have two segments under `/api/servers/{key}/mods/…` so that they cannot
collide with single-segment routes such as start/stop/restart (same rule as for the version
routes).
"""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from pydantic import BaseModel
from sqlmodel import Session

from ..auth.deps import get_server, require_flag
from ..db import get_session
from ..errors import detail_of, err
from ..models import User
from ..models.server import GameServer, GameType
from ..services.audit import record
from ..services import mod_check
from ..services import mod_portal
from ..services import operations
from ..services import world_lock
from ..services.world_lock import OPERATION_LABELS
from ..services.drivers import get_driver, running_version
from ..services.mods import apply as mod_apply
from ..services.mods import (
    avorion_apply, foreign, foreign_apply, inventory, preflight, preflight_avorion, state,
)

router = APIRouter(prefix="/api/servers", tags=["mods"])

_manage = require_flag("can_manage_mods")
_view = require_flag("can_view_status")

# Mod files are rarely large. The cap applies BEFORE the full buffer (Content-Length first,
# then block by block while writing).
#
# 100 MB: a proxy/CDN in front typically limits request bodies to about that anyway; a higher
# value would be unreachable on the public path.
# What the cap does NOT prevent: Starlette's multipart parser spools the body to /tmp before
# this code runs. Memory is protected; the disk is only protected by the proxy's body limit.
MAX_UPLOAD_BYTES = 100 * 1024 * 1024


class DraftIn(BaseModel):
    workshop_items: list[str]
    mods: list[str]
    maps: list[str]


class ApplyIn(BaseModel):
    # Confirmation that already generated world areas of removed maps may be reset.
    confirm_world_reset: bool = False


class ItemIn(BaseModel):
    workshop_id: str


class OrderIn(BaseModel):
    mods: list[str]


class MapsIn(BaseModel):
    maps: list[str]


def _require_pz(server: GameServer) -> None:
    if server.game_type != GameType.pz:
        raise HTTPException(
            status.HTTP_501_NOT_IMPLEMENTED,
            detail=err("mods.pz_only", "Guided mod management exists only for Project Zomboid so far."),
        )


def _plan(body: DraftIn) -> preflight.ModPlan:
    return preflight.ModPlan(
        workshop_items=tuple(x.strip() for x in body.workshop_items if x.strip()),
        mods=tuple(x.strip() for x in body.mods if x.strip()),
        maps=tuple(x.strip() for x in body.maps if x.strip()),
    )


@router.get("/{key}/mods/status")
async def mod_status(server: GameServer = Depends(get_server), _: User = Depends(_view)) -> dict:
    """The traffic light: is the running configuration sound? Cheap enough for constant polling.

    It covers the file-based games as well: `mods.foreign.check` and
    `mods.preflight_avorion.check` return real BLOCKERS for Factorio, Valheim and Avorion.
    `ModHealth.tsx` hides chip and card completely for `level === "none"`, so a hard-coded
    "none" for non-PZ servers would leave the question this component is there to answer ("is
    my mod configuration still working?", without opening a page first) unanswered for them.
    For Factorio that is the most expensive case — an incomplete mod set does not show up as a
    crash in the log but as players who cannot join.
    """
    # PZ has its own version of the traffic light (it additionally shows pending changes and
    # maps) — internally it runs the same preflight.
    if server.game_type == GameType.pz:
        return state.status(server)

    # For all others the result comes from `state.live_check` — the SAME function the boot
    # gate asks. A second, identical-looking branch here would let the traffic light and
    # `guard_boot` disagree about a broken mod state.
    # An unreadable mod folder arrives as the warning `MOD_FOLDER_UNREADABLE`, the same answer
    # the boot gate gets.
    result = await state.live_check(server)
    if result is None:
        return {"level": "none", "headline": "", "blockers": 0, "warnings": 0,
                "has_pending_changes": False, "pending_summary": "", "mods": 0, "maps": 0}
    if server.game_type == GameType.avorion:
        return _traffic_light(result)
    return _traffic_light(result, count=len(_active_files(server)))


def _active_files(server) -> set[str]:
    """How many mod files are active — only for the traffic light's headline."""
    try:
        return {m.filename for m in foreign.scan(server) if m.enabled}
    except OSError:
        return set()


def _traffic_light(result, *, count: int = 0) -> dict:
    """Bring a `PreflightResult` into the shape `ModHealth.tsx` expects."""
    if result.blockers:
        level, headline = "blocked", _short(result.blockers[0].message)
    elif result.warnings:
        level, headline = "warn", _short(result.warnings[0].message)
    else:
        level, headline = "ok", "Mod configuration is fine"
    return {
        "level": level, "headline": headline,
        "blockers": len(result.blockers), "warnings": len(result.warnings),
        "has_pending_changes": False, "pending_summary": "",
        "mods": count, "maps": 0,
    }


def _short(text: str, limit: int = 120) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


@router.get("/{key}/mods/draft")
def get_draft(server: GameServer = Depends(get_server), _: User = Depends(_manage)) -> dict:
    """Draft + running state + difference — everything the UI needs."""
    _require_pz(server)
    live = state.live_plan(server)
    draft = state.get_draft(server)
    changes = state.diff(live, draft)
    return {
        "live": {"workshop_items": list(live.workshop_items), "mods": list(live.mods),
                 "maps": list(live.maps)},
        "draft": {"workshop_items": list(draft.workshop_items), "mods": list(draft.mods),
                  "maps": list(draft.maps)},
        "diff": changes,
        "has_changes": state.has_changes(changes),
        "summary": state.summarize(changes),
    }


@router.put("/{key}/mods/draft")
def put_draft(
    body: DraftIn,
    server: GameServer = Depends(get_server),
    session: Session = Depends(get_session),
    user: User = Depends(_manage),
) -> dict:
    """Set the draft. Does NOT affect the running server — hence harmless."""
    _require_pz(server)
    state.set_draft(server, _plan(body))
    record(session, user, "mods.draft", server.key, server_id=server.id)
    return get_draft(server=server, _=user)


@router.post("/{key}/mods/draft/reset")
def reset_draft(
    server: GameServer = Depends(get_server),
    session: Session = Depends(get_session),
    user: User = Depends(_manage),
) -> dict:
    """Discard the draft and reset it to the running state."""
    _require_pz(server)
    state.reset_draft(server)
    record(session, user, "mods.draft.reset", server.key, server_id=server.id)
    return get_draft(server=server, _=user)


# --- Editing: affects the draft ONLY -------------------------------------------------------

@router.post("/{key}/mods/items")
async def install_item(
    body: ItemIn,
    server: GameServer = Depends(get_server),
    session: Session = Depends(get_session),
    user: User = Depends(_manage),
) -> dict:
    """Download a workshop item and add it to the draft. The server stays untouched."""
    _require_pz(server)
    from ..services import steam_download

    wid = body.workshop_id.strip()
    if not wid.isdigit():
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail=err("mods.workshop_id_not_numeric", "Workshop ID must be numeric."),
        )
    result = await steam_download.download_item(wid, server)
    if not result.get("ok"):
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            detail=err(
                "mods.download_failed", "Download failed: {error}",
                error=result.get("log", "")[-400:],
            ),
        )
    state.add_item(server, wid)
    record(session, user, "mods.item.add", server.key, wid, server_id=server.id)
    return {**get_draft(server=server, _=user), "log": result.get("log", "")}


@router.delete("/{key}/mods/items/{workshop_id}")
def remove_item(
    workshop_id: str,
    server: GameServer = Depends(get_server),
    session: Session = Depends(get_session),
    user: User = Depends(_manage),
) -> dict:
    """Take an item out of the draft. The files only move to quarantine on apply."""
    _require_pz(server)
    state.remove_item(server, workshop_id)
    record(session, user, "mods.item.remove", server.key, workshop_id, server_id=server.id)
    return get_draft(server=server, _=user)


@router.get("/{key}/mods/health")
async def mod_health(
    force: bool = False,
    server: GameServer = Depends(get_server),
    _: User = Depends(_manage),
) -> dict:
    """The Steam check: do the mods still exist, do they match the game, is there an update?

    Deliberately separate from the preflight: that is **network-free** and stays so
    (`services/mods/preflight.py`). This answer comes from the network and can therefore be
    missing — the report then says so in `summary.steam_ok` and per item in `availability`
    instead of hiding it.
    """
    _require_pz(server)
    return await mod_check.get_report(server, force=force)


@router.post("/{key}/mods/cleanup")
async def cleanup_unavailable(
    server: GameServer = Depends(get_server),
    session: Session = Depends(get_session),
    user: User = Depends(_manage),
) -> dict:
    """Take items out of the draft that are verifiably no longer available on the workshop.

    **Only what is PROVEN gone is removed** (`availability == REMOVED`: Steam reports `banned`,
    or the workshop page is an error page). A `result != 1` from the legacy web API is
    explicitly not enough — it can return the same value for an intact, widely subscribed item
    as for a really removed one, and cleaning up on that basis throws working mods out of a
    running world. The reasoning is at `mod_check.probe_workshop_page`.

    **Fail closed:** if the check was not possible, NOTHING is removed and the caller gets a
    reason — not the reassuring answer "nothing found".

    As with every draft change nothing happens to the running server: the files only move to
    quarantine on apply, with mandatory backup, boot check and a way back.
    """
    _require_pz(server)
    report = await mod_check.get_report(server, force=True)
    summary = report["summary"]

    if not summary.get("list_readable"):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail=err(
                "mods.list_unreadable",
                "The mod list is not readable — nothing was checked and nothing removed.",
            ),
        )
    if not summary.get("steam_ok"):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail=err(
                "mods.workshop_check_incomplete",
                "The workshop check was incomplete (Steam unreachable or unclear answer). "
                "Nothing was removed — an unchecked item is not a removed one.",
            ),
        )

    removed: list[str] = list(summary.get("removed") or [])
    if not removed:
        return {**get_draft(server=server, _=user), "removed_items": []}

    for wid in removed:
        state.remove_item(server, wid)
    record(session, user, "mods.cleanup", server.key, ",".join(removed), server_id=server.id)
    return {**get_draft(server=server, _=user), "removed_items": removed}


@router.put("/{key}/mods/order")
def set_order(
    body: OrderIn,
    server: GameServer = Depends(get_server),
    session: Session = Depends(get_session),
    user: User = Depends(_manage),
) -> dict:
    _require_pz(server)
    state.set_load_order(server, body.mods)
    record(session, user, "mods.order", server.key, server_id=server.id)
    return get_draft(server=server, _=user)


@router.post("/{key}/mods/order/sort")
def sort_order(
    server: GameServer = Depends(get_server),
    session: Session = Depends(get_session),
    user: User = Depends(_manage),
) -> dict:
    """Sort the load order automatically (dependencies before their dependants)."""
    _require_pz(server)
    draft = state.get_draft(server)
    state.set_load_order(server, state.sort_load_order(server, list(draft.mods)))
    record(session, user, "mods.order.sort", server.key, server_id=server.id)
    return get_draft(server=server, _=user)


@router.put("/{key}/mods/maps")
def set_maps(
    body: MapsIn,
    server: GameServer = Depends(get_server),
    session: Session = Depends(get_session),
    user: User = Depends(_manage),
) -> dict:
    """Set the map list. It is normalised: mod maps first, base map always last."""
    _require_pz(server)
    state.set_maps(server, body.maps)
    record(session, user, "mods.maps", server.key, server_id=server.id)
    return get_draft(server=server, _=user)


@router.get("/{key}/mods/available")
def available_items(server: GameServer = Depends(get_server), _: User = Depends(_manage)) -> dict:
    """What is on disk — marking what of it is active in the draft."""
    _require_pz(server)
    inv = inventory.scan(server)
    draft = state.get_draft(server)
    active = set(draft.workshop_items)
    return {
        "readable": inv.readable,
        "items": [
            {
                "workshop_id": wid,
                "mod_ids": list(item.mod_ids),
                "requires": list(item.requires),
                "tile_maps": list(item.tile_maps),
                "spawn_regions": list(item.spawn_regions),
                "builds": sorted(item.builds),
                "active": wid in active,
                "cells": {name: len(cs) for name, cs in item.map_cells.items() if cs},
            }
            for wid, item in sorted(inv.items.items())
        ],
    }


@router.get("/{key}/mods/preflight")
def check_draft(server: GameServer = Depends(get_server), _: User = Depends(_manage)) -> dict:
    """Full check of the draft — the same one "apply" uses."""
    _require_pz(server)
    from ..services import ini_editor as ini

    live = state.live_plan(server)
    draft = state.get_draft(server)
    # ONE inventory per request, shared by the check and `plan_world_reset`: `inventory.scan`
    # walks the complete workshop directory recursively, and the UI calls this endpoint after
    # EVERY draft change.
    inv = inventory.scan(server)
    result = preflight.check(
        draft, inv,
        world_dir=ini.world_save_path(server), live_maps=tuple(live.maps),
        prune_orphans=True,   # same view as "apply": it also clears away downloads no longer needed
    )
    data = result.as_dict()
    # So that the UI can say before applying HOW MUCH of the world would be affected.
    data["world_reset_files"] = len(mod_apply.plan_world_reset(server, live, draft, inv))
    return data


@router.post("/{key}/mods/apply")
async def apply_draft(
    body: ApplyIn,
    server: GameServer = Depends(get_server),
    session: Session = Depends(get_session),
    user: User = Depends(_manage),
) -> dict:
    """Apply the draft — preflight, mandatory backup, quarantine, boot check, auto-rollback.

    Runs as a **background operation**: the boot check can take minutes, and an HTTP request
    that long does not survive a typical proxy timeout (~100 s). The operation ID is returned
    immediately; `GET …/operations/{id}` reports the progress.
    """
    _require_pz(server)
    # World lock rather than a per-kind lock: `operations.running(server, "mods.apply")` only
    # sees its OWN kind. Two tabs would be enough to run a mod change and a version change over
    # the same world at the same time — both with a mandatory backup and an assumed way back,
    # both on the same files.
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
        return await mod_apply.apply_draft(
            server.key, confirm_world_reset=body.confirm_world_reset, progress=progress
        )

    op = operations.start(server, "mods.apply", work)
    record(session, user, "mods.apply", server.key,
           f"operation {op['id']} started (world_reset={body.confirm_world_reset})",
           server_id=server.id)
    return op


@router.get("/{key}/operations/{op_id}")
def operation_status(
    op_id: int, server: GameServer = Depends(get_server), _: User = Depends(_view)
) -> dict:
    """Progress of an operation — survives reloads and dropped connections."""
    op = operations.get(op_id, server)
    if op is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, detail=err("operation.not_found", "Operation not found")
        )
    return op


@router.get("/{key}/operations")
def latest_operation(
    kind: str | None = None,
    server: GameServer = Depends(get_server),
    _: User = Depends(_view),
) -> dict:
    """Latest operation — so that the UI can re-attach after a reload."""
    return operations.latest(server, kind) or {}


# --- File-based mods (Minecraft, Vintage Story, …) ------------------------------------------

class ForeignSelectionIn(BaseModel):
    enabled: list[str]


def _require_file_based(server: GameServer) -> None:
    if not foreign.supported(server):
        raise HTTPException(
            status.HTTP_501_NOT_IMPLEMENTED,
            detail=err("mods.not_file_based", "This game has no file-based mod management."),
        )


@router.get("/{key}/mods/files")
async def list_mod_files(server: GameServer = Depends(get_server), _: User = Depends(_manage)) -> dict:
    """All mod files with the metadata read from them — active and disabled ones."""
    _require_file_based(server)
    # `scan` raises when a mod folder EXISTS but cannot be listed — a silently shorter list
    # would look like "no mods". Here that becomes a meaningful 503 instead of a raw 500: the
    # page should say what is wrong, not show an empty list.
    try:
        mods = foreign.scan(server)
    except OSError as e:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=err(
                "mods.folder_unreadable_now", "The mod folder cannot be read right now: {error}",
                error=str(e),
            ),
        ) from e
    enabled = {m.filename for m in mods if m.enabled}
    result = await foreign.check(server, enabled, mods)
    return {
        "loader": foreign.expected_loader(server),
        "directory": foreign.mods_dir(server),
        "mods": [vars(m) for m in mods],
        "preflight": result.as_dict(),
        # Return the limits instead of repeating them in the frontend. Without them the page
        # would send any file of any size: the rejection only comes after Starlette's
        # multipart parser has spooled the complete body to /tmp — on THE disk that also holds
        # the game worlds. A modpack archive (200-500 MB is normal for Minecraft) would travel
        # in full just to be told "file too large".
        "upload": {
            "max_bytes": MAX_UPLOAD_BYTES,
            "extensions": list(foreign.EXTENSIONS.get(server.game_type, ())),
        },
    }


@router.post("/{key}/mods/files")
async def upload_mod_file(
    file: UploadFile = File(...),
    server: GameServer = Depends(get_server),
    session: Session = Depends(get_session),
    user: User = Depends(_manage),
) -> dict:
    """Upload a mod file. It lands **disabled** — the running server stays untouched."""
    _require_file_based(server)
    # Order: name first, then size, and only then write. A `await file.read()` BEFORE the size
    # check would put the complete file into memory before anyone could reject it.
    try:
        foreign.check_filename(server, file.filename or "")
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=detail_of(e)) from e
    if file.size is not None and file.size > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=err(
                "mods.file_too_large", "File too large (max. {max_mb} MB).",
                max_mb=MAX_UPLOAD_BYTES // 1024 // 1024,
            ),
        )

    def _chunks():
        # `file.file` is the multipart parser's SpooledTemporaryFile: from 1 MB on it lives on
        # disk, not in memory. From there block by block into the target — no full buffer.
        file.file.seek(0)
        while True:
            block = file.file.read(1 << 20)
            if not block:
                return
            yield block

    try:
        mod = await asyncio.to_thread(
            foreign.store_upload_stream, server, file.filename or "", _chunks(), MAX_UPLOAD_BYTES,
        )
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=detail_of(e)) from e
    record(session, user, "mods.file.upload", server.key, mod.filename, server_id=server.id)
    return vars(mod)


@router.post("/{key}/mods/files/check")
async def check_mod_files(
    body: ForeignSelectionIn,
    server: GameServer = Depends(get_server),
    _: User = Depends(_manage),
) -> dict:
    """Check the PLANNED selection — the same check "apply" uses.

    `GET /mods/files` runs the check over the files ACTIVE on disk. If the UI locked the apply
    button on that result alone (`preflight.ok === false`), exactly the button needed to repair
    a broken live state would be dead: deselecting would not change the lock because the check
    would not know the selection.

    `apply` itself checks the NEW selection and readily accepts a removal. This route provides
    the same answer BEFORE applying — what Project Zomboid has with `/mods/preflight` over its
    draft.
    """
    _require_file_based(server)
    enabled = {f.strip() for f in body.enabled if f.strip()}
    return (await foreign.check(server, enabled)).as_dict()


def _factorio_only(server: GameServer) -> None:
    if server.game_type != GameType.factorio:
        raise HTTPException(
            status.HTTP_501_NOT_IMPLEMENTED,
            detail=err(
                "mods.portal_factorio_only",
                "Only Factorio has a mod portal with an open API. For the other file-based "
                "games the file is uploaded.",
            ),
        )


def _portal_credentials(server: GameServer) -> tuple[str, str]:
    """Username and token from `server-settings.json` — the same source the editor uses."""
    from ..services.drivers.factorio import FactorioDriver

    data = FactorioDriver()._config(server)
    from ..services.drivers.base import json_get

    return str(json_get(data, "username") or ""), str(json_get(data, "token") or "")


@router.get("/{key}/mods/portal/search")
async def portal_search(
    q: str = "",
    server: GameServer = Depends(get_server),
    _: User = Depends(_manage),
) -> dict:
    """Search the Factorio mod portal.

    **The portal itself cannot search** — `?q=` and `?search=` are silently ignored there (the
    result is the same full list as without a parameter). Filtering therefore happens here and
    costs a download of about 13 MB per search. That is why the UI searches on button press,
    not on every keystroke.
    """
    _factorio_only(server)
    username, token = _portal_credentials(server)
    try:
        results = await mod_portal.search(q, game_version=running_version(server))
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=detail_of(e)) from e
    except mod_portal.PortalError as e:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, detail=detail_of(e)) from e
    return {
        "results": results,
        # Searching works without an account, downloading does not. That belongs in the same
        # response, otherwise someone clicks "install" and only finds out there.
        "download_possible": not mod_portal.credentials_missing(username, token),
        "download_reason": mod_portal.credentials_missing(username, token),
    }


@router.get("/{key}/mods/portal/updates")
async def portal_updates(
    server: GameServer = Depends(get_server),
    _: User = Depends(_manage),
) -> dict:
    """For which installed mods is a newer release available?

    Only queries the installed names (`namelist=`) — one request instead of a 13 MB download.
    A mod the portal does not know comes back as `unknown` instead of silently missing.
    """
    _factorio_only(server)
    try:
        mods = await asyncio.to_thread(foreign.scan, server)
    except OSError as e:
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            detail=err("mods.folder_not_readable", "Mod folder not readable ({error})", error=str(e)),
        ) from e
    installed = {m.mod_id: m.version for m in mods if m.mod_id and m.version}
    try:
        lines = await mod_portal.updates(
            installed, game_version=running_version(server),
        )
    except mod_portal.PortalError as e:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, detail=detail_of(e)) from e
    username, token = _portal_credentials(server)
    return {
        "mods": lines,
        "updates": sum(1 for z in lines if z.get("update")),
        "download_possible": not mod_portal.credentials_missing(username, token),
        "download_reason": mod_portal.credentials_missing(username, token),
    }


@router.post("/{key}/mods/portal/{name}")
async def portal_install(
    name: str,
    server: GameServer = Depends(get_server),
    session: Session = Depends(get_session),
    user: User = Depends(_manage),
) -> dict:
    """Download a mod from the portal. It lands **disabled**, like every upload.

    **Without a factorio.com account this is refused, not downloaded.** The portal redirects a
    request without credentials to the login page — the result would be an HTML page with the
    extension `.zip` in the mod folder, and the server would not come up afterwards.
    """
    _factorio_only(server)
    username, token = _portal_credentials(server)
    if (reason := mod_portal.credentials_missing(username, token)):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=reason)
    try:
        info = await mod_portal.details(name)
    except mod_portal.PortalError as e:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, detail=detail_of(e)) from e
    releases = info.get("releases") or []
    if not releases:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            detail=err("mods.portal_no_release", "“{name}” has no release in the portal.", name=name),
        )
    release = releases[-1]
    result = await mod_portal_download(server, release, username, token)
    record(session, user, "mods.portal.install", server.key,
           f"{name} {release.get('version')}", server_id=server.id)
    return result


async def mod_portal_download(server: GameServer, release: dict, username: str, token: str) -> dict:
    """Download, verify the sha1, store disabled.

    **The sha1 check is the point.** Without it every redirect, every proxy and every abort
    would be a file in the mod folder that looks like a mod. The portal supplies the checksum;
    not verifying it would give away the advantage over a file upload.
    """
    import hashlib

    import httpx

    filename = str(release.get("file_name") or "")
    expected = str(release.get("sha1") or "")
    try:
        foreign.check_filename(server, filename)
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=detail_of(e)) from e

    url = mod_portal.download_url(str(release.get("download_url") or ""),
                                  username=username, token=token)
    try:
        async with httpx.AsyncClient(timeout=180.0, follow_redirects=True) as c:
            r = await c.get(url)
            r.raise_for_status()
            raw = r.content
    except Exception as e:
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            detail=err("mods.download_failed", "Download failed: {error}", error=str(e)),
        ) from e

    if len(raw) > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=err(
                "mods.file_too_large", "File too large (max. {max_mb} MB).",
                max_mb=MAX_UPLOAD_BYTES // 1024 // 1024,
            ),
        )
    actual = hashlib.sha1(raw).hexdigest()  # noqa: S324 — the portal prescribes sha1
    if expected and actual != expected:
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            detail=err(
                "mods.checksum_mismatch",
                "The downloaded file does not match the portal's checksum "
                "({actual}… instead of {expected}…). It was not stored.",
                actual=actual[:12], expected=expected[:12],
            ),
        )

    mod = await asyncio.to_thread(
        foreign.store_upload_stream, server, filename, iter((raw,)), MAX_UPLOAD_BYTES,
    )
    return {"ok": True, "file": filename, "version": release.get("version"),
            "mod": mod, "active": False}


@router.get("/{key}/mods/files/removed")
def list_removed_mods(
    server: GameServer = Depends(get_server), _: User = Depends(_manage),
) -> list[dict]:
    """What has been put aside. "Not deleted" is only useful if one can also see it."""
    _require_file_based(server)
    try:
        return foreign.list_removed(server)
    except ValueError as e:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, detail=detail_of(e)) from e


@router.delete("/{key}/mods/files/{name}")
async def remove_mod_file(
    name: str,
    server: GameServer = Depends(get_server),
    session: Session = Depends(get_session),
    user: User = Depends(_manage),
) -> dict:
    """Take a mod file out of service — **moved, not deleted**.

    Without this route files could be uploaded and disabled but never removed, and
    `mods.disabled` would grow without bound.

    An ACTIVE file is refused (409): it would be gone while the running server still has it
    loaded. The way there is disable + apply, where preflight, mandatory backup and boot check
    apply.
    """
    _require_file_based(server)
    try:
        mods = await asyncio.to_thread(foreign.scan, server)
    except OSError as e:
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            detail=err("mods.folder_not_readable", "Mod folder not readable ({error})", error=str(e)),
        ) from e
    active = {m.filename for m in mods if m.enabled}
    try:
        result = await asyncio.to_thread(foreign.remove_file, server, name, active=active)
    except FileNotFoundError as e:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=detail_of(e)) from e
    except ValueError as e:
        # An ACTIVE file is the one refusal that is a conflict rather than a bad request. Asked
        # of the selection, not of the wording of the message.
        code = status.HTTP_409_CONFLICT if name in active else status.HTTP_400_BAD_REQUEST
        raise HTTPException(code, detail=detail_of(e)) from e
    record(session, user, "mods.file.remove", server.key, name, server_id=server.id)
    return result


@router.post("/{key}/mods/files/apply")
async def apply_mod_files(
    body: ForeignSelectionIn,
    server: GameServer = Depends(get_server),
    session: Session = Depends(get_session),
    user: User = Depends(_manage),
) -> dict:
    """Activate the selection: move the files, restart, verify the boot via RCON.

    If the server does not come up, the previous selection is restored — the same promise as
    for Project Zomboid, just without map cells.
    """
    _require_file_based(server)
    enabled = {f.strip() for f in body.enabled if f.strip()}
    result = await foreign.check(server, enabled)
    if result.blockers:
        blocked = err(
            "mods.selection_blocked", "The selection was not applied — it would damage the server."
        )
        raise HTTPException(status.HTTP_409_CONFLICT, detail={
            **blocked,
            "error": blocked["message"],
            "blockers": [vars(f) for f in result.blockers],
        })
    if operations.running(server, "mods.files.apply"):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail=err("mods.change_running", "A mod change is already running for this server."),
        )

    async def work(progress):
        return await foreign_apply.apply(server, enabled, progress=progress)

    op = operations.start(server, "mods.files.apply", work)
    record(session, user, "mods.files.apply", server.key,
           f"{len(enabled)} active (operation {op['id']})", server_id=server.id)
    return op


# --- Quarantine: view, restore, clean up ---------------------------------------------------

@router.get("/{key}/mods/quarantine")
def list_quarantine(server: GameServer = Depends(get_server), _: User = Depends(_manage)) -> list[dict]:
    """What has been put aside? World cells and mod downloads of removed maps — nothing is deleted."""
    _require_pz(server)
    return mod_apply.list_quarantines(server)


@router.post("/{key}/mods/quarantine/{name}/restore")
async def restore_quarantine(
    name: str,
    server: GameServer = Depends(get_server),
    session: Session = Depends(get_session),
    user: User = Depends(_manage),
) -> dict:
    """Bring back world data and downloads that were put aside (only with the server stopped)."""
    _require_pz(server)
    result = await mod_apply.restore_quarantine(server, name)
    record(session, user, "mods.quarantine.restore", server.key, name, server_id=server.id)
    return result


@router.delete("/{key}/mods/quarantine/{name}")
async def delete_quarantine(
    name: str,
    server: GameServer = Depends(get_server),
    session: Session = Depends(get_session),
    user: User = Depends(_manage),
) -> dict:
    """Delete for good — only here is something really gone."""
    _require_pz(server)
    result = await mod_apply.delete_quarantine(server, name)
    record(session, user, "mods.quarantine.delete", server.key, name, server_id=server.id)
    return result


@router.get("/{key}/mods/history")
def mod_history(server: GameServer = Depends(get_server), _: User = Depends(_manage)) -> list[dict]:
    _require_pz(server)
    return mod_apply.history(server)


@router.post("/{key}/mods/restore-last-good")
def restore_last_good(
    server: GameServer = Depends(get_server),
    session: Session = Depends(get_session),
    user: User = Depends(_manage),
) -> dict:
    """Fetch the last successfully applied set into the draft (applying remains a separate step).

    Deliberately no automatic activation: after an incident one should see where one is going
    back to before the server is touched.
    """
    _require_pz(server)
    plan = state.last_good(server)
    if plan is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            detail=err("mods.no_last_good", "There is no successfully applied mod set yet."),
        )
    state.set_draft(server, plan)
    record(session, user, "mods.restore_last_good", server.key, server_id=server.id)
    return get_draft(server=server, _=user)



# --- Avorion: workshop IDs instead of files --------------------------------------------------
#
# Avorion downloads its mods from Steam itself; the galaxy folder only holds `modconfig.lua`
# with a list of IDs. There is nothing to upload and nothing to move here — hence a separate,
# narrow path instead of the file-based routes above.


class AvorionModsIn(BaseModel):
    workshop_ids: list[str]
    allowed: list[str] = []


def _require_avorion(server: GameServer) -> None:
    if server.game_type != GameType.avorion:
        raise HTTPException(
            status.HTTP_501_NOT_IMPLEMENTED,
            detail=err("mods.avorion_only", "This mod management exists only for Avorion."),
        )


@router.get("/{key}/mods/workshop")
async def avorion_mods(server: GameServer = Depends(get_server), _: User = Depends(_manage)) -> dict:
    """The configured workshop IDs together with their check.

    The check runs against the LIVE state — i.e. against what would really be loaded at the
    next start. It fetches Steam metadata for that; this is the only way to tell whether an ID
    still exists at all.
    """
    _require_avorion(server)
    driver = get_driver(server)
    mods = driver.list_mods(server)
    result = await preflight_avorion.check(server)
    return {
        "workshop_ids": mods.get("workshop_ids", []),
        # **`allowed` kept separate** from `workshop_ids`. Mixed together, the mod page would
        # show the whitelist entries like installed mods and send `allowed: []` back on apply:
        # the mods that were only ALLOWED would become real mods and the access list would be
        # wiped, with nothing in the response hinting at it.
        "allowed": mods.get("allowed", []),
        "present": mods.get("present", False),
        "preflight": result.as_dict(),
    }


@router.post("/{key}/mods/workshop/preflight")
async def avorion_preflight(
    body: AvorionModsIn,
    server: GameServer = Depends(get_server),
    _: User = Depends(_manage),
) -> dict:
    """Check a DRAFT without writing anything.

    Why the route exists although there is the live check above: the live check does not know
    the planned selection. An apply button tied to a check that cannot see the new selection
    makes a broken state impossible to repair.
    """
    _require_avorion(server)
    ids = [i.strip() for i in body.workshop_ids if i.strip()]
    try:
        draft = avorion_apply.build_modconfig(ids, [a.strip() for a in body.allowed if a.strip()])
    except ValueError as e:
        # The builder validates the IDs itself. Passing everything through would let a draft
        # without a single valid ID produce an EMPTY, i.e. green result for an unchecked
        # selection.
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=detail_of(e)) from e
    result = await avorion_apply._check_draft(server, draft)
    return result.as_dict()


# No `_no_world_operation` on the decorator, on purpose: `avorion_apply.apply` takes the world
# lock ITSELF (`world_lock.hold`), just like `foreign_apply.apply` does. The rule sits at the
# choke point rather than at the caller — otherwise there are two places where it has to be,
# and one of them gets forgotten.
@router.put("/{key}/mods/workshop")
async def set_avorion_mods(
    body: AvorionModsIn,
    server: GameServer = Depends(get_server),
    session: Session = Depends(get_session),
    user: User = Depends(_manage),
) -> dict:
    """Activate the list: check, back up, write, start, verify the boot.

    If the server does not come up, the previous `modconfig.lua` is restored — the same promise
    as for the other games, just with one file instead of many.
    """
    _require_avorion(server)
    ids = [i.strip() for i in body.workshop_ids if i.strip()]
    allowed = [a.strip() for a in body.allowed if a.strip()]
    if any(not i.isdigit() for i in ids + allowed):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail=err(
                "mods.workshop_ids_numeric",
                "Workshop IDs are plain numbers — please use the ID from the address bar of "
                "the workshop page, not the title.",
            ),
        )
    if operations.running(server, "mods.files.apply"):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail=err("mods.change_running", "A mod change is already running for this server."),
        )

    async def work(progress):
        return await avorion_apply.apply(server, ids, allowed, progress=progress)

    op = operations.start(server, "mods.files.apply", work)
    record(session, user, "mods.files.apply", server.key,
           f"{len(ids)} workshop IDs (operation {op['id']})", server_id=server.id)
    return op
