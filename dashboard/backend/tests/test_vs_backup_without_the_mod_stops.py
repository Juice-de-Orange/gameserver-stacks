"""Vintage Story without the RCON mod is stopped for a backup instead of copied while it runs.

Without the mod there is no console, so the save point (`genbackup`) can never be set: every
backup logged "genbackup failed" and packed the running SQLite world with its `-wal`, while the
page said "completed" (A14 recheck). Like Valheim, such a server needs the stop. With the mod the
save command is the cheaper way and stays; when the data mount is missing (cannot tell) nothing
changes.
"""
from __future__ import annotations

from app.models.server import GameServer, GameType
from app.services.drivers.vintagestory import VintageStoryDriver


def _vs(tmp_path) -> GameServer:
    return GameServer(key="vs-1", game_type=GameType.vintagestory, data_dir=str(tmp_path),
                      container_name="vs-1", extra="{}")


def test_without_the_mod_the_backup_stops_the_server(tmp_path):
    (tmp_path / "gamedata" / "vs" / "Mods").mkdir(parents=True)
    spec = VintageStoryDriver().backup_spec(_vs(tmp_path))
    assert spec.stop_for_consistency is True
    assert spec.pre_hook is None, "no genbackup attempt that can only fail"


def test_with_the_mod_the_save_command_stays(tmp_path):
    (tmp_path / "gamedata" / "vs" / "ModConfig").mkdir(parents=True)
    (tmp_path / "gamedata" / "vs" / "ModConfig" / "vsrcon.json").write_text("{}")
    spec = VintageStoryDriver().backup_spec(_vs(tmp_path))
    assert spec.stop_for_consistency is False
    assert spec.pre_hook is not None


def test_unknown_mod_state_keeps_the_old_behaviour(tmp_path):
    spec = VintageStoryDriver().backup_spec(_vs(tmp_path / "missing"))
    assert spec.stop_for_consistency is False
    assert spec.pre_hook is not None
