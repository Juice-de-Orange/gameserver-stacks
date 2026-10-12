"""On-demand download of workshop items via steamcmd (anonymous login, app 108600).

**steamcmd is not part of the API image.** It runs in a small helper image
(`settings.steamcmd_image`, built from `dashboard/images/steamcmd`) that is pulled the first time
an active game needs a workshop item — today only Project Zomboid. An installation that never
downloads a workshop item never downloads steamcmd or its 32-bit libraries.

The helper runs as a short-lived container through the socket proxy (CONTAINERS + POST + IMAGES,
nothing else). Its cache — steamcmd's self-update and the downloaded items — is a bind mount
under DATA_ROOT (`<DATA_ROOT>/.steamcmd`), created here with the API's uid/gid and handed to the
helper as its user: the proxy cannot create volumes, and the API sees DATA_ROOT under the same
path as the host, so it can read what the helper wrote. The helper's entrypoint only accepts an
app ID and a workshop ID and always logs in anonymously.

Downloaded items are mirrored into the workshop folder OF THE TARGET SERVER
(`<data_dir>/workshop-mods/content/108600/<id>`) so the dashboard can read mod.info IMMEDIATELY
(to derive mod IDs) without waiting for a server restart. The server verifies the items itself
on its next start anyway (idempotent).

**Scoped to one server** (`server` is required): the target is the mod directory of exactly the
server in question.

Robust: the real download path is parsed from the steamcmd output
(`Downloaded item ... to "<path>"`), not guessed. Errors degrade cleanly with a truncated log
for the UI.
"""
from __future__ import annotations

import asyncio
import hashlib
import itertools
import logging
import os
import posixpath
import re
import shutil
import stat

from ..config import settings
from .docker_client import docker

log = logging.getLogger("uvicorn.error")

PZ_APP_ID = 108600
_DL_RE = re.compile(r'Downloaded item \d+ to "([^"]+)"')

# Where the helper container sees the cache, and the cache's folder name under DATA_ROOT.
STEAM_HOME = "/steam"
CACHE_DIRNAME = ".steamcmd"
# `console-` like every container the platform itself creates (`tests/test_naming_rule.py`).
# The full name is `<prefix><installation id>-<workshop id>` (`helper_name`).
HELPER_PREFIX = "console-steamcmd-"
# Container start, steamcmd's self-update on the first run (about 200 MB on disk, measured) and
# the item itself.
DOWNLOAD_TIMEOUT_S = 600.0

# One lock per workshop ID around the mirroring step.
#
# The step runs in `asyncio.to_thread` (necessary, because `copytree` would otherwise stall the
# whole API), so it is no longer atomic with respect to other coroutines — and
# `POST …/mods/items`, unlike `…/mods/apply`, has NO in-flight lock. Without this lock a double
# click on "install" lets two runs delete each other's staging directory and leave HALF a mod
# folder at the destination, reported as `ok=True`.
_mirror_locks: dict[str, asyncio.Lock] = {}
_run_counter = itertools.count()


def _mirror_lock(wid: str) -> asyncio.Lock:
    lock = _mirror_locks.get(wid)
    if lock is None:
        lock = _mirror_locks[wid] = asyncio.Lock()
    return lock


def _skip_links_and_special_files(folder: str, names: list[str]) -> set[str]:
    """`copytree` filter: copy only real folders and regular files.

    The cache is writable by the helper container, which runs downloaded content. `copytree`
    follows symlinks by default, so a workshop item holding `x -> /some/host/file` would make
    the API copy that file into a server's workshop folder. FIFOs and device files are skipped
    as well: `copy2` would wait on a FIFO forever and hold the mirror thread.
    """
    skipped: set[str] = set()
    for name in names:
        path = os.path.join(folder, name)
        try:
            mode = os.lstat(path).st_mode
        except OSError:
            skipped.add(name)
            continue
        if stat.S_ISLNK(mode):
            log.warning("workshop mirror: skipping symlink %s -> %s", path, os.readlink(path))
            skipped.add(name)
        elif not (stat.S_ISDIR(mode) or stat.S_ISREG(mode)):
            log.warning("workshop mirror: skipping special file %s", path)
            skipped.add(name)
    return skipped


async def mirror_into_workshop(wid: str, src: str, dst: str) -> str | None:
    """Mirror the downloaded folder into place atomically.

    Returns `None` on success, otherwise the error message for the UI.

    ATOMIC via a sibling folder: copy completely next to the destination first, then move the
    old folder aside and the new one into place. A plain `rmtree` + `copytree` would leave HALF
    a mod folder when interrupted — and the preflight cannot tell half a mod from a broken one.

    The sibling folders carry the PID and a counter in their names. The lock above covers the
    normal case (one process, one worker); the unique names cover the case the lock cannot see —
    two processes on the same bind mount, e.g. while a new backend container replaces the old one.
    """
    async with _mirror_lock(str(wid)):
        base = os.path.dirname(dst)
        marker = f"{os.getpid()}.{next(_run_counter)}"

        def _work() -> str | None:
            os.makedirs(base, exist_ok=True)
            if os.path.islink(src):
                return f"The downloaded folder {src} is a symlink — not copied."
            if os.path.realpath(src) == os.path.realpath(dst):
                return None
            staging = f"{dst}.incoming.{marker}"
            retired = f"{dst}.old.{marker}"
            try:
                for leftover in (staging, retired):
                    if os.path.isdir(leftover):
                        shutil.rmtree(leftover, ignore_errors=True)
                shutil.copytree(src, staging, ignore=_skip_links_and_special_files)
                if os.path.isdir(dst):
                    os.replace(dst, retired)
                os.replace(staging, dst)
                shutil.rmtree(retired, ignore_errors=True)
            except OSError as e:
                # Clean up so no half state is left behind; bring the old folder back if needed.
                shutil.rmtree(staging, ignore_errors=True)
                if not os.path.isdir(dst) and os.path.isdir(retired):
                    try:
                        os.replace(retired, dst)
                    except OSError:
                        pass
                return f"Copying into the workshop folder failed: {e}"
            return None

        # `to_thread`, because `copytree` would otherwise stall the ONE uvicorn worker for the
        # duration of the copy: no HTTP response, no WebSocket, no scheduler tick. PZ workshop
        # maps range from hundreds of MB to GBs.
        return await asyncio.to_thread(_work)


def server_content_dir(server) -> str:
    """Workshop folder of the target server (identical to `services/mods/inventory.content_dir`)."""
    return os.path.join(server.data_dir, "workshop-mods", "content", str(PZ_APP_ID))


def cache_dir() -> str:
    """Host path of the steamcmd cache (same path inside the API container, see compose)."""
    return os.path.join(settings.data_root, CACHE_DIRNAME)


def install_id() -> str:
    """Eight hex digits that tell this installation's helpers from another one's on the same host.

    Container names are host-wide. With `console-steamcmd-<item>` alone, two installations that
    downloaded the same item at the same time removed each other's helper ("leftover of an
    aborted run"). Derived from what already has to differ between two installations on one host
    — the game network (`GAME_NETWORK`) and the cache under `DATA_ROOT` — so it is stable across
    API restarts and needs no state of its own.
    """
    seed = f"{settings.game_network}\0{cache_dir()}"
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()[:8]


def helper_name(wid: str) -> str:
    return f"{HELPER_PREFIX}{install_id()}-{wid}"


def helper_spec(app_id: int, wid: str, cache: str, uid: int, gid: int, image: str) -> dict:
    """Create payload of the helper container.

    No network setting: the default bridge reaches Steam, and the helper has no business on the
    game network. All capabilities dropped — steamcmd needs none as a normal user.
    """
    return {
        "Image": image,
        "Cmd": [str(app_id), str(wid)],
        "User": f"{uid}:{gid}",
        "Env": [f"HOME={STEAM_HOME}"],
        "Labels": {"stacks-console.helper": "steamcmd"},
        "HostConfig": {
            "Binds": [f"{cache}:{STEAM_HOME}"],
            "CapDrop": ["ALL"],
            "SecurityOpt": ["no-new-privileges"],
            "AutoRemove": False,
        },
    }


def host_path(container_path: str, cache: str) -> str | None:
    """A path steamcmd reported inside the helper → the same file as the API sees it.

    `None` for anything outside the cache mount: the path comes from the helper's output and
    must not point the mirror step at an arbitrary folder.
    """
    norm = posixpath.normpath(container_path)
    if norm != STEAM_HOME and not norm.startswith(STEAM_HOME + "/"):
        return None
    return os.path.join(cache, posixpath.relpath(norm, STEAM_HOME))


# One helper at a time: two steamcmd runs on the same cache fight over its lock files.
_helper_lock: asyncio.Lock | None = None


def _lock() -> asyncio.Lock:
    global _helper_lock
    if _helper_lock is None:
        _helper_lock = asyncio.Lock()
    return _helper_lock


async def _ensure_image(image: str) -> str | None:
    """Pull the helper image if it is missing. `None` on success, otherwise the text for the UI."""
    try:
        if not await docker.image_exists(image):
            await docker.pull(image)
    except Exception as e:  # noqa: BLE001 — every failure ends in the same advice
        return (
            f"The steamcmd helper image {image} is not available ({e}). Build it with "
            f"`docker build -t {image} dashboard/images/steamcmd` or set GSS_STEAMCMD_IMAGE."
        )
    return None


async def run_steamcmd(app_id: int, wid: str, timeout: float) -> tuple[int | None, str]:
    """Run the helper once. Returns (exit code, output); exit code `None` = timed out.

    Raises if the cache cannot be created or the Docker client fails.
    """
    cache = cache_dir()
    # The API's own ids: the helper writes as this user, so the mirror step can read the result.
    uid, gid = os.getuid(), os.getgid()
    await asyncio.to_thread(os.makedirs, cache, exist_ok=True)
    name = helper_name(wid)
    try:
        await docker.remove(name, force=True)   # leftover of an aborted run
    except Exception:  # noqa: BLE001
        pass
    await docker.create(name, helper_spec(app_id, wid, cache, uid, gid, settings.steamcmd_image))
    try:
        await docker.action(name, "start")
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        finished = False
        while loop.time() < deadline:
            if not (await docker.state(name)).get("running"):
                finished = True
                break
            await asyncio.sleep(1)
        output = await docker.logs(name, tail=400)
        if not finished:
            return None, output
        info = await docker.inspect(name) or {}
        return int((info.get("State") or {}).get("ExitCode", -1)), output
    finally:
        try:
            await docker.remove(name, force=True)
        except Exception:  # noqa: BLE001 — a leftover is removed by the next run
            pass


def _stale_name_re() -> re.Pattern[str]:
    """This installation's helper names, plus the old `console-steamcmd-<item>` form that a
    version before the installation id left behind."""
    return re.compile(re.escape(HELPER_PREFIX) + r"(?:" + re.escape(install_id()) + r"-)?\d+")


async def remove_stale_helpers() -> list[str]:
    """Remove helper containers left by an earlier API process of THIS installation. Called at start.

    `run_steamcmd` removes its helper in a `finally`, which does not run when the API process
    dies mid-download. Only the exact name pattern with this installation's id is touched (and
    the old name form without one), and only helpers that mount THIS installation's cache: a
    second installation on the same host may be downloading right now. At start no download of
    this process can be running. Best effort: an error is logged and never stops the start.
    """
    ours = f"{cache_dir()}:{STEAM_HOME}"
    pattern = _stale_name_re()
    try:
        names = await docker.container_names(HELPER_PREFIX)
    except Exception:  # noqa: BLE001
        log.warning("steamcmd: could not look for leftover helper containers", exc_info=True)
        return []
    removed = []
    for name in sorted(n for n in names if pattern.fullmatch(n)):
        try:
            info = await docker.inspect(name) or {}
            if ours not in ((info.get("HostConfig") or {}).get("Binds") or []):
                continue
            await docker.remove(name, force=True)
            removed.append(name)
            log.info("steamcmd: removed leftover helper container %s", name)
        except Exception:  # noqa: BLE001
            log.warning("steamcmd: could not remove leftover helper %s", name, exc_info=True)
    return removed


async def download_item(wid: str, server, timeout: float = DOWNLOAD_TIMEOUT_S) -> dict:
    """Download a workshop item via the steamcmd helper and mirror it into the server's
    workshop folder.

    Returns {ok: bool, log: str, path?: str}. ok=False on any error.
    """
    from . import game_settings

    if not str(wid).isdigit():
        return {"ok": False, "log": "Invalid workshop ID (must be numeric)."}
    if not await asyncio.to_thread(game_settings.is_active, server.game_type):
        return {"ok": False, "log": "This game is not active on this installation (Settings → Games)."}

    async with _lock():
        problem = await _ensure_image(settings.steamcmd_image)
        if problem:
            return {"ok": False, "log": problem}
        try:
            code, text = await run_steamcmd(PZ_APP_ID, str(wid), timeout)
        except Exception as e:  # noqa: BLE001 — cache folder, proxy or Docker: the UI gets the reason
            return {"ok": False, "log": f"Could not run the steamcmd helper: {e}"}

    if code is None:
        return {"ok": False, "log": f"Timed out ({int(timeout)}s) while downloading via steamcmd."}
    match = _DL_RE.search(text)
    if code != 0 or not match:
        return {"ok": False, "log": text[-1500:]}

    src = host_path(match.group(1), cache_dir())
    if src is None or not await asyncio.to_thread(os.path.isdir, src):
        return {"ok": False, "log": f"Downloaded folder is missing: {match.group(1)}\n{text[-800:]}"}
    # `host_path` checks the path as TEXT; a folder on the way could still be a symlink the
    # helper planted. Resolved, the item must still lie inside the cache.
    cache_real = os.path.realpath(cache_dir())
    if not os.path.realpath(src).startswith(cache_real + os.sep):
        return {"ok": False, "log": f"The downloaded folder lies outside the steamcmd cache: {match.group(1)}"}

    # Mirror into the workshop folder the server reads — locking and sequence live in
    # `mirror_into_workshop`.
    dst = os.path.join(server_content_dir(server), str(wid))
    error = await mirror_into_workshop(str(wid), src, dst)
    if error:
        return {"ok": False, "log": error}

    return {"ok": True, "log": text[-600:], "path": dst}
