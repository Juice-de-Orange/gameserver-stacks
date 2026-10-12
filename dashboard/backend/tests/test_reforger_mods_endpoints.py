"""Scoped Reforger mod endpoints (server_ops): catalog, replace, preset apply, remove, gating.

DB-free like test_servers_router: the endpoint functions are called directly, `record` (audit)
is stubbed, and a fake driver captures set_mods/set_scenario/patch_config.
"""
import asyncio
import types

import pytest
from fastapi import HTTPException

from app.models.server import GameType
from app.routers import server_ops
from app.routers.server_ops import ModsReplace, PresetApply


def _srv(game_type=GameType.arma_reforger, key="rf"):
    return types.SimpleNamespace(key=key, game_type=game_type)


class FakeReforger:
    """Fake driver with the Reforger write methods (no file/RCON access)."""

    def __init__(self, existing=None):
        self._mods = list(existing or [])
        self.scenario = None
        self.patched = None

    def list_mods(self, server):
        return {"mods": self._mods, "scenarioId": self.scenario or ""}

    def set_mods(self, server, mods):
        self._mods = list(mods)

    def set_scenario(self, server, sid):
        self.scenario = sid

    def patch_config(self, server, pairs):
        self.patched = dict(pairs)


@pytest.fixture(autouse=True)
def _no_audit(monkeypatch):
    monkeypatch.setattr(server_ops, "record", lambda *a, **k: None)


def test_catalog_endpoint_lists_presets():
    out = server_ops.mods_catalog(server=_srv(), _=None)
    ids = {p["id"] for p in out["presets"]}
    assert {"freedom_fighters", "overthrow_eden", "realism_addon"} <= ids


def test_catalog_empty_for_non_reforger():
    out = server_ops.mods_catalog(server=_srv(GameType.minecraft, "mc"), _=None)
    assert out["presets"] == []


def test_replace_mods():
    drv = FakeReforger()
    out = server_ops.replace_mods(ModsReplace(mods=[{"modId": "AAA"}]), server=_srv(), driver=drv, session=None, user=None)
    assert out["restart_required"] is True and drv._mods == [{"modId": "AAA"}]


def test_apply_preset_merges_sets_scenario_and_patches():
    drv = FakeReforger(existing=[{"modId": "EXISTING"}])
    out = asyncio.run(
        server_ops.apply_preset(
            PresetApply(preset_id="freedom_fighters"), server=_srv(), driver=drv, session=None, user=None
        )
    )
    # fake without active_mode/save_swap_script → no mode switch path → restart_required True
    assert out["scenario_set"] is True and out["restart_required"] is True
    ids = [m["modId"] for m in drv._mods]
    assert "EXISTING" in ids and "CAFEBEEFF0CACC1A" in ids
    assert drv.scenario.endswith("FreedomFighters/Everon.conf")
    assert drv.patched == {"game.gameProperties.VONCanTransmitCrossFaction": True}


class FakeReforgerWithSlots(FakeReforger):
    """Like above, but WITH `active_mode`/`save_swap_script`/`has_live_save`.

    The fake above has none of these, so it never reaches the mode switch path, where saves are
    moved between directories.
    """

    def __init__(self, *, active_mode=None, live_save=True, existing=None):
        super().__init__(existing)
        self._active_mode = active_mode
        self._live_save = live_save

    def active_mode(self, server):
        return self._active_mode

    def has_live_save(self, server):
        return self._live_save

    def save_swap_script(self):
        return "echo SWAP_DONE"


def test_a_mode_switch_without_a_detectable_source_mode_is_refused():
    """`preset.id != from_mode` is always true with `None`: an unknown mode would slip silently
    into the switch path, where the live save cannot be put into any archive."""
    drv = FakeReforgerWithSlots(active_mode=None, live_save=True)

    with pytest.raises(HTTPException) as ei:
        asyncio.run(server_ops.apply_preset(
            PresetApply(preset_id="freedom_fighters"), server=_srv(), driver=drv, session=None, user=None
        ))

    assert ei.value.status_code == 409
    assert ei.value.detail["code"] == "save.mode_undetermined"
    assert drv.scenario is None, "the config was written despite the refusal"


def test_without_a_save_an_unknown_source_mode_is_harmless(monkeypatch):
    """Fresh server, no save yet: nothing to lose, the switch may run."""
    drv = FakeReforgerWithSlots(active_mode=None, live_save=False)
    switches: list[tuple] = []

    async def _fake_switch(server, driver, from_mode, to_mode, apply_config):
        switches.append((from_mode, to_mode))
        apply_config()

    monkeypatch.setattr(server_ops, "_switch_mode_with_saves", _fake_switch)

    out = asyncio.run(server_ops.apply_preset(
        PresetApply(preset_id="freedom_fighters"), server=_srv(), driver=drv, session=None, user=None
    ))

    assert out["mode_switched"] is True
    assert switches == [(None, "freedom_fighters")]


def test_apply_preset_dedups_existing_preset_mod():
    # preset mod already present → not duplicated, moved to the end as a block instead.
    drv = FakeReforger(existing=[{"modId": "CAFEBEEFF0CACC1A"}, {"modId": "OTHER"}])
    asyncio.run(server_ops.apply_preset(PresetApply(preset_id="freedom_fighters"), server=_srv(), driver=drv, session=None, user=None))
    ids = [m["modId"] for m in drv._mods]
    assert ids.count("CAFEBEEFF0CACC1A") == 1 and ids[-1] == "CAFEBEEFF0CACC1A" and "OTHER" in ids


def test_apply_unknown_preset_404():
    with pytest.raises(HTTPException) as ei:
        asyncio.run(server_ops.apply_preset(PresetApply(preset_id="nope"), server=_srv(), driver=FakeReforger(), session=None, user=None))
    assert ei.value.status_code == 404


def test_remove_mod():
    drv = FakeReforger(existing=[{"modId": "AAA"}, {"modId": "BBB"}])
    out = server_ops.remove_mod("AAA", server=_srv(), driver=drv, session=None, user=None)
    assert out["restart_required"] is True and [m["modId"] for m in drv._mods] == ["BBB"]


def test_write_rejects_driver_without_set_mods():
    class Plain:  # no set_mods → 400 (game without mod management)
        pass

    with pytest.raises(HTTPException) as ei:
        server_ops.replace_mods(ModsReplace(mods=[]), server=_srv(), driver=Plain(), session=None, user=None)
    assert ei.value.status_code == 400
