"""The config traffic light (`GET /{key}/mods/status`) must not be hard-wired green for three games.

The route used to answer every non-PZ server with a hard-coded dummy:
`{"level": "none", "blockers": 0, "warnings": 0, …}`. That was right while only Project Zomboid
had a preflight. Now `mods.foreign.check` (MOD_WRONG_LOADER, MODFILE_UNREADABLE, MOD_DUPLICATE,
FAC_DEP_MISSING, VH_LOADER_OFF) and `mods.preflight_avorion.check` (AV_MODCONFIG_UNREADABLE,
AV_ITEM_REMOVED, …) deliver real BLOCKERS for Factorio, Valheim and Avorion.

`ModHealth.tsx` hides chip and card entirely on `level === "none"`. The question this component
exists to answer ("is my mod configuration still working?", without opening a page first) would
stay unanswered for those games — and for Factorio that is the most expensive case: an incomplete
mod set is "not a crash you see in the log but a door that stays shut".
"""
from __future__ import annotations

import asyncio
import json
import zipfile
from pathlib import Path

from app.models.server import GameServer, GameType
from app.routers.mod_config import mod_status
from app.services.mods import foreign


def _srv(tmp_path, game, **over):
    base = dict(key="x", game_type=game, data_dir=str(tmp_path), container_name="c")
    base.update(over)
    return GameServer(**base)


def _fac_mod(folder: Path, name: str, version: str, deps=None) -> str:
    folder.mkdir(parents=True, exist_ok=True)
    filename = f"{name}_{version}.zip"
    info = {"name": name, "version": version, "factorio_version": "2.0", "title": name}
    if deps:
        info["dependencies"] = deps
    with zipfile.ZipFile(folder / filename, "w") as z:
        z.writestr(f"{name}_{version}/info.json", json.dumps(info))
    return filename


def test_a_factorio_blocker_reaches_the_traffic_light(tmp_path):
    """A missing required dependency is exactly the case nobody sees in the log."""
    srv = _srv(tmp_path, GameType.factorio, image_tag="2.0.77")
    folder = Path(foreign.mods_dir(srv))
    _fac_mod(folder, "K2Assets", "1.0.0", deps=["Krastorio2 >= 1.3"])

    response = asyncio.run(mod_status(server=srv, _=None))
    assert response["level"] == "blocked"
    assert response["blockers"] >= 1
    assert response["headline"], "a traffic light without text tells the operator nothing"


def test_a_clean_mod_set_is_green(tmp_path):
    srv = _srv(tmp_path, GameType.factorio, image_tag="2.0.77")
    _fac_mod(Path(foreign.mods_dir(srv)), "Krastorio2", "1.3.24", deps=["base >= 2.0"])
    response = asyncio.run(mod_status(server=srv, _=None))
    assert response["level"] == "ok" and response["blockers"] == 0
    assert response["mods"] == 1


def test_minecraft_is_evaluated_as_well(tmp_path):
    """Minecraft already had a preflight — the dummy hid it just the same."""
    srv = _srv(tmp_path, GameType.minecraft)
    folder = Path(foreign.mods_dir(srv))
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "broken.jar").write_bytes(b"this is not a jar")
    response = asyncio.run(mod_status(server=srv, _=None))
    assert response["level"] == "blocked"


def test_a_game_without_mod_management_stays_silent(tmp_path):
    """Arma Reforger has no file-based mod path — there `none` is the right answer."""
    srv = _srv(tmp_path, GameType.arma_reforger)
    assert asyncio.run(mod_status(server=srv, _=None))["level"] == "none"
