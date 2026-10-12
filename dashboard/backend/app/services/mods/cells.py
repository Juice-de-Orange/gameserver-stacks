"""Cell maths for Project Zomboid maps: the basis of the map checks.

A PZ map consists of **cells**; each cell is a `<CellX>_<CellY>.lotheader` file in the map
folder (`media/maps/<Name>/`). That makes everything that goes wrong with map mods computable
instead of guessed:

- **Map conflict** = two maps claim the same cell. The one loaded later then overwrites the
  earlier one, and you get half buildings, missing tiles or crashes.
- **World binding** = a cell for which the world has already written terrain is bound to the
  map that generated it. If the map disappears, the world points at tiles that no longer exist.

**All constants here were verified against a running Build 42 server, not taken from forums**
(the guides in circulation describe the B41 layout and are wrong for B42):

- `map.info` of `Muldraugh, KY`: "Chunk size is 8x8, Cell size is 256x256" → 256/8 = **32**.
- Cross-check on a real world: the cells reached up to CellX 46, the chunk folders up to
  ChunkX 1503, and `46*32+31 == 1503`.

Save layout in Build 42 (verified):
- `map/<ChunkX>/<ChunkY>.bin`                     → **chunk** level, the actual terrain
- `chunkdata/chunkdata_<CellX>_<CellY>.bin`       → cell level
- `apop/apop_<CellX>_<CellY>.bin`                 → cell level
- `zpop/zpop_<CellX>_<CellY>.bin`                 → cell level
- `metagrid/metacell_<CellX>_<CellY>.bin`         → cell level
- `isoregiondata/`                                → **a third grid that cannot be mapped**

Safety decision: file names are **constructed, never searched by pattern**. A non-cell file
therefore cannot be hit at all: `zpop/zpop_virtual.bin` really exists in the save and a naive
regex (`zpop_.*\\.bin`) would delete it too.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass

# Edge length of a cell in chunks (derivation in the module docstring).
CHUNKS_PER_CELL = 32


class ModTreeReadError(OSError):
    """A directory tree under `workshop-mods/` could not be read, so the answer is UNKNOWN,
    not empty.

    It lives in this module because `cells` is the lowest layer of the `mods` package; it is
    also used for the `mod.info` trees in `inventory` (same decision chain).

    Returning an empty set on a read error would be fail-open: `MAP_WITHOUT_PROVIDER` is a
    blocker when `Map=` names a map for which no item provides cells, and "not readable" would
    look exactly like "is a spawn overlay". The caller decides, so it has to be told.

    **Missing is NOT unreadable.** A fresh server has no `media/maps`, a young save has no
    `isoregiondata/`. Those cases stay an empty answer; otherwise normal operation would be
    blocked.
    """


def report_read_error(error: OSError) -> None:
    """`onerror` callback for `os.walk`.

    `os.walk` DOES NOT RAISE. It reports every error it meets to this callback and discards it
    by default (`onerror=None`). An `except OSError` around the loop alone is therefore dead
    code: even the error of the very first `scandir` goes to `onerror`.
    """
    if isinstance(error, (FileNotFoundError, NotADirectoryError)):
        return                                    # nothing there is a valid answer
    raise ModTreeReadError(f"{getattr(error, 'filename', '?')}: {error}") from error

# Cell-addressed stores: (subfolder, file prefix) → "<prefix>_<x>_<y>.bin"
CELL_STORES: tuple[tuple[str, str], ...] = (
    ("chunkdata", "chunkdata"),
    ("apop", "apop"),
    ("zpop", "zpop"),
    ("metagrid", "metacell"),
)

# Derived cache with its own grid; only ever handled as a WHOLE (see `isoregion_files`).
ISOREGION_DIR = "isoregiondata"

# `<CellX>_<CellY>.lotheader`; negative coordinates are valid (B42 extends to the west).
_LOTHEADER_RE = re.compile(r"^(-?\d+)_(-?\d+)\.lotheader$", re.IGNORECASE)

Cell = tuple[int, int]


@dataclass(frozen=True)
class MapConflict:
    """Two maps claim the same cells."""

    map_a: str
    map_b: str
    cells: tuple[Cell, ...]

    @property
    def count(self) -> int:
        return len(self.cells)

    def sample(self, limit: int = 6) -> list[str]:
        """A few cells as `X_Y` for the message (the list can be thousands long)."""
        return [f"{x}_{y}" for x, y in self.cells[:limit]]


# --------------------------------------------------------------------- reading maps

def cells_of_map_dir(map_dir: str) -> set[Cell]:
    """Cells of a map, from its `.lotheader` files.

    Recursive, because B42 mods may place their map under `common/media/maps/<Name>/` or
    `42/media/maps/<Name>/`.

    **Unreadable → `ModTreeReadError`, not an empty set.** The caller decides whether that is
    a blocker, but it has to be told; an empty set is an ANSWER here (see the exception class).
    A missing folder, by contrast, stays the empty set.
    """
    out: set[Cell] = set()
    try:
        for _root, _dirs, files in os.walk(map_dir, onerror=report_read_error):
            for name in files:
                m = _LOTHEADER_RE.match(name)
                if m:
                    out.add((int(m.group(1)), int(m.group(2))))
    except ModTreeReadError:
        raise                                     # raised by `report_read_error`, already correct
    except OSError as e:
        raise ModTreeReadError(f"{map_dir}: {e}") from e
    return out


def find_map_dirs(item_dir: str) -> dict[str, str]:
    """Map folders of a workshop item: {folder name: path}.

    Looks for directories `.../media/maps/<Name>`. If an item provides the same map name more
    than once (e.g. `common/` and `42/`), the path found last wins; the cells are identical in
    that case anyway, and the conflict calculation only needs the name.

    **Unreadable → `ModTreeReadError`** (reasoning on the class). An item without `media/maps`
    still returns `{}`: that is the normal case, not an error.
    """
    found: dict[str, str] = {}
    try:
        for root, dirs, _files in os.walk(item_dir, onerror=report_read_error):
            if root.replace("\\", "/").lower().endswith("/media/maps"):
                for folder in dirs:
                    name = folder.strip()
                    if name:
                        found[name] = os.path.join(root, folder)
    except ModTreeReadError:
        raise
    except OSError as e:
        raise ModTreeReadError(f"{item_dir}: {e}") from e
    return found


def is_tile_map(map_dir: str) -> bool:
    """A real map (provides tiles) rather than a pure spawn overlay.

    In B42 this is the decisive difference: only `Muldraugh, KY` carries the tiles; the ten
    other vanilla towns are overlays whose `map.info` points at Muldraugh via `lots=`.
    Only real maps belong in `Map=`.
    """
    return bool(cells_of_map_dir(map_dir))


def lots_of(map_dir: str) -> str:
    """`lots=` line from `map.info` (the tile source of a spawn overlay). Empty if none."""
    try:
        with open(os.path.join(map_dir, "map.info"), "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                if line.strip().lower().startswith("lots="):
                    return line.split("=", 1)[1].strip()
    except OSError:
        pass
    return ""


# --------------------------------------------------------------------- conflicts

def conflicts(cells_by_map: dict[str, set[Cell]]) -> list[MapConflict]:
    """Pairwise cell overlaps. Sorted deterministically (names, then cells)."""
    names = sorted(cells_by_map)
    out: list[MapConflict] = []
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            shared = cells_by_map[a] & cells_by_map[b]
            if shared:
                out.append(MapConflict(a, b, tuple(sorted(shared))))
    return out


# --------------------------------------------------------------------- world ↔ cells

def chunk_range(cell: int) -> range:
    """Chunk coordinates of a cell coordinate: cell 46 → 1472..1503."""
    start = cell * CHUNKS_PER_CELL
    return range(start, start + CHUNKS_PER_CELL)


def cell_of_chunk(chunk: int) -> int:
    """The inverse; also correct for negative coordinates (wilderness west of the vanilla map)."""
    return chunk // CHUNKS_PER_CELL


def generated_cells(save_dir: str, candidates: set[Cell]) -> set[Cell]:
    """Which of the `candidates` has the world already generated?

    The yardstick is `map/<ChunkX>/<ChunkY>.bin`, which holds the terrain actually written.
    (The cell stores are incomplete: a real world had 86 `chunkdata` files but 139 `metagrid`
    files, so they would give a wrong answer to "does the world have this cell?".)

    ONLY the chunk folders of the candidates are read, not the whole world (which easily has
    65,000 files).
    """
    map_root = os.path.join(save_dir, "map")
    if not candidates or not os.path.isdir(map_root):
        return set()

    # Chunk-X folder → cells that need it. One listdir per folder instead of one per cell.
    wanted_x: dict[int, set[Cell]] = {}
    for cell in candidates:
        for cx in chunk_range(cell[0]):
            wanted_x.setdefault(cx, set()).add(cell)

    hit: set[Cell] = set()
    for chunk_x, cells in wanted_x.items():
        pending = cells - hit
        if not pending:
            continue
        try:
            names = os.listdir(os.path.join(map_root, str(chunk_x)))
        except OSError:
            continue  # folder does not exist: this chunk strip was never generated
        chunk_ys = {int(n[:-4]) for n in names if n.endswith(".bin") and _is_int(n[:-4])}
        for cell in pending:
            if any(y in chunk_ys for y in chunk_range(cell[1])):
                hit.add(cell)
    return hit


def world_files_for_cells(save_dir: str, cells: set[Cell]) -> list[str]:
    """All world files belonging to these cells, as save-relative paths.

    File names are **constructed**, not searched: `zpop/zpop_virtual.bin` (really exists, has
    no cell pattern) therefore cannot be picked up. Only existing files are returned; the order
    is stable so the quarantine manifest is reproducible.
    """
    out: list[str] = []
    for cell in sorted(cells):
        cx, cy = cell
        # Chunk level: the actual terrain (up to 32*32 files per cell).
        for chunk_x in chunk_range(cx):
            x_dir = os.path.join(save_dir, "map", str(chunk_x))
            if not os.path.isdir(x_dir):
                continue
            for chunk_y in chunk_range(cy):
                rel = os.path.join("map", str(chunk_x), f"{chunk_y}.bin")
                if os.path.isfile(os.path.join(save_dir, rel)):
                    out.append(rel)
        # Cell level
        for folder, prefix in CELL_STORES:
            rel = os.path.join(folder, f"{prefix}_{cx}_{cy}.bin")
            if os.path.isfile(os.path.join(save_dir, rel)):
                out.append(rel)
    return out


def isoregion_files(save_dir: str) -> list[str]:
    """The complete `isoregiondata/` folder as save-relative paths.

    Deliberately all-or-nothing: this directory follows a third grid that could not be mapped
    to either cells or chunks (observed X 248..322 with cells 0..46 and chunks up to 1503). It
    is a small, **derived** cache; having it rebuilt completely is more honest than filtering
    it half correctly.

    **Unreadable → `ModTreeReadError`** (reasoning on the class): this decides which world
    files go along when a map is removed. A save without `isoregiondata/` stays the empty list.
    """
    root = os.path.join(save_dir, ISOREGION_DIR)
    try:
        names = sorted(os.listdir(root))
    except (FileNotFoundError, NotADirectoryError):
        return []
    except OSError as e:
        raise ModTreeReadError(f"{root}: {e}") from e
    return [os.path.join(ISOREGION_DIR, n) for n in names
            if os.path.isfile(os.path.join(root, n))]


def _is_int(text: str) -> bool:
    return bool(text) and (text[1:] if text[0] == "-" else text).isdigit()
