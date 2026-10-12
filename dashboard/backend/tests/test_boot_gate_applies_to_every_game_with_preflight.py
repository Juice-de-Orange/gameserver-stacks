"""No game with a preflight falls out of the boot gate.

`mods/state.guard_boot` must not stop at PZ. Avorion has `preflight_avorion`; Minecraft, Vintage
Story, Valheim and Factorio have `mods/foreign` — both feed the mod traffic light and show red
there. If the guard before the start did not ask them, a red mod save could be armed via the
start button, the restart schedule, watchdog autoheal, version change or restore — exactly the
five places where `guard_boot` is meant to stand according to its own docstring.

For Factorio that is the most expensive case: an incomplete mod set is not a crash you see in the
log but a door that stays shut.

**This test is deliberately not written as a hand-maintained list.** The games with a preflight
are derived from `foreign.EXTENSIONS`/`FOLDERS_ARE_MODS` and the two special cases — an eighth
game is covered automatically instead of being forgotten.
"""
from __future__ import annotations

import asyncio

import pytest

from app.models.server import GameServer, GameType
from app.services.mods import foreign
from app.services.mods import state as mod_state


def _with_preflight() -> set[GameType]:
    """Every game for which any check exists at all."""
    return {GameType.pz, GameType.avorion} | set(foreign.EXTENSIONS) | set(foreign.FOLDERS_ARE_MODS)


def _srv(game_type: GameType, tmp_path) -> GameServer:
    s = GameServer(key="x", game_type=game_type, container_name="x", data_dir=str(tmp_path),
                   display_name="x", server_type="PAPER")
    s.id = 1
    return s


def test_there_are_games_with_a_preflight_at_all() -> None:
    """Otherwise the test below would be green because it runs over an empty set."""
    assert len(_with_preflight()) >= 5


@pytest.mark.parametrize("game_type", sorted(_with_preflight(), key=lambda g: g.value))
def test_every_game_with_a_preflight_is_checked_before_the_start(game_type, tmp_path, monkeypatch, mem_engine):
    """`live_check` must return a result for this game — not `None`.

    `None` means "there is nothing to check for this game", and a PZ-only guard gave exactly that
    answer for every other game.
    """
    monkeypatch.setattr(mod_state, "engine", mem_engine)
    srv = _srv(game_type, tmp_path)
    result = asyncio.run(mod_state.live_check(srv))
    assert result is not None, (
        f"{game_type.value} has a preflight but is not asked before the start"
    )


def test_a_game_without_a_preflight_stays_unchecked(tmp_path, monkeypatch, mem_engine):
    """The other direction: where there is nothing to check, nothing is invented.

    Arma Reforger manages its mods via JSON presets the server pulls itself — there is no mod
    folder that could be checked beforehand.
    """
    monkeypatch.setattr(mod_state, "engine", mem_engine)
    assert asyncio.run(mod_state.live_check(_srv(GameType.arma_reforger, tmp_path))) is None
    assert asyncio.run(mod_state.guard_boot(_srv(GameType.arma_reforger, tmp_path))) == []


def test_the_guard_never_blocks_through_its_own_error(tmp_path, monkeypatch, mem_engine):
    """A guard that itself becomes the outage is worse than the problem."""
    monkeypatch.setattr(mod_state, "engine", mem_engine)

    async def _boom(server):
        raise RuntimeError("broken")

    monkeypatch.setattr(mod_state, "live_check", _boom)
    assert asyncio.run(mod_state.guard_boot(_srv(GameType.factorio, tmp_path))) == []


# --------------------------------------------------------------- an unreadable mod folder
#
# Owner's decision: the dashboard failing to READ a mod folder never blocks a start, restart,
# autoheal, version change or restore — for every game. It becomes one warning,
# `MOD_FOLDER_UNREADABLE`, through `live_check`, so the start check and the status light give
# the same answer. (Applying a DRAFT stays fail-closed; that is `preflight.check`, not this.)

def _foreign_games() -> list[GameType]:
    return sorted(set(foreign.EXTENSIONS) | set(foreign.FOLDERS_ARE_MODS), key=lambda g: g.value)


@pytest.mark.parametrize("game_type", _foreign_games(), ids=lambda g: g.value)
def test_an_unreadable_mod_folder_warns_but_does_not_block_a_file_based_game(
    game_type, tmp_path, monkeypatch, mem_engine,
):
    from app.routers import mod_config

    monkeypatch.setattr(mod_state, "engine", mem_engine)

    def _unlistable(server):
        raise OSError("The mod folder /x/mods exists but cannot be listed (Permission denied)")

    monkeypatch.setattr(foreign, "scan", _unlistable)
    srv = _srv(game_type, tmp_path)

    result = asyncio.run(mod_state.live_check(srv))
    assert [(f.code, f.level) for f in result.findings] == [("MOD_FOLDER_UNREADABLE", "warning")]
    assert "Permission denied" in result.findings[0].detail
    assert asyncio.run(mod_state.guard_boot(srv)) == [], "an unreadable folder blocked the start"

    light = asyncio.run(mod_config.mod_status(server=srv, _=None))
    assert (light["level"], light["blockers"], light["warnings"]) == ("warn", 0, 1)
    assert light["headline"].startswith(result.findings[0].message[:40])


def test_an_unreadable_mod_folder_warns_but_does_not_block_pz(tmp_path, monkeypatch, mem_engine):
    """Before: `WORKSHOP_DIR_UNREADABLE` as a BLOCKER — PZ alone refused the start."""
    import json
    import os

    from app.services.mods import inventory

    monkeypatch.setattr(mod_state, "engine", mem_engine)
    srv = _srv(GameType.pz, tmp_path)
    srv.extra = json.dumps({"servername": "servertest"})
    ini = os.path.join(tmp_path, "data", "Server", "servertest.ini")
    os.makedirs(os.path.dirname(ini), exist_ok=True)
    with open(ini, "w", encoding="utf-8") as f:
        f.write("Mods=SomeMod\nWorkshopItems=111\nMap=Muldraugh, KY\n")
    monkeypatch.setattr(inventory, "scan", lambda server: inventory.Inventory(
        items={}, readable=False, detail="Permission denied"))
    mod_state.invalidate(srv.key)

    result = asyncio.run(mod_state.live_check(srv))
    assert [(f.code, f.level) for f in result.findings] == [("MOD_FOLDER_UNREADABLE", "warning")]
    assert asyncio.run(mod_state.guard_boot(srv)) == [], "an unreadable folder blocked the start"

    light = mod_state.status(srv, force=True)
    assert (light["level"], light["blockers"], light["warnings"]) == ("warn", 0, 1)
    assert light["headline"].startswith(result.findings[0].message[:40])
