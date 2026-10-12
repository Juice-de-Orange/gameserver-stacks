"""Apply mod changes as a transaction that either succeeds completely or is rolled back in full.

Flow and order are deliberately taken from `services/version_manager.py`; the pattern has
proven itself for version changes:

```
lock per server             → a second call gets a 409 instead of a collision
preflight                   → blocker ⇒ abort, NOTHING has happened
announcement + save         → players warned, world flushed
watchdog.suppress           → otherwise the watchdog restarts into the long mod boot
stop
mandatory backup            → if it fails: start the server again, abort
back up .ini + spawnregions → way back without archive surgery
world cells into quarantine → MOVE, never delete
write
start + verify the boot     → RCON response as proof
   error ⇒ everything back, the server runs as before
```

The **world quarantine** is the most delicate part. Hence: move instead of delete, a manifest
for the way back, and `isoregiondata/` goes along as a whole (see `cells.py`: its grid could
not be mapped to cells, and a derived cache may be rebuilt).
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import os
import re
import shutil

from fastapi import HTTPException, status as http

from ...models.mod_set import ModChange
from ...db import engine
from ...errors import err, note
from .. import ini_editor as ini
from .. import lifecycle
from .. import watchdog
from .. import world_lock
from ..backup import create_backup
from ..docker_client import docker
from ..drivers import get_driver
from ..version_manager import verify_boot
from . import inventory, preflight, state

from sqlmodel import Session

log = logging.getLogger("uvicorn.error")

QUARANTINE_DIRNAME = "quarantine"
BOOT_TIMEOUT = 900.0
SUPPRESS_MINUTES = 20

# Our own naming scheme, mirrored from `_new_quarantine`: YYYYMMDD-HHMMSS-<label>.
# Checked positively instead of filtered negatively; see `_quarantine_path`.
_QUARANTINE_NAME_RE = re.compile(r"^\d{8}-\d{6}-[A-Za-z0-9._-]+$")



# --------------------------------------------------------------------- quarantine

def quarantine_root(server) -> str:
    return os.path.join(server.data_dir, "data", QUARANTINE_DIRNAME)


def _new_quarantine(server, label: str) -> tuple[str, str]:
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d-%H%M%S")
    name = f"{stamp}-{label}"
    path = os.path.join(quarantine_root(server), name)
    os.makedirs(os.path.join(path, "world"), exist_ok=True)
    os.makedirs(os.path.join(path, "config"), exist_ok=True)
    return name, path


def _stash_world(save_dir: str, rel_paths: list[str], qpath: str) -> list[str]:
    """MOVE world files into the quarantine. Returns the paths actually moved."""
    moved: list[str] = []
    for rel in rel_paths:
        src = os.path.join(save_dir, rel)
        dst = os.path.join(qpath, "world", rel)
        try:
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.move(src, dst)
            moved.append(rel)
        except OSError as e:
            log.warning("mods: quarantine could not move %s: %s", rel, e)
    return moved


def _restore_world(save_dir: str, qpath: str, rel_paths: list[str]) -> int:
    """Move the quarantine back (rollback). Returns the number of files brought back."""
    back = 0
    for rel in rel_paths:
        src = os.path.join(qpath, "world", rel)
        dst = os.path.join(save_dir, rel)
        try:
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.move(src, dst)
            back += 1
        except OSError as e:
            log.warning("mods: quarantine could not bring %s back: %s", rel, e)
    return back


def planned_downloads(server, keep: set[str]) -> list[str]:
    """Which downloads are dropped; **asked before the first file is moved**.

    Separate from the move so that the quarantine manifest can already exist when the first
    `shutil.move` runs: `list_quarantines` and `restore_quarantine` read only the manifest, and
    an operation dying in between (e.g. a backend restart during the apply) would otherwise
    leave a folder full of files that the UI shows as empty.
    """
    base = inventory.content_dir(server)
    if not os.path.isdir(base):
        return []             # fresh server without downloads: really nothing to do
    # **An unreadable directory is not "nothing to do".** An empty list would be read by the
    # caller as "there was nothing to move", which is a claim, not a measurement.
    try:
        present = sorted(os.listdir(base))
    except OSError as e:
        raise RuntimeError(
            f"The download directory {base} cannot be read ({e}) — it cannot be determined "
            f"which mod downloads would have to go."
        ) from e
    return [wid for wid in present
            if wid.isdigit() and wid not in keep and os.path.isdir(os.path.join(base, wid))]


def _stash_downloads(server, wids: list[str], qpath: str) -> list[str]:
    """Move the planned downloads into the quarantine.

    Mandatory: the PZ server loads **downloaded** maps automatically, even when they are not
    enabled anywhere. Taking a map mod out of the lists therefore does NOT remove it. Moving
    instead of deleting keeps the way back open (and saves a re-download).
    """
    base = inventory.content_dir(server)
    moved: list[str] = []
    target_root = os.path.join(qpath, "downloads")
    # **Every failure aborts.** The caller does the same for the world files
    # (`if len(moved) != len(world_files): raise`); for the downloads the check has to live
    # here, because the caller does not know the expected set ("everything not in `keep`").
    #
    # What depends on it: `check(..., prune_orphans=True)` suppresses the blocker
    # ORPHAN_MAP_DOWNLOAD on the grounds that this operation clears the downloads away. If a
    # map download stays behind, the PZ server keeps loading it (join crash without warning),
    # and the next live check turns it into a blocker that the boot gate holds against every
    # start: the server would no longer be startable after a change reported as "ok".
    for wid in wids:
        src = os.path.join(base, wid)
        try:
            os.makedirs(target_root, exist_ok=True)
            shutil.move(src, os.path.join(target_root, wid))
            moved.append(wid)
        except OSError as e:
            raise RuntimeError(
                f"The mod download {wid} could not be moved into the quarantine ({e}). "
                f"If it stayed, the server would keep loading it — exactly the state this "
                f"function is built against."
            ) from e
    return moved


def _restore_downloads(server, qpath: str, wids: list[str]) -> list[str]:
    """Bring downloads back from the quarantine. Returns those that were LEFT BEHIND.

    The return value is the point: the caller must fold it into its "complete" verdict.
    `_restore_config` writes the OLD `.ini` back, which still lists the item and its map. If
    the files stay in the quarantine, that is exactly the state for which `preflight.check`
    raises `ITEM_NOT_DOWNLOADED` or `MAP_WITHOUT_PROVIDER` as a BLOCKER, except that the server
    is running (RCON responded) and joining players run into the map crash.
    """
    base = inventory.content_dir(server)
    left_behind: list[str] = []
    for wid in wids:
        src = os.path.join(qpath, "downloads", wid)
        target = os.path.join(base, wid)
        # **`shutil.move` does not merge.** If the target exists as a directory, it puts the
        # source INSIDE it: `content/108600/111` becomes `content/108600/111/111`, without an
        # error. The server would then load half a mod, and `cells.find_map_dirs` would compute
        # against a stale copy when a map name appears twice. Not touching it and being loud is
        # right here: what is already there is the newer state.
        if os.path.exists(target):
            log.error("mods: download %s not brought back — %s already exists", wid, target)
            left_behind.append(wid)
            continue
        try:
            os.makedirs(base, exist_ok=True)
            shutil.move(src, target)
        except OSError as e:
            log.warning("mods: download %s could not be brought back: %s", wid, e)
            left_behind.append(wid)
    return left_behind


def _world_dir_snapshot(save_dir: str, rel_paths: list[str]) -> set[str]:
    """What is in the affected world folders, recorded BEFORE the start with the new configuration.

    Without this snapshot "newly written" cannot be decided: `map/<x>/` also holds chunks of
    uninvolved cells in the same column that have always been there. Comparing against `moved`
    alone would count them as new and report every rollback as incomplete.
    """
    snapshot: set[str] = set()
    for folder in {os.path.dirname(rel) for rel in rel_paths if os.path.dirname(rel)}:
        full = os.path.join(save_dir, folder)
        try:
            for f in os.listdir(full):
                snapshot.add(os.path.join(folder, f).replace("\\", "/"))
        except OSError:
            continue
    return snapshot


def _new_world_files(save_dir: str, moved: list[str], before: set[str]) -> list[str]:
    """World files the failed run wrote NEWLY; measured, not cleaned up.

    `_stash_world` only picks up what exists at the time of the stop; `_restore_world` moves
    exactly that list back. In between, the server runs for up to 900 s with the new
    configuration and writes fresh terrain for the cleared cells: chunks for which there was no
    file before. They are not in `moved`, and `restored != len(moved)` cannot see them.

    **Why only measured and not cleared away:** which of the new files belong to a restored
    cell and which to an uninvolved one in the same column cannot be separated cleanly here,
    and `apply_draft` deliberately restores only the quarantined cells, not the whole world.
    Clearing away something that may be legitimate progress would be worse than an honest
    report.
    """
    new: list[str] = []
    for folder in {os.path.dirname(rel) for rel in moved if os.path.dirname(rel)}:
        full = os.path.join(save_dir, folder)
        try:
            for f in os.listdir(full):
                rel = os.path.join(folder, f).replace("\\", "/")
                if rel not in before and rel not in moved:
                    new.append(rel)
        except OSError:
            continue
    return sorted(new)


# The configuration files a mod operation can write. Whoever adds one registers it HERE; then
# `_stash_config` backs it up and `_restore_config` takes it back.
_CONFIG_PATHS = {
    "ini": ini.ini_path,
    "spawnregions": ini.spawnregions_path,
}


# There is deliberately NO fingerprint check that detects foreign changes to the `.ini` during
# the operation before it is written back:
#
# * **The operation writes the file itself** (`write_plan`, `write_spawnregions`), and the PZ
#   image rewrites `Public`/`Password`/`RCONPassword`/`PublicName` on EVERY boot. The
#   fingerprint would have to be updated after each of these writes, including the ones we do
#   not make.
# * **`restore_quarantine` runs the same function days later** without a `_stash_config` of its
#   own. It would compare against the state of another operation, or against nothing at all
#   after a backend restart.
# * **The false alarm costs more than the gap.** It would report a clean rollback as
#   "incomplete, please check manually" and record `failed` instead of `rolled_back`.
#
# The gap is closed at the root instead: ALL writing config routes ask the world lock
# (`world_lock.refuse_during_world_operation`).


def _stash_config(server, qpath: str) -> dict[str, str]:
    """Copy `.ini` and `spawnregions.lua`: the way back without archive surgery.

    (`extract_backup` is all-or-nothing and would overwrite the whole world on rollback.)

    **Absence is backed up too** (value `""`). `write_spawnregions` writes unconditionally. If
    a server never had map mods (the normal case), there was no `spawnregions.lua` before; the
    rollback has to remove the Lua file created by the failed draft, which it can only do if it
    knows the file did not exist.
    """
    saved: dict[str, str] = {}
    for key, path in _CONFIG_PATHS.items():
        src = path(server)
        if os.path.isfile(src):
            dst = os.path.join(qpath, "config", os.path.basename(src))
            shutil.copy2(src, dst)
            saved[key] = dst
        else:
            saved[key] = ""   # "did not exist": the rollback then removes it again
    return saved


def _restore_config(server, saved: dict[str, str]) -> bool:
    """Write the configuration back from the quarantine. `False` if something was left behind.

    The return value is the point: an `.ini` that still carries the NEW mod list while the
    world was brought back to the old state is not a restored state; it is exactly the mixture
    that does not come up at the next boot.
    """
    ok = True
    for key, src in saved.items():
        path = _CONFIG_PATHS.get(key)
        if path is None:
            continue          # unknown key from an older manifest
        target = path(server)
        try:
            if src:
                shutil.copy2(src, target)
            elif os.path.exists(target):
                # It did NOT exist before. Nothing can be written back, but it can be removed,
                # and that is the way back here.
                os.remove(target)
        except OSError as e:
            log.error("mods: %s could not be restored: %s", target, e)
            ok = False
    return ok


# --------------------------------------------------------------------- writing

# Control characters and quotes break the generated Lua file: a folder name with a line break
# would produce syntactically broken Lua and thus a boot crash.
def _lua_safe(name: str) -> str:
    return "".join(ch for ch in name if ch.isprintable() and ch not in "\\\"")


def write_spawnregions(server, regions: list[str]) -> None:
    """Regenerate `<servername>_spawnregions.lua`, only from regions that really exist."""
    lines = ["function SpawnRegions()", "\treturn {"]
    for name in regions:
        safe = _lua_safe(name)
        if not safe:
            continue
        lines.append(f'\t\t{{ name = "{safe}", file = "media/maps/{safe}/spawnpoints.lua" }},')
    lines.extend(["\t}", "end"])
    ini.write_raw(ini.spawnregions_path(server), "\n".join(lines) + "\n")


def write_plan(server, plan: preflight.ModPlan) -> None:
    """Mods/WorkshopItems/Map in ONE write, so there is no inconsistent intermediate state."""
    ini.set_ini_keys_at(ini.ini_path(server), {
        "WorkshopItems": ";".join(plan.workshop_items),
        "Mods": ";".join(plan.mods),
        "Map": ";".join(plan.maps),
    })


# --------------------------------------------------------------------- history

def _record(server, *, from_id, to_id, backup, quarantine, summary) -> int:
    with Session(engine) as s:
        row = ModChange(
            server_id=server.id, status="pending", from_set_id=from_id, to_set_id=to_id,
            backup_name=backup, quarantine_name=quarantine, summary=summary,
        )
        s.add(row)
        s.commit()
        s.refresh(row)
        return row.id


def _finish(change_id: int, status_: str, detail: str = "") -> None:
    with Session(engine) as s:
        row = s.get(ModChange, change_id)
        if row:
            row.status = status_
            row.detail = detail[:400]
            s.add(row)
            s.commit()


# --------------------------------------------------------------------- managing quarantines

def _dir_size(path: str) -> int:
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(root, name))
            except OSError:
                pass
    return total


def list_quarantines(server) -> list[dict]:
    """All quarantines of this server: what is set aside, how large, since when."""
    root = quarantine_root(server)
    out: list[dict] = []
    try:
        names = sorted(os.listdir(root), reverse=True)
    except OSError:
        return out
    for name in names:
        path = os.path.join(root, name)
        if not os.path.isdir(path):
            continue
        manifest: dict = {}
        try:
            with open(os.path.join(path, "manifest.json"), encoding="utf-8") as f:
                manifest = json.load(f)
        except (OSError, ValueError):
            pass
        out.append({
            "name": name,
            "size": _dir_size(path),
            "world_files": len(manifest.get("world") or []),
            "downloads": manifest.get("downloads") or [],
            "summary": manifest.get("summary") or "",
            "created": os.path.getmtime(path),
        })
    return out


def _write_manifest(qpath: str, *, world, downloads, config, summary: str, state: str,
                       config_absent_before=()) -> None:
    """Write the manifest; twice per operation: planned, then done.

    `state` says which of the two versions is present. If someone later finds a quarantine with
    `"state": "planned"`, the operation died midway: the lists then name what was SUPPOSED to
    be moved. `restore_quarantine` copes with that (it brings back what is there and reports
    the rest); it could not cope with a quarantine without any manifest.
    """
    # `config_absent_before`: configuration files that did NOT exist before the operation and that the
    # rollback therefore has to remove instead of writing back. The key defaults to empty, so
    # older quarantines without it behave as before.
    with open(os.path.join(qpath, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump({"world": list(world), "downloads": list(downloads), "config": list(config),
                   "config_absent_before": list(config_absent_before),
                   "summary": summary, "state": state}, f, ensure_ascii=False, indent=1)


def _quarantine_path(server, name: str) -> str:
    """Path of a quarantine: only names of our own scheme, and never the root directory itself.

    A check of the form `path != root and not path.startswith(root + os.sep)` has a hole worse
    than the `..` it guards against: for `name="."` the resolved path is **exactly** `root`,
    `path != root` is false and the `and` expression short-circuits. `delete_quarantine` runs
    `shutil.rmtree` on the result, so `DELETE …/mods/quarantine/%2E` (uvicorn decodes the
    segment, Starlette passes `.` through unchanged) would delete the rollback material of
    **every** past mod change, with `can_manage_mods`, not admin rights.

    Hence a **positive** name pattern, as `backup.valid_backup_name` uses. It also rules out
    `""`, `" "`, `"./"` and embedded null bytes in one go.
    """
    if not _QUARANTINE_NAME_RE.fullmatch(name or ""):
        raise HTTPException(http.HTTP_400_BAD_REQUEST,
                            detail=err("mods.quarantine_name_invalid", "Invalid quarantine name"))

    root = os.path.realpath(quarantine_root(server))
    path = os.path.realpath(os.path.join(root, name))
    if path == root or not path.startswith(root + os.sep):
        raise HTTPException(http.HTTP_400_BAD_REQUEST,
                            detail=err("mods.quarantine_name_invalid", "Invalid quarantine name"))
    if not os.path.isdir(path):
        raise HTTPException(http.HTTP_404_NOT_FOUND,
                            detail=err("mods.quarantine_not_found", "Quarantine not found"))
    return path


def _check_quarantine_lock(server) -> None:
    """No quarantine operation while an operation that writes the world is running.

    **The running state alone is not enough here; it even points the wrong way.** "Is the
    container running? Then 409" opens up in precisely the dangerous state: a STOPPED container
    is exactly what `restore.py`, `version_manager.py` and `apply_draft` themselves produce
    while they work on the same world for minutes. A click on "restore quarantine" in that
    window would pass the running check and push world cells into a running `extract_backup`.
    """
    if (holder := world_lock.held_by(server.key)):
        raise HTTPException(
            http.HTTP_409_CONFLICT,
            detail=err(
                "mods.quarantine_world_operation_running",
                "This server already has an operation running: {operation} — please wait. "
                "Touching the quarantine in the meantime hits exactly the world that operation "
                "is rebuilding.",
                operation=world_lock.OPERATION_LABELS.get(holder, holder),
            ),
        )


async def restore_quarantine(server, name: str) -> dict:
    """Restore a quarantine: world files AND mod downloads.

    Only on a **stopped** server, and only when no operation that writes the world is running:
    the same rule as in the rollback and the backup restore, including the world lock.
    """
    _check_quarantine_lock(server)
    async with world_lock.hold(server.key, "quarantine.restore"):
        path = _quarantine_path(server, name)
        # Via `lifecycle`, not a `docker.state` of our own: the question "is it still running"
        # has a single choke point (`running_or_restarting`). `when_in_doubt=True` is the right
        # direction: if you do not know whether the server is running, do not write.
        running = await lifecycle.is_running(server, when_in_doubt=True)
        if running:
            raise HTTPException(
                http.HTTP_409_CONFLICT,
                detail=err(
                    "mods.quarantine_server_running",
                    "The server is running — stop it first. Restoring over the running world "
                    "would damage it.",
                ),
            )

        # **The manifest IS the quarantine.** Without it `world` and `downloads` would be empty
        # and the function would answer `restored_world_files: 0`, a green result that did
        # nothing. A way back that cannot be read is no way back, and that must be said rather
        # than reported as success.
        try:
            with open(os.path.join(path, "manifest.json"), encoding="utf-8") as f:
                manifest = json.load(f)
        except (OSError, ValueError) as e:
            raise HTTPException(
                http.HTTP_409_CONFLICT,
                detail=err(
                    "mods.quarantine_manifest_unreadable",
                    "The quarantine “{name}” has no readable manifest ({error}). Without the "
                    "manifest it is unknown which files belong where — so nothing is touched. "
                    "The folder is at `data/quarantine/{name}`.",
                    name=name, error=str(e),
                ),
            ) from e

        world = list(manifest.get("world") or [])
        downloads = list(manifest.get("downloads") or [])
        back = await asyncio.to_thread(_restore_world, ini.world_save_path(server), path, world)
        left_behind: list[str] = []
        if downloads:
            left_behind = await asyncio.to_thread(_restore_downloads, server, path, downloads)

        # **The configuration belongs to it.** Restoring only world and downloads would create
        # the one state the preflight forbids fail-closed: the downloads of the removed map mod
        # back in place, but `Mods=`/`Map=` on the new state (`ORPHAN_MAP_DOWNLOAD`), after
        # which `state.guard_boot` refuses manual start, scheduled restart and autoheal.
        saved = {key: os.path.join(path, "config", os.path.basename(_CONFIG_PATHS[key](server)))
                 for key in (manifest.get("config") or []) if key in _CONFIG_PATHS}
        saved = {k: v for k, v in saved.items() if os.path.isfile(v)}
        # **The same asymmetry one level up.** What did not exist before the operation is in
        # the manifest as `config_absent_before`; the way back removes it (value `""`). Otherwise a
        # `spawnregions.lua` created by the operation would stay behind even when restoring the
        # quarantine, the last way back there is.
        saved.update({key: "" for key in (manifest.get("config_absent_before") or [])
                      if key in _CONFIG_PATHS})
        config_ok = await asyncio.to_thread(_restore_config, server, saved) if saved else True

        state.invalidate(server.key)
        # **Report what was done, not what was wanted.** Reporting the list from the manifest
        # would make three of 1200 restored files look like full success. This is the LAST way
        # back there is; being wrong about it costs more than anywhere else.
        return {
            "restored_world_files": back,
            "world_files_total": len(world),
            "restored_downloads": [w for w in downloads if w not in left_behind],
            "downloads_left": left_behind,
            "restored_config": sorted(saved),
            "complete": back == len(world) and not left_behind and config_ok,
            "state": manifest.get("state", "unknown"),
        }


async def delete_quarantine(server, name: str) -> dict:
    """Delete a quarantine, but never one that somebody currently depends on.

    `shutil.rmtree` hits the directory a running `apply_draft` needs as its **only way back**,
    and the fresh quarantine folder already shows up in `list_quarantines` during the operation.
    """
    _check_quarantine_lock(server)
    async with world_lock.hold(server.key, "quarantine.delete"):
        path = _quarantine_path(server, name)
        # `to_thread` is mandatory here, not taste: `rmtree` blocks, and the backend has exactly
        # ONE uvicorn worker; a large quarantine folder would otherwise stall the whole API.
        await asyncio.to_thread(shutil.rmtree, path, ignore_errors=True)
        return {"deleted": name, "gone": not os.path.isdir(path)}


def prune_quarantines(server, keep_days: int = 30) -> int:
    """Clean up old quarantines; otherwise the folder grows without bound.

    One operation can set aside several GB (world cells + complete mod downloads); without
    retention the disk fills up. Analogous to `backup.prune_backups`.
    """
    import time

    root = quarantine_root(server)
    cutoff = time.time() - keep_days * 86400
    removed = 0
    try:
        names = os.listdir(root)
    except OSError:
        return 0
    for name in names:
        path = os.path.join(root, name)
        try:
            if os.path.isdir(path) and os.path.getmtime(path) < cutoff:
                shutil.rmtree(path, ignore_errors=True)
                removed += 1
        except OSError:
            pass
    return removed


async def scheduled_prune() -> None:
    """Scheduler job: clean up quarantines and removed mod files once their retention has passed."""
    from ...models.server import GameType
    from .. import servers as servers_registry

    from . import foreign

    try:
        for srv in servers_registry.list_enabled():
            if srv.game_type == GameType.pz:
                n = prune_quarantines(srv)
                if n:
                    log.info("mods[%s]: cleaned up %s old quarantine(s)", srv.key, n)
            elif foreign.supported(srv):
                # Removed mod files are set aside instead of deleted. Without a retention period
                # that would only be a slower way to fill the disk. Same period as the PZ
                # quarantine next to it (`prune_quarantines`, 30 days).
                n = foreign.purge_removed(srv, days=30)
                if n:
                    log.info("mods[%s]: deleted %s removed mod file(s) for good", srv.key, n)
    except Exception:
        log.exception("mods: quarantine clean-up failed")


def history(server) -> list[dict]:
    from sqlmodel import select

    with Session(engine) as s:
        rows = s.exec(
            select(ModChange).where(ModChange.server_id == server.id).order_by(ModChange.id.desc())
        ).all()
    return [
        {"id": r.id, "at": r.at.isoformat(), "status": r.status, "summary": r.summary,
         "backup_name": r.backup_name, "quarantine_name": r.quarantine_name, "detail": r.detail}
        for r in rows
    ]


# --------------------------------------------------------------------- apply

def plan_world_reset(server, live: preflight.ModPlan, draft: preflight.ModPlan, inv=None) -> list[str]:
    """World files that must be reset because maps drop out (save-relative).

    Pass `inv` when the caller already has the inventory. `inventory.scan` has no cache and
    walks every workshop entry twice with `os.walk`, plus `cells.find_map_dirs`; map mods bring
    thousands of `.lotheader` files, all on the same disk the running server is writing its
    world to. `/mods/preflight` would otherwise scan everything twice on EVERY call, and the UI
    calls that endpoint after every draft change.
    """
    from . import cells

    if inv is None:
        inv = inventory.scan(server)
    save_dir = ini.world_save_path(server)
    affected: set = set()
    # **The same set as in the preflight and as `_stash_downloads`**; the calculation lives in
    # `preflight.dropped_maps`. A separate version here that only knew `Map=` would move
    # a deselected map download into quarantine while leaving its world cells in place.
    for entry in preflight.dropped_maps(inv, live.maps, draft):
        affected |= set(cells.generated_cells(save_dir, set(inv.cells_of_map(entry))))
    if not affected:
        return []
    return cells.world_files_for_cells(save_dir, affected) + cells.isoregion_files(save_dir)


async def apply_draft(server_key: str, *, confirm_world_reset: bool = False, progress=None) -> dict:
    # The world lock instead of one of our own: it covers ALL operations that touch world
    # files, not just mod changes. Otherwise a version change or a restore could run into the
    # middle of this one; both stop and remove the container whose boot we are waiting on for
    # up to 900 s.
    if (holder := world_lock.held_by(server_key)):
        raise HTTPException(
            http.HTTP_409_CONFLICT,
            detail=err(
                "mods.world_operation_running_wait",
                "This server already has an operation running: {operation} — please wait.",
                operation=world_lock.OPERATION_LABELS.get(holder, holder),
            ))
    async with world_lock.hold(server_key, "mods.apply"):
        try:
            return await _apply_locked(
                server_key, confirm_world_reset=confirm_world_reset, progress=progress
            )
        finally:
            # **The workshop report is stale afterwards, also and especially on failure.**
            # Without discarding it, the status light would show the finding for the OLD mod
            # list for up to six hours, right when someone looks because they just changed
            # something.
            from .. import mod_check

            mod_check.discard(server_key)


def _noop_step(text: str, **kw) -> None:
    pass


async def _apply_locked(server_key: str, *, confirm_world_reset: bool, progress=None) -> dict:
    step = progress.step if progress is not None else _noop_step
    from .. import servers as servers_registry

    server = servers_registry.get_by_key(server_key)
    if server is None or not server.enabled:
        raise HTTPException(
            http.HTTP_404_NOT_FOUND,
            detail=err("mods.server_not_found", "Server '{key}' not found", key=server_key))
    driver = get_driver(server)

    live = state.live_plan(server)
    # **The fail-closed check belongs on the LIVE plan, not on the draft.** `preflight.check`
    # tests `plan.readable`, but this path hands it the DRAFT, which comes from the database
    # and is always readable. So the blocker would not fire where its message promises "nothing
    # is applied".
    #
    # Without this check the damage is more than a wasted stop round: `live.maps` would be
    # empty, `state.diff` would take every draft entry for new, and `dropped_maps` would
    # find no map dropping out. The world binding, the core of this feature, would be silently
    # off.
    if not live.readable and live.detail:
        raise HTTPException(
            http.HTTP_409_CONFLICT,
            detail=err(
                "mods.ini_unreadable_nothing_applied",
                "The server configuration (.ini) cannot be read ({error}) — which mods are "
                "running is therefore unknown. Nothing is applied.",
                error=live.detail,
            ),
        )
    draft = state.get_draft(server)
    changes = state.diff(live, draft)
    if not state.has_changes(changes):
        raise HTTPException(http.HTTP_400_BAD_REQUEST,
                            detail=err("mods.draft_unchanged",
                                       "The draft already matches the running server."))

    # --- 1. Preflight: everything that can kill the server ends here ------------------------
    step("Checking the configuration…")
    inv = inventory.scan(server)
    result = preflight.check(
        draft, inv, world_dir=ini.world_save_path(server), live_maps=tuple(live.maps),
        prune_orphans=True,   # this operation also clears away downloads that are no longer listed
    )
    blockers = [
        f for f in result.blockers
        if not (f.code == "WORLD_MAP_REMOVED" and confirm_world_reset)
    ]
    if blockers:
        rejected = err("mods.config_blocked",
                        "The configuration was not applied — it would damage the server.")
        raise HTTPException(http.HTTP_409_CONFLICT, detail={
            **rejected,
            "error": rejected["message"],
            "blockers": [vars(f) for f in blockers],
        })

    spawn_regions = preflight.active_spawn_regions(draft, inv)
    save_dir = ini.world_save_path(server)
    name = server.container_name

    # --- 2. Stop the server (announcement + world save are part of the game-appropriate shutdown)
    step("Stopping the server…")

    # The stop is CONFIRMED, not just issued. If a failed `docker.state` query were swallowed,
    # `was_running` would stay `False`, the stop would be skipped, and `_stash_world` further
    # down would pull the world cells out from under the running game. `verify_boot` would even
    # report success afterwards: RCON responds because the server was never down.
    #
    # **The watchdog suppression goes along** (`suppress_minutes`) instead of being a separate
    # line before the stop: if the stop cannot be confirmed, this function leaves with a 409
    # "nothing was changed", and the watchdog must not stay silent for 20 minutes just when the
    # server may be hanging. `stop_and_confirm` takes the suppression back itself.
    was_running, stopped = await lifecycle.stop_and_confirm(
        server, announce_text="Server is about to restart — a mod change is being applied.",
        suppress_minutes=SUPPRESS_MINUTES,
    )
    if not stopped:
        raise HTTPException(
            http.HTTP_409_CONFLICT,
            detail=err(
                "mods.server_still_running",
                "The server is still running — nothing was changed. A mod change over the "
                "running world would damage it.",
            ),
        )

    # The list of world files to quarantine is built ONLY NOW, after the confirmed stop. It is
    # a file system snapshot (`cells.generated_cells` lists `map/<x>/`, `world_files_for_cells`
    # only picks up what exists), and PZ's `save`+`quit` still writes chunks during shutdown:
    # loaded but never persisted cells of the map being dropped, and new `isoregiondata` files.
    # Built before the stop, the list would miss exactly those; they would stay in the world
    # and result in an orphaned cell without a map provider.
    #
    # **It is always measured, even without confirmation.** `confirm_world_reset` comes from
    # the preflight, which ran before the stop. If that found no generated cells yet and the
    # measurement were skipped, the map would still be removed from `Map=` and its download
    # cleared away: terrain without a map provider. Nothing has been written at this point
    # (`write_plan` only follows in step 5), so aborting only costs the restart.
    async with lifecycle.ensure_restart(server, was_running, what="mod change"):
        world_files = plan_world_reset(server, live, draft)
        if world_files and not confirm_world_reset:
            raise HTTPException(
                http.HTTP_409_CONFLICT,
                detail=err(
                    "mods.world_reset_unconfirmed",
                    "While shutting down, the server wrote terrain for the maps being dropped: "
                    "{count} world files would have to be reset. The preflight could not see "
                    "that beforehand. Nothing was changed — please apply the change again with "
                    "the world reset confirmed.",
                    count=len(world_files),
                ),
            )

    # --- 3. Mandatory backup ----------------------------------------------------------------
    step("Creating the mandatory backup…")
    spec = driver.backup_spec(server)
    # The restart on failure goes through the `lifecycle` helper instead of a hand-written
    # copy: it attaches the actual server state to the exception, so the 502 does not claim
    # "nothing changed" when the restart failed and the server is down.
    async with lifecycle.ensure_restart(server, was_running, what="mandatory backup"):
        try:
            backup = await asyncio.to_thread(
                create_backup, "pre-mod-apply", data_dir=spec.data_dir,
                world_subdirs=spec.world_subdirs, config_subdirs=spec.config_subdirs,
                pre_hook=spec.pre_hook, post_hook=spec.post_hook, server=server,
            )
        except Exception as e:
            raise HTTPException(
                http.HTTP_502_BAD_GATEWAY,
                detail=err("mods.mandatory_backup_failed",
                           "Mandatory backup failed — nothing changed: {error}", error=str(e)))

    # **These lines need the safety net too.** They run after the CONFIRMED stop but before the
    # protected `try` with the full rollback: `os.makedirs` in `_new_quarantine`,
    # `shutil.copy2` in `_stash_config`, a DB insert in `_record`. If one of them raised (disk
    # full right after the mandatory archive, quarantine directory not writable, database
    # briefly gone), the exception would leave the function raw: no rollback, no start, no
    # `ModChange` row. And the `watchdog.suppress` from above keeps autoheal away for 20
    # minutes: the server would simply stay down.
    async with lifecycle.ensure_restart(server, was_running, what="mod change"):
        qname, qpath = _new_quarantine(server, "mod-apply")
        saved_config = _stash_config(server, qpath)
        change_id = _record(server, from_id=None, to_id=None, backup=backup["name"],
                            quarantine=qname, summary=state.summarize(changes))

    moved: list[str] = []
    dropped_downloads: list[str] = []
    # Initialised before the `try`, not only on the success path: otherwise the name would be
    # undefined in the `except`, and an UnboundLocalError there would hide the real error.
    world_before: set[str] = set()
    try:
        # --- 4. Reset the world surgically + clean up downloads (move, never delete)
        #
        # **The manifest exists BEFORE the first file is moved.** It is the only way back:
        # `list_quarantines` and `restore_quarantine` read nothing else. If it were written
        # last and the operation died in between (e.g. a backend restart during the apply), the
        # UI would show an empty quarantine folder while the files sat in it, and the server
        # would no longer start because of missing downloads. The planned list is already known
        # here; after the move it is replaced by what was really moved.
        planned = await asyncio.to_thread(
            planned_downloads, server, set(draft.workshop_items)
        )
        _write_manifest(qpath, world=world_files, downloads=planned,
                           config=list(saved_config), summary=state.summarize(changes),
                           state="planned")
        if world_files:
            step(f"{len(world_files)} world files are moving into the quarantine…")
            moved = await asyncio.to_thread(_stash_world, save_dir, world_files, qpath)
            # Right after the move, i.e. BEFORE the server runs with the new configuration: the
            # comparison snapshot for `_new_world_files`.
            world_before = await asyncio.to_thread(_world_dir_snapshot, save_dir, world_files)
            if len(moved) != len(world_files):
                # `_stash_world` swallows every OSError per file and only returns what was
                # really moved. If even one world file of the dropped map stays behind, that is
                # exactly the state this feature is built against: a world cell without a map
                # provider, i.e. a join crash without warning. The manifest then does not know
                # the file, so `restore_quarantine` cannot assign it either. Raising here means
                # triggering the existing rollback instead of building a second error path.
                raise RuntimeError(
                    f"only {len(moved)} of {len(world_files)} world files could be moved into "
                    "the quarantine"
                )
        dropped_downloads = await asyncio.to_thread(_stash_downloads, server, planned, qpath)
        _write_manifest(qpath, world=moved, downloads=dropped_downloads,
                           config=[k for k, v in saved_config.items() if v],
                           config_absent_before=[k for k, v in saved_config.items() if not v],
                           summary=state.summarize(changes), state="completed")

        # --- 5. Write the configuration ----------------------------------------------------
        step("Writing the configuration…")
        write_plan(server, draft)
        write_spawnregions(server, spawn_regions)

        # --- 6. Start and PROVE the boot ---------------------------------------------------
        step("Server is starting — the boot is being verified via RCON (this can take a few minutes)…")
        await docker.action(name, "start")
        if not await verify_boot(server, driver, timeout=BOOT_TIMEOUT):
            raise RuntimeError("the server did not come up with the new mod configuration")

    except Exception as e:
        log.warning("mods[%s]: apply failed → rollback (%s)", server_key, e)
        await _report_rollback(server, server_key, container=name, saved_config=saved_config,
                               save_dir=save_dir, qpath=qpath, moved=moved, driver=driver,
                               dropped_downloads=dropped_downloads, change_id=change_id,
                               world_before=world_before, error=e, step=step)
        raise AssertionError("unreachable — `_report_rollback` always raises")  # pragma: no cover

    # --- 7. Bookkeeping, OUTSIDE the rollback `try` ------------------------------------------
    #
    # **From here on the operation has succeeded: the boot is proven via RCON.** If these lines
    # were inside the `try` above and the database were gone at this very moment, a pure
    # bookkeeping error would stop the running server again, write the old `.ini` back and
    # fetch world cells from the quarantine. The damage would be greater than the error.
    #
    # The transaction boundary belongs after the boot proof, not after the bookkeeping. What
    # goes wrong afterwards is a note in the result. The same pattern as in `_report_rollback`
    # (the rollback must not fail on its own history row) and `operations.run`.
    set_id = None
    history_note = None
    try:
        set_id = state.snapshot_applied(server, draft, spawn_regions,
                                        label=state.summarize(changes))
        _finish(change_id, "ok")
    except Exception as e:
        log.error("mods[%s]: apply succeeded, but the history could not be written (%s)",
                  server_key, e)
        history_note = note(
            "mods.history_not_written",
            "The change is applied and the server is running — only the history could not be "
            "written ({error}). The entry therefore still shows as running.",
            error=str(e),
        )
    state.invalidate(server_key)
    return {
        "status": "ok", "backup": backup["name"],
        "quarantine": qname if (moved or dropped_downloads) else "",
        "world_files_moved": len(moved), "downloads_removed": dropped_downloads,
        "summary": state.summarize(changes), "set_id": set_id,
        **({"note": history_note} if history_note else {}),
    }


async def _report_rollback(server, server_key, *, container, saved_config, save_dir, qpath, moved,
                           driver, dropped_downloads, change_id, error, step,
                           world_before=frozenset()):
    """Run the rollback, record the result, inform the operator, in that order.

    **The bookkeeping must not take the message down with it.** If `_finish` ran unprotected
    between the rollback and the error message and the DB access raised, the **result of the
    rollback** would be lost, the only thing that really counts in this situation: did it go
    through or not? The operator would see a raw DB error instead.

    The same pattern as in `operations.run`, which puts work and bookkeeping into two separate
    `try` blocks, and as `backup._incident`, which encapsulates its DB access so that a side
    record does not topple the main operation.
    """
    step("Failed — restoring the previous state…")
    restored = await _rollback(server, container, saved_config, save_dir, qpath, moved,
                               driver, dropped_downloads, world_before=world_before)
    recorded = True
    try:
        _finish(change_id, "rolled_back" if restored else "failed", str(error))
        state.invalidate(server_key)
    except Exception as bookkeeping_error:
        log.exception("mods[%s]: the rollback could not be recorded — %s", server_key, bookkeeping_error)
        recorded = False
    # One whole sentence per outcome, so that each can be translated as a whole.
    if restored and recorded:
        detail = err("mods.change_failed_rolled_back",
                     "Mod change failed: {error}. The previous configuration is running again.",
                     error=str(error))
    elif restored:
        detail = err("mods.change_failed_rolled_back_history_open",
                     "Mod change failed: {error}. The previous configuration is running again. "
                     "(The history entry could not be closed and therefore still shows as "
                     "running.)",
                     error=str(error))
    elif recorded:
        detail = err("mods.change_failed_rollback_incomplete",
                     "Mod change failed: {error}. WARNING: rollback incomplete — please check "
                     "manually.",
                     error=str(error))
    else:
        detail = err("mods.change_failed_rollback_incomplete_history_open",
                     "Mod change failed: {error}. WARNING: rollback incomplete — please check "
                     "manually. (The history entry could not be closed and therefore still shows "
                     "as running.)",
                     error=str(error))
    raise HTTPException(http.HTTP_502_BAD_GATEWAY, detail=detail)



async def _rollback(server, name, saved_config, save_dir, qpath, moved, driver,
                    dropped_downloads=(), world_before=frozenset()) -> bool:
    """Restore the old state: stop the container, bring configuration and world back, start."""
    # Never write back over a running world (the same rule as in version_manager).
    #
    # A failed state query must NOT be read as "not running", least of all in the rollback, the
    # path one only enters BECAUSE something is broken. Boot proof and running state go through
    # the same socket proxy; if that is gone, both fail, and the rollback would otherwise push
    # quarantined world cells back into a game it does not know to be stopped.
    _, stopped = await lifecycle.stop_and_confirm(server)
    if not stopped:
        log.error("mods[%s]: container not stopped — rollback aborted", server.key)
        return False

    # The return values ARE read. `verify_boot` alone is only an RCON proof that SOME server
    # came up. If the world files did not come back, the operator would otherwise still read
    # "the previous configuration is running again" while world cells sat in the quarantine:
    # missing terrain, indistinguishable from a broken mod for the next preflight.
    complete = _restore_config(server, saved_config)
    if moved:
        restored = await asyncio.to_thread(_restore_world, save_dir, qpath, moved)
        if restored != len(moved):
            log.error("mods[%s]: rollback incomplete — %s of %s world files back",
                      server.key, restored, len(moved))
            complete = False
    if dropped_downloads:
        leftover = await asyncio.to_thread(_restore_downloads, server, qpath, list(dropped_downloads))
        if leftover:
            log.error("mods[%s]: rollback incomplete — mod downloads stayed in the "
                      "quarantine: %s", server.key, ", ".join(leftover))
            complete = False
    # What the failed run wrote NEWLY in the cleared cells is not in `moved`; see
    # `_new_world_files`. Measured and reported, not cleared away.
    if moved:
        new = await asyncio.to_thread(_new_world_files, save_dir, moved, set(world_before))
        if new:
            log.error("mods[%s]: rollback incomplete — the failed attempt newly wrote %s world "
                      "file(s) that nobody can take back (e.g. %s)",
                      server.key, len(new), new[0])
            complete = False
    try:
        await docker.action(name, "start")
    except Exception as e:
        log.error("mods[%s]: start after rollback failed: %s", server.key, e)
        return False
    watchdog.suppress(server.key, SUPPRESS_MINUTES)
    # The boot proof is necessary but not sufficient: a server that comes up with half a world
    # is not "restored".
    return await verify_boot(server, driver, timeout=BOOT_TIMEOUT) and complete
