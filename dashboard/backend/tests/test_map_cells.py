"""Cell math for PZ maps — the basis of the map checks and the world quarantine.

The constants were measured on a real Build 42 server; these tests pin them so a later "cleanup"
does not silently change them.
"""
import os

from app.services.mods import cells


def _touch(path: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write("x")


def _map_with_cells(root: str, name: str, coords) -> str:
    map_dir = os.path.join(root, "media", "maps", name)
    os.makedirs(map_dir, exist_ok=True)
    for x, y in coords:
        _touch(os.path.join(map_dir, f"{x}_{y}.lotheader"))
    return map_dir


# --- Reading lotheaders --------------------------------------------------------------------
def test_cells_are_read_from_lotheader_names(tmp_path):
    d = _map_with_cells(str(tmp_path), "TestMap", [(0, 18), (1, 18), (12, 34)])
    assert cells.cells_of_map_dir(d) == {(0, 18), (1, 18), (12, 34)}


def test_negative_cell_coordinates_are_accepted(tmp_path):
    """B42 extended the map westwards — negative coordinates occur for real."""
    d = _map_with_cells(str(tmp_path), "West", [(-5, 20), (-1, 34)])
    assert cells.cells_of_map_dir(d) == {(-5, 20), (-1, 34)}


def test_only_lotheader_files_count(tmp_path):
    d = _map_with_cells(str(tmp_path), "Mixed", [(3, 4)])
    _touch(os.path.join(d, "map.info"))
    _touch(os.path.join(d, "spawnpoints.lua"))
    _touch(os.path.join(d, "world_3_4.lotpack"))     # tile data, but no cell index
    _touch(os.path.join(d, "chunkdata_3_4.bin"))
    assert cells.cells_of_map_dir(d) == {(3, 4)}


def test_spawn_overlay_without_tiles_is_not_a_tile_map(tmp_path):
    """In B42, 10 of the 11 vanilla towns carry NO tiles — they point to Muldraugh via `lots=`.
    Only real tile maps belong in `Map=`."""
    overlay = os.path.join(str(tmp_path), "media", "maps", "Rosewood, KY")
    os.makedirs(overlay)
    _touch(os.path.join(overlay, "spawnpoints.lua"))
    with open(os.path.join(overlay, "map.info"), "w", encoding="utf-8") as f:
        f.write("title=Rosewood\nlots=Muldraugh, KY\n")

    assert cells.is_tile_map(overlay) is False
    assert cells.lots_of(overlay) == "Muldraugh, KY"


def test_find_map_dirs_locates_maps_in_b42_version_folders(tmp_path):
    """B42 mods come as `common/` + `42/`, each with its own media tree."""
    item = os.path.join(str(tmp_path), "3399505732")
    _map_with_cells(os.path.join(item, "42"), "NewTown", [(50, 50)])
    _map_with_cells(os.path.join(item, "common"), "OldTown", [(51, 51)])
    assert set(cells.find_map_dirs(item)) == {"NewTown", "OldTown"}


# --- Conflicts -----------------------------------------------------------------------------
def test_overlapping_maps_are_reported_with_the_shared_cells():
    found = cells.conflicts({
        "MapA": {(10, 10), (10, 11), (10, 12)},
        "MapB": {(10, 11), (10, 12), (99, 99)},
        "MapC": {(70, 70)},
    })
    assert len(found) == 1
    c = found[0]
    assert (c.map_a, c.map_b) == ("MapA", "MapB")
    assert c.cells == ((10, 11), (10, 12))
    assert c.count == 2
    assert c.sample() == ["10_11", "10_12"]


def test_maps_side_by_side_do_not_conflict():
    assert cells.conflicts({"A": {(1, 1)}, "B": {(2, 2)}, "C": {(3, 3)}}) == []


# --- Cell ↔ chunk --------------------------------------------------------------------------
def test_chunk_range_matches_the_measured_server_values():
    """Cross-check against real data: cells up to 46, chunk folders up to 1503 — and 46*32+31 == 1503."""
    r = cells.chunk_range(46)
    assert (r.start, r.stop - 1) == (1472, 1503)
    assert len(r) == cells.CHUNKS_PER_CELL == 32
    assert cells.cell_of_chunk(1503) == 46
    assert cells.cell_of_chunk(0) == 0


def test_cell_of_chunk_rounds_down_for_negative_coordinates():
    """ChunkX -160 belonged to wilderness cell -5 (observed for real), not to -4."""
    assert cells.cell_of_chunk(-160) == -5
    assert cells.cell_of_chunk(-1) == -1


# --- World ↔ cells -------------------------------------------------------------------------
def _world(tmp_path) -> str:
    """Replica of a B42 save: cell (1,2) generated, cell (5,5) not."""
    save = os.path.join(str(tmp_path), "Saves", "Multiplayer", "servertest")
    _touch(os.path.join(save, "map", "32", "64.bin"))     # cell (1,2)
    _touch(os.path.join(save, "map", "63", "95.bin"))     # cell (1,2), other corner
    _touch(os.path.join(save, "chunkdata", "chunkdata_1_2.bin"))
    _touch(os.path.join(save, "apop", "apop_1_2.bin"))
    _touch(os.path.join(save, "zpop", "zpop_1_2.bin"))
    _touch(os.path.join(save, "metagrid", "metacell_1_2.bin"))
    _touch(os.path.join(save, "zpop", "zpop_virtual.bin"))  # NOT a cell file — the trap
    _touch(os.path.join(save, "players.db"))
    _touch(os.path.join(save, "isoregiondata", "datachunk_248_1843.bin"))
    return save


def test_generated_cells_only_reports_cells_the_world_actually_wrote(tmp_path):
    save = _world(tmp_path)
    assert cells.generated_cells(save, {(1, 2), (5, 5)}) == {(1, 2)}


def test_generated_cells_is_empty_without_a_world(tmp_path):
    assert cells.generated_cells(str(tmp_path / "missing"), {(1, 2)}) == set()


def test_world_files_cover_every_cell_store(tmp_path):
    save = _world(tmp_path)
    found = cells.world_files_for_cells(save, {(1, 2)})
    assert os.path.join("map", "32", "64.bin") in found
    assert os.path.join("map", "63", "95.bin") in found
    for folder, prefix in cells.CELL_STORES:
        assert os.path.join(folder, f"{prefix}_1_2.bin") in found


def test_world_files_never_touch_non_cell_files(tmp_path):
    """`zpop_virtual.bin` really sits in the save and a regex like `zpop_.*\\.bin` would delete it
    too. Because names are constructed rather than searched, that cannot happen."""
    save = _world(tmp_path)
    found = cells.world_files_for_cells(save, {(1, 2), (5, 5)})
    assert not any(f.endswith("zpop_virtual.bin") for f in found)
    assert not any(f.endswith("players.db") for f in found)
    # Cells never generated yield no ghost paths.
    assert all("_5_5" not in f for f in found)


def test_world_files_are_stable_and_relative(tmp_path):
    save = _world(tmp_path)
    found = cells.world_files_for_cells(save, {(1, 2)})
    assert found == cells.world_files_for_cells(save, {(1, 2)})   # reproducible for the manifest
    assert all(not os.path.isabs(f) for f in found)


def test_isoregion_is_handled_as_a_whole(tmp_path):
    save = _world(tmp_path)
    assert cells.isoregion_files(save) == [os.path.join("isoregiondata", "datachunk_248_1843.bin")]
    assert cells.isoregion_files(str(tmp_path / "empty")) == []
