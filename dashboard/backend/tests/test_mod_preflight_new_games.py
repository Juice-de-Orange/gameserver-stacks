"""Mod preflight for Factorio, Valheim and Avorion.

Three games, three very different degrees of verifiability — and that is the point of this file:
* **Factorio** declares dependencies machine-readably. Here they are really RESOLVED.
* **Valheim** keeps its metadata in .NET attributes. What can be checked is checked; the rest is
  reported explicitly as "unverifiable", not as "fine".
* **Avorion** has no files at all, only workshop IDs — the list is checked against Steam.
"""
import asyncio
import json
import os
import zipfile
from pathlib import Path

import pytest

from app.models.server import GameServer, GameType
from app.services.mods import foreign, preflight_avorion
from app.services.mods.avorion_apply import build_modconfig
from app.services.mods.preflight_avorion import ids_and_allowed


def _srv(tmp_path, game, **over):
    base = dict(key="x", game_type=game, data_dir=str(tmp_path), container_name="c")
    base.update(over)
    return GameServer(**base)


def _codes(result):
    return {f.code for f in result.findings}


# ================================================================== Factorio

def _fac_mod(folder: Path, name: str, version: str, *, deps=None, game="2.0") -> str:
    """A real Factorio mod zip with `info.json` where Factorio expects it."""
    folder.mkdir(parents=True, exist_ok=True)
    filename = f"{name}_{version}.zip"
    info = {"name": name, "version": version, "title": name, "factorio_version": game}
    if deps is not None:
        info["dependencies"] = deps
    with zipfile.ZipFile(folder / filename, "w") as z:
        z.writestr(f"{name}_{version}/info.json", json.dumps(info))
    return filename


@pytest.fixture()
def fac(tmp_path):
    # **`image_tag`, not `game_version`** — for Factorio `version_source = image_tag`, and
    # `game_version` stays empty on a newly created server. A fixture that sets the field that is
    # never filled in reality confirms a check that is switched off live.
    srv = _srv(tmp_path, GameType.factorio, image_tag="2.0.77", image_repo="factoriotools/factorio")
    return srv, Path(foreign.mods_dir(srv))


def test_factorio_reads_the_info_json(fac):
    srv, folder = fac
    file = _fac_mod(folder, "Krastorio2", "1.3.24", deps=["base >= 2.0"])
    mods = foreign.scan(srv)
    assert [m.mod_id for m in mods] == ["Krastorio2"]
    assert mods[0].version == "1.3.24" and mods[0].loader == "factorio"
    assert mods[0].dependencies == ("base >= 2.0",)
    assert asyncio.run(foreign.check(srv, {file})).ok


def test_a_missing_required_dependency_is_a_blocker(fac):
    """The case that is particularly expensive with Factorio.

    The server does start with an incomplete mod set — but nobody could get in, because Factorio
    compares the client's mod set EXACTLY. A crash would show in the log; a door that stays shut
    does not.
    """
    srv, folder = fac
    file = _fac_mod(folder, "K2Assets", "1.0.0", deps=["Krastorio2 >= 1.3"])
    result = asyncio.run(foreign.check(srv, {file}))
    assert "FAC_DEP_MISSING" in _codes(result) and not result.ok


def test_a_missing_optional_dependency_has_no_consequence(fac):
    srv, folder = fac
    file = _fac_mod(folder, "Helper", "1.0.0", deps=["? Krastorio2", "(?) Hidden"])
    assert asyncio.run(foreign.check(srv, {file})).ok


def test_base_and_the_dlc_mods_count_as_present(fac):
    """They do not exist as files — without this exception `base` would be a blocker in EVERY
    mod set."""
    srv, folder = fac
    file = _fac_mod(folder, "Thing", "1.0.0", deps=["base >= 2.0", "space-age", "quality"])
    assert asyncio.run(foreign.check(srv, {file})).ok


def test_a_too_old_dependency_is_noticed(fac):
    srv, folder = fac
    old = _fac_mod(folder, "Krastorio2", "1.2.0")
    new = _fac_mod(folder, "K2Assets", "1.0.0", deps=["Krastorio2 >= 1.3"])
    result = asyncio.run(foreign.check(srv, {old, new}))
    assert "FAC_DEP_VERSION" in _codes(result) and not result.ok


def test_a_satisfied_version_requirement_does_no_harm(fac):
    srv, folder = fac
    a = _fac_mod(folder, "Krastorio2", "1.3.24")
    b = _fac_mod(folder, "K2Assets", "1.0.0", deps=["Krastorio2 >= 1.3"])
    assert asyncio.run(foreign.check(srv, {a, b})).ok


def test_a_declared_incompatibility_blocks(fac):
    srv, folder = fac
    a = _fac_mod(folder, "PrettyWorld", "1.0.0", deps=["! UglyWorld"])
    b = _fac_mod(folder, "UglyWorld", "1.0.0")
    result = asyncio.run(foreign.check(srv, {a, b}))
    assert "FAC_INCOMPATIBLE" in _codes(result) and not result.ok


def test_an_incompatibility_without_the_counterpart_is_no_problem(fac):
    srv, folder = fac
    a = _fac_mod(folder, "PrettyWorld", "1.0.0", deps=["! UglyWorld"])
    assert asyncio.run(foreign.check(srv, {a})).ok


def test_a_disabled_mod_does_not_count_as_a_dependency(fac):
    """It is in the folder but not selected — the server would never load it."""
    srv, folder = fac
    present = _fac_mod(folder, "Krastorio2", "1.3.24")
    needs = _fac_mod(folder, "K2Assets", "1.0.0", deps=["Krastorio2"])
    result = asyncio.run(foreign.check(srv, {needs}))       # `present` deliberately NOT enabled
    assert "FAC_DEP_MISSING" in _codes(result)
    assert present in os.listdir(folder)


def test_an_unparseable_dependency_is_reported_instead_of_ignored(fac):
    srv, folder = fac
    file = _fac_mod(folder, "Thing", "1.0.0", deps=["<<< broken >>>"])
    result = asyncio.run(foreign.check(srv, {file}))
    assert "FAC_DEP_UNREADABLE" in _codes(result)
    assert result.ok          # a warning, not a blocker: it is a statement about the check


def test_a_too_new_game_version_warns(fac):
    srv, folder = fac
    file = _fac_mod(folder, "Future", "1.0.0", game="2.1")
    result = asyncio.run(foreign.check(srv, {file}))
    assert "MOD_VERSION_MISMATCH" in _codes(result) and result.ok


def test_the_version_check_reads_the_image_tag(fac):
    """The check must not depend on `game_version`, which is empty for Factorio.

    `ServerCreate.game_version` defaults to "", and the stats poller writes `detect_version()`
    back only for Reforger. The field is filled only by the first version change — until then
    `MOD_VERSION_MISMATCH` would be switched off, and a mod for 2.1 would pass `check` AND `apply`
    without a single finding, failing only at the boot proof after stop and mandatory backup.
    """
    srv, folder = fac
    assert not (srv.game_version or "").strip(), "exactly the live state of a new Factorio server"
    file = _fac_mod(folder, "Future", "1.0.0", game="2.1")
    assert "MOD_VERSION_MISMATCH" in _codes(asyncio.run(foreign.check(srv, {file})))


def test_a_broken_zip_is_a_blocker(fac):
    srv, folder = fac
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "broken.zip").write_bytes(b"this is not a zip")
    result = asyncio.run(foreign.check(srv, {"broken.zip"}))
    assert "MODFILE_UNREADABLE" in _codes(result) and not result.ok


def test_an_unpacked_mod_is_read_too(fac):
    """Factorio also loads mods as folders — looking only at files would miss them."""
    srv, folder = fac
    package = folder / "Krastorio2_1.3.24"
    package.mkdir(parents=True)
    (package / "info.json").write_text(json.dumps(
        {"name": "Krastorio2", "version": "1.3.24", "factorio_version": "2.0"}), encoding="utf-8")
    assert [m.mod_id for m in foreign.scan(srv)] == ["Krastorio2"]


# ================================================================== Valheim

@pytest.fixture()
def vh(tmp_path):
    srv = _srv(tmp_path, GameType.valheim,
               extra=json.dumps({"env": {"BEPINEX": "true"}}))
    return srv, Path(foreign.mods_dir(srv))


def _dll(folder: Path, name: str, *, real=True) -> str:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / name).write_bytes(b"MZ\x90\x00" if real else b"just text")
    return name


def test_valheim_plugins_live_where_bepinex_looks_for_them(vh):
    srv, folder = vh
    assert folder.as_posix().endswith("config/bepinex/plugins")


def test_a_dll_without_mz_signature_is_a_blocker(vh):
    """The most common real error: a renamed text file or a half-finished download."""
    srv, folder = vh
    file = _dll(folder, "Broken.dll", real=False)
    result = asyncio.run(foreign.check(srv, {file}))
    assert "MODFILE_UNREADABLE" in _codes(result) and not result.ok


def test_a_real_dll_passes_but_is_reported_as_unverifiable(vh):
    """Same rule as the workshop check: "unverifiable" is not "fine".

    Version and dependencies live in .NET attributes. Keeping quiet about that would sell a gap
    as an approval.
    """
    srv, folder = vh
    file = _dll(folder, "Thing.dll")
    result = asyncio.run(foreign.check(srv, {file}))
    assert result.ok                                   # not a blocker
    assert "MOD_UNVERIFIABLE" in _codes(result)      # but not silently green either


class _DockerWithEnv:
    """Docker dummy that reports a container env — or no container at all."""

    def __init__(self, env=None, exists=True):
        self._env = env
        self._exists = exists

    async def state(self, name):
        if not self._exists:
            return {"exists": False, "running": False, "status": "missing"}
        return {"exists": True, "running": True, "status": "running",
                "env": self._env if self._env is not None else {}}


def test_without_bepinex_the_server_loads_not_a_single_mod(tmp_path, monkeypatch):
    """The most misleading state this game has: everything looks right, nothing takes effect."""
    monkeypatch.setattr("app.services.docker_client.docker", _DockerWithEnv({"BEPINEX": "false"}))
    srv = _srv(tmp_path, GameType.valheim, extra=json.dumps({"env": {"BEPINEX": "false"}}))
    file = _dll(Path(foreign.mods_dir(srv)), "Thing.dll")
    result = asyncio.run(foreign.check(srv, {file}))
    assert "VH_LOADER_OFF" in _codes(result) and not result.ok


def test_the_blocker_depends_on_the_CONTAINER_not_on_the_registry(tmp_path, monkeypatch):
    """`set_config` writes only the registry; the container gets its env only on creation.

    With `config_reload = recreate`, switching on "load BepInEx" in the editor and saving leaves a
    registry that says `true` and a container still running with `false`. A preflight that reads
    the registry returns no blocker, `foreign_apply` runs mandatory backup, move, restart and boot
    proof and reports success — and the server loads not a single plugin.
    """
    monkeypatch.setattr("app.services.docker_client.docker", _DockerWithEnv({"BEPINEX": "false"}))
    srv = _srv(tmp_path, GameType.valheim, extra=json.dumps({"env": {"BEPINEX": "true"}}))
    file = _dll(Path(foreign.mods_dir(srv)), "Thing.dll")
    result = asyncio.run(foreign.check(srv, {file}))
    assert "VH_LOADER_OFF" in _codes(result), "the registry must not decide here"

    # And the other direction: container on, registry silent (e.g. after an adoption) — that must
    # NOT be a blocker, or the preflight blocks with a reason that does not apply to the running
    # server.
    monkeypatch.setattr("app.services.docker_client.docker", _DockerWithEnv({"BEPINEX": "true"}))
    srv2 = _srv(tmp_path, GameType.valheim, extra="{}")
    assert asyncio.run(foreign.check(srv2, {file})).ok


def test_a_container_that_cannot_be_queried_is_a_warning_not_a_free_pass(tmp_path, monkeypatch):
    """"Unverifiable" is not "fine" — the same rule as for the workshop check."""
    monkeypatch.setattr("app.services.docker_client.docker", _DockerWithEnv(exists=False))
    srv = _srv(tmp_path, GameType.valheim, extra=json.dumps({"env": {"BEPINEX": "true"}}))
    file = _dll(Path(foreign.mods_dir(srv)), "Thing.dll")
    result = asyncio.run(foreign.check(srv, {file}))
    assert "VH_LOADER_UNKNOWN" in _codes(result)
    assert result.ok, "unknown does not block — it warns"


def test_without_plugins_the_disabled_loader_does_no_harm(tmp_path, monkeypatch):
    """A vanilla server without mods must not be blocked because of a switch."""
    monkeypatch.setattr("app.services.docker_client.docker", _DockerWithEnv({"BEPINEX": "false"}))
    srv = _srv(tmp_path, GameType.valheim, extra=json.dumps({"env": {"BEPINEX": "false"}}))
    assert asyncio.run(foreign.check(srv, set())).ok


def test_a_thunderstore_package_brings_its_metadata(vh):
    srv, folder = vh
    package = folder / "Author-Package"
    package.mkdir(parents=True)
    (package / "manifest.json").write_text(json.dumps(
        {"name": "Package", "version_number": "1.2.3", "dependencies": ["denikson-BepInExPack_Valheim-5.4.2202"]}),
        encoding="utf-8")
    mods = {m.filename: m for m in foreign.scan(srv)}
    assert mods["Author-Package"].version == "1.2.3"
    assert mods["Author-Package"].dependencies == ("denikson-BepInExPack_Valheim-5.4.2202",)


def test_two_files_of_the_same_mod_block(vh):
    """With Valheim the file is the identifier — the same DLL twice under another name does not
    show up that way, and two identical names cannot exist. Checked is the case that does exist:
    the same mod_id from two packages."""
    srv, folder = vh
    for name in ("Author-Package", "Other-Package"):
        package = folder / name
        package.mkdir(parents=True)
        (package / "manifest.json").write_text(
            json.dumps({"name": "Package", "version_number": "1.0.0"}), encoding="utf-8")
    result = asyncio.run(foreign.check(srv, {"Author-Package", "Other-Package"}))
    assert "MOD_DUPLICATE" in _codes(result) and not result.ok


# ================================================================== Avorion

def _modconfig(tmp_path, content: str) -> GameServer:
    srv = _srv(tmp_path, GameType.avorion)
    galaxy = Path(tmp_path) / "galaxy"
    galaxy.mkdir(parents=True, exist_ok=True)
    (galaxy / "modconfig.lua").write_text(content, encoding="utf-8")
    return srv


def _without_steam(monkeypatch, response=None):
    """Replace the Steam query. `None` = unreachable (the most common real case)."""
    async def fetch(ids):
        return response if response is not None else {}

    async def availability(ids, steam):
        from app.services.mod_check import UNVERIFIABLE, AVAILABLE

        return {i: (AVAILABLE if steam.get(i) else UNVERIFIABLE) for i in ids}

    monkeypatch.setattr(preflight_avorion, "fetch_steam", fetch)
    monkeypatch.setattr(preflight_avorion, "check_availability", availability)


def test_ids_and_allowed_ids_are_read_separately():
    text = build_modconfig(["111", "222"], ["111"])
    mods, allowed, duplicates = ids_and_allowed(text)
    assert mods == ["111", "222"] and allowed == ["111"] and duplicates == []


def test_duplicate_ids_are_noticed(tmp_path, monkeypatch):
    _without_steam(monkeypatch)
    srv = _modconfig(tmp_path, 'mods = {\n{workshopid = "111"},\n{workshopid = "111"},\n}\n')
    result = asyncio.run(preflight_avorion.check(srv))
    assert "AV_ID_DUPLICATE" in _codes(result)


def test_without_modconfig_there_is_nothing_to_report(tmp_path):
    """A vanilla server is not an error."""
    srv = _srv(tmp_path, GameType.avorion)
    assert asyncio.run(preflight_avorion.check(srv)).findings == []


def test_a_file_without_mods_block_blocks(tmp_path, monkeypatch):
    """Avorion would ignore it and start without mods — clients with mods could no longer join."""
    _without_steam(monkeypatch)
    srv = _modconfig(tmp_path, "-- just a comment\n")
    result = asyncio.run(preflight_avorion.check(srv))
    assert "AV_MODCONFIG_WITHOUT_MODS" in _codes(result) and not result.ok


def test_a_present_but_unreadable_file_is_a_blocker(tmp_path, monkeypatch):
    """The difference that matters: "does not exist" is allowed, "cannot be read" must not
    yield the same empty mod set."""
    srv = _modconfig(tmp_path, "mods = {}\n")

    def broken(*a, **k):
        raise OSError("access denied")

    monkeypatch.setattr("builtins.open", broken)
    result = asyncio.run(preflight_avorion.check(srv))
    assert "AV_MODCONFIG_UNREADABLE" in _codes(result) and not result.ok


def test_a_removed_mod_blocks(tmp_path, monkeypatch):
    from app.services.mod_check import REMOVED

    async def fetch(ids):
        return {"111": {"title": "Gone", "app_id": 445220}}

    async def availability(ids, steam):
        return {"111": REMOVED}

    monkeypatch.setattr(preflight_avorion, "fetch_steam", fetch)
    monkeypatch.setattr(preflight_avorion, "check_availability", availability)
    srv = _modconfig(tmp_path, build_modconfig(["111"]))
    result = asyncio.run(preflight_avorion.check(srv))
    assert "AV_ITEM_REMOVED" in _codes(result) and not result.ok


def test_a_mod_from_another_game_blocks(tmp_path, monkeypatch):
    _without_steam(monkeypatch, response={"111": {"title": "PZ-Mod", "app_id": 108600}})
    srv = _modconfig(tmp_path, build_modconfig(["111"]))
    result = asyncio.run(preflight_avorion.check(srv))
    assert "AV_ITEM_WRONG_GAME" in _codes(result) and not result.ok


def test_steam_unreachable_is_a_warning_not_an_acquittal(tmp_path, monkeypatch):
    """The same classification as for Project Zomboid (`WORKSHOP_ITEM_UNVERIFIABLE`).

    A Steam outage is not a finding AGAINST a mod — but it must not pass as green either. Visible
    in the list, without stopping the apply.
    """
    _without_steam(monkeypatch)
    srv = _modconfig(tmp_path, build_modconfig(["111"]))
    result = asyncio.run(preflight_avorion.check(srv))
    assert "AV_ITEM_UNVERIFIABLE" in _codes(result)
    assert result.ok


def test_an_id_outside_the_allowed_list_warns(tmp_path, monkeypatch):
    _without_steam(monkeypatch, response={"111": {"app_id": 445220}, "222": {"app_id": 445220}})
    srv = _modconfig(tmp_path, build_modconfig(["111", "222"], ["111"]))
    result = asyncio.run(preflight_avorion.check(srv))
    assert "AV_NOT_ALLOWED" in _codes(result) and result.ok


def test_the_generated_file_is_readable_again():
    """Round trip: what `build_modconfig` writes, `ids_and_allowed` must return — otherwise the
    next preflight would not find its own output again."""
    text = build_modconfig(["1819452708", "2909895451"], ["1819452708"])
    assert ids_and_allowed(text)[0] == ["1819452708", "2909895451"]
    assert ids_and_allowed(text)[1] == ["1819452708"]


def test_moving_updates_mod_list_json(fac):
    """What decides is `mod-list.json`, so moving a file must write it too.

    This module derives `enabled` from whether the file is in the active folder — Factorio itself
    reads the on/off state from `mods/mod-list.json`. A zip in the active folder with
    `"enabled": false` would count as active for the preflight, the apply would report success,
    the boot proof would pass (the server does start) — but the mod would never load, and clients
    with the mod would hang at join, because Factorio compares the mod set exactly.
    """
    srv, folder = fac
    file = _fac_mod(folder, "Krastorio2", "1.3.24")
    listing = folder / "mod-list.json"
    listing.write_text(json.dumps({"mods": [
        {"name": "base", "enabled": True},
        {"name": "Krastorio2", "enabled": False},      # contradicts the folder
    ]}), encoding="utf-8")

    foreign.set_enabled(srv, {file})
    enabled = {e["name"]: e["enabled"] for e in json.loads(listing.read_text(encoding="utf-8"))["mods"]}
    assert enabled["Krastorio2"] is True, "the list must follow the folder"
    assert enabled["base"] is True, "built-in mods stay untouched"

    # And the other direction: deselecting also switches off in the list.
    foreign.set_enabled(srv, set())
    enabled = {e["name"]: e["enabled"] for e in json.loads(listing.read_text(encoding="utf-8"))["mods"]}
    assert enabled["Krastorio2"] is False


def test_a_missing_mod_list_is_created(fac):
    srv, folder = fac
    file = _fac_mod(folder, "Helper", "1.0.0")
    foreign.set_enabled(srv, {file})
    listing = json.loads((folder / "mod-list.json").read_text(encoding="utf-8"))
    assert {"name": "Helper", "enabled": True} in listing["mods"]
