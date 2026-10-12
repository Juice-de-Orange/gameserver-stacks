"""Version change with world preservation and rollback (multi-game).

**create-then-swap** with fixed ports/container names: the old and the new container can never
run at the same time → the old one is STOPPED and renamed aside (`{name}-prev`), the new one is
created, its boot is verified and only then is the old one removed. If anything fails,
everything is rolled back (old container + world backup). **Mandatory snapshot before every
change.**

- PZ, Factorio, Avorion: version == `image_tag` (image change; the target image must be
  available locally, it is pulled first).
- MC/VS: version == `VERSION` env (`game_version`) [+ MC `TYPE`=`server_type`]; the image stays
  and the container downloads the version at boot.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
import re
import shutil
from typing import TYPE_CHECKING

from fastapi import HTTPException, status
from sqlmodel import Session, select

from ..db import engine
from ..errors import err, note
from ..models import VersionChange
from ..models.server import ConsoleKind, GameServer, ManagementMode
from ..services import lifecycle
from ..services import servers as servers_registry
from ..services import watchdog
from ..services import world_lock
from ..services.mods import state as mod_state
from ..services.backup import extract_backup, check_restorable
from ..services.container_spec import build_create_payload, image_ref
from ..services.docker_client import docker, start_time as docker_start_time
from ..services.drivers import get_driver, versioned_by_image_tag

if TYPE_CHECKING:
    from ..services.drivers import GameDriver

log = logging.getLogger("uvicorn.error")

_PREV_SUFFIX = "-prev"


# --------------------------------------------------------------- Version heuristics (pure)

def parse_ver(s: str) -> tuple:
    """Extract the leading number groups: '42.19.0-unstable' → (42, 19, 0).

    Public because `version_catalog` sorts the version catalog with it — a second parser would
    be where sorting and the downgrade gate drift apart.
    """
    return tuple(int(n) for n in re.findall(r"\d+", s or "")[:4])


def _direction(old: str, new: str) -> str:
    """'up' | 'down' | 'same' | 'unknown' (unparsable versions → 'unknown', NOT a downgrade)."""
    if old == new:
        return "same"
    a, b = parse_ver(old), parse_ver(new)
    if not a or not b:
        return "unknown"
    return "down" if b < a else "up" if b > a else "unknown"


# --------------------------------------------------------------- Registry/history helpers

def _load(server_key: str) -> GameServer:
    srv = servers_registry.get_by_key(server_key)
    if srv is None or not srv.enabled:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=err("version.server_not_found", "Server '{key}' not found", key=server_key))
    return srv


# The six fields that make up a version state. The rollback resets ALL of them.
_VERSION_FIELDS = (
    "image_tag", "game_version", "server_type",
    "previous_image_tag", "previous_game_version", "previous_type",
)


def _restore_version_fields(server_key: str, snapshot: dict) -> None:
    """Write back the version state from BEFORE the change — all six fields.

    Restoring only the three current fields (`image_tag = previous_image_tag` etc.) is not
    enough: `_persist_version(save_previous=True)` has just moved `previous_*` to the state that
    was running, so after a rolled-back change `image_tag == previous_image_tag`. `rollback()`
    would then pick the running tag as its target and get a 400 "no version change" — the
    emergency path would be dead after the first failed change, exactly when it is needed
    (same for MC/VS via `previous_game_version`).

    So `_persist_version` takes a snapshot of ALL six fields and returns it; the symmetry is
    pinned in `tests/test_forward_and_rollback_are_symmetric.py`.
    """
    with Session(engine) as s:
        row = s.exec(select(GameServer).where(GameServer.key == server_key)).first()
        if row is None:
            return
        for field in _VERSION_FIELDS:
            if field in snapshot:
                setattr(row, field, snapshot[field])
        s.add(row)
        s.commit()


def _persist_version(server_key: str, *, image_tag, game_version, server_type,
                     save_previous: bool) -> dict:
    """Write the new version state and return the OLD one as a snapshot.

    The return value is the way back: `_rollback_swap` passes it to `_restore_version_fields`
    (it cannot be reconstructed from `previous_*`, which this function overwrites).
    """
    with Session(engine) as s:
        row = s.exec(select(GameServer).where(GameServer.key == server_key)).first()
        snapshot = {field: getattr(row, field) for field in _VERSION_FIELDS} if row else {}
        if save_previous:
            row.previous_image_tag = row.image_tag
            row.previous_game_version = row.game_version
            row.previous_type = row.server_type
        if image_tag is not None:
            row.image_tag = image_tag
            row.game_version = image_tag  # image-tag games: game_version mirrors the tag
        if game_version is not None:
            row.game_version = game_version
        if server_type is not None:
            row.server_type = server_type
        s.add(row)
        s.commit()
    return snapshot


def _record_pending(server, direction, old_ver, new_ver, old_type, new_type, backup_name) -> int:
    with Session(engine) as s:
        vc = VersionChange(server_id=server.id, direction=direction, backup_name=backup_name, status="pending")
        if versioned_by_image_tag(server):
            vc.old_image_tag, vc.new_image_tag = old_ver, new_ver
        else:
            vc.old_game_version, vc.new_game_version = old_ver, new_ver
            vc.old_type, vc.new_type = old_type, new_type
        s.add(vc)
        s.commit()
        s.refresh(vc)
        return vc.id


def _finish(vc_id: int, status_: str, detail: str) -> None:
    with Session(engine) as s:
        vc = s.get(VersionChange, vc_id)
        if vc:
            vc.status = status_
            vc.detail = detail
            s.add(vc)
            s.commit()


# --------------------------------------------------------------- Image provisioning
#
# Where the image tag is the version (e.g. PZ), a change needs the target image on disk
# (~10 GB for PZ). The download therefore runs as a **background task with progress**: a
# synchronous 10 GB pull would have to outlive a browser request (reverse proxies commonly cut
# requests off after about 100 s). State is process-local — consistent with the backend's
# single-worker invariant.

_pulls: dict[str, dict] = {}
_pull_tasks: dict[str, asyncio.Task] = {}


# One source for image references. `image_ref` also handles digest pins (`repo@sha256:…`); a
# local `repo:tag` join would build `repo:sha256:…`, a reference Docker does not accept.
_image_ref = image_ref


def _progress_sink(state: dict):
    """Docker reports progress per layer → sum it up to get one overall figure."""
    layers: dict[str, tuple[int, int]] = {}

    def sink(frame: dict) -> None:
        layer_id = str(frame.get("id") or "")
        detail = frame.get("progressDetail") or {}
        total = detail.get("total")
        if layer_id and isinstance(total, int) and total > 0:
            layers[layer_id] = (int(detail.get("current") or 0), total)
        state["current"] = sum(c for c, _ in layers.values())
        state["total"] = sum(t for _, t in layers.values())
        state["percent"] = (
            round(state["current"] / state["total"] * 100, 1) if state["total"] else None
        )
        if frame.get("status"):
            state["status"] = str(frame["status"])

    return sink


def _idle_pull(image: str, tag: str) -> dict:
    return {"image": image, "tag": tag, "status": "", "current": 0, "total": 0,
            "percent": None, "done": True, "ok": False, "error": "", "running": False}


def _image_target(server: GameServer, tag: str) -> str:
    """Target image for a version change — only where the tag IS the version.

    The condition is `VersionSource.image_tag`, not a game type (PZ, Factorio, Avorion).
    """
    if not versioned_by_image_tag(server) or not server.image_repo:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail=err("version.no_image_pull",
                       "This game pulls no images — the version comes from the image at boot."),
        )
    return _image_ref(server.image_repo, tag)


async def start_pull(server_key: str, tag: str) -> dict:
    """Start the image download in the background (idempotent: a running download is shared)."""
    server = _load(server_key)
    image = _image_target(server, tag)
    # First ask whether a pull is needed at all (same order as in `ensure_image`). Otherwise the
    # disk space check would block a change to an image that is already local and needs no more
    # space.
    try:
        if await docker.image_exists(image):
            return _idle_pull(image, tag) | {"status": "already present", "done": True,
                                             "running": False, "ok": True, "percent": 100.0}
    except Exception as e:
        log.warning("version: image_exists(%s) cannot be checked (%s) → trying the pull", image, e)
    await _check_space(server.image_repo, tag)
    running = _pulls.get(image)
    if running and not running["done"]:
        return dict(running)
    state = _idle_pull(image, tag) | {"status": "preparing", "done": False, "running": True}
    _pulls[image] = state

    async def run() -> None:
        try:
            await docker.pull(image, on_progress=_progress_sink(state))
            state.update(ok=True, status="done", percent=100.0)
        except Exception as e:
            log.warning("version: pull %s failed: %s", image, e)
            state.update(error=str(e)[:300], status="failed")
        finally:
            state.update(done=True, running=False)

    _pull_tasks[image] = asyncio.create_task(run())
    return dict(state)


def pull_status(server_key: str, tag: str) -> dict:
    image = _image_target(_load(server_key), tag)
    return dict(_pulls.get(image) or _idle_pull(image, tag))


def _free_space_bytes() -> int | None:
    """Free space on the filesystem where Docker stores its images — or None.

    Measured is `/` **inside the backend container**. That is not arbitrary: with overlay2 the
    container rootfs lives under `/var/lib/docker/overlay2`, so `statvfs("/")` reports the
    figures of exactly the filesystem that also holds the images. `<data_dir>` is unsuitable —
    it is a bind mount and may be on another disk.

    Known limit: if the images are stored on a filesystem that is NOT the one backing the
    container rootfs, this measures the wrong one. That is why the check below blocks only on a
    clear finding and never on an unclear measurement: an unanswered question must not prevent
    a legitimate version change.
    """
    try:
        usage = shutil.disk_usage("/")
        return int(usage.free) if usage.total > 0 else None
    except Exception:
        return None


async def _check_space(repo: str, tag: str) -> None:
    """Check before a large (~10 GB) pull whether it fits at all.

    Without this check a change could fail on a full disk in the middle of the pull — after the
    mandatory backup.

    The estimate comes from the images of the same repo that are already present: a server image
    is about as large as the previous one. If there is none, nothing is guessed and nothing is
    blocked.
    """
    free = _free_space_bytes()
    if free is None:
        return
    try:
        sizes = await docker.image_sizes(repo)
    except Exception:
        return
    if not sizes:
        return
    # 20 % margin: the pull briefly needs the layer archive AND the unpacked layer.
    needed = int(max(sizes.values()) * 1.2)
    if free >= needed:
        return
    raise HTTPException(
        status.HTTP_507_INSUFFICIENT_STORAGE,
        detail=err(
            "version.disk_space_low",
            "Not enough space for '{image}': {free_gb} GB are free, about {needed_gb} GB are "
            "needed. Images that are no longer needed can be removed in the version tab.",
            image=f"{repo}:{tag}", free_gb=f"{free / 1e9:.1f}", needed_gb=f"{needed / 1e9:.1f}",
        ),
    )


async def _cleanup_old_images(server, keep: set[str]) -> list[str]:
    """Remove images of the same repo that are neither current nor the way back.

    **Call only after the boot is verified** and always pass `previous_image_tag`: that is what
    `rollback()` points to. Deleting an image that is still the way back would be worse than
    keeping one. Errors have no consequences here — cleanup must never overturn a successful
    change.
    """
    removed: list[str] = []
    if not server.image_repo:
        return removed
    try:
        present = await docker.image_sizes(server.image_repo)
    except Exception:
        return removed
    for tag in present:
        if tag in keep:
            continue
        ref = _image_ref(server.image_repo, tag)
        try:
            if not await docker.remove_image(ref):
                log.info("version[%s]: %s stays — still referenced by a container", server.key, ref)
                continue
            removed.append(ref)
            log.info("version[%s]: old image removed: %s", server.key, ref)
        except Exception as e:
            log.warning("version[%s]: image %s cannot be removed: %s", server.key, ref, e)
    return removed


async def ensure_image(repo: str, tag: str) -> None:
    """Make sure the target image is available locally — otherwise pull it synchronously. Raises
    a descriptive error instead of failing silently (`/containers/create` would otherwise abort
    with a bare 404 "no such image")."""
    if not repo:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=err("version.image_repo_missing", "image_repo is missing"))
    image = _image_ref(repo, tag)
    try:
        if await docker.image_exists(image):
            return
    except Exception as e:  # e.g. proxy without IMAGES permission → try the pull, whose error is descriptive
        log.warning("version: image_exists(%s) cannot be checked (%s) → trying the pull", image, e)
    running = _pulls.get(image)
    if running and not running["done"]:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail=err("version.image_pull_in_progress",
                       "Image '{image}' is being pulled ({percent} %) — please wait.",
                       image=image, percent=running.get("percent") or 0),
        )
    await _check_space(repo, tag)
    try:
        await docker.pull(image)
    except Exception as e:
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            detail=err(
                "version.image_unavailable",
                "Image '{image}' is neither present locally nor pullable: {error} — "
                "does the Docker socket proxy allow image operations (IMAGES=1)?",
                image=image, error=str(e),
            ),
        )


# --------------------------------------------------------------- Boot verification + swap

async def verify_boot(server: GameServer, driver: GameDriver, timeout: float = 900.0) -> bool:
    """Poll until the container is running AND a liveness signal confirms it (source_rcon: RCON
    answers).

    Public because mod management (`services/mods/apply.py`) needs the same boot proof: after a
    mod change "container is running" is worthless — only an RCON answer shows that the game
    server really digested the new configuration.

    The timeout is generous (default 15 min): a PZ world migration/re-indexing on the first boot
    of a new version can take long, and a false negative here triggers a rollback.

    Important: containers run `unless-stopped` (NOT `RestartPolicy=no`) — Docker restarts a
    crashing new container immediately, so a single `running=False` poll usually misses the
    crash. Crash detection therefore relies primarily on the **restart counter**: if `restarts`
    rises above its initial value, the new version is crash-looping → fail immediately
    (→ rollback) instead of waiting for the timeout. The (rare) clean exit without restart is
    caught by the `was_up` + `running=False` path. Console kinds without an RCON signal count
    as booted once the container runs **stably** (not `restarting`) for several rounds.

    A driver with `boot_ready_line` (Vintage Story, whose RCON needs an optional third-party
    mod) has a second proof: when `players()` does not answer, the game's own ready line in the
    log of the current start plus several stable rounds count as booted.
    """
    # The boot check mutes the watchdog itself — for exactly as long as it takes.
    #
    # Suppressing only BEFORE stop and mandatory backup is not enough: the check has its own
    # `timeout` and starts only after stop and archiving are done, so that window is guaranteed
    # to end before the boot deadline. The watchdog would then see a running container without
    # an RCON answer, count three rounds and call `lifecycle.restart_server`; `verify_boot`
    # would see `running=False` after `was_up=True` and report failure — a **rollback of a
    # healthy boot**: new container gone, `extract_backup` over the already migrated world.
    #
    # The rule lives here, in the one place every boot check passes through.
    # `watchdog.suppress` extends via `max()` and never shortens, so a double call is harmless.
    # The extra margin is there because the watchdog counts in 60 s rounds.
    watchdog.suppress(server.key, timeout / 60.0 + 2)

    was_up = False
    base_restarts: int | None = None
    # Without an RCON signal "stable" needs several rounds. Returning `True` in the FIRST round
    # (five seconds after `docker start`) would never reach the crash detection via the restart
    # counter, which needs at least two rounds because the first only sets the baseline — and a
    # version change removes `{name}-prev` with `force=True` right afterwards, so the fast way
    # back would be gone before the new container has loaded.
    #
    # Reforger (`battleye_rcon`) used to be the one console kind that ended here, observed for
    # longer only. Its driver now sets `boot_proof_via_players`: the BattlEye `#players` answer
    # is the proof (measured in A12: "completed" 5 s after the game answered A2S, where it used
    # to come about 15 minutes before the server was playable).
    STABLE_ROUNDS = 3
    stable = 0
    # The game's own "ready" line (`GameDriver.boot_ready_line`): the proof where an answer is
    # not to be had. `getattr`, because not every driver-like object derives from the base.
    ready_line = getattr(driver, "boot_ready_line", None)
    for _ in range(max(1, int(timeout / 5))):
        await asyncio.sleep(5)
        try:
            st = await docker.state(server.container_name)
        except Exception:
            # The count starts over. Otherwise "three stable rounds" would not be three
            # CONSECUTIVE ones: running / proxy gone / running / running would be enough. An
            # unanswered round is no proof of stability.
            stable = 0
            continue
        restarts = int(st.get("restarts") or 0)
        if base_restarts is None:
            base_restarts = restarts
        elif restarts > base_restarts:
            return False  # new version crashes and is restarted (unless-stopped) → failure
        if not st.get("running"):
            if was_up:
                return False  # was running, then stopped/exited cleanly without restart → failure
            continue
        was_up = True
        # Docker is still restarting it → the count starts over.
        stable = 0 if st.get("restarting") else stable + 1
        if server.console_kind == ConsoleKind.source_rcon or driver.players_usable_as_boot_proof(server):
            try:
                await driver.players(server)
                return True  # real boot proof: the server ANSWERS
            except Exception:
                if ready_line is None:
                    continue  # running but not ready yet → keep waiting
        if ready_line is not None:
            # No answer (RCON not set up, or not up yet) → the game's own ready line, written
            # since THIS start, on a container that has been running stably for several rounds.
            # A server that never writes it fails as before; a crash after it shows up in the
            # restart counter of the next rounds as long as no proof has been accepted.
            if stable >= STABLE_ROUNDS and await _ready_line_logged(server, ready_line, st.get("started_at")):
                log.info("boot[%s]: proven by the server's ready line in the log (no answer from "
                         "players()/RCON)", server.key)
                return True
            continue
        if stable >= STABLE_ROUNDS:
            return True  # no RCON signal → running for several rounds without a new restart
    # Window exhausted without a confirmed boot (e.g. RCON never reachable) → failure (triggers rollback)
    return False


async def _ready_line_logged(server: GameServer, pattern: re.Pattern[str], started_at: str | None) -> bool:
    """Has the container written `pattern` to its log since its current start?

    `since` is what makes it proof: a restore restarts the SAME container, whose log still holds
    the ready line of every earlier run. Without a readable start time there is no proof.
    """
    started = docker_start_time(started_at)
    if started is None:
        return False
    since = int(started.replace(tzinfo=dt.timezone.utc).timestamp())
    try:
        text = await docker.logs(server.container_name, tail=20000, since=since)
    except Exception as e:  # noqa: BLE001 — an unreadable log is no proof, the next round asks again
        log.info("boot[%s]: log not readable (%s)", server.key, e)
        return False
    return bool(pattern.search(text))


async def _remove_old_leftover(prev: str) -> None:
    """Remove a leftover `{name}-prev` — or fail loudly.

    **Why loudly.** A leftover is exactly the state in which `docker.rename(name, prev)` fails
    with 409 "name already in use". If the removal failed silently, the change would start
    anyway: server stopped, mandatory backup taken, `rename` fails — and the rollback would then
    deal with `name`, which at that point is still the OLD, good container.

    Such a leftover is an expected state, not an accident: the success path only logs a warning
    when it cannot remove the old container.

    Same rule as in the scheduled restart: **an operation must not activate a state whose way
    back is known to be missing.**
    """
    try:
        await docker.remove(prev, force=True, volumes=True)
    except Exception as e:
        log.warning("version: leftover %s cannot be removed: %s", prev, e)
    if (await docker.state(prev)).get("exists"):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail=err("version.prev_container_stuck",
                       "The container “{name}” from an earlier version change is still there "
                       "and cannot be removed. As long as it exists, this change has no way "
                       "back — so it is not even started. Check on the host: docker rm -f {name}",
                       name=prev),
        )


async def _rollback_swap(
    server_key, name, prev, spec, backup_name, was_running, driver, old_existed,
    *, renamed: bool, version_state_before: dict | None = None,
) -> tuple[bool, bool]:
    """Roll back a failed swap. Returns `(container_restored, world_restored)`.

    **Why two values:** the container being back does not mean the world is. The world restore
    can be skipped or fail in three ways: `extract_backup` raises, the container is still
    running (the restore is then deliberately skipped), or `docker.state` itself raises. A single
    `True` would let the caller write "rolled back" into the history and the error message while
    the world is still in the state the new version migrated it to.

    Order: (1) **force**-remove the failed NEW container (it may still be running), (2) reset
    the version fields, (3) bring the old container back (rename `prev`→`name`, otherwise
    rebuild it from the row), (4) restore the world backup **only over a STOPPED container**
    (never overwrite a live world), (5) start if it was running before.
    """
    # `renamed` decides WHO `name` currently belongs to. Only after `rename(name, prev)` is
    # `name` the new container; if that very `rename` fails (409 "name already in use" because a
    # `{name}-prev` from an earlier failed attempt is still there), `name` still points to the
    # OLD, known-good container — and a `remove(name, force=True)` here would be the damage, not
    # the way back. `rename(prev, name)` would then promote a leftover two generations old.
    if renamed:
        try:
            await docker.remove(name, force=True)  # remove the failed NEW container
        except Exception:
            pass
    _restore_version_fields(server_key, version_state_before or {})
    old = _load(server_key)
    # bring the old container back
    restored = False
    if old_existed and renamed:
        try:
            await docker.rename(prev, name)
            restored = True
        except Exception:
            pass
    elif old_existed and not renamed:
        # The old container sits untouched under `name` — there is nothing to bring back. That
        # is the good outcome: the change never reached it.
        restored = True
    if not restored:
        try:  # no old container (or rename failed) → rebuild from the reset row
            await docker.create(name, build_create_payload(old, servers_registry.resolve_secret(old)))
            restored = True
        except Exception as e:
            log.error("version[%s]: rollback could not restore the old container: %s", server_key, e)
            return False, False
    # Restore the world backup — ONLY over a container confirmed stopped (no live overwrite).
    # `old` is the reset registry row. `stop_and_confirm` is the shared helper for "stop and
    # verify the stop"; do not hand-roll that check here.
    world_restored = False
    try:
        _, stopped = await lifecycle.stop_and_confirm(old)
        if stopped:
            # Check first, then touch — the same checkpoint as in the restore. An empty or
            # truncated mandatory archive would otherwise unpack without an exception (or fail
            # midway), and `world_restored` would be True while the world is in the new version's
            # state.
            await asyncio.to_thread(
                check_restorable, backup_name,
                data_dir=spec.data_dir, world_subdirs=spec.world_subdirs,
            )
            await asyncio.to_thread(extract_backup, backup_name, data_dir=spec.data_dir)
            world_restored = True
        else:
            log.error("version[%s]: container not stopped — world restore SKIPPED", server_key)
    except Exception as e:
        log.error("version[%s]: world restore in the rollback failed: %s", server_key, e)
    # The result of the restart counts. If the start fails, `container_restored` must be False:
    # otherwise it would only attest that `rename`/`create` worked, not that the server runs.
    # There is no boot proof at this point (unlike in `mods/apply._rollback`), and the watchdog
    # is muted for 15 minutes below, so autoheal would not bring it up either.
    if was_running:
        try:
            await docker.action(name, "start")
        except Exception as e:
            log.error("version[%s]: rollback — the old container does NOT start again: %s",
                      server_key, e)
            watchdog.suppress(server_key, 15)
            return False, world_restored
    watchdog.suppress(server_key, 15)
    return True, world_restored


# Serialisation per server is done by `world_lock` (see `change_version`): a second version
# change/rollback must not run into the verify window of a running swap and delete its
# set-aside known-good container (`{name}-prev`) or scramble `previous_*`.



def check_change_preconditions(
    server_key: str, *, image_tag=None, game_version=None, server_type=None,
    confirm_downgrade: bool = False, rebuild: bool = False,
) -> None:
    """All preconditions of a version change — checkable SYNCHRONOUSLY, without touching anything.

    The endpoint must call this **before** it starts the background operation. Checks that live
    only in `_change_version_locked` run INSIDE `operations.start`, i.e. after the HTTP response
    has already reported 200. The frontend relies on the downgrade gate arriving as an HTTP 400
    to offer `confirm_downgrade`; and "no version change", "compose-managed" and "game type not
    supported" would otherwise first report success and fail seconds later in the operation
    panel.

    The checks also stay in `_change_version_locked`: `rollback()` and the CLI call straight
    into it, and a precondition that is checked only at the edge is none.
    """
    server = _load(server_key)
    if server.management_mode == ManagementMode.compose:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail=err("version.compose_managed",
                       "compose-managed: a version change is only possible once the dashboard "
                       "manages the container"),
        )
    try:
        get_driver(server)
    except NotImplementedError:
        raise HTTPException(
            status.HTTP_501_NOT_IMPLEMENTED,
            detail=err("version.game_type_unsupported",
                       "Game type '{game_type}' is not supported yet",
                       game_type=str(server.game_type)),
        )
    if versioned_by_image_tag(server):
        old_ver = server.image_tag
        new_ver = image_tag if image_tag is not None else server.image_tag
        old_type = new_type = server.server_type
    else:
        old_ver = server.game_version
        new_ver = game_version if game_version is not None else server.game_version
        old_type = server.server_type
        new_type = server_type if server_type is not None else server.server_type
    if new_ver == old_ver and new_type == old_type and not rebuild:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=err("version.no_change", "No version change given"))
    # A value the image does not accept would otherwise fail only AFTER announcement, stop,
    # backup and container swap — minutes of downtime for a typo like `v1.22.8`.
    driver = get_driver(server)
    if (not versioned_by_image_tag(server) and driver.version_required
            and not driver.version_valid(new_ver or "")):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail=err("version.invalid_version",
                       "'{version}' is not a valid version for this game (e.g. 1.22.7).",
                       version=new_ver),
        )
    if _direction(old_ver, new_ver) == "down" and not confirm_downgrade:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail={**err("version.downgrade_needs_confirmation",
                          "Downgrade needs confirmation (confirm_downgrade)"),
                    "error": "Downgrade needs confirmation (confirm_downgrade)",
                    "old": old_ver, "new": new_ver},
        )


async def change_version(
    server_key: str, *, image_tag=None, game_version=None, server_type=None,
    confirm_downgrade: bool = False, direction_label: str | None = None, progress=None,
    rebuild: bool = False,
) -> dict:
    # The world lock, not a lock of its own: it covers ALL operations that touch world files.
    # A lock against other version changes only would let a mod apply or a restore run into the
    # middle and remove the container whose boot is being awaited for up to 900 s.
    if (holder := world_lock.held_by(server_key)):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail=err("world_lock.busy_please_wait",
                       "Already running for this server: {operation} — please wait.",
                       operation=world_lock.OPERATION_LABELS.get(holder, holder)),
        )
    def locked():
        return _change_version_locked(
            server_key, image_tag=image_tag, game_version=game_version, server_type=server_type,
            confirm_downgrade=confirm_downgrade, direction_label=direction_label, progress=progress,
            rebuild=rebuild,
        )

    # A rebuild holds the lock under its own name, so a 409 elsewhere says what is running. Two
    # literal calls: `tests/test_world_lock_complete.py` reads the names from the syntax tree.
    if rebuild:
        async with world_lock.hold(server_key, "version.recreate"):
            return await locked()
    async with world_lock.hold(server_key, "version.change"):
        return await locked()


def _noop_step(text: str, **kw) -> None:
    pass


async def _change_version_locked(
    server_key: str, *, image_tag=None, game_version=None, server_type=None,
    confirm_downgrade: bool = False, direction_label: str | None = None, progress=None,
    rebuild: bool = False,
) -> dict:
    step = progress.step if progress is not None else _noop_step
    server = _load(server_key)
    if server.management_mode == ManagementMode.compose:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail=err("version.compose_managed",
                       "compose-managed: a version change is only possible once the dashboard "
                       "manages the container"),
        )
    try:
        driver = get_driver(server)
    except NotImplementedError:
        raise HTTPException(
            status.HTTP_501_NOT_IMPLEMENTED,
            detail=err("version.game_type_unsupported",
                       "Game type '{game_type}' is not supported yet",
                       game_type=str(server.game_type)),
        )

    if versioned_by_image_tag(server):
        old_ver = server.image_tag
        new_ver = image_tag if image_tag is not None else server.image_tag
        old_type = new_type = server.server_type
    else:
        old_ver = server.game_version
        new_ver = game_version if game_version is not None else server.game_version
        old_type = server.server_type
        new_type = server_type if server_type is not None else server.server_type
    if new_ver == old_ver and new_type == old_type and not rebuild:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=err("version.no_change", "No version change given"))
    if rebuild:
        # Rebuild WITHOUT a version change — the same create-then-swap, with mandatory backup,
        # boot proof and full rollback. It is the only way to make changed container parameters
        # take effect: `ConfigReload.recreate` means the values live in the container's env and
        # can only be set at creation. For Valheim ALL fields (name, world, join password,
        # publicly listed, BepInEx) are env-managed — without a rebuild the registry would show
        # the NEW value as the running one while the container keeps running with the old.
        direction_label = direction_label or "recreate"

    computed = _direction(old_ver, new_ver)
    if computed == "down" and not confirm_downgrade:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail={**err("version.downgrade_needs_confirmation",
                          "Downgrade needs confirmation (confirm_downgrade)"),
                    "error": "Downgrade needs confirmation (confirm_downgrade)",
                    "old": old_ver, "new": new_ver},
        )
    direction = direction_label or computed  # rollback() sets "rollback" explicitly

    # 0. Make sure the target image exists (only where the tag IS the version — PZ, Factorio,
    #    Avorion; Minecraft and Vintage Story download the version at boot). Deliberately FIRST:
    #    if the image is missing and cannot be pulled, the change is impossible — the running
    #    server then stays completely untouched and neither backup nor rollback is needed. A
    #    pull in the middle of the swap would be the wrong order.
    if versioned_by_image_tag(server):
        step("Providing the target image…")
        await ensure_image(server.image_repo, new_ver)

    # 1. Stop, CONFIRM the stop, THEN back up (the world rollback point). Three invariants, all
    #    about the swap's only way back:
    #
    #   * The mandatory backup is taken AFTER the stop. PZ has no quiesce hooks, so a backup of
    #     a running server would be a hot copy of a world being written — and `_rollback_swap`
    #     restores exactly this archive.
    #   * An unreadable run state must not be read as "not running", or the server would stay
    #     down after a rollback.
    #   * The stop must be confirmed: `docker.rename` and `docker.create` below use the SAME
    #     binds, and an unconfirmed stop could leave two game processes on one world directory.
    #
    # Before the stop: if a `{name}-prev` from an earlier failed attempt is still there, this
    # change has no way back — then it is not started at all. A guard that only takes effect
    # after the stop is none.
    prev_name = f"{server.container_name}{_PREV_SUFFIX}"
    await _remove_old_leftover(prev_name)

    # A rebuild names its archive and its announcement after what it does: `pre-recreate-…`,
    # not `pre-version-…` with an unchanged version. Nothing parses these labels (a backup's
    # name is `<stamp>-<label>.tar.gz`, `backup._NAME_RE`), so archives with the old label stay
    # listed and restorable as they are.
    step("Stopping the server and backing it up…")
    was_running, backup = await lifecycle.stop_and_snapshot(
        server, driver, f"pre-recreate-{new_ver}" if rebuild else f"pre-version-{new_ver}",
        announce_text=("Recreating the server — it is shutting down…" if rebuild
                       else "Version change — the server is shutting down…"),
        suppress_minutes=15,
    )
    # The steps between mandatory backup and the swap block are protected too:
    # `_record_pending` and `_persist_version` are DB writes, `backup_spec` is a driver call —
    # if one raises (database briefly gone, dropped connection), the exception must not leave
    # the function without the server, which was just confirmed stopped, being started again.
    async with lifecycle.ensure_restart(server, was_running,
                                        what="recreate" if rebuild else "version change"):
        vc_id = _record_pending(server, direction, old_ver, new_ver, old_type, new_type, backup["name"])
        # For the way back below (`_rollback_swap` restores the world from it). The driver call
        # is cheap; repeating it here is simpler than passing it out of the helper.
        spec = driver.backup_spec(server)
        name = server.container_name
        prev = f"{name}{_PREV_SUFFIX}"
        # The return value IS the way back: all six version fields as they were before the
        # change.
        version_state_before = _persist_version(
            server_key, image_tag=image_tag, game_version=game_version,
            server_type=server_type, save_previous=True,
        )

    old_exists = False
    renamed = False
    try:
        # Rename the old container (if any) aside. If it exists, the rename MUST succeed
        # (409 → outer except → rollback), otherwise a wrong container would be promoted.
        old_exists = bool((await docker.state(name)).get("exists"))
        if old_exists:
            # The game-appropriate stop already happened above — CONFIRMED, via
            # `stop_and_snapshot`. No second, unconfirmed stop here.
            await docker.rename(name, prev)
            renamed = True
        fresh = _load(server_key)  # row with the NEW version
        secret = servers_registry.resolve_secret(fresh)
        # What the registry owns (port, console password) into the game's own file before the
        # new container exists, as the edit page and the container create do: the images write
        # it only on the first start, and a drifted file makes the boot check fail on RCON.
        sync = getattr(driver, "sync_config_from_registry", None)
        if callable(sync) and fresh.data_dir:
            await asyncio.to_thread(sync, fresh, secret)
        payload = build_create_payload(fresh, secret)
        await docker.create(name, payload)
        # The same gate as in the restore. A blocker falls into the outer `except` and thus
        # into the regular rollback: the old state comes back instead of a broken configuration
        # booting with the new version.
        if (blockers := await mod_state.guard_boot(fresh)):
            raise RuntimeError(mod_state.guard_message(blockers))
        step("Server is starting — verifying the boot (may take a few minutes)…")
        await docker.action(name, "start")
        if not await verify_boot(fresh, driver, timeout=900):
            raise RuntimeError("the server did not prove its boot within 15 minutes (no answer "
                               "from the console and no ready line in its log)")
        if old_exists:
            try:
                # With its anonymous volumes (an image's `VOLUME`): left behind, each recreate
                # would leave one more on the host. Binds and named volumes are never touched.
                await docker.remove(prev, force=True, volumes=True)  # success → remove the old container
            except Exception:
                log.warning("version[%s]: old container %s cannot be removed (check manually)", server_key, prev)
    except Exception as e:
        log.warning("version[%s]: change failed → rollback (%s)", server_key, e)
        step("Failed — rolling back…")
        container_restored, world_restored = await _rollback_swap(
            server_key, name, prev, spec, backup["name"], was_running, driver, old_exists,
            renamed=renamed, version_state_before=version_state_before,
        )
        # Three outcomes, not two. The middle one is the dangerous one: the server runs again,
        # but on a world the new version has already migrated.
        #
        # A rebuild has its own sentences. Its settings were saved BEFORE the swap (edit page:
        # PUT, then recreate), and the way back brings the previous container, so the saved
        # values are not the running ones. That is said here and shown on the edit page (from
        # the history row: newest entry a failed `recreate`), instead of the saved row silently
        # claiming a RAM limit the container does not have.
        if rebuild:
            state, detail = _recreate_failure(container_restored, world_restored, backup["name"], e)
        elif container_restored and world_restored:
            # `extract_backup` REPLACES the archived folders, so whatever the new version
            # created in up to 900 s (`apop/`, `metagrid/`, `isoregiondata/`, the B42 layout) is
            # gone with them: "rolled back" is what happened.
            state = "rolled_back"
            detail = err(
                "version.change_failed_rolled_back",
                "Version change failed, container and world rolled back: {error}",
                error=str(e),
            )
        elif container_restored:
            state = "rolled_back_partial"
            detail = err(
                "version.change_failed_world_not_rolled_back",
                "Version change failed, container rolled back, BUT NOT THE WORLD — it is still "
                "in the state of the new version. The backup '{backup}' is the way back: {error}",
                backup=backup["name"], error=str(e),
            )
        else:
            state = "failed"
            detail = err(
                "version.change_failed_rollback_incomplete",
                "Version change failed, ROLLBACK INCOMPLETE — check manually: {error}",
                error=str(e),
            )
        _finish(vc_id, state, str(e)[:400])
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, detail=detail)

    # --- Bookkeeping — OUTSIDE the rollback `try` --------------------------------------------
    #
    # From here on the operation has succeeded: the boot is proven. `_cleanup_old_images`
    # and `_finish` must not sit in the `try` above, whose `except` runs `_rollback_swap`
    # (`docker.remove(name, force=True)` on the container just verified, plus `extract_backup`
    # of the pre-version archive over the data folder): an error while cleaning up old images
    # or a briefly unavailable database would throw away the new version AND its world.
    completion_note = None
    try:
        # Clean up old images — but NEVER the one of the way back. `rollback()` points to
        # `previous_image_tag`; deleting an image that is still the way back would be worse than
        # keeping one. Only here, after the boot proof.
        await _cleanup_old_images(fresh, {fresh.image_tag, fresh.previous_image_tag} - {None, ""})
        _finish(vc_id, "ok", "")
    except Exception as e:
        log.error("version[%s]: change succeeded, but the completion failed (%s)", server_key, e)
        completion_note = note(
            "version.completion_failed",
            "The new version is running — only the completion (cleaning up old images, writing "
            "the history) failed: {error}",
            error=str(e),
        )
    # A rebuild changed no version: the edit page and the config editor recreate the container
    # to apply saved settings, and "The version change is complete." would not fit that.
    summary = {"summary": "The container was recreated with the saved settings."} if rebuild else {}
    return {"status": "ok", "direction": direction, "old": old_ver, "new": new_ver,
            "backup": backup["name"], **summary,
            **({"note": completion_note} if completion_note else {})}


def _recreate_failure(container_restored: bool, world_restored: bool, backup: str,
                      error: Exception) -> tuple[str, dict]:
    """History state and coded message of a failed rebuild (`rebuild=True`)."""
    if container_restored and world_restored:
        return "rolled_back", err(
            "version.recreate_failed_rolled_back",
            "Recreating the container failed; the previous container and the world are back. The "
            "saved settings are NOT applied: the server runs with the ones from before. {error}",
            error=str(error),
        )
    if container_restored:
        return "rolled_back_partial", err(
            "version.recreate_failed_world_not_rolled_back",
            "Recreating the container failed; the previous container is back, BUT NOT THE WORLD. "
            "The saved settings are NOT applied. The backup '{backup}' is the way back: {error}",
            backup=backup, error=str(error),
        )
    return "failed", err(
        "version.recreate_failed_rollback_incomplete",
        "Recreating the container failed, ROLLBACK INCOMPLETE — check manually: {error}",
        error=str(error),
    )


async def rollback(server_key: str, *, progress=None) -> dict:
    """Switch back to the previously running version (previous_*), always with downgrade
    confirmation.

    `progress` is passed through so the rollback can run as a background operation — it performs
    the same create-then-swap as `change_version`, including `verify_boot` with a time budget of
    up to 900 s.
    """
    server = _load(server_key)
    with Session(engine) as s:
        vc = s.exec(
            select(VersionChange).where(VersionChange.server_id == server.id).order_by(VersionChange.id.desc())
        ).first()
    if vc is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=err("version.no_history", "No version history to roll back to"))
    if versioned_by_image_tag(server):
        target = server.previous_image_tag or vc.old_image_tag
        if not target:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=err("version.no_previous_image_tag", "No previous_image_tag for a rollback"))
        return await change_version(server_key, image_tag=target, confirm_downgrade=True,
                                    direction_label="rollback", progress=progress)
    target = server.previous_game_version or vc.old_game_version
    if not target:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=err("version.no_previous_game_version", "No previous_game_version for a rollback"))
    return await change_version(
        server_key, game_version=target, server_type=(server.previous_type or vc.old_type),
        confirm_downgrade=True, direction_label="rollback", progress=progress,
    )


def history(server_key: str) -> list[dict]:
    server = _load(server_key)
    with Session(engine) as s:
        rows = s.exec(
            select(VersionChange).where(VersionChange.server_id == server.id).order_by(VersionChange.id.desc())
        ).all()
    return [
        {
            "id": r.id, "at": r.at, "direction": r.direction, "status": r.status,
            "backup_name": r.backup_name, "detail": r.detail,
            "old": r.old_image_tag or r.old_game_version, "new": r.new_image_tag or r.new_game_version,
            "old_type": r.old_type, "new_type": r.new_type,
        }
        for r in rows
    ]
