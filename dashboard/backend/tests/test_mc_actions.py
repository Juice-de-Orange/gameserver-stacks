"""Minecraft actions — catalog + parameter validation + command building.

DB-free: checks only the pure catalog/validation logic (no RCON/Docker).
"""
import pytest
from fastapi import HTTPException

from app.models.server import GameType
from app.routers.actions import _validate
from app.services import action_catalog as cat


def test_catalogs_present_per_game():
    ids = {a["id"] for a in cat.serialize_for(GameType.minecraft)}
    assert {"mc_give", "mc_weather", "mc_time", "mc_gamemode", "mc_effect"} <= ids
    # Vintage Story has a small catalog: at least a server message.
    vs_ids = {a["id"] for a in cat.serialize_for(GameType.vintagestory)}
    assert "vs_announce" in vs_ids
    # The PZ catalog stays reachable unchanged (backwards compatibility).
    assert cat.by_id_for(GameType.pz, "additem") is not None


def test_mc_give_builds_expected_command():
    spec = cat.by_id_for(GameType.minecraft, "mc_give")
    built = _validate(spec, {"player": "Alice", "item": "minecraft:diamond", "count": "5"}, ["Alice"])
    assert spec.template.format(**built) == "give Alice minecraft:diamond 5"


def test_mc_gamemode_builds_expected_command():
    spec = cat.by_id_for(GameType.minecraft, "mc_gamemode")
    built = _validate(spec, {"mode": "creative", "player": "Alice"}, ["Alice"])
    assert spec.template.format(**built) == "gamemode creative Alice"


def test_mc_enum_rejects_unknown_value():
    spec = cat.by_id_for(GameType.minecraft, "mc_weather")
    with pytest.raises(HTTPException):
        _validate(spec, {"state": "sunny", "duration": "600"}, [])


def test_mc_item_rejects_command_injection():
    spec = cat.by_id_for(GameType.minecraft, "mc_give")
    with pytest.raises(HTTPException):
        _validate(spec, {"player": "Alice", "item": "diamond; op Alice", "count": "1"}, ["Alice"])


def test_mc_player_must_be_online():
    spec = cat.by_id_for(GameType.minecraft, "mc_give")
    with pytest.raises(HTTPException):
        _validate(spec, {"player": "Bob", "item": "minecraft:diamond", "count": "1"}, ["Alice"])
