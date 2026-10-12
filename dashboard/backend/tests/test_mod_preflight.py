"""Check catalog for PZ mods — the blockers that must never reach the server.

Network-free: the workshop folders are rebuilt as real file trees in `tmp_path`.
"""
import os
from types import SimpleNamespace

import pytest

from app.services.mods import inventory, preflight
from app.services.mods.preflight import BASE_MAP, ModPlan


def _write(path: str, text: str = "x") -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def _item(root, wid, *, mod_ids=(), requires=(), maps=None, build="42", spawn=()):
    """Put a workshop item on disk (B42 layout with version folder)."""
    base = os.path.join(root, "workshop-mods", "content", "108600", str(wid), build)
    info = "\n".join([f"id={m}" for m in mod_ids] + [f"require={r}" for r in requires])
    _write(os.path.join(base, "mod.info"), info + "\n")
    for name, cell_list in (maps or {}).items():
        map_dir = os.path.join(base, "media", "maps", name)
        for x, y in cell_list:
            _write(os.path.join(map_dir, f"{x}_{y}.lotheader"))
        if name in spawn:
            _write(os.path.join(map_dir, "spawnpoints.lua"))
        if not cell_list:
            _write(os.path.join(map_dir, "map.info"), f"lots={BASE_MAP}\n")


def _server(root):
    return SimpleNamespace(data_dir=str(root), key="pz-test")


def _check(root, plan, **kw):
    return preflight.check(plan, inventory.scan(_server(root)), **kw)


def _codes(result):
    return {f.code for f in result.findings}


# --- The good case ---------------------------------------------------------------------------
def test_a_sound_configuration_passes(tmp_path):
    _item(tmp_path, "111", mod_ids=["CityMod"], maps={"Newtown": [(90, 90)]})
    result = _check(tmp_path, ModPlan(("111",), ("CityMod",), ("Newtown", BASE_MAP)))
    assert result.ok, [f.message for f in result.blockers]


def test_vanilla_only_configuration_passes(tmp_path):
    os.makedirs(os.path.join(tmp_path, "workshop-mods", "content", "108600"))
    assert _check(tmp_path, ModPlan((), (), (BASE_MAP,))).ok


# --- The real crash path ----------------------------------------------------------------------
def test_map_without_provider_is_blocked(tmp_path):
    """The crash path: the download is deleted, the Map= entry stays. Without a provider it must
    not count as "vanilla" and slip through without error — up to the join crash."""
    os.makedirs(os.path.join(tmp_path, "workshop-mods", "content", "108600"))
    result = _check(tmp_path, ModPlan((), (), ("GhostMap", BASE_MAP)))
    assert not result.ok
    assert "MAP_WITHOUT_PROVIDER" in _codes(result)


def test_orphan_map_download_is_blocked(tmp_path):
    """Downloaded but inactive: the server loads the map anyway — disabling is never enough."""
    _item(tmp_path, "222", mod_ids=["Orphan"], maps={"Orphan Town": [(80, 80)]})
    result = _check(tmp_path, ModPlan((), (), (BASE_MAP,)))
    assert not result.ok
    found = [f for f in result.blockers if f.code == "ORPHAN_MAP_DOWNLOAD"]
    assert found and found[0].fix == "remove_download"


# --- Map conflicts -----------------------------------------------------------------------------
def test_overlapping_map_mods_are_blocked_with_cell_details(tmp_path):
    _item(tmp_path, "111", mod_ids=["A"], maps={"MapA": [(90, 90), (90, 91)]})
    _item(tmp_path, "222", mod_ids=["B"], maps={"MapB": [(90, 91), (95, 95)]})
    result = _check(tmp_path, ModPlan(("111", "222"), ("A", "B"), ("MapA", "MapB", BASE_MAP)))

    conflict = [f for f in result.blockers if f.code == "MAP_CELL_CONFLICT"]
    assert conflict, _codes(result)
    assert "90_91" in conflict[0].detail
    assert "share 1 map cell(s)" in conflict[0].message


def test_maps_side_by_side_are_fine(tmp_path):
    _item(tmp_path, "111", mod_ids=["A"], maps={"MapA": [(90, 90)]})
    _item(tmp_path, "222", mod_ids=["B"], maps={"MapB": [(95, 95)]})
    result = _check(tmp_path, ModPlan(("111", "222"), ("A", "B"), ("MapA", "MapB", BASE_MAP)))
    assert "MAP_CELL_CONFLICT" not in _codes(result)


def test_base_map_must_be_present_and_last(tmp_path):
    _item(tmp_path, "111", mod_ids=["A"], maps={"MapA": [(90, 90)]})
    without = _check(tmp_path, ModPlan(("111",), ("A",), ("MapA",)))
    assert "BASE_MAP_MISSING" in _codes(without)

    wrong = _check(tmp_path, ModPlan(("111",), ("A",), (BASE_MAP, "MapA")))
    assert "BASE_MAP_NOT_LAST" in _codes(wrong)


def test_an_empty_map_list_is_not_judged_milder_than_a_half_broken_one(tmp_path):
    """A completely empty `Map=` must trigger the same base-map requirement as a mis-sorted one.

    The trap is a check under `if map_entries:`: a map list in the wrong order is a blocker, an
    **empty** one passes — the more broken configuration is judged more leniently. Since
    `state.status()` and `state.guard_boot()` run the same catalog, the mod traffic light would
    show green and the boot gate would stay open.
    """
    _item(tmp_path, "111", mod_ids=["A"], maps={"MapA": [(90, 90)]})
    empty = _check(tmp_path, ModPlan(("111",), ("A",), ()))
    assert "BASE_MAP_MISSING" in _codes(empty)

    # The boundary, honestly named: without any mod configuration nothing is judged here — a
    # vanilla server without a `Map=` line runs on the base map, and the boot gate must not lock
    # it out of nowhere. (Section 0 of the preflight draws the same boundary for an unreadable
    # directory.)
    nothing_at_all = _check(tmp_path, ModPlan((), (), ()))
    assert "BASE_MAP_MISSING" not in _codes(nothing_at_all)


def test_active_map_mod_missing_from_map_line_is_only_a_warning(tmp_path):
    _item(tmp_path, "111", mod_ids=["A"], maps={"MapA": [(90, 90)]})
    result = _check(tmp_path, ModPlan(("111",), ("A",), (BASE_MAP,)))
    assert "MAP_NOT_LISTED" in {f.code for f in result.warnings}
    assert "MAP_NOT_LISTED" not in {f.code for f in result.blockers}


# --- Items and mod IDs ------------------------------------------------------------------------
def test_missing_download_is_blocked(tmp_path):
    os.makedirs(os.path.join(tmp_path, "workshop-mods", "content", "108600"))
    assert "ITEM_NOT_DOWNLOADED" in _codes(_check(tmp_path, ModPlan(("999",), (), (BASE_MAP,))))


def test_modid_without_provider_is_blocked(tmp_path):
    _item(tmp_path, "111", mod_ids=["Real"])
    result = _check(tmp_path, ModPlan(("111",), ("Real", "Invented"), (BASE_MAP,)))
    assert "MODID_NOT_PROVIDED" in _codes(result)


def test_duplicate_modid_is_blocked(tmp_path):
    _item(tmp_path, "111", mod_ids=["Twice"])
    _item(tmp_path, "222", mod_ids=["Twice"])
    result = _check(tmp_path, ModPlan(("111", "222"), ("Twice",), (BASE_MAP,)))
    assert "DUPLICATE_MODID" in _codes(result)


def test_missing_dependency_is_a_blocker_not_a_warning(tmp_path):
    """Not just a warning — a missing dependency costs the boot."""
    _item(tmp_path, "111", mod_ids=["Needs"], requires=["Base"])
    result = _check(tmp_path, ModPlan(("111",), ("Needs",), (BASE_MAP,)))
    assert "MISSING_DEPENDENCY" in {f.code for f in result.blockers}


# --- The message must point at the RIGHT item -------------------------------------------------
#
# Detection alone is not enough; the instruction must be right too. A loop variable leaking from
# an outer loop (`wid` instead of the own `_wid`) makes the message name the wrong workshop item,
# and whoever follows it removes the wrong one. Hence these tests check `message` and `detail`,
# not only `f.code` — and create TWO items, because the error needs two to show.

def test_duplicate_modid_names_the_real_providers(tmp_path):
    _item(tmp_path, "111", mod_ids=["Twice"])
    _item(tmp_path, "222", mod_ids=["Twice"])
    _item(tmp_path, "999", mod_ids=["Harmless"])          # uninvolved, last in the plan
    result = _check(tmp_path, ModPlan(("111", "222", "999"), ("Twice", "Harmless"), (BASE_MAP,)))

    finding = next(f for f in result.blockers if f.code == "DUPLICATE_MODID")
    assert finding.detail == "111, 222"
    assert "111" in finding.message and "222" in finding.message
    assert "999" not in finding.message and "999" not in finding.detail, (
        "the message points at an innocent item — whoever follows it removes the wrong one"
    )


def test_missing_dependency_names_the_item_that_needs_the_dependency(tmp_path):
    _item(tmp_path, "111", mod_ids=["Needs"], requires=["Missing"])
    _item(tmp_path, "888", mod_ids=["Harmless"])          # uninvolved, last in the plan
    result = _check(tmp_path, ModPlan(("111", "888"), ("Needs", "Harmless"), (BASE_MAP,)))

    finding = next(f for f in result.blockers if f.code == "MISSING_DEPENDENCY")
    assert "111" in finding.message and "888" not in finding.message


def test_map_not_listed_names_the_active_item_not_a_foreign_download(tmp_path):
    """The worst case: a variable leaking from a loop over ALL downloads would name an item that
    is not even in the plan."""
    _item(tmp_path, "111", mod_ids=["A"], maps={"MapA": [(90, 90)]})
    _item(tmp_path, "900", mod_ids=["Foreign"])            # downloaded, NOT active
    result = _check(tmp_path, ModPlan(("111",), ("A",), (BASE_MAP,)))

    finding = next(f for f in result.warnings if f.code == "MAP_NOT_LISTED")
    assert "111" in finding.message and "900" not in finding.message


def test_wrong_load_order_is_a_warning(tmp_path):
    _item(tmp_path, "111", mod_ids=["Needs"], requires=["Base"])
    _item(tmp_path, "222", mod_ids=["Base"])
    result = _check(tmp_path, ModPlan(("111", "222"), ("Needs", "Base"), (BASE_MAP,)))
    assert "LOAD_ORDER" in {f.code for f in result.warnings}
    assert result.ok


def test_build41_only_mod_is_blocked(tmp_path):
    _item(tmp_path, "111", mod_ids=["Old"], build="41")
    result = _check(tmp_path, ModPlan(("111",), (), (BASE_MAP,)))
    assert "BUILD41_ONLY" in _codes(result)


def test_semicolon_in_a_value_is_blocked(tmp_path):
    """A semicolon separates the entries in the .ini — inside a value it silently saws the list apart."""
    os.makedirs(os.path.join(tmp_path, "workshop-mods", "content", "108600"))
    result = _check(tmp_path, ModPlan((), ("Bad;Mod",), (BASE_MAP,)))
    assert "ILLEGAL_CHARACTER" in _codes(result)


# --- fail-closed ------------------------------------------------------------------------------
def test_unreadable_workshop_dir_blocks_instead_of_reporting_success(tmp_path):
    """A read error must not turn into {} → preflight reporting "all good" although it simply
    could not see anything."""
    result = _check(tmp_path, ModPlan(("111",), ("Whatever",), (BASE_MAP,)))
    assert not result.ok
    assert _codes(result) == {"WORKSHOP_DIR_UNREADABLE"}


def test_missing_workshop_dir_is_fine_without_mods(tmp_path):
    assert _check(tmp_path, ModPlan((), (), (BASE_MAP,))).ok


# --- World binding ----------------------------------------------------------------------------
def test_removing_a_map_the_world_already_built_is_blocked(tmp_path):
    _item(tmp_path, "111", mod_ids=["A"], maps={"MapA": [(3, 4)]})
    save = os.path.join(tmp_path, "world")
    _write(os.path.join(save, "map", str(3 * 32), f"{4 * 32}.bin"))   # cell (3,4) generated

    result = _check(
        tmp_path,
        ModPlan(("111",), ("A",), (BASE_MAP,)),          # MapA drops out of the map list
        world_dir=save,
        live_maps=("MapA", BASE_MAP),
    )
    found = [f for f in result.blockers if f.code == "WORLD_MAP_REMOVED"]
    assert found and found[0].fix == "quarantine_cells"
    assert "1 map cell(s)" in found[0].message


def test_removing_a_map_the_world_never_built_is_allowed(tmp_path):
    _item(tmp_path, "111", mod_ids=["A"], maps={"MapA": [(3, 4)]})
    save = os.path.join(tmp_path, "world")
    _write(os.path.join(save, "map", "0", "0.bin"))       # a completely different corner

    result = _check(tmp_path, ModPlan(("111",), ("A",), (BASE_MAP,)),
                    world_dir=save, live_maps=("MapA", BASE_MAP))
    assert "WORLD_MAP_REMOVED" not in _codes(result)


# --- Spawn regions -----------------------------------------------------------------------------
def test_all_eleven_vanilla_towns_are_offered_as_spawns(tmp_path):
    """All eleven towns, not a hard-coded subset — every missing one is a missing spawn."""
    assert len(preflight.VANILLA_SPAWN_REGIONS) == 11
    assert "Ekron, KY" in preflight.VANILLA_SPAWN_REGIONS
    assert "challengemaps" not in preflight.VANILLA_SPAWN_REGIONS


def test_mod_spawn_regions_come_before_the_vanilla_towns(tmp_path):
    _item(tmp_path, "111", mod_ids=["A"], maps={"SpawnTown": []}, spawn=("SpawnTown",))
    regions = preflight.active_spawn_regions(
        ModPlan(("111",), ("A",), (BASE_MAP,)), inventory.scan(_server(tmp_path))
    )
    assert regions[0] == "SpawnTown"
    assert regions[1:] == list(preflight.VANILLA_SPAWN_REGIONS)


def test_preflight_reads_the_disk_only_once(tmp_path, monkeypatch):
    """`/mods/preflight` must scan the workshop directory ONCE per request, not twice.

    Once for the check and once in `plan_world_reset` would be twice: `inventory.scan` has no
    cache and runs `os.walk` twice per workshop entry, plus `cells.find_map_dirs` — map mods bring
    thousands of `.lotheader` files, all on the same disk the running server writes its world to.

    And the UI calls the endpoint after EVERY draft change: moving a mod from position 20 to
    position 1 is 19 single steps, so 19 calls.
    """
    from app.routers import mod_config
    from app.services import ini_editor as ini
    from app.services.mods import state as mod_state

    srv = SimpleNamespace(key="pz-1", game_type=None, data_dir=str(tmp_path), id=1, extra="{}")
    runs: list[str] = []
    real = inventory.scan

    def _counted(server):
        runs.append(server.key)
        return real(server)

    monkeypatch.setattr(mod_config, "_require_pz", lambda s: s)
    monkeypatch.setattr(inventory, "scan", _counted)
    monkeypatch.setattr(mod_config.inventory, "scan", _counted)
    monkeypatch.setattr(mod_state, "live_plan", lambda s: ModPlan(workshop_items=(), mods=(), maps=(BASE_MAP,)))
    monkeypatch.setattr(mod_state, "get_draft", lambda s: ModPlan(workshop_items=(), mods=(), maps=(BASE_MAP,)))
    monkeypatch.setattr(ini, "world_save_path", lambda s: str(tmp_path / "world"))

    mod_config.check_draft(server=srv, _=None)
    assert len(runs) == 1, f"the workshop directory was walked {len(runs)}x: {runs}"
