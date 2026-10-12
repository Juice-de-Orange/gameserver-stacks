"""Editing affects ONLY the draft — the running server stays untouched.

That is the core of the safeguard: if every click wrote straight into the production server's
`.ini`, the half-finished configuration would go live on the next arbitrary restart (scheduled,
watchdog, idle autostop) without anyone pressing "Apply".
"""
import json
import os

import pytest

from app.models.server import GameServer, GameType, ManagementMode
from app.services.mods import state
from app.services.mods.preflight import BASE_MAP, ModPlan


def _write(path, text="x"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def _item(root, wid, *, mod_ids=(), requires=(), maps=None):
    base = os.path.join(root, "workshop-mods", "content", "108600", str(wid), "42")
    body = "".join(f"id={m}\n" for m in mod_ids) + "".join(f"require={r}\n" for r in requires)
    _write(os.path.join(base, "mod.info"), body)
    for name, cell_list in (maps or {}).items():
        for x, y in cell_list:
            _write(os.path.join(base, "media", "maps", name, f"{x}_{y}.lotheader"))


@pytest.fixture()
def srv(tmp_path, monkeypatch, mem_engine):
    monkeypatch.setattr(state, "engine", mem_engine)
    root = str(tmp_path)
    _write(os.path.join(root, "data", "Server", "servertest.ini"),
           f"Mods=\nWorkshopItems=\nMap={BASE_MAP}\nPVP=true\n")
    s = GameServer(key="pz-1", game_type=GameType.pz, data_dir=root,
                   management_mode=ManagementMode.dashboard,
                   extra=json.dumps({"servername": "servertest"}))
    s.id = 1
    return s


def _ini_mtime(server) -> float:
    from app.services import ini_editor as ini
    return os.path.getmtime(ini.ini_path(server))


# --- The core contract -------------------------------------------------------------------------
def test_editing_never_touches_the_running_configuration(srv, tmp_path):
    _item(tmp_path, "111", mod_ids=["CityMod"], maps={"Newtown": [(90, 90)]})
    before = _ini_mtime(srv)

    state.add_item(srv, "111")
    state.set_load_order(srv, ["CityMod"])
    state.set_maps(srv, ["Newtown", BASE_MAP])
    state.remove_item(srv, "111")

    assert _ini_mtime(srv) == before                    # file not touched
    assert state.live_plan(srv).mods == ()              # running state unchanged


def test_added_item_brings_its_mod_ids_and_maps_into_the_draft(srv, tmp_path):
    _item(tmp_path, "111", mod_ids=["CityMod"], maps={"Newtown": [(90, 90)]})
    draft = state.add_item(srv, "111")

    assert draft.workshop_items == ("111",)
    assert draft.mods == ("CityMod",)
    assert "Newtown" in draft.maps


def test_removing_an_item_takes_its_mods_and_maps_with_it(srv, tmp_path):
    _item(tmp_path, "111", mod_ids=["CityMod"], maps={"Newtown": [(90, 90)]})
    state.add_item(srv, "111")
    draft = state.remove_item(srv, "111")

    assert draft.workshop_items == ()
    assert draft.mods == ()
    assert "Newtown" not in draft.maps
    # The base map survives every removal — without it vanilla references do not resolve.
    assert draft.maps == (BASE_MAP,)


# --- Map order ---------------------------------------------------------------------------------
def test_base_map_always_ends_up_last(srv, tmp_path):
    _item(tmp_path, "111", mod_ids=["A"], maps={"MapA": [(90, 90)]})
    state.add_item(srv, "111")
    draft = state.set_maps(srv, [BASE_MAP, "MapA", "Rosewood, KY"])

    assert draft.maps[-1] == BASE_MAP
    assert draft.maps.index("MapA") < draft.maps.index("Rosewood, KY")  # mod before vanilla


def test_map_list_is_deduplicated_case_insensitively(srv):
    draft = state.set_maps(srv, ["Newtown", "newtown", "NEWTOWN", BASE_MAP])
    assert [m for m in draft.maps if m.lower() == "newtown"] == ["Newtown"]


def test_missing_base_map_is_added_back(srv):
    assert state.set_maps(srv, ["Newtown"]).maps[-1] == BASE_MAP


# --- Load order --------------------------------------------------------------------------------
def test_dependencies_are_sorted_before_their_dependents(srv, tmp_path):
    _item(tmp_path, "111", mod_ids=["Needs"], requires=["Base"])
    _item(tmp_path, "222", mod_ids=["Base"])
    order = state.sort_load_order(srv, ["Needs", "Base"])
    assert order.index("Base") < order.index("Needs")


def test_sorting_keeps_the_order_when_there_are_no_dependencies(srv, tmp_path):
    _item(tmp_path, "111", mod_ids=["A"])
    _item(tmp_path, "222", mod_ids=["B"])
    assert state.sort_load_order(srv, ["B", "A"]) == ["B", "A"]


def test_dependency_cycles_do_not_hang(srv, tmp_path):
    _item(tmp_path, "111", mod_ids=["A"], requires=["B"])
    _item(tmp_path, "222", mod_ids=["B"], requires=["A"])
    assert sorted(state.sort_load_order(srv, ["A", "B"])) == ["A", "B"]


def test_unknown_dependencies_are_ignored(srv, tmp_path):
    _item(tmp_path, "111", mod_ids=["A"], requires=["DoesNotExist"])
    assert state.sort_load_order(srv, ["A"]) == ["A"]


# --- Draft vs. live ----------------------------------------------------------------------------
def test_draft_starts_as_a_copy_of_the_running_state(srv):
    assert state.get_draft(srv) == state.live_plan(srv)
    assert state.has_changes(state.diff(state.live_plan(srv), state.get_draft(srv))) is False


def test_diff_names_what_would_change(srv, tmp_path):
    _item(tmp_path, "111", mod_ids=["CityMod"], maps={"Newtown": [(90, 90)]})
    state.add_item(srv, "111")
    d = state.diff(state.live_plan(srv), state.get_draft(srv))

    assert d["items_added"] == ["111"]
    assert d["mods_added"] == ["CityMod"]
    assert "Newtown" in d["maps_added"]
    assert state.has_changes(d)
    assert "mod" in state.summarize(d)


def test_reset_discards_the_draft(srv, tmp_path):
    _item(tmp_path, "111", mod_ids=["CityMod"])
    state.add_item(srv, "111")
    assert state.reset_draft(srv) == state.live_plan(srv)


def test_status_reports_pending_changes(srv, tmp_path):
    _item(tmp_path, "111", mod_ids=["CityMod"])
    state.add_item(srv, "111")
    st = state.status(srv, force=True)
    assert st["has_pending_changes"] is True
    assert st["pending_summary"]
