"""What is actually on disk? Inventory of the downloaded workshop items.

Deliberately **server-scoped** (`server.data_dir`), unlike the older `mod_check`, which points
at ONE fixed PZ data folder. The dashboard manages several game servers, each with its own mod
directory; a module that points at a fixed path reliably answers the question for the wrong
server.

**Fail-closed:** if the mod directory cannot be read, the inventory reports `readable=False`
instead of returning an empty list. An empty result on a read error would make the preflight
report "no errors" although it simply could not see anything.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

from ..mod_check import _parse_mod_info
from . import cells

WORKSHOP_APP_ID = "108600"


@dataclass(frozen=True)
class ModItem:
    """A downloaded workshop item."""

    wid: str
    mod_ids: tuple[str, ...] = ()
    requires: tuple[str, ...] = ()
    builds: frozenset[str] = frozenset()          # {"41","42","flat"}, from the mod.info paths
    tile_maps: tuple[str, ...] = ()               # real maps (with tiles) → belong in Map=
    spawn_regions: tuple[str, ...] = ()           # folders with spawnpoints.lua
    map_cells: dict[str, frozenset] = field(default_factory=dict)
    map_dirs: dict[str, str] = field(default_factory=dict)

    @property
    def has_mod_info(self) -> bool:
        return bool(self.mod_ids)

    @property
    def build41_only(self) -> bool:
        """The mod only ships a Build 41 branch: unusable on Build 42."""
        return self.builds == {"41"}


@dataclass(frozen=True)
class Inventory:
    items: dict[str, ModItem] = field(default_factory=dict)
    readable: bool = True
    detail: str = ""

    def provider_of_mod(self, mod_id: str) -> list[str]:
        """Which items provide this mod ID? (more than one = conflict)"""
        return [wid for wid, it in self.items.items() if mod_id in it.mod_ids]

    def provider_of_map(self, folder: str) -> list[str]:
        """Which items provide this map folder? Compared case-insensitively (like the .ini)."""
        low = folder.strip().lower()
        return [wid for wid, it in self.items.items() if any(m.lower() == low for m in it.map_dirs)]

    def cells_of_map(self, folder: str) -> frozenset:
        low = folder.strip().lower()
        for it in self.items.values():
            for name, cs in it.map_cells.items():
                if name.lower() == low:
                    return cs
        return frozenset()


def workshop_dir(server) -> str:
    return os.path.join(server.data_dir, "workshop-mods")


def content_dir(server) -> str:
    return os.path.join(workshop_dir(server), "content", WORKSHOP_APP_ID)


def _builds_of(item_dir: str) -> set[str]:
    """Build branches of an item, from the paths of its mod.info files.

    B42 mods come as `common/` + `42/` (+ optionally `41/`), each branch with its own mod.info.
    """
    found: set[str] = set()
    # `onerror`: a missed branch would mean "this build does not exist", which is an answer, not
    # a gap. Without the callback `os.walk` silently discards read errors.
    for root, _dirs, files in os.walk(item_dir, onerror=cells.report_read_error):
        if "mod.info" in files:
            low = root.replace("\\", "/").lower()
            found.add("42" if "/42" in low else "41" if "/41" in low else "flat")
    return found


def _scan_item(item_dir: str, wid: str) -> ModItem:
    mod_ids: list[str] = []
    requires: list[str] = []
    # Same one level further: a missed `mod.info` would mean "this mod ID does not exist", and
    # the preflight blocker for missing mod IDs depends on exactly that.
    for root, _dirs, files in os.walk(item_dir, onerror=cells.report_read_error):
        if "mod.info" not in files:
            continue
        # A Build 41 branch yields no valid mod IDs on B42; otherwise the preflight would report
        # "mod ID present" although the server does not load it at all.
        if "/41" in root.replace("\\", "/").lower():
            continue
        parsed = _parse_mod_info(os.path.join(root, "mod.info"))
        for mid in parsed["ids"]:
            if mid not in mod_ids:
                mod_ids.append(mid)
        for req in parsed["requires"]:
            if req not in requires:
                requires.append(req)

    map_dirs = cells.find_map_dirs(item_dir)
    map_cells = {name: frozenset(cells.cells_of_map_dir(path)) for name, path in map_dirs.items()}
    tile_maps = tuple(sorted(name for name, cs in map_cells.items() if cs))
    spawn_regions = tuple(sorted(
        name for name, path in map_dirs.items()
        if os.path.isfile(os.path.join(path, "spawnpoints.lua"))
    ))
    return ModItem(
        wid=wid,
        mod_ids=tuple(mod_ids),
        requires=tuple(requires),
        builds=frozenset(_builds_of(item_dir)),
        tile_maps=tile_maps,
        spawn_regions=spawn_regions,
        map_cells=map_cells,
        map_dirs=map_dirs,
    )


def scan(server) -> Inventory:
    """Read all downloaded items of a server.

    A missing directory is NOT an error (fresh server without mods), but it is not "readable"
    either. The caller decides: unreadability is a blocker only when mods are configured.
    """
    base = content_dir(server)
    try:
        names = sorted(os.listdir(base))
    except OSError as e:
        return Inventory(items={}, readable=False, detail=str(e))

    items: dict[str, ModItem] = {}
    for wid in names:
        item_dir = os.path.join(base, wid)
        if wid.isdigit() and os.path.isdir(item_dir):
            try:
                items[wid] = _scan_item(item_dir, wid)
            except cells.ModTreeReadError as e:
                # An unreadable map tree is not an empty map list. It takes the same exit as an
                # unreadable base directory: `readable=False`, which is a preflight blocker as
                # soon as mods are configured. An empty set here would disable
                # `MAP_WITHOUT_PROVIDER`, the very blocker against the join crash.
                return Inventory(items={}, readable=False, detail=f"Item {wid}: {e}")
    return Inventory(items=items, readable=True)
