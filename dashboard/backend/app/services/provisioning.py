"""Creating and removing the container of a dashboard-managed server — for the UI and `app.cli`.

The dashboard can recreate containers (version change, rebuild) and start them, but all of these
paths assume one already exists. This module is the first step for a new server: the "Create
container" button runs it as a background operation (`routers/servers.create_container`), and
`python -m app.cli create-container <key>` calls the same function. One implementation, so the
two cannot drift apart.

**Order, and why:**

1. **Checks.** Compose-managed rows are refused (Compose creates their container). An existing
   container is never overwritten silently — it is a running game or a stopped one with a world
   next to it; replacing a STOPPED one is an explicit `replace=True` (the edit page of a switched-
   off server), a running one is refused. Minecraft is refused without the recorded EULA
   acceptance: the driver sets `EULA=TRUE` for the container, so the acceptance has to exist
   server-side, not only as a checkbox in the browser. A host port that another server or a
   running container holds is refused before the download (`services/ports.py`).
2. **Image.** Pulled through the socket proxy (`IMAGES=1`) if it is missing, with the progress of
   the download stream reported per byte. Docker answers 200 even for a failed pull, so the
   image is looked up again afterwards.
3. **Data directories.** The data directory and every bind source of the driver are created by
   the API, i.e. with the API's uid/gid. Without this Docker creates a missing bind source
   itself, owned by root — and the config editor, the backups and the mod installs of the API
   fail with "Permission denied" from then on (before A2 this happened for Minecraft, Project
   Zomboid and Vintage Story, which have no `ensure_config`).
4. **Configuration first, then the container.** `resolve_secret` alone is not enough for Avorion
   and Factorio: their `container_env` deliberately does NOT pass the secret on (the images
   have no env variable for it); it reaches the server only through a file. If that is
   missing, Avorion writes its own `server.ini` without `rconPassword` at first start — and does
   NOT start the RCON interface at all. Fail closed: if seeding fails, no container is created.
   A container without a control channel is worse than none — the latter is simply created
   again, the former has to be repaired by hand on the host.
   Then the folders a bind would hide are filled from the image (`seed_from_image`, Reforger's
   SteamCMD), so the driver can bind them.
5. **Create** with the payload of `container_spec.build_create_payload` — the SAME function the
   version change and the rebuild use. A second payload builder would be a second source of
   truth about binds, ports and memory limits. Nothing is started: the start goes through
   `lifecycle` (boot gate, watchdog muting, idle clock).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import posixpath
from collections.abc import Callable

from fastapi import HTTPException, status
from sqlmodel import Session, select

from ..errors import err
from ..models.server import GameServer, GameType, ManagementMode

log = logging.getLogger("uvicorn.error")

KIND = "server.provision"


def _noop_step(text: str, **kw) -> None:
    return None


def load(server_key: str) -> GameServer:
    """The registry row, also a switched-off one (a new server is created switched off)."""
    from .. import db

    with Session(db.engine) as s:
        server = s.exec(select(GameServer).where(GameServer.key == server_key)).first()
    if server is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            detail=err("server.not_found", "Server '{key}' not found", key=server_key),
        )
    return server


def eula_missing(server: GameServer) -> bool:
    """Minecraft without a recorded EULA acceptance (`extra.eula_accepted`)."""
    if server.game_type != GameType.minecraft:
        return False
    try:
        extra = json.loads(server.extra or "{}")
    except ValueError:
        extra = {}
    return extra.get("eula_accepted") is not True


def check_static(server: GameServer) -> None:
    """The checks that need no Docker: management mode and EULA."""
    if server.management_mode != ManagementMode.dashboard:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail=err(
                "provision.compose_managed",
                "'{key}' is managed by Docker Compose: Compose creates its container, not the dashboard.",
                key=server.key,
            ),
        )
    if eula_missing(server):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail=err(
                "provision.eula_required",
                "The Minecraft EULA has not been accepted for '{key}'. The container would be "
                "created with EULA=TRUE, so the acceptance has to be recorded first.",
                key=server.key,
            ),
        )


async def check_container(server: GameServer, *, replace: bool) -> bool:
    """Whether a container exists now. Raises if it is in the way.

    An unanswered question ("Docker not reachable") raises as well: creating over a container
    that may exist is exactly the silent overwrite this module refuses.
    """
    from .docker_client import docker

    state = await docker.state(server.container_name)
    if not state.get("exists"):
        return False
    if not replace:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail=err(
                "provision.container_exists",
                "Container '{container}' already exists ({state}). Nothing was done; to replace "
                "it, use the version change or recreate it from the edit page.",
                container=server.container_name, state=str(state.get("status") or "?"),
            ),
        )
    if state.get("running") or state.get("status") in ("running", "restarting"):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail=err(
                "provision.container_running",
                "Container '{container}' is running. Stop the server before its container is replaced.",
                container=server.container_name,
            ),
        )
    return True


async def check_ports(server: GameServer) -> None:
    """Format and conflicts of the server's host ports; Docker must answer (`strict`)."""
    from .. import db
    from . import ports

    with Session(db.engine) as s:
        await ports.check(server, s, strict=True)


def bind_sources(server: GameServer) -> list[str]:
    """The data directory plus every host side of the driver's bind mounts."""
    from .drivers import get_driver

    paths = [server.data_dir]
    for bind in get_driver(server).container_binds(server):
        host = bind.split(":", 1)[0]
        if host and host not in paths:
            paths.append(host)
    return paths


def prepare_data_dirs(server: GameServer) -> list[str]:
    """Create the data directory and the bind sources with the API's uid/gid. Returns the paths.

    Every path is checked against the allowed roots first (the same hardening as the container
    payload), so a crafted bind can never make the API create a directory elsewhere.
    """
    from .container_spec import ContainerSpecError, _check_data_dir

    created = []
    for path in bind_sources(server):
        try:
            _check_data_dir(path)
        except ContainerSpecError as e:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                detail=err("provision.data_dir_invalid", "Data directory not allowed: {reason}", reason=str(e)),
            ) from None
        try:
            os.makedirs(posixpath.normpath(path), exist_ok=True)
        except OSError as e:
            raise HTTPException(
                status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=err(
                    "provision.data_dir_failed",
                    "The directory {path} could not be created ({reason}). It must be writable for "
                    "the dashboard's user (APP_UID); nothing else was done.",
                    path=path, reason=e.strerror or str(e),
                ),
            ) from None
        created.append(path)
    return created


def _pull_sink(step: Callable[..., None]):
    """Docker reports progress per layer → one overall figure in bytes for the operation row."""
    layers: dict[str, tuple[int, int]] = {}

    def sink(frame: dict) -> None:
        layer_id = str(frame.get("id") or "")
        detail = frame.get("progressDetail") or {}
        total = detail.get("total")
        if layer_id and isinstance(total, int) and total > 0:
            layers[layer_id] = (int(detail.get("current") or 0), total)
        current = sum(c for c, _ in layers.values())
        whole = sum(t for _, t in layers.values())
        if whole:
            step("Downloading the image…", current=min(current, whole), total=whole)

    return sink


async def provision(server_key: str, *, replace: bool = False, progress=None) -> dict:
    """Create the container of a registry server. Returns `{"summary", "container", "image", …}`.

    `progress` is an `operations.Progress` (or anything with `step(text, current=, total=)`).
    Errors are `HTTPException`s with a coded detail; the operation shows them as they are.
    """
    from . import servers as servers_registry
    from .container_spec import build_create_payload, image_ref
    from .docker_client import docker
    from .drivers import get_driver

    step = progress.step if progress is not None else _noop_step
    step("Checking the server…")
    server = load(server_key)
    check_static(server)
    exists = await check_container(server, replace=replace)
    # Before the download: a port another server or a running container holds would only show
    # at the first start, after minutes of pulling (`services/ports.py`).
    step("Checking the ports…")
    await check_ports(server)

    ref = image_ref(server.image_repo, server.image_tag)
    pulled = False
    if not await docker.image_exists(ref):
        step("Downloading the image…")
        try:
            await docker.pull(ref, on_progress=_pull_sink(step))
        except Exception as e:  # noqa: BLE001 — every failure ends in the same coded answer
            # A driver can name the way out (Vintage Story: build the runner image locally).
            hint = getattr(get_driver(server), "pull_failed_detail", None)
            raise HTTPException(
                status.HTTP_502_BAD_GATEWAY,
                detail=(hint(server, ref, str(e)[:300]) if callable(hint) else None)
                or err("provision.pull_failed", "The image {image} could not be downloaded: {reason}",
                       image=ref, reason=str(e)[:300]),
            ) from None
        if not await docker.image_exists(ref):
            raise HTTPException(
                status.HTTP_502_BAD_GATEWAY,
                detail=err("provision.pull_failed", "The image {image} could not be downloaded: {reason}",
                           image=ref, reason="it is still missing after the download"),
            )
        pulled = True

    step("Creating the data directories…")
    dirs = await asyncio.to_thread(prepare_data_dirs, server)

    step("Writing the configuration…")
    secret = servers_registry.resolve_secret(server)
    seed = getattr(get_driver(server), "ensure_config", None)
    if callable(seed):
        try:
            await asyncio.to_thread(seed, server, secret)
        except Exception as e:  # noqa: BLE001 — fail closed, see the module docstring
            raise HTTPException(
                status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=err(
                    "provision.config_failed",
                    "The configuration could not be created ({reason}). No container was created — "
                    "otherwise the server would start without a control channel.",
                    reason=str(e)[:300],
                ),
            ) from None

    if callable(getattr(get_driver(server), "image_seed_dirs", None)):
        step("Copying files out of the image…")
        await seed_from_image(server, ref)
    # Folders the configuration step or the copy created, which the driver now binds.
    dirs += [p for p in bind_sources(server) if p not in dirs]

    payload = build_create_payload(server, secret)
    if exists:
        step("Removing the old container…")
        await docker.remove(server.container_name)
    step("Creating the container…")
    await docker.create(server.container_name, payload)
    log.info("provision[%s]: container %s created from %s", server.key, server.container_name, ref)
    return {
        "summary": "The container was created.",
        "container": server.container_name,
        "image": ref,
        "pulled": pulled,
        "replaced": exists,
        "directories": dirs,
    }


# ================================================================== removing a server
#
# The other end of the lifecycle: "Remove" on the edit page (`routers/servers.remove_server`)
# and `python -m app.cli remove-server`. The DATABASE part is shared; the UI additionally
# removes the container and, on request, the data directory.

# Tables that hold RECORDS: what people did. They outlive the server they are about — a
# dangling `server_id` is more honest there than a deleted row or, worse, a wrong one.
RECORDS: frozenset[str] = frozenset({"audit_log", "tickets"})

# Operational telemetry: meaningless without the server, would otherwise pile up as orphans.
# Explicitly enumerated and not derived — see `delete_server_rows`.
TELEMETRY: frozenset[str] = frozenset({
    "player_sessions", "player_events", "restart_schedules", "broadcast_schedules",
    "metric_samples", "server_incidents", "operations", "mod_sets", "mod_changes",
    "version_change", "chat_bridge_messages",
    # Not telemetry, but just as meaningless without the server: its encrypted console password.
    "server_secrets",
})


class UnclassifiedTables(RuntimeError):
    """Per-server tables that are neither in TELEMETRY nor in RECORDS — nothing is deleted."""

    def __init__(self, tables: list[str]) -> None:
        super().__init__(", ".join(tables))
        self.tables = tables


def per_server_tables() -> list[str]:
    """Every table with a `server_id` column — derived from the model, not maintained by hand.

    **Why derived and not enumerated.** A hand-written list goes stale: tables missing from it
    would be neither reported nor deleted — exactly the orphans removing a server must not leave.
    A new per-server table is included here automatically.
    """
    from sqlmodel import SQLModel

    from .. import models  # noqa: F401 — registers all tables on SQLModel.metadata
    return sorted(t.name for t in SQLModel.metadata.sorted_tables if "server_id" in t.c)


def unclassified_tables() -> list[str]:
    return [t for t in per_server_tables() if t not in TELEMETRY and t not in RECORDS]


def row_counts(server_id: int) -> dict[str, int]:
    """Rows per per-server table for this server (for the report), zero counts left out."""
    from sqlalchemy import text

    from .. import db

    counts: dict[str, int] = {}
    with db.engine.connect() as conn:
        for tbl in per_server_tables():
            n = conn.execute(
                text(f"SELECT count(*) FROM {tbl} WHERE server_id = :sid"), {"sid": server_id}
            ).scalar_one()
            if n:
                counts[tbl] = int(n)
    return counts


def delete_server_rows(server_id: int) -> None:
    """Delete the registry row and its telemetry in ONE transaction; records stay.

    **`server_id` is NOT set to NULL:** a row without a server belongs to nobody and would be
    left behind as an orphan. So telemetry is deleted and everything else is left untouched.

    **Reporting is derived, DELETING is not.** The derived list may grow — the report should
    name every per-server table, including a new one. Feeding the DELETE loop from the same
    derivation would flip the failure mode from "forgotten, rows are left behind" to "deleted
    along unnoticed": a future table with `server_id` that holds records would silently end up
    in the DELETE. So the delete list is an EXPLICIT enumeration, and an unknown table aborts
    instead of guessing.
    """
    from sqlalchemy import text

    from .. import db

    tables = per_server_tables()
    unknown = [t for t in tables if t not in TELEMETRY and t not in RECORDS]
    if unknown:
        raise UnclassifiedTables(unknown)
    to_delete = [t for t in tables if t in TELEMETRY]
    with db.engine.begin() as conn:
        for tbl in to_delete:
            conn.execute(text(f"DELETE FROM {tbl} WHERE server_id = :sid"), {"sid": server_id})
        conn.execute(text("DELETE FROM game_servers WHERE id = :sid"), {"sid": server_id})


def _data_dir_to_delete(server: GameServer) -> str:
    """The data directory, checked so that "delete data" can only ever hit this server's folder.

    Refused: an empty value, a path outside the allowed roots, a root itself, and a folder that
    another registry row uses, contains or lies in (two rows on one world, or a row whose data
    sits below another's).

    **Checked twice: as text, and as the real path on disk** (`os.path.realpath`). The text alone
    can be fooled by a symlink: `<root>/old` pointing to `/` or to another server's folder passes
    every text check. So the data directory must not itself be a symlink, its real path must lie
    inside the real path of a root, and it must not overlap another row's real path.
    """
    from .. import db
    from .container_spec import ContainerSpecError, _check_data_dir
    from ..config import settings

    path = posixpath.normpath(server.data_dir or "")
    try:
        _check_data_dir(server.data_dir)
    except ContainerSpecError as e:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail=err("provision.data_dir_invalid", "Data directory not allowed: {reason}", reason=str(e)),
        ) from None
    if any(path == posixpath.normpath(r) for r in settings.allowed_data_roots):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail=err("server.remove_data_is_root",
                       "{path} is a data root, not the folder of one server — nothing was deleted.",
                       path=path),
        )
    real = os.path.realpath(path)
    real_roots = [os.path.realpath(r) for r in settings.allowed_data_roots if r and r.strip("/")]
    if os.path.islink(path) or not any(real.startswith(r.rstrip("/") + "/") for r in real_roots):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail=err("server.remove_data_link",
                       "{path} is a symbolic link or leads outside the data roots (to {real}) — nothing "
                       "was deleted. Delete it on the host if that is really meant.",
                       path=path, real=real),
        )
    with Session(db.engine) as s:
        others = [o for o in s.exec(select(GameServer).where(GameServer.id != server.id)).all() if o.data_dir]
    for other in others:
        theirs = posixpath.normpath(other.data_dir)
        theirs_real = os.path.realpath(theirs)
        if _overlaps(path, theirs) or _overlaps(real, theirs_real):
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                detail=err("server.remove_data_shared",
                           "{path} is also used by server '{other}' — nothing was deleted. Keep the data, "
                           "or remove that server first.",
                           path=path, other=other.key),
            )
    return path


def _overlaps(a: str, b: str) -> bool:
    """The same folder, or one inside the other."""
    return a == b or a.startswith(b.rstrip("/") + "/") or b.startswith(a.rstrip("/") + "/")


def _delete_tree(path: str) -> list[str]:
    """Delete a directory tree; returns the paths that could not be removed (empty = all gone)."""
    import shutil

    failed: list[str] = []

    def _onexc(func, p, exc) -> None:  # noqa: ANN001 — shutil's callback signature
        failed.append(p)

    if os.path.lexists(path):
        shutil.rmtree(path, onexc=_onexc)
    return failed


# ================================================================== deleting as root
#
# A game that runs as root leaves files the API (APP_UID) cannot delete: Arma Reforger runs as
# root (the image has no PUID/PGID), and Vintage Story's runner downloads as root. Without help
# "Remove and delete data" stops with "N entries could not be deleted", and a restore leaves its
# `restore-old-*` copy behind. A short-lived helper container deletes them as root — with the
# server's OWN folder as its only mount, so whatever its arguments say, nothing outside that
# folder is reachable from inside. It goes through the socket proxy like every other container
# (`CONTAINERS` + `POST`, `IMAGES` only for the fallback image).

ROOT_HELPER_WAIT_S = 300
# Used only when the server's own image is no longer on the host; small, and has `find`/`rm`.
ROOT_HELPER_FALLBACK_IMAGE = "busybox:1.36"
_HELPER_MOUNT = "/x"


def root_helper_name(server: GameServer) -> str:
    return f"{server.container_name or server.key}-cleanup"


_DELETE_CAPS = ("DAC_OVERRIDE", "FOWNER")


def root_helper_spec(image: str, mount: str, argv: list[str], caps: tuple[str, ...] = _DELETE_CAPS) -> dict:
    """Create payload of the helper: root, no network, only the capabilities its job needs.

    For deleting: `DAC_OVERRIDE` lets root delete in folders that belong to another uid,
    `FOWNER` in sticky ones; everything else is dropped. `Entrypoint` replaces the image's own,
    which would start the game.
    """
    return {
        "Image": image,
        "Entrypoint": argv[:1],
        "Cmd": argv[1:],
        "User": "0:0",
        "Labels": {"stacks-console.helper": "cleanup"},
        "HostConfig": {
            "Binds": [f"{mount}:{_HELPER_MOUNT}"],
            "NetworkMode": "none",
            "CapDrop": ["ALL"],
            "CapAdd": list(caps),
            "SecurityOpt": ["no-new-privileges"],
            "AutoRemove": False,
        },
    }


async def _helper_image(server: GameServer) -> str:
    """The server's own image (already on the host, no download), else the small fallback."""
    from .container_spec import image_ref
    from .docker_client import docker

    if server.image_repo:
        own = image_ref(server.image_repo, server.image_tag)
        if await docker.image_exists(own):
            return own
    if not await docker.image_exists(ROOT_HELPER_FALLBACK_IMAGE):
        await docker.pull(ROOT_HELPER_FALLBACK_IMAGE)
    return ROOT_HELPER_FALLBACK_IMAGE


async def _run_root_helper(server: GameServer, mount: str, argv: list[str],
                           image: str | None = None, caps: tuple[str, ...] = _DELETE_CAPS) -> int | None:
    """Run the helper once with `mount` at `/x`. Returns its exit code, `None` when it timed out.

    `image` defaults to `_helper_image`; seeding passes the server's image, whose files it copies.

    Removed afterwards together with its anonymous volumes: an image that declares `VOLUME`s
    (the Reforger image declares four) gets one for each of them in the helper as well.
    """
    from .docker_client import docker

    name = root_helper_name(server)
    try:
        await docker.remove(name, force=True, volumes=True)  # leftover of an aborted run
    except Exception:  # noqa: BLE001
        pass
    await docker.create(name, root_helper_spec(image or await _helper_image(server), mount, argv, caps))
    try:
        await docker.action(name, "start")
        for _ in range(ROOT_HELPER_WAIT_S):
            if not (await docker.state(name)).get("running"):
                info = await docker.inspect(name) or {}
                return int((info.get("State") or {}).get("ExitCode", -1))
            await asyncio.sleep(1)
        return None
    finally:
        try:
            await docker.remove(name, force=True, volumes=True)
        except Exception:  # noqa: BLE001 — the next run removes a leftover
            log.warning("cleanup helper %s could not be removed", name, exc_info=True)


async def delete_tree_as_root(server: GameServer, path: str) -> list[str]:
    """Delete `path` (the CHECKED data directory, `_data_dir_to_delete`); returns what is left.

    The API tries first: most games write as the API's uid, and then no container is needed.
    What it cannot delete, the helper deletes as root; the API then removes the emptied folder
    itself (its parent, the data root, belongs to the API).
    """
    failed = await asyncio.to_thread(_delete_tree, path)
    if not failed:
        return []
    log.info("remove: %d entries under %s belong to another user — deleting them as root",
             len(failed), path)
    try:
        code = await _run_root_helper(
            server, os.path.realpath(path), ["find", _HELPER_MOUNT, "-mindepth", "1", "-delete"],
        )
        if code != 0:
            log.warning("remove: the cleanup helper for %s ended with %s", path, code)
    except Exception:  # noqa: BLE001 — what is left is reported by the caller
        log.warning("remove: the cleanup helper for %s failed", path, exc_info=True)
    return await asyncio.to_thread(_delete_tree, path)


def _is_empty_dir(path: str) -> bool:
    try:
        return not os.listdir(path)
    except FileNotFoundError:
        return True


async def seed_from_image(server: GameServer, image: str) -> list[str]:
    """Copy what the driver names (`image_seed_dirs`) out of the image into empty host folders.

    A bind hides whatever the image has at that path; only a named volume is filled from the
    image. Reforger's launcher, for instance, calls `/steamcmd/steamcmd.sh` from the image. So
    the folder is filled once, here, before the container is created; the driver binds it only
    once it holds the files. Best effort: without the copy the container is created without
    that bind, as before. Returns the host folders that were filled.

    `cp -a`, which needs `CHOWN`: the copy must belong to whom the image's folder belongs,
    the folder itself included. Measured with Reforger: SteamCMD, although run as root, writes
    as the owner of its folder, and a folder of the API's uid made its self-update fail
    ("Failed to apply update … Failed to load steamconsole.so"), so the game was never fetched.
    """
    from .container_spec import _check_data_dir
    from .drivers import get_driver

    seeds = getattr(get_driver(server), "image_seed_dirs", None)
    filled: list[str] = []
    for src, host in (seeds(server) if callable(seeds) else []):
        try:
            _check_data_dir(host)
            await asyncio.to_thread(os.makedirs, host, exist_ok=True)
            if not await asyncio.to_thread(_is_empty_dir, host):
                continue
            code = await _run_root_helper(
                server, host, ["cp", "-a", posixpath.join(src, "."), _HELPER_MOUNT + "/"], image=image,
                caps=("CHOWN", "DAC_OVERRIDE", "FOWNER"),
            )
            if code == 0:
                filled.append(host)
            else:
                log.warning("provision[%s]: copying %s out of the image ended with %s", server.key, src, code)
        except Exception:  # noqa: BLE001 — best effort, see above
            log.warning("provision[%s]: could not copy %s out of the image", server.key, src, exc_info=True)
    return filled


def _leftover_relpaths(server: GameServer, paths: list[str]) -> tuple[str, list[str]]:
    """(real data directory, the leftovers relative to it). Raises on anything that is not a
    `restore-old-*` copy strictly inside this server's own folder."""
    from .backup import _ASIDE_SUFFIX
    from .container_spec import _check_data_dir

    _check_data_dir(server.data_dir)
    base = os.path.realpath(server.data_dir)
    rel: list[str] = []
    for p in paths:
        norm = posixpath.normpath(p)
        real = os.path.realpath(norm)
        if (os.path.islink(norm) or _ASIDE_SUFFIX not in posixpath.basename(norm)
                or not real.startswith(base.rstrip("/") + "/")):
            raise ValueError(f"not a restore-old copy inside {base}: {p}")
        rel.append(posixpath.relpath(real, base))
    return base, rel


async def remove_leftovers_as_root(server: GameServer, paths: list[str]) -> list[str]:
    """Delete `restore-old-*` copies the API could not delete (`backup.extract_backup`).

    Returns the paths that still exist; the caller names them. Best effort: a refused path or a
    failed helper leaves the list as it was.
    """
    if not paths:
        return []
    try:
        base, rel = _leftover_relpaths(server, paths)
        code = await _run_root_helper(
            server, base, ["rm", "-rf", "--", *(posixpath.join(_HELPER_MOUNT, r) for r in rel)],
        )
        if code != 0:
            log.warning("restore: the cleanup helper for %s ended with %s", server.key, code)
    except Exception:  # noqa: BLE001 — the restore succeeded; the caller reports what is left
        log.warning("restore: leftovers of %s could not be deleted as root", server.key, exc_info=True)
    return [p for p in paths if os.path.lexists(p)]


async def remove_server(server_key: str, *, delete_data: bool) -> dict:
    """Remove a SWITCHED-OFF server: its container, on request its data, then its rows.

    Order: container → data → database. A failure stops right there and leaves the row, so the
    admin sees the server again and can repeat the step; the database goes last because it is
    the only way back to the container name and the data path.

    Refused: an enabled server (switch it off first — that stops it), and "delete data" for a
    compose-managed row (its folder belongs to the Compose block). A compose row's container is
    left alone as well: Compose created it.
    """
    from .docker_client import docker

    server = load(server_key)
    if server.enabled:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail=err("server.remove_enabled",
                       "'{key}' is switched on. Switch it off first (that stops its container), then remove it.",
                       key=server.key),
        )
    if unclassified_tables():
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=err("server.remove_unclassified",
                       "Per-server tables that are neither telemetry nor records: {tables}. Nothing "
                       "was removed.",
                       tables=", ".join(unclassified_tables())),
        )
    compose = server.management_mode == ManagementMode.compose
    if compose and delete_data:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail=err("server.remove_compose_data",
                       "'{key}' is managed by Docker Compose: its data belongs to the Compose block and "
                       "is not deleted from here.",
                       key=server.key),
        )
    data_dir = _data_dir_to_delete(server) if delete_data and server.data_dir else ""

    container_removed = False
    if not compose and server.container_name:
        try:
            state = await docker.state(server.container_name)
            if state.get("exists"):
                if state.get("running") or state.get("status") == "restarting":
                    from . import lifecycle

                    # Confirmed, not just requested: the data directory may be deleted next.
                    _, stopped = await lifecycle.stop_and_confirm(server)
                    if not stopped:
                        raise RuntimeError("the container did not stop")
                # With its ANONYMOUS volumes (a `VOLUME` of the image that no bind covers):
                # nothing else refers to them once the server is gone. Named volumes and
                # binds are never touched by this.
                await docker.remove(server.container_name, volumes=True)
                container_removed = True
        except Exception as e:  # noqa: BLE001 — every failure: nothing else is touched
            raise HTTPException(
                status.HTTP_502_BAD_GATEWAY,
                detail=err("server.remove_container_failed",
                           "The container could not be removed ({reason}) — nothing else was changed.",
                           reason=str(e)[:300]),
            ) from None

    if data_dir:
        failed = await delete_tree_as_root(server, data_dir)
        if failed:
            raise HTTPException(
                status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=err("server.remove_data_failed",
                           "{count} entries under {path} could not be deleted, not even by the cleanup "
                           "container (first: {first}). The container is gone, the server is still "
                           "listed: delete the rest on the host, then remove it again.",
                           count=len(failed), path=data_dir, first=failed[0]),
            )

    counts = row_counts(server.id)
    delete_server_rows(server.id)
    log.info("remove[%s]: removed (container=%s, data=%s)", server.key, container_removed,
             data_dir or "kept")
    return {
        "key": server.key,
        "container_removed": container_removed,
        "data_deleted": bool(data_dir),
        "data_dir": server.data_dir,
        "rows_deleted": {t: n for t, n in counts.items() if t in TELEMETRY},
        "rows_kept": {t: n for t, n in counts.items() if t in RECORDS},
    }
