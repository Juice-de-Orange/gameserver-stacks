"""Versions API: version change preserving the world + rollback + history.

Paths deliberately have **two segments** (`.../version/change`) so that they do NOT collide
with the catch-all `POST /api/servers/{key}/{action}` (start/stop/restart).
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlmodel import Session

from ..auth.deps import get_server, require_flag
from ..db import get_session
from ..errors import err
from ..models import User
from ..models.server import GameServer
from ..services import operations
from ..services import version_catalog, version_manager
from ..services import world_lock
from ..services.world_lock import OPERATION_LABELS
from ..services.audit import record
from ..services.docker_client import docker
from ..services.drivers import versioned_by_image_tag

router = APIRouter(prefix="/api/servers", tags=["versions"])

_manage = require_flag("can_manage_versions")


class VersionChangeIn(BaseModel):
    image_tag: str | None = None       # PZ: target image tag (== version)
    game_version: str | None = None    # MC/VS: target VERSION env
    server_type: str | None = None     # MC: TYPE (VANILLA/PAPER/FABRIC/...)
    confirm_downgrade: bool = False


class VersionDownloadIn(BaseModel):
    image_tag: str                     # PZ: which image to download


@router.post("/{key}/version/change")
async def change_version(
    key: str,
    body: VersionChangeIn,
    server: GameServer = Depends(get_server),
    session: Session = Depends(get_session),
    user: User = Depends(_manage),
) -> dict:
    """Start a version change as a **background operation**.

    The create-then-swap including boot verification takes up to 15 minutes — far longer than
    an HTTP request survives behind a typical proxy/CDN (~100 s). Run synchronously, the browser
    would see an error while the change keeps running correctly, and a reload would lose track
    of it.
    """
    # World lock rather than a per-kind lock: `operations.running(server, "version.change")`
    # only sees its OWN kind. Two tabs would be enough to run two world-changing operations
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

    # Preconditions SYNCHRONOUSLY, before the operation starts: otherwise the endpoint answers
    # 200 and the failure only shows up seconds later in the operations panel. For the downgrade
    # gate that would be a dead end, not just ugly — the UI expects a 400 there to ask for
    # confirmation, and without that prompt there is no way to send `confirm_downgrade` at all.
    version_manager.check_change_preconditions(
        key, image_tag=body.image_tag, game_version=body.game_version,
        server_type=body.server_type, confirm_downgrade=body.confirm_downgrade,
    )

    async def work(progress):
        return await version_manager.change_version(
            key,
            image_tag=body.image_tag,
            game_version=body.game_version,
            server_type=body.server_type,
            confirm_downgrade=body.confirm_downgrade,
            progress=progress,
        )

    op = operations.start(server, "version.change", work)
    # Destructive admin action (container recreate, possibly a world downgrade) → audit trail tied to the user.
    target_ver = body.image_tag or body.game_version or ""
    record(session, user, "version.change", server.key,
           f"{target_ver} downgrade={body.confirm_downgrade} (operation {op['id']})",
           server_id=server.id)
    return op


@router.post("/{key}/recreate")
async def recreate_container(
    key: str,
    server: GameServer = Depends(get_server),
    session: Session = Depends(get_session),
    user: User = Depends(_manage),
) -> dict:
    """RECREATE the container without changing the version.

    **Why it exists.** `ConfigReload.recreate` means: this value lives in the container's env
    and can only be set at creation. That applies to ALL Valheim fields (name, world, join
    password, publicly listed, BepInEx) and to PZ's `Public`/`Password`. Without a recreate
    path for an existing server, saving such a value would produce a false display:
    `config_fields` and `connect_info` read the same registry that `set_config` writes, so the
    config editor and the join page would show the new value as the running one while the
    container keeps running with the old one.

    It is the same code path as the version change — create-then-swap with mandatory backup,
    boot check and full fallback — and therefore a **background operation** as well.
    """
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
        return await version_manager.change_version(key, rebuild=True, progress=progress)

    # Its own kind: the version page and the edit page name it "Recreating the container", and
    # a page that tracks version changes does not pick up a recreate started elsewhere.
    op = operations.start(server, "version.recreate", work)
    record(session, user, "version.recreate", server.key,
           f"container recreate (operation {op['id']})", server_id=server.id)
    return op


@router.post("/{key}/version/rollback")
async def rollback_version(
    key: str,
    server: GameServer = Depends(get_server),
    session: Session = Depends(get_session),
    user: User = Depends(_manage),
) -> dict:
    # As a background operation, like `change_version` — it is the same code path:
    # create-then-swap including `verify_boot` with up to 900 s. Synchronously in the request a
    # proxy would cut it off after ~100 s and the browser would report an error while the swap
    # continues; there would be no operation ID a reload could attach to; and the audit entry
    # would only be written after up to 15 minutes — on failure not at all.
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
        return await version_manager.rollback(key, progress=progress)

    # Same `kind` as the regular change: the guard there and the frontend's progress polling
    # thereby cover both paths.
    op = operations.start(server, "version.change", work)
    record(session, user, "version.rollback", server.key, f"(operation {op['id']})",
           server_id=server.id)
    return op


@router.get("/{key}/version/history")
def version_history(
    key: str, server: GameServer = Depends(get_server), _: User = Depends(_manage)
) -> list[dict]:
    return version_manager.history(key)


# --- Version catalogue (feeds the dropdown, refreshed daily) -------------------------------

@router.get("/{key}/version/available")
async def available_versions(
    server: GameServer = Depends(get_server), _: User = Depends(_manage)
) -> dict:
    """Selectable versions for this server — incl. change notes and an "already downloaded" marker."""
    data = await version_catalog.entries_for(server)
    # Where the tag is the version, a change costs an image download (PZ: ~10 GB) → show
    # honestly what is already available locally. ONE call for all entries, errors are non-critical.
    if versioned_by_image_tag(server) and server.image_repo and data.get("entries"):
        local = await docker.image_tags(server.image_repo)
        for entry in data["entries"]:
            entry["installed"] = entry["ref"] in local
    return data


@router.post("/{key}/version/refresh")
async def refresh_versions(
    server: GameServer = Depends(get_server), _: User = Depends(_manage)
) -> dict:
    """"Check for updates now" — the same reconciliation the daily job runs."""
    return await version_catalog.refresh_for(server.game_type, version_catalog.variant_for(server))


# --- Image download (PZ only; runs in the background because of ~10 GB) --------------------

@router.post("/{key}/version/download")
async def start_download(
    body: VersionDownloadIn,
    server: GameServer = Depends(get_server),
    session: Session = Depends(get_session),
    user: User = Depends(_manage),
) -> dict:
    result = await version_manager.start_pull(server.key, body.image_tag)
    record(session, user, "version.download", server.key, body.image_tag, server_id=server.id)
    return result


@router.get("/{key}/version/download")
def download_status(
    image_tag: str, server: GameServer = Depends(get_server), _: User = Depends(_manage)
) -> dict:
    """Progress of the running/latest download (the frontend polls this)."""
    return version_manager.pull_status(server.key, image_tag)
