"""Check catalogue: can this mod configuration be applied safely?

Purely local and deterministic, with **no network**, so the check is always cheap and
repeatable (the Steam comparison keeps running in the 6-hourly `mod_check` job).

Two severities: **blockers** prevent the apply, **warnings** only inform. Blockers
deliberately CANNOT be overridden; overriding is exactly what breaks the server. In return
every finding carries a plain-text message and, where possible, a one-click fix.

Every finding has a **stable code**. The frontend decides by code, never by matching message
text (any wording change would silently break the UI).
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

from ..mod_check import _strip_modid
from . import cells
from .inventory import Inventory

BLOCKER = "blocker"
WARNING = "warning"

# --- Vanilla content of Build 42 ----------------------------------------------------------
# Collected from a running 42.20.0 container (`media/maps/`). B42 merged Knox County into ONE
# map: only `Muldraugh, KY` carries tiles; the other ten towns are spawn overlays whose
# `map.info` points at the tile source via `lots=Muldraugh, KY`.
#
# The list is deliberately a maintained constant: the game container cannot be read through the
# Docker socket proxy (no EXEC, a security decision). If a game update adds new towns, an
# unknown `Map=` entry blocks until the list has been extended. That is intended: a visible
# stop is better than a silent crash on join.
BASE_MAP = "Muldraugh, KY"
VANILLA_MAPS: tuple[str, ...] = (
    "Brandenburg, KY", "Echo Creek, KY", "Ekron, KY", "Fallas Lake, KY", "Irvington, KY",
    "March Ridge, KY", "Muldraugh, KY", "Riverside, KY", "Rosewood, KY", "Valley Station, KY",
    "West Point, KY", "challengemaps",
)
# All eleven towns ship a `spawnpoints.lua` (verified), so each of them is a selectable spawn
# location.
VANILLA_SPAWN_REGIONS: tuple[str, ...] = tuple(m for m in VANILLA_MAPS if m != "challengemaps")

# Cell range of the vanilla map (from the 4065 .lotheader files). Only known as a bounding box:
# the real cell set has holes and lives in the container, not in the bind mount. Good enough
# for a warning, not for a blocker.
VANILLA_CELL_BOUNDS = (0, 0, 77, 62)   # minX, minY, maxX, maxY

_ILLEGAL = (";", "\n", "\r")


@dataclass(frozen=True)
class Finding:
    code: str
    level: str
    message: str          # plain text for humans
    subject: str = ""     # affected mod ID / workshop ID / map name
    detail: str = ""
    fix: str = ""         # identifier of a one-click fix, e.g. "drop_map"

    @property
    def is_blocker(self) -> bool:
        return self.level == BLOCKER


@dataclass(frozen=True)
class ModPlan:
    """The state to be checked (draft or live)."""

    workshop_items: tuple[str, ...] = ()
    mods: tuple[str, ...] = ()
    maps: tuple[str, ...] = ()
    # **Kept at the end on purpose:** `ModPlan` is constructed positionally in the tests
    # (`ModPlan((), (), (BASE_MAP,))`). New fields in front would shift every one of them.
    #
    # `readable=False` means: the live state could NOT be read. This plan is empty because
    # nothing is known, not because nothing is installed. If the two cases were
    # indistinguishable, `preflight.check` would find nothing, `status()` would report green and
    # `guard_boot` (the guard before a scheduled restart, autoheal and the start button) would
    # return an empty blocker list: the guard would be off without anyone noticing.
    #
    # Deliberately a field and not an exception: `guard_boot` has a blanket
    # `except Exception: return []`. An exception would be fail-open again right there.
    readable: bool = True
    detail: str = ""

    def clean_mod_ids(self) -> list[str]:
        """Mod IDs without leading backslashes (the .ini allows `\\ModID`)."""
        return [m for m in (_strip_modid(x) for x in self.mods) if m]


@dataclass
class PreflightResult:
    findings: list[Finding] = field(default_factory=list)

    @property
    def blockers(self) -> list[Finding]:
        return [f for f in self.findings if f.is_blocker]

    @property
    def warnings(self) -> list[Finding]:
        return [f for f in self.findings if not f.is_blocker]

    @property
    def ok(self) -> bool:
        return not self.blockers

    def as_dict(self) -> dict:
        return {
            "ok": self.ok,
            "blockers": [vars(f) for f in self.blockers],
            "warnings": [vars(f) for f in self.warnings],
        }


def _is_vanilla_map(name: str) -> bool:
    return name.strip().lower() in {m.lower() for m in VANILLA_MAPS}


def _overlaps_vanilla(cell_set) -> bool:
    min_x, min_y, max_x, max_y = VANILLA_CELL_BOUNDS
    return any(min_x <= x <= max_x and min_y <= y <= max_y for x, y in cell_set)


def dropped_maps(inv: Inventory, live_maps, plan: ModPlan) -> list[str]:
    """Maps whose terrain no longer has a provider after this change.

    **The single place where this set is determined.** `check()` (blocker `WORLD_MAP_REMOVED`)
    and `apply.plan_world_reset` (what goes into quarantine) both ask here, so the two cannot
    drift apart.

    A map can drop out in two ways:

    1. **It is no longer in `Map=`.**
    2. **The workshop item that provides it is deselected**, even if the map was never in
       `Map=`. `_stash_downloads` moves every download directory that is no longer listed, and
       the PZ server loads downloaded maps on its own. Their cells are therefore in the world
       without the map list ever knowing about them; after the clean-up the world points at
       tiles that no longer exist, which is a join crash without warning.

    If another item that is STILL selected provides the same map, it does not drop out.
    Vanilla maps never do: their tiles come with the game.
    """
    listed = {e.strip().lower() for e in plan.maps if e.strip()}
    kept_items = {w.strip() for w in plan.workshop_items if w.strip()}
    remaining_maps = {
        name.strip().lower()
        for wid, item in inv.items.items() if wid in kept_items
        for name in item.map_dirs
    }

    out: list[str] = []

    def _remember(name: str) -> None:
        entry = name.strip()
        if entry and not _is_vanilla_map(entry) and entry not in out:
            out.append(entry)

    for name in live_maps:                                    # 1. taken out of the map list
        if name.strip() and name.strip().lower() not in listed:
            _remember(name)

    for wid, item in inv.items.items():                       # 2. provider is deselected
        if wid in kept_items:
            continue
        for name in item.tile_maps:
            if name.strip().lower() not in remaining_maps:
                _remember(name)

    return out


def check(
    plan: ModPlan,
    inv: Inventory,
    *,
    world_dir: str | None = None,
    live_maps: tuple[str, ...] = (),
    prune_orphans: bool = False,
) -> PreflightResult:
    """Full check of a mod plan.

    `world_dir` + `live_maps` are only needed for the world binding (which map drops out
    although the world has already generated its cells).

    `prune_orphans=True` means "the caller also clears away downloaded items that are no longer
    listed"; an orphaned download is then not a blocker. That is the case when checking the
    DRAFT (`apply.py` moves them into quarantine), but not when checking the RUNNING state,
    because there the server actually loads them.
    """
    out: list[Finding] = []
    active_ids = plan.clean_mod_ids()
    active_set = set(active_ids)

    # --- 0. Can I see anything at all? (fail-closed) --------------------------------------
    if not inv.readable and (plan.workshop_items or plan.mods):
        return PreflightResult([Finding(
            code="WORKSHOP_DIR_UNREADABLE",
            level=BLOCKER,
            message="The server's mod directory cannot be read — the configuration cannot be "
                    "checked and is therefore not applied.",
            detail=inv.detail,
        )])

    # --- 0b. Could the LIVE state be read at all? (fail-closed) ---------------------------
    #
    # Section 0 asks the same for the mod DIRECTORY; this is the question for the `.ini`. An
    # unreadable `.ini` must not look like "no mods": the status light would be green and
    # `guard_boot` would return an empty blocker list.
    #
    # **A MISSING file is not a blocker.** PZ only writes the `.ini` on first boot; a freshly
    # created server could otherwise never be started. `live_plan` distinguishes the two via
    # `detail`: it sets `readable=False` in both cases, but only a real read error produces a
    # blocker here.
    if not plan.readable and plan.detail:
        return PreflightResult([Finding(
            code="INI_UNREADABLE",
            level=BLOCKER,
            message="The server configuration (.ini) cannot be read — which mods the server "
                    "has loaded is therefore unknown. Nothing is applied and nothing is booted "
                    "until that is resolved.",
            detail=plan.detail,
        )])

    # --- 1. Characters that split the lists -----------------------------------------------
    # The semicolon separates the entries in the .ini; a semicolon INSIDE a value silently
    # creates two broken entries. Line breaks would inject foreign keys.
    for label, values in (("Workshop ID", plan.workshop_items), ("Mod ID", plan.mods),
                          ("Map", plan.maps)):
        for value in values:
            if any(ch in value for ch in _ILLEGAL):
                out.append(Finding(
                    code="ILLEGAL_CHARACTER", level=BLOCKER, subject=value,
                    message=f"{label} “{value.strip()}” contains a character that is not "
                            f"allowed (semicolon or line break).",
                ))

    # --- 2. Items present -----------------------------------------------------------------
    for item_id in plan.workshop_items:
        item = inv.items.get(item_id)
        # Order matters: a Build 41 mod yields no valid mod IDs on B42. Without this distinction
        # the preflight would report "not downloaded", and one would re-download endlessly
        # although the files are there and merely belong to the wrong game version.
        if item is not None and item.build41_only:
            out.append(Finding(
                code="BUILD41_ONLY", level=BLOCKER, subject=item_id,
                message=f"Workshop item {item_id} only contains a Build 41 version and does not "
                        f"run on Build 42. The files are there — the mod just does not match "
                        f"the game version.",
                fix="remove_item",
            ))
        elif item is None or not item.has_mod_info:
            out.append(Finding(
                code="ITEM_NOT_DOWNLOADED", level=BLOCKER, subject=item_id,
                message=f"Workshop item {item_id} is not (completely) downloaded.",
                fix="download_item",
            ))

    # --- 3. Mod IDs: provided, unique, dependencies ---------------------------------------
    active_items = {wid: it for wid, it in inv.items.items() if wid in set(plan.workshop_items)}
    provided: dict[str, list[str]] = {}
    for wid, item in active_items.items():
        for mid in item.mod_ids:
            provided.setdefault(mid, []).append(wid)

    for mid in active_ids:
        if mid not in provided:
            out.append(Finding(
                code="MODID_NOT_PROVIDED", level=BLOCKER, subject=mid,
                message=f"Mod “{mid}” is in the load order but is not provided by any active "
                        f"workshop item.",
                fix="drop_mod",
            ))
    for mid, wids in provided.items():
        if len(wids) > 1 and mid in active_set:
            out.append(Finding(
                code="DUPLICATE_MODID", level=BLOCKER, subject=mid,
                message=f"Mod ID “{mid}” is provided by several items "
                        f"({', '.join(sorted(wids))}) — one of them has to go.",
                detail=", ".join(sorted(wids)),
            ))

    position = {mid: i for i, mid in enumerate(active_ids)}
    for wid, item in active_items.items():
        if not (set(item.mod_ids) & active_set):
            continue
        for req in item.requires:
            if req not in active_set:
                out.append(Finding(
                    code="MISSING_DEPENDENCY", level=BLOCKER, subject=req,
                    message=f"Item {wid} requires the mod “{req}”, which is not loaded.",
                    fix="enable_dependency",
                ))
                continue
            for mid in item.mod_ids:
                if mid in position and position[req] > position[mid]:
                    out.append(Finding(
                        code="LOAD_ORDER", level=WARNING, subject=req,
                        message=f"Load order: “{req}” loads after “{mid}” although “{mid}” "
                                f"depends on it.",
                        fix="sort_load_order",
                    ))

    # --- 4. Maps --------------------------------------------------------------------------
    map_entries = [m.strip() for m in plan.maps if m.strip()]

    for entry in map_entries:
        providers = inv.provider_of_map(entry)
        active_providers = [w for w in providers if w in active_items]
        if active_providers or _is_vanilla_map(entry):
            continue
        # Neither provided by an active mod nor a known vanilla map. Treating an entry without
        # a provider as "vanilla" would let it pass, and the server would then not find the map
        # on join.
        out.append(Finding(
            code="MAP_WITHOUT_PROVIDER", level=BLOCKER, subject=entry,
            message=f"The map “{entry}” is in the map list but is not provided by any active "
                    f"mod and is not a vanilla map. The server crashes with it when a player "
                    f"joins.",
            fix="drop_map",
        ))

    # The base map must exist and be the very last entry; otherwise vanilla references do not
    # resolve.
    #
    # **Also with an EMPTY map list**, provided there is a mod configuration at all. Otherwise
    # the completely empty list would be judged more leniently than the wrongly sorted one, and
    # `state.status()` and `state.guard_boot()`, which run this same catalogue, would show green
    # and leave the boot gate open. `state._sorted_maps` only enforces "the base map is always
    # included" when WRITING a draft; nothing enforces it on the live path.
    #
    # **The boundary is the same as in section 0** (`inv.readable`): without items and without
    # mods there is no mod configuration to judge. A vanilla server without a `Map=` line runs
    # on the base map and must not be reported as unstartable.
    if (map_entries or plan.workshop_items or plan.mods) and             not any(e.lower() == BASE_MAP.lower() for e in map_entries):
        out.append(Finding(
            code="BASE_MAP_MISSING", level=BLOCKER, subject=BASE_MAP,
            message=f"The base map “{BASE_MAP}” is missing from the map list. It must always "
                    f"be included and come last.",
            fix="fix_map_order",
        ))
    elif map_entries and map_entries[-1].lower() != BASE_MAP.lower():
        out.append(Finding(
            code="BASE_MAP_NOT_LAST", level=BLOCKER, subject=BASE_MAP,
            message=f"The base map “{BASE_MAP}” must be the LAST entry of the map list, "
                    f"otherwise it covers the mod maps.",
            fix="fix_map_order",
        ))

    # Cell conflicts between the active mod maps: the real map killer.
    cells_by_map: dict[str, frozenset] = {}
    for entry in map_entries:
        if _is_vanilla_map(entry):
            continue
        cs = inv.cells_of_map(entry)
        if cs:
            cells_by_map[entry] = cs
    for conflict in cells.conflicts({k: set(v) for k, v in cells_by_map.items()}):
        out.append(Finding(
            code="MAP_CELL_CONFLICT", level=BLOCKER,
            subject=f"{conflict.map_a} / {conflict.map_b}",
            message=f"The maps “{conflict.map_a}” and “{conflict.map_b}” share "
                    f"{conflict.count} map cell(s). Only one of them can exist there — the other "
                    f"is overwritten.",
            detail="Affected cells include: " + ", ".join(conflict.sample()),
            fix="drop_map",
        ))
    for name, cs in cells_by_map.items():
        if _overlaps_vanilla(cs):
            out.append(Finding(
                code="MAP_OVERLAPS_VANILLA", level=WARNING, subject=name,
                message=f"The map “{name}” lies within the area of the base map. That is normal "
                        f"for extensions but can replace parts of Knox County.",
            ))

    # Downloaded but not active: the server loads such maps ANYWAY.
    for download_id, item in inv.items.items():
        if download_id in active_items or not item.tile_maps or prune_orphans:
            continue
        out.append(Finding(
            code="ORPHAN_MAP_DOWNLOAD", level=BLOCKER, subject=download_id,
            message=f"The map mod {download_id} is downloaded but not active. The server loads "
                    f"downloaded maps anyway — disabling is not enough here, the download has "
                    f"to be removed.",
            detail=", ".join(item.tile_maps),
            fix="remove_download",
        ))

    # Active map mod that is not in the map list.
    listed = {e.lower() for e in map_entries}
    for wid, item in active_items.items():
        for name in item.tile_maps:
            if name.lower() not in listed:
                out.append(Finding(
                    code="MAP_NOT_LISTED", level=WARNING, subject=name,
                    message=f"The map “{name}” (item {wid}) is active but not in the map list "
                            f"— it cannot be played this way.",
                    fix="add_map",
                ))

    # --- 5. World binding: does a map drop out that the world has already generated? ------
    if world_dir:
        for name in dropped_maps(inv, live_maps, plan):
            cs = inv.cells_of_map(name)
            if not cs:
                out.append(Finding(
                    code="WORLD_MAP_UNKNOWN", level=BLOCKER, subject=name,
                    message=f"The map “{name}” is to be removed, but its files can no longer "
                            f"be found. That makes it impossible to determine which parts of "
                            f"the world are affected.",
                ))
                continue
            generated = cells.generated_cells(world_dir, set(cs))
            if generated:
                out.append(Finding(
                    code="WORLD_MAP_REMOVED", level=BLOCKER, subject=name,
                    message=f"The world has already generated {len(generated)} map cell(s) for "
                            f"“{name}”. These areas have to be reset, otherwise the world "
                            f"points at terrain that no longer exists.",
                    detail=f"{len(generated)} cells affected",
                    fix="quarantine_cells",
                ))

    # --- 6. Spawn regions -----------------------------------------------------------------
    for item in active_items.values():
        for region in item.spawn_regions:
            path = item.map_dirs.get(region)
            if path and not os.path.isfile(os.path.join(path, "spawnpoints.lua")):
                out.append(Finding(
                    code="SPAWNREGION_FILE_MISSING", level=BLOCKER, subject=region,
                    message=f"The spawn location “{region}” points at a missing spawnpoints.lua.",
                ))

    return PreflightResult(out)


def active_spawn_regions(plan: ModPlan, inv: Inventory) -> list[str]:
    """Selectable spawn locations: those of the active mods, then the vanilla towns.

    Basis for `<servername>_spawnregions.lua`. ACTIVE mods only: a region without its mod
    loaded leads to a spawn-init crash at boot.
    """
    active = set(plan.workshop_items)
    out: list[str] = []
    for wid in plan.workshop_items:
        item = inv.items.get(wid)
        if item is None or wid not in active:
            continue
        for region in item.spawn_regions:
            if region not in out and region not in VANILLA_SPAWN_REGIONS:
                out.append(region)
    return out + list(VANILLA_SPAWN_REGIONS)
