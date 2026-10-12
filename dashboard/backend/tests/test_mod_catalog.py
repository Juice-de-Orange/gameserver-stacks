"""Curated Arma Reforger mod preset catalog: default, load order, serialization, scoping."""
from app.models.server import GameType
from app.services import mod_catalog as mc


def test_default_preset_is_freedom_fighters():
    p = mc.default_preset_for(GameType.arma_reforger)
    assert p is not None and p.id == "freedom_fighters"
    assert p.sets_scenario and p.scenarioId.endswith("FreedomFighters/Everon.conf")
    assert p.mods[0].modId == "CAFEBEEFF0CACC1A"
    assert ("game.gameProperties.VONCanTransmitCrossFaction", True) in p.config_patch


def test_freedom_fighters_plus_preset():
    # Add-on content, NOT a mode → sets neither scenario nor save slot (layers on top of FF).
    p = mc.by_id_for(GameType.arma_reforger, "freedom_fighters_plus")
    assert p is not None and p.kind == "content" and not p.sets_scenario and p.scenarioId == ""
    ids = [m.modId for m in p.mods]
    assert len(ids) == 40 and len(set(ids)) == 40  # 40 add-on mods, no dupes
    assert all(len(i) == 16 and all(c in "0123456789ABCDEF" for c in i) for i in ids)  # valid GUIDs
    assert ids[0] == "5D6EA74A94173EDF"  # Enfusion DB framework first
    assert "CAFEBEEFF0CACC1A" not in ids  # FF base mod NOT included (the FF mode brings it)
    assert "621D3771875C1D3D" not in ids  # Car Radio4All (broken) left out


def test_overthrow_dependency_load_order():
    p = mc.by_id_for(GameType.arma_reforger, "overthrow_eden")
    # Deps FIRST: Enfusion DB → Enfusion Persistence → Overthrow.
    assert [m.modId for m in p.mods] == ["5D6EA74A94173EDF", "5D6EBC81EB1842EF", "59B657D731E2A11D"]
    assert p.scenarioId == "{3DAD390C31623F04}Missions/24_OVT_Eden.conf"


def test_realism_addon_is_optional_content():
    p = mc.by_id_for(GameType.arma_reforger, "realism_addon")
    assert p.kind == "content" and not p.sets_scenario
    assert {m.modId for m in p.mods} == {"595F2BF2F44836FB", "60C4E0B49618CC62", "5964E0B3BB7410CE"}


def test_serialize_shape_is_json_friendly():
    presets = mc.serialize_for(GameType.arma_reforger)
    assert isinstance(presets, list) and presets
    first = presets[0]
    for k in ("id", "label", "description", "kind", "group", "mods", "scenarioId", "sets_scenario", "config_patch"):
        assert k in first
    assert isinstance(first["mods"], list) and "modId" in first["mods"][0]


def test_catalog_empty_for_other_games():
    assert mc.catalog_for(GameType.minecraft) == ()
    assert mc.serialize_for(GameType.pz) == []
    assert mc.default_preset_for(GameType.vintagestory) is None
    assert mc.by_id_for(GameType.arma_reforger, "does-not-exist") is None


def test_vanilla_and_extra_map_presets():
    ids = {p.id for p in mc.catalog_for(GameType.arma_reforger)}
    assert {
        "game_master_everon", "game_master_arland", "conflict_everon", "conflict_arland",
        "freedom_fighters_kolguyev",
    } <= ids
    # Game Master + Conflict are vanilla (no mods) but do set the scenario.
    gm = mc.by_id_for(GameType.arma_reforger, "game_master_everon")
    assert gm.mods == () and gm.sets_scenario and gm.scenarioId.endswith("21_GM_Eden.conf")
    conflict = mc.by_id_for(GameType.arma_reforger, "conflict_everon")
    assert conflict.mods == () and conflict.scenarioId.endswith("23_Campaign.conf")
    # Kolguyev = 3rd FF map (with FF mod + VON patch).
    kol = mc.by_id_for(GameType.arma_reforger, "freedom_fighters_kolguyev")
    assert kol.mods[0].modId == "CAFEBEEFF0CACC1A" and kol.scenarioId.endswith("Kolguyev.conf")
