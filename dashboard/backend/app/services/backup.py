"""World/config backups (saves + server files) as tar.gz, with schedule, retention and restore.

Data directory and world folders come from the caller (game-specific via `drivers.BackupSpec`);
`world_subdirs` defaults to `("Saves","Server")`. The pack/read functions stay SYNCHRONOUS;
thread-pool execution deliberately happens at the call sites (`asyncio.to_thread`).
The only exception is `scheduled_backup()`: it IS a call site (the scheduler job) and fans out
over the registry, so it is async and calls `to_thread` itself.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
import os
import posixpath
import re
import shutil
import tarfile
import threading
from collections.abc import Callable

from ..config import settings
from ..errors import err
from . import servers as servers_registry
from .drivers.base import QuiesceWarning

log = logging.getLogger("uvicorn.error")

# Our own naming scheme: YYYYMMDD-HHMMSS-<label>.tar.gz; reject everything else.
_NAME_RE = re.compile(r"^\d{8}-\d{6}-[A-Za-z0-9._-]+\.tar\.gz$")


def valid_backup_name(name: str) -> bool:
    """Defence in depth: accept only our own backup file names (no traversal or junk)."""
    return bool(_NAME_RE.fullmatch(name))


def _base(data_dir: str | None) -> str:
    # There is no default server any more: every caller names the data directory it means.
    if data_dir is None:
        raise ValueError("data_dir is required")
    return data_dir


def _incident(server, kind: str, detail: str) -> None:
    """Record an incident, same pattern as `restarts._incident`.

    Only for conditions that concern the SERVER, not the archive: a failed `save-on` leaves
    autosave off permanently, and nobody would learn of it otherwise.
    """
    from sqlmodel import Session

    from ..db import engine
    from ..models import ServerIncident

    try:
        with Session(engine) as s:
            s.add(ServerIncident(kind=kind, detail=detail[:500], server_id=server.id))
            s.commit()
    except Exception:
        log.warning("backup: incident could not be saved", exc_info=False)


def backup_dir(data_dir: str | None = None) -> str:
    base = _base(data_dir)
    path = os.path.join(base, "backups")
    os.makedirs(path, exist_ok=True)
    return path


# One lock per DATA DIRECTORY around packing. NOT `world_lock`: that one protects the
# world-WRITING operations and is `asyncio`, while `create_backup` runs in the thread pool.
#
# Two concurrent runs over the same world tear each other apart: Minecraft's `pre_hook` issues
# `save-off` + `save-all flush`, the `post_hook` `save-on`. When run 1 finishes its archive, its
# `post_hook` turns autosave back on in the middle of run 2's `tarfile.open`. From then on run 2
# packs a world that is being written and stores the result as a regular, successful backup.
# A second click after a client-side timeout is enough to get there.
#
# **`create_backup` takes the lock ITSELF**, so every caller is covered: the manual backup, the
# weekly run, the mandatory backups before mod apply and scheduled restart, the config backup
# and `lifecycle.stop_and_snapshot` (and thus restore, version change and MC/VS mod changes).
# A helper with the right rule is useless if callers can bypass it, so the rule sits at the
# choke point.
#
# The key is the **data directory**, not the server key: the directory is the thing that gets
# torn. `RLock` because `create_backup_exclusive` holds the same lock from outside and
# `create_backup` re-acquires it in the same thread.
_pack_locks: dict[str, threading.RLock] = {}
_pack_locks_guard = threading.Lock()

# Generous but finite: a ~4 GB Minecraft world takes minutes. Anyone waiting longer is waiting
# for something that hangs, and a backup that blocks forever may keep a stopped server down
# (`stop_and_snapshot` stops BEFORE backing up).
PACK_TIMEOUT_S = 1800.0


# Which data directories are being packed RIGHT NOW. Tracked explicitly next to the lock so the
# question "is a tar running there?" can be answered from outside and from any thread.
_in_progress: set[str] = set()


def _pack_key(data_dir: str) -> str:
    return os.path.normcase(os.path.abspath(data_dir))


def _pack_lock(data_dir: str) -> threading.RLock:
    key = _pack_key(data_dir)
    with _pack_locks_guard:
        return _pack_locks.setdefault(key, threading.RLock())


def is_packing(data_dir: str) -> bool:
    """Is a `tarfile` run in progress for this data directory RIGHT NOW?

    `scheduled_backup` asks `world_lock.held_by()` before each server and skips it with an
    incident if a world-writing operation is running, because otherwise "a regularly reported
    archive of an intermediate state" would result. This function serves the opposite
    direction: idle stop, scheduled restart and watchdog autoheal must not stop the server
    while its folder is being packed. `lifecycle._stop_locked` shuts the game down through its
    own console, and PZ then writes the COMPLETE world while `tarfile` reads exactly those
    files.

    **Not answered via the lock itself.** `RLock.acquire(blocking=False)` would be wrong
    because `RLock` is reentrant: from the same thread it would report "free" while packing is
    in progress. In practice `tarfile` runs via `asyncio.to_thread` in a different thread than
    the asking jobs, but that is the kind of assurance that only holds until someone
    restructures the concurrency. Hence a separate, explicit marker.
    """
    with _pack_locks_guard:
        return _pack_key(data_dir) in _in_progress


def create_backup_exclusive(data_dir: str, work: Callable[[], dict]) -> dict:
    """Run `work`, but at most once at a time over the same data directory.

    Raises `RuntimeError` if a run is already in progress; the routers translate that into 409.
    Deliberately non-waiting: a second archive of the same state helps nobody, and the person
    at the button should get an answer instead of a spinner. The background paths wait instead:
    nobody is watching them, and a missed weekly backup is only noticed when it is needed.
    """
    lock = _pack_lock(data_dir)
    if not lock.acquire(blocking=False):
        raise RuntimeError("A backup is already running for this server.")
    with _pack_locks_guard:
        _in_progress.add(_pack_key(data_dir))
    try:
        return work()
    finally:
        with _pack_locks_guard:
            _in_progress.discard(_pack_key(data_dir))
        lock.release()


def create_backup(
    label: str = "manual",
    *,
    data_dir: str | None = None,
    world_subdirs: tuple[str, ...] = ("Saves", "Server"),
    config_subdirs: tuple[str, ...] = (),
    pre_hook: Callable[[], None] | None = None,
    post_hook: Callable[[], None] | None = None,
    server=None,
) -> dict:
    """Pack one archive, with quiesce pre- and post-hook under the pack lock.

    **This function needs `config_subdirs` not for packing but for checking.** Everything from
    `world_subdirs` is packed as before; the argument only says which part of it is
    *configuration only* and therefore does NOT count as world content. Without it the guard
    below would be blunt: Valheim's three moderation lists are part of the archive, and a run
    in which only they arrive (`worlds_local` gone) would look like a valid backup.
    `tests/test_mandatory_backup_knows_its_server.py` pins down that every caller passes it.

    **`server` is optional and still the point.** For Minecraft a failed quiesce hook means the
    autosave of the RUNNING server stays off until someone restarts it: silent data loss that
    has nothing to do with the backup itself any more. A `quiesce_warning` in the result is
    only visible if the caller reads it, and in the worst case (the pre-hook raises) there is
    no result to read at all.

    So this function records the incident itself as soon as it knows which server it is
    packing for. `tests/test_mandatory_backup_knows_its_server.py` pins down that every caller
    passes it.
    """
    base = _base(data_dir)
    # The lock spans pre_hook, tar AND post_hook: the damage described above is precisely that
    # one run's post_hook falls into the other run's tar.
    lock = _pack_lock(base)
    if not lock.acquire(timeout=PACK_TIMEOUT_S):
        raise RuntimeError(
            f"Another backup of {base} has been running for more than {PACK_TIMEOUT_S / 60:.0f} minutes — "
            f"not packing in between."
        )
    try:
        return _create_backup_locked(
            label, base=base, world_subdirs=world_subdirs, config_subdirs=config_subdirs,
            pre_hook=pre_hook, post_hook=post_hook, server=server,
        )
    finally:
        lock.release()


def _create_backup_locked(
    label: str,
    *,
    base: str,
    world_subdirs: tuple[str, ...],
    config_subdirs: tuple[str, ...] = (),
    pre_hook: Callable[[], None] | None = None,
    post_hook: Callable[[], None] | None,
    server=None,
) -> dict:
    quiesce_note = ""
    quiesce_structural = False
    packed: list[str] = []
    result: dict | None = None
    incident_recorded = False
    # **The post-hook encloses the pre-hook.** Minecraft's pre-hook consists of TWO steps
    # (`save-off`, then `save-all flush`); if the second fails, autosave is already off, and
    # with `pre_hook()` outside the `try` the exception would leave the function without
    # `save-on` ever running. The server would stop saving until someone restarts it.
    #
    # If the pre-hook fails, **no archive** is created: a snapshot of a world that is being
    # written is a torso that looks like a backup.
    try:
        if pre_hook:
            try:
                pre_hook()
            except QuiesceWarning as e:
                # Explicitly marked as "archive still usable" (Vintage Story). Everything else
                # propagates and prevents the archive, as intended.
                #
                # **The incident belongs here as well.** A warning that only lives in the
                # result dict is invisible unless the caller reads it, e.g. a failed VS save
                # point before mod apply, version change, restore or scheduled restart.
                quiesce_note = str(e)
                # **The distinction must reach the CALLER.** The branch below deliberately
                # records no incident for `structural`; `scheduled_backup` must know that too,
                # or it would record one as soon as a `quiesce_warning` is in the result.
                quiesce_structural = bool(getattr(e, "structural", False))
                if quiesce_structural:
                    # **No incident: nothing failed here.** Valheim has no external save
                    # command, the hook can never succeed in principle and only carries a
                    # notice text. Recorded as a fault, that would yield a red incident line
                    # per weekly run, mod apply and version change, even with the container
                    # stopped. The notice stays in the result and the log so nobody assumes
                    # consistency that does not exist; an incident would be noise that hides
                    # the one real quiesce failure.
                    log.info("backup[%s]: %s", label, e)
                else:
                    log.error("backup[%s]: %s", label, e)
                    _quiesce_incident(server, label, f"save point incomplete: {e}")
                    incident_recorded = server is not None
            except Exception as e:
                _quiesce_incident(server, label, f"pre-hook failed: {e}")
                raise
        result = _pack(label, base=base, world_subdirs=world_subdirs,
                           config_subdirs=config_subdirs, packed=packed,
                           quiesce_note=quiesce_note, server=server)
        if incident_recorded:
            # So that `scheduled_backup` does not record a second, identical incident.
            result["incident_recorded"] = True
        if quiesce_structural:
            # And so that it records none at all: nothing failed here (see above).
            result["quiesce_structural"] = True
    finally:
        if post_hook:
            # The post-hook must not discard a FINISHED archive, but it must not stay silent
            # either. If MC's `save-on` fails, the running server's autosave stays **off
            # permanently** until someone restarts it.
            try:
                post_hook()
            except Exception as e:
                log.error("backup[%s]: post-hook failed: %s", label, e)
                _quiesce_incident(
                    server, label,
                    f"post-hook failed: {e}. For Minecraft this leaves autosave OFF "
                    f"until the server is restarted.",
                )
                if result is not None and server is not None:
                    result["incident_recorded"] = True
                # The caller STILL gets it in the result: the incident is the second channel,
                # not a replacement. `result` is `None` if packing failed; then the
                # exception propagates anyway and there is nothing to add to.
                if result is not None:
                    result["quiesce_warning"] = "; ".join(
                        filter(None, [result.get("quiesce_warning"), str(e)])
                    )
    return result


def _quiesce_incident(server, label: str, text: str) -> None:
    """Record a failed quiesce hook as an incident, immediately and not via the caller.

    The `quiesce_warning` in the result is not enough: few callers read it, and in the worst
    case (the pre-hook raises) it does not exist at all.
    """
    if server is None:
        return
    # **No blanket sentence about autosave here.** If `save-off` itself fails (RCON dead),
    # autosave was never off, so the caller supplies the accurate text.
    _incident(server, "backup_quiesce_failed", f"Backup '{label}': {text}")


def _pack(label, *, base, world_subdirs, packed, quiesce_note, server, config_subdirs=()) -> dict:
    ts = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d-%H%M%S")
    name = f"{ts}-{label}.tar.gz"
    path = os.path.join(backup_dir(base), name)
    # **Packing uses a working name; only the last step assigns the real one.**
    #
    # If `tarfile` wrote directly to the final name, cleaning up a torso would depend solely on
    # the `except BaseException` below, which never runs on SIGKILL. That happens in normal
    # operation: `scheduler.shutdown(wait=False)` waits for no running job, a container stop
    # grants about 10 s by default, and a 4 GB Minecraft tar takes minutes. A restart during
    # the run would leave half a `.tar.gz` under a valid name; `list_backups` sorts descending
    # and `routers/server` uses `backups[0]` as `last_backup`, so the torso would be shown as
    # the **latest backup**, and `prune_backups` would additionally protect it from deletion
    # via `keep_min`.
    #
    # `.part` falls through every filter in this module (all check `.endswith(".tar.gz")`), so
    # it shows up neither in the list nor in the prune. `os.replace` is atomic on the same
    # file system: the archive either exists completely or not at all.
    work_name = path + ".part"
    try:
        with tarfile.open(work_name, "w:gz") as tar:
            for sub in world_subdirs:
                full = os.path.join(base, sub)
                # **Single FILES as well.** With only `isdir` here, an entry that is not a
                # directory would be skipped silently. Valheim's `adminlist.txt`,
                # `bannedlist.txt` and `permittedlist.txt` are exactly that.
                if os.path.isdir(full) or os.path.isfile(full):
                    tar.add(full, arcname=sub)
                    packed.append(sub)
        # **Not a single world folder is not a backup.** Missing folders are deliberately
        # skipped (Reforger's `save-slots` only exists after the first save), but if ALL are
        # missing, the result is a valid, empty 51-byte tar.gz that would be reported as
        # success. The damage happens on the way back: `_rollback_swap` calls `extract_backup`
        # without `subdirs`, an empty archive extracts without an exception, `world_restored`
        # becomes `True`, and the operation reports "rolled back" while the world remains
        # unchanged in the new version's state. The same applies to a full restore.
        #
        # An empty archive is worse than the PARTIAL one that the `except BaseException` below
        # removes, because it looks intact.
        #
        # **What counts is world content, not the mere number of entries.** A plain
        # `if not packed` is not enough: Valheim also passes its three moderation lists, so a
        # run without `worlds_local` but with the three text files would pass, 300 bytes that
        # look like a way back.
        world = [s for s in packed if s not in set(config_subdirs)]
        if not world:
            expected = [s for s in world_subdirs if s not in set(config_subdirs)] or list(world_subdirs)
            raise RuntimeError(
                f"Not a single world folder found ({', '.join(expected)}) under {base} — "
                f"an archive without world content would not be a way back, it would only look like one."
            )
    except BaseException:
        # Remove a partial archive (disk full / I/O error). The branch stays important because
        # it cleans up immediately instead of at the next prune, but thanks to the working
        # name it is not the only safeguard: what it does not catch (SIGKILL) no longer
        # carries a valid name.
        try:
            os.remove(work_name)
        except OSError:
            pass
        raise

    os.replace(work_name, path)   # the archive exists from here on, not before

    result = {"name": name, "size": os.path.getsize(path)}
    if quiesce_note:
        result["quiesce_warning"] = quiesce_note
    return result


def list_backups(*, data_dir: str | None = None) -> list[dict]:
    bdir = backup_dir(data_dir)
    result = []
    for fname in sorted(os.listdir(bdir), reverse=True):
        if fname.endswith(".tar.gz"):
            full = os.path.join(bdir, fname)
            try:
                stat = os.stat(full)
            except OSError:
                # There is a window between `listdir` and `stat`, and it is hit regularly in
                # operation: `prune_backups` deletes in the same directory, and the weekly run
                # calls it right after packing. Unprotected, the WHOLE list would end in a 500
                # instead of returning the remaining archives.
                continue
            result.append(
                {
                    "name": fname,
                    "size": stat.st_size,
                    "created": dt.datetime.fromtimestamp(stat.st_mtime, dt.timezone.utc).isoformat(),
                }
            )
    return result


def prune_backups(
    keep_days: int = 90,
    *,
    data_dir: str | None = None,
    max_bytes: int | None = None,
    keep_min: int = 2,
) -> int:
    """Delete old backups, by age AND, if set, by total size.

    Age alone is enough while archives are small (~40 MB for Project Zomboid); a Minecraft
    world yields **4 GB** per archive, and 90 days of those can exceed the free disk space. A
    retention rule that fills the disk protects nothing: it brings down the host, and first of
    all the game server that is trying to write.

    Deletion goes from OLD to NEW. `keep_min` archives always stay, even if they exceed the
    limit: a rule that may delete the last backup is worse than none.
    """
    bdir = backup_dir(data_dir)
    # Leftover working files of an aborted run. They are not backups and no list shows them,
    # but without these lines they would stay on disk forever, and with a 4 GB world that is
    # not a rounding error.
    now = dt.datetime.now(dt.timezone.utc).timestamp()
    for fname in os.listdir(bdir):
        if not fname.endswith(".tar.gz.part"):
            continue
        full = os.path.join(bdir, fname)
        try:
            if now - os.stat(full).st_mtime > 24 * 3600:
                os.remove(full)
                log.warning("backup: removed leftover working file: %s", fname)
        except OSError:
            pass

    files = []
    for fname in os.listdir(bdir):
        if not fname.endswith(".tar.gz"):
            continue
        full = os.path.join(bdir, fname)
        try:
            st = os.stat(full)
        except OSError:
            continue
        files.append((st.st_mtime, st.st_size, full))
    files.sort()  # oldest first

    cutoff = dt.datetime.now(dt.timezone.utc).timestamp() - keep_days * 86400
    removed = 0

    def _remove(entry) -> bool:
        nonlocal removed
        try:
            os.remove(entry[2])
        except OSError:
            return False
        removed += 1
        return True

    # 1) by age, but never below keep_min
    for entry in list(files):
        if len(files) <= keep_min:
            break
        if entry[0] < cutoff and _remove(entry):
            files.remove(entry)

    # 2) by total size
    if max_bytes is not None and max_bytes > 0:
        while len(files) > keep_min and sum(d[1] for d in files) > max_bytes:
            oldest = files[0]
            if not _remove(oldest):
                break
            files.remove(oldest)
        rest = sum(d[1] for d in files)
        if rest > max_bytes:
            # Not silently: the limit is exceeded because keep_min takes precedence.
            log.warning(
                "backup retention %s: %.1f GB despite a limit of %.1f GB — %d archives are kept "
                "as the minimum stock. Raise the limit or check the world size.",
                bdir, rest / 1e9, max_bytes / 1e9, len(files),
            )
    return removed


def backup_path(name: str, *, data_dir: str | None = None) -> str:
    return os.path.join(backup_dir(data_dir), os.path.basename(name))


# Suffix of a world folder set aside while its archived version is unpacked (`extract_backup`).
_ASIDE_SUFFIX = ".restore-old-"


def _archive_units(names: list[str]) -> list[str]:
    """The entries the archive packed as a whole: members whose parent is not a member itself.

    `_pack` adds every world folder with `tar.add(full, arcname=sub)`, which writes `sub` and its
    content but no parent entries. So `vs/Saves` (Vintage Story) is one unit and its sibling
    `vs/serverconfig.json` is none of the archive's business.
    """
    members = {n.rstrip("/") for n in names}
    return sorted(n for n in members if n and posixpath.dirname(n) not in members)


def _remove(path: str) -> None:
    if os.path.isdir(path) and not os.path.islink(path):
        shutil.rmtree(path)
    elif os.path.lexists(path):
        os.remove(path)


def _put_back(moved: list[tuple[str, str]]) -> None:
    """Undo `_set_aside`: drop what was unpacked so far, rename the old version back."""
    for target, aside in reversed(moved):
        try:
            _remove(target)
            os.rename(aside, target)
        except OSError:
            log.error("backup: could not put %s back — the previous version is at %s",
                      target, aside, exc_info=True)


def _set_aside(base: str, units: list[str]) -> list[tuple[str, str]]:
    """Rename every existing unit to `<unit>.restore-old-<timestamp>`. Instant, no copy."""
    ts = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d-%H%M%S")
    moved: list[tuple[str, str]] = []
    try:
        for unit in units:
            target = os.path.join(base, unit)
            if not os.path.lexists(target):
                continue
            aside = f"{target}{_ASIDE_SUFFIX}{ts}"
            if os.path.lexists(aside):
                raise FileExistsError(f"{aside} already exists — nothing was changed")
            os.rename(target, aside)
            moved.append((target, aside))
    except BaseException:
        _put_back(moved)
        raise
    return moved


def extract_backup(
    name: str, *, data_dir: str | None = None, subdirs: tuple[str, ...] | None = None,
) -> list[str]:
    """Restore an archive: every folder (or file) it holds REPLACES the current one.

    Not an overlay: a plain `tar.extractall` would reset the archived files and keep everything
    created since (`map/<x>/<y>.bin`, `chunkdata_*`, new `r.x.z.mca` for Minecraft) — a world
    from two points in time, reported as restored. That affected the restore and the rollback
    of a failed version change (`version_manager._rollback_swap`) alike.

    Replacing is done without ever leaving an empty world: each unit of the archive
    (`_archive_units`, or the requested `subdirs`) is first RENAMED aside, then the archive is
    unpacked, and only after that the set-aside copy is deleted. If unpacking fails (disk full,
    broken archive), the partial result is removed and the old version renamed back. The price
    is disk space: during the restore the old and the restored version exist side by side.
    Anything outside the archive's units (other folders in the data directory, `backups/`)
    stays untouched.

    Returns the set-aside copies that could NOT be deleted afterwards (root-owned files a game
    wrote, for instance): the restore succeeded, but each still takes the space of a world until
    someone removes it, so the caller has to say where it is.
    """
    base = _base(data_dir)
    path = backup_path(name, data_dir=data_dir)
    if not os.path.exists(path):
        raise FileNotFoundError(name)
    with tarfile.open(path, "r:gz") as tar:
        members = tar.getmembers()
        if subdirs:
            wanted = tuple(s.strip("/") for s in subdirs if s.strip("/"))
            members = [
                m for m in members
                if any(m.name == w or m.name.startswith(w + "/") for w in wanted)
            ]
            if not members:
                raise FileNotFoundError(f"{name}: contains none of the folders {wanted}")
            units = [w for w in wanted if any(m.name == w or m.name.startswith(w + "/") for m in members)]
        else:
            units = _archive_units([m.name for m in members])
        moved = _set_aside(base, units)
        try:
            tar.extractall(base, members=members, filter="data")
        except BaseException:
            _put_back(moved)
            raise
    left: list[str] = []
    for _target, aside in moved:
        try:
            _remove(aside)
        except OSError:
            # The restore itself succeeded; the old copy only costs space.
            log.warning("backup: the replaced version at %s could not be deleted — remove it "
                        "by hand", aside, exc_info=True)
            left.append(aside)
    return left


def missing_parts(name: str, subdirs: tuple[str, ...], *, data_dir: str | None = None) -> list[str]:
    """Which of the requested top-level folders the archive does NOT contain, same rule as `extract_backup`.

    A separate function because the answer is needed BEFORE the stop: `extract_backup` only
    notices once the server is long stopped and the safety archive is written. And not via
    `backup_contents`: that only returns the topmost folders, but Vintage Story backs up under
    `vs/Saves`, so `parts=vs/Saves` would wrongly be reported as missing although
    `extract_backup` restores it without complaint.
    """
    path = backup_path(name, data_dir=data_dir)
    wanted = [s.strip("/") for s in subdirs if s.strip("/")]
    with tarfile.open(path, "r:gz") as tar:
        names = tar.getnames()
    return [w for w in wanted if not any(n == w or n.startswith(w + "/") for n in names)]


def check_restorable(name: str, *, data_dir: str | None = None,
                       world_subdirs: tuple[str, ...] = (),
                       subdirs: tuple[str, ...] | None = None) -> None:
    """Can this archive be restored? Raises 400 if not, **before** the stop.

    Without this check a truncated or empty archive is only noticed in the middle of
    extraction, when the server is already stopped and the safety archive is written. The full
    restore, which overwrites the whole world, needs the check even more than the partial one.

    Two rules, depending on the path:

    * **with `subdirs`** (partial restore): every named part must be present (the
      `missing_parts` rule).
    * **without `subdirs`** (full restore): **at least one** of the `world_subdirs` must be
      present, not all. Same reasoning as in `create_backup`: Reforger's `save-slots` only
      exists after the first save and may legitimately be missing. An archive without ANY world
      folder, however, is not a way back; it only looks like one.

    A broken archive raises a clean 400 here instead of a raw `tarfile` error.
    """
    from fastapi import HTTPException, status as http

    path = backup_path(name, data_dir=data_dir)
    try:
        with tarfile.open(path, "r:gz") as tar:
            names = tar.getnames()
    except (OSError, tarfile.TarError, EOFError) as e:
        raise HTTPException(
            http.HTTP_400_BAD_REQUEST,
            detail=err("backup.archive_unreadable",
                       "The archive {name} cannot be read ({error}). Nothing was touched.",
                       name=name, error=str(e)),
        ) from e

    def _included(wanted: str) -> bool:
        w = wanted.strip("/")
        return bool(w) and any(n == w or n.startswith(w + "/") for n in names)

    if subdirs:
        missing = [s for s in subdirs if s.strip("/") and not _included(s)]
        if missing:
            tops = sorted({n.split("/", 1)[0] for n in names if n.split("/", 1)[0]})
            raise HTTPException(
                http.HTTP_400_BAD_REQUEST,
                detail=err("backup.parts_not_in_archive",
                           "“{parts}” is not in {name}. The archive contains: {contents}.",
                           parts=", ".join(missing), name=name,
                           contents=", ".join(tops) or "(nothing)"),
            )
        return

    if world_subdirs and not any(_included(w) for w in world_subdirs):
        # **The message states what is ACTUALLY inside.** Without that it reads like "the
        # archive is broken", while the most common cause is a changed path layout: older
        # Vintage Story archives use `Saves/`, newer ones `vs/Saves/`, and Minecraft's
        # `world_subdirs` depend on `level-name`. Both kinds of archive sit in the same folder
        # and both appear in the list. The operator must see that the world folder is there,
        # just under a different name.
        tops = sorted({n.split("/", 1)[0] for n in names if n.split("/", 1)[0]})
        raise HTTPException(
            http.HTTP_400_BAD_REQUEST,
            detail=err("backup.no_world_folder_in_archive",
                       "{name} contains none of the expected world folders ({expected}). The "
                       "archive contains: {contents}. If the path layout no longer matches "
                       "(older archives use a different one), the partial restore can be used. "
                       "Nothing was touched.",
                       name=name, expected=", ".join(world_subdirs),
                       contents=", ".join(tops) or "(nothing)"),
        )


def backup_contents(name: str, *, data_dir: str | None = None) -> list[str]:
    """Topmost folders in the archive (e.g. ["Saves", "Server"]): what can be restored individually."""
    path = backup_path(name, data_dir=data_dir)
    tops: list[str] = []
    try:
        with tarfile.open(path, "r:gz") as tar:
            for m in tar.getmembers():
                top = m.name.split("/", 1)[0]
                if top and top not in tops:
                    tops.append(top)
    except (OSError, tarfile.TarError):
        return []
    return tops


async def _cleanup(spec) -> int:
    """Prune old archives by age AND size.

    A separate function because it is needed in two places: after a successful pack and after
    a failed one. The second call is the more important one, see `scheduled_backup`.
    """
    return await asyncio.to_thread(
        prune_backups, settings.backup_retention_days,
        data_dir=spec.data_dir,
        max_bytes=int(settings.backup_max_gb_per_server * 1e9),
        keep_min=settings.backup_keep_min,
    )


async def backup_with_stop(server, drv, label: str, *, announce_text: str) -> dict:
    """**For games without an external save command: stop, back up, start.**

    Valheim has neither console nor stdin; its quiesce hook can never save in principle and says
    so (`BackupSpec.stop_for_consistency`). Without the stop the archive is a copy of a world that
    is being written, or, before the first autosave, of nothing at all. The weekly run and the
    manual "Back up now" both come through here, so the two cannot drift apart again.

    `stop_and_snapshot` is the same choke point that mod apply, version change and restore go
    through: confirmed stop, and on an error `ensure_restart` brings the server back up. The
    import is here and not at the top because `lifecycle` in turn imports `create_backup`.

    Returns the archive's result plus `stopped` (whether a running server was stopped for it) and,
    when the server did not come back, `restart_error`; that case is also an incident.
    """
    from . import lifecycle

    ran_before, result = await lifecycle.stop_and_snapshot(
        server, drv, label, announce_text=announce_text,
    )
    result = {**result, "stopped": ran_before}
    # **The restart in the success case belongs HERE.** `ensure_restart` only starts if the
    # block raised; for mod apply and version change that is right, they start later
    # themselves. A backup must not leave the server down.
    if ran_before:
        try:
            await lifecycle.start_server(server)
        except Exception as e:
            log.error("%s backup %s: backup ok, but the server did not come back: %s",
                      label, server.key, e, exc_info=True)
            _incident(server, "restart_failed",
                      f"The server could not be started again after the {label} backup: {e}")
            result["restart_error"] = str(e)
    return result


async def restart_with_backup_while_stopped(server, drv, label: str, *, precondition) -> dict | None:
    """**The scheduled restart of a game without an external save command: stop, archive, start.**

    The restart stops the server anyway, so the mandatory backup is taken in that gap instead of
    from the running server. Before, the archive copied a Valheim world while it was being
    written; Valheim only saves on stop and every 20 minutes, so it held the last autosave at
    best. This is the same order as `backup_with_stop`, but the stop and the start ARE the
    restart, not an extra round.

    * The world lock is held from the stop to the start, as for every world operation: a
      restore or mod apply that started during the pack would find the server stopped and
      move world files under the running `tarfile`, and the start below would boot into it.
      A running world operation answers 409 here, before anything is touched.
    * `precondition` is asked INSIDE `lifecycle`'s lock (`stop_and_confirm` → `stop_server`),
      like the `restart_server` path: an idle stop that held the lock may have stopped the
      server on purpose meanwhile. `None` is returned then, and nothing was touched.
    * The stop is confirmed before the pack (`stop_and_confirm`); an unconfirmed one archives
      nothing and leaves the server as it is. No quiesce hooks: the server is stopped.
    * A failed archive does not leave the server down. It was running a moment ago, and a
      backup must not be the reason it stays off (the rule of `backup_with_stop` and
      `stop_and_snapshot`); the failure becomes a `backup_failed` incident and the result
      carries `error`.

    Returns the archive's result, `{"error": …}` when packing failed, or `None` when the restart
    was cancelled. A failed start raises, so the caller records it as a failed restart.
    """
    from . import lifecycle, world_lock

    spec = drv.backup_spec(server)
    async with world_lock.hold(server.key, "restart.backup"):
        was_running, stopped = await lifecycle.stop_and_confirm(server, precondition=precondition)
        if not was_running:
            return None
        if not stopped:
            # Nothing happened: the server still runs, so there is nothing to start either.
            log.warning("%s backup %s: the stop could not be confirmed — no archive, no restart",
                        label, server.key)
            _incident(server, "restart_failed",
                      "Scheduled restart failed: the stop could not be confirmed. The server "
                      "still runs; nothing was archived.")
            return {"error": "the stop could not be confirmed"}
        try:
            result = await asyncio.to_thread(
                create_backup, label, data_dir=spec.data_dir, world_subdirs=spec.world_subdirs,
                config_subdirs=spec.config_subdirs, pre_hook=None, post_hook=None, server=server,
            )
        except Exception as e:
            log.warning("%s backup %s failed — the server is started again without it",
                        label, server.key, exc_info=True)
            _incident(server, "backup_failed",
                      f"Backup before the scheduled restart failed: {e}. The server had been "
                      f"stopped for it and was started again without a new backup.")
            result = {"error": str(e)}
        # The start of the restart, inside the world lock like the stop and through the same
        # Docker client as `lifecycle.ensure_restart`: `lifecycle.start_server` would refuse it,
        # because this restart holds the world lock itself.
        await lifecycle.docker.action(server.container_name, "start")
    return result


async def scheduled_backup() -> list[dict]:
    """Weekly backup + retention, for EVERY enabled server.

    The fan-out uses the same canonical source as watchdog, stats poller and bridge
    (`servers_registry.list_enabled_with_driver()`); the game-specific paths, world folders and
    quiesce hooks come from `driver.backup_spec(server)`. A failing server does not take the
    others down; otherwise the whole night would depend on one broken container.

    `asyncio.to_thread` is **mandatory** here: the Minecraft and Vintage Story hooks call
    `asyncio.run(...)` internally, which raises inside an already running event loop. The hook
    does not swallow that, so in that case no archive is created at all. Pinned down in
    `tests/test_mc_vs_drivers.py::test_mc_quiesce_wirkt_nicht_in_einem_laufenden_loop_und_sagt_es`.
    """
    from . import world_lock

    results: list[dict] = []
    for server, drv in servers_registry.list_enabled_with_driver():
        spec = None   # the error path prunes with it; it can trigger BEFORE the assignment
        try:
            # **Do not pack into a running world operation.** The pack lock only protects
            # packing against packing; a mod apply, a restore or a version change moves world
            # files aside in the meantime, and `create_backup` silently skips whatever has
            # vanished. The result would be a regularly reported archive of an intermediate
            # state that then sits at the top of `list_backups` as the latest one, a torso
            # that invites a restore. After that `prune_backups` might remove an older,
            # complete archive.
            #
            # The lock is QUERIED synchronously (`held_by`), not taken: `apply_draft` and the
            # version change hold the lock and call `create_backup` themselves in the middle,
            # so taking a lock there would deadlock. The other direction is sufficient.
            if (holder := world_lock.held_by(server.key)):
                label = world_lock.OPERATION_LABELS.get(holder, holder)
                log.warning("weekly backup %s: skipped — %s is running", server.key, label)
                _incident(server, "backup_skipped",
                          f"Weekly backup skipped: {label} was running. An archive taken "
                          f"meanwhile would hold an intermediate state of the world. The next "
                          f"backup runs as scheduled; a manual one is possible at any time.")
                results.append({"server": server.key, "skipped": True, "reason": holder})
                continue
            spec = drv.backup_spec(server)
            if spec.stop_for_consistency:
                result = await backup_with_stop(
                    server, drv, "weekly",
                    announce_text="Weekly backup — the server will be down briefly in a moment.",
                )
            else:
                result = await asyncio.to_thread(
                    create_backup, "weekly",
                    data_dir=spec.data_dir, world_subdirs=spec.world_subdirs,
                    config_subdirs=spec.config_subdirs,
                    pre_hook=spec.pre_hook, post_hook=spec.post_hook, server=server,
                )
            removed = await _cleanup(spec)
            log.info(
                "weekly backup %s: %s (%.0f MB), %d old ones removed",
                server.key, result["name"], result["size"] / 1e6, removed,
            )
            # `create_backup` records the incident itself as soon as it knows the server; here
            # only for the `QuiesceWarning` that it deliberately does NOT treat as a failure
            # (Vintage Story: archive still usable). Without this distinction each failure
            # would produce two identical incidents.
            if (result.get("quiesce_warning") and not result.get("incident_recorded")
                    and not result.get("quiesce_structural")):
                # The archive is fine, the SERVER is not: for Minecraft a failed `save-on`
                # means autosave stays off until the next restart. That belongs visibly in the
                # dashboard, not just in a log line at four in the morning.
                _incident(server, "backup_quiesce_failed", str(result["quiesce_warning"]))
            results.append({"server": server.key, **result, "pruned": removed})
        except Exception as e:
            # **Pruning does not depend on packing.** If pruning only ran after a successful
            # pack, an ENOSPC failure would never be cleaned up and the state would be stably
            # wrong: from then on every mandatory backup fails too, and mod apply, restore and
            # version change are blocked. The way out of the full disk would sit behind the
            # very condition the full disk prevents.
            #
            # The order stays: pack first, then prune. Pruning first would throw away ways
            # back before the new one exists.
            try:
                if spec is not None:
                    await _cleanup(spec)
            except Exception as cleanup_error:
                log.error("weekly backup %s: pruning after the failure failed as well: %s",
                          server.key, cleanup_error)
            # **The more severe case needs at least the visibility of the milder one.** Above,
            # `backup_quiesce_failed` records an incident although the archive EXISTS and is
            # explicitly usable. Here no archive is created at all, so a log line plus an entry
            # in `results` (the return value of a scheduler cron job that nobody receives) is
            # not enough.
            #
            # The weekly run is the ONLY backup job in the backend (`main.py`) and fans out
            # over every enabled server. Whether a server is backed up beyond that depends on
            # `backup_first` of a restart schedule row, i.e. on data, not on this file. If the
            # run fails, it fails for everyone, and without an incident that would only become
            # visible when someone needs a way back.
            log.error("weekly backup %s failed: %s", server.key, e, exc_info=True)
            _incident(server, "backup_failed",
                      f"The weekly backup failed — no archive was created: {e}")
            results.append({"server": server.key, "error": str(e)})
    return results
