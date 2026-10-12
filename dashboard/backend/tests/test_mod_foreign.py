"""File-based mod management for Minecraft and Vintage Story.

Different failure modes than PZ — there are no map cells. What goes wrong here: wrong loader,
duplicate mod ID, unreadable archive, version mismatch.
The archives are built as real zips so the metadata readers are really exercised.
"""
import asyncio
import json
import os
import zipfile

import pytest

from app.models.server import GameServer, GameType
from app.services.mods import foreign


def _srv(root, game=GameType.minecraft, server_type="PAPER", version="1.21.10"):
    s = GameServer(key="mc", game_type=game, data_dir=str(root), server_type=server_type,
                   game_version=version)
    s.id = 3
    return s


def _jar(path: str, entries: dict[str, str]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with zipfile.ZipFile(path, "w") as z:
        for name, body in entries.items():
            z.writestr(name, body)


def _paper_plugin(path, name="Chunky", version="1.4.40", api="1.21"):
    _jar(path, {"plugin.yml": f"name: {name}\nversion: {version}\napi-version: {api}\nmain: x.Y\n"})


def _fabric_mod(path, mod_id="sodium", version="0.5", mc="1.21"):
    _jar(path, {"fabric.mod.json": json.dumps(
        {"id": mod_id, "name": mod_id, "version": version, "depends": {"minecraft": mc}})})


def _vs_mod(path, mod_id="vsrcon", version="2.0.0"):
    _jar(path, {"modinfo.json": json.dumps({"modid": mod_id, "name": mod_id, "version": version})})


# --- Storage -----------------------------------------------------------------------------------
def test_paper_uses_plugins_fabric_uses_mods(tmp_path):
    assert foreign.mods_dir(_srv(tmp_path, server_type="PAPER")).endswith(os.path.join("data", "plugins"))
    assert foreign.mods_dir(_srv(tmp_path, server_type="FABRIC")).endswith(os.path.join("data", "mods"))
    assert foreign.expected_loader(_srv(tmp_path, server_type="PAPER")) == "paper"
    assert foreign.expected_loader(_srv(tmp_path, server_type="FORGE")) == "forge"


def test_vintage_story_uses_the_real_data_path(tmp_path):
    """Verified trap: VS loads from `gamedata/vs/Mods`, not from `gamedata/Mods` — reading the
    wrong folder makes the mod list look permanently empty."""
    srv = _srv(tmp_path, game=GameType.vintagestory, server_type="")
    os.makedirs(os.path.join(str(tmp_path), "gamedata", "vs", "Mods"))
    # The driver builds Linux paths (posixpath) — the target is the container, not Windows.
    assert foreign.mods_dir(srv).endswith("gamedata/vs/Mods")


def test_vintage_story_falls_back_to_the_old_layout(tmp_path):
    """An installation that deviates stays usable instead of silently looking empty."""
    srv = _srv(tmp_path, game=GameType.vintagestory, server_type="")
    os.makedirs(os.path.join(str(tmp_path), "gamedata", "Mods"))
    assert foreign.mods_dir(srv).endswith("gamedata/Mods")


# --- Metadata ----------------------------------------------------------------------------------
def test_metadata_is_read_from_every_supported_format(tmp_path):
    p = os.path.join(str(tmp_path), "a.jar")
    _paper_plugin(p, name="Chunky", version="1.4.40")
    meta = foreign.read_metadata(p)
    assert (meta["mod_id"], meta["version"], meta["loader"]) == ("Chunky", "1.4.40", "paper")

    _fabric_mod(p, mod_id="sodium", version="0.5")
    assert foreign.read_metadata(p)["loader"] == "fabric"

    _jar(p, {"META-INF/mods.toml": 'modId="jei"\ndisplayName="JEI"\nversion="15.0"\n'})
    meta = foreign.read_metadata(p)
    assert (meta["mod_id"], meta["loader"]) == ("jei", "forge")

    _vs_mod(p, mod_id="vsrcon")
    assert foreign.read_metadata(p)["loader"] == "vintagestory"


def test_broken_archive_is_reported_not_swallowed(tmp_path):
    p = os.path.join(str(tmp_path), "broken.jar")
    with open(p, "wb") as f:
        f.write(b"this is not a zip")
    assert "problem" in foreign.read_metadata(p)


def test_archive_without_known_descriptor_is_reported(tmp_path):
    p = os.path.join(str(tmp_path), "empty.jar")
    _jar(p, {"README.txt": "nothing"})
    assert "problem" in foreign.read_metadata(p)


# --- Inventory ---------------------------------------------------------------------------------
def test_scan_sees_active_and_disabled_files(tmp_path):
    srv = _srv(tmp_path)
    _paper_plugin(os.path.join(foreign.mods_dir(srv), "Chunky.jar"))
    _paper_plugin(os.path.join(foreign.disabled_dir(srv), "Old.jar"), name="Old")

    by_name = {m.filename: m for m in foreign.scan(srv)}
    assert by_name["Chunky.jar"].enabled is True
    assert by_name["Old.jar"].enabled is False
    assert by_name["Chunky.jar"].mod_id == "Chunky"


def test_scan_ignores_foreign_extensions(tmp_path):
    srv = _srv(tmp_path)
    d = foreign.mods_dir(srv)
    _paper_plugin(os.path.join(d, "good.jar"))
    os.makedirs(d, exist_ok=True)
    open(os.path.join(d, "note.txt"), "w").close()
    assert [m.filename for m in foreign.scan(srv)] == ["good.jar"]


# --- Check catalog -----------------------------------------------------------------------------
def test_wrong_loader_is_blocked(tmp_path):
    """A Fabric mod in a Paper server's plugin folder is never loaded."""
    srv = _srv(tmp_path, server_type="PAPER")
    _fabric_mod(os.path.join(foreign.mods_dir(srv), "sodium.jar"))
    result = asyncio.run(foreign.check(srv, {"sodium.jar"}))
    assert not result.ok
    assert [f.code for f in result.blockers] == ["MOD_WRONG_LOADER"]


def test_duplicate_mod_id_is_blocked(tmp_path):
    srv = _srv(tmp_path)
    d = foreign.mods_dir(srv)
    _paper_plugin(os.path.join(d, "chunky-1.jar"), name="Chunky", version="1.0")
    _paper_plugin(os.path.join(d, "chunky-2.jar"), name="Chunky", version="2.0")
    result = asyncio.run(foreign.check(srv, {"chunky-1.jar", "chunky-2.jar"}))
    assert "MOD_DUPLICATE" in {f.code for f in result.blockers}


def test_unreadable_file_is_blocked(tmp_path):
    srv = _srv(tmp_path)
    os.makedirs(foreign.mods_dir(srv), exist_ok=True)
    with open(os.path.join(foreign.mods_dir(srv), "broken.jar"), "wb") as f:
        f.write(b"not a zip")
    assert "MODFILE_UNREADABLE" in {f.code for f in asyncio.run(foreign.check(srv, {"broken.jar"})).blockers}


def test_an_older_api_version_is_NOT_a_warning(tmp_path):
    """`api-version` in plugin.yml is the MINIMUM API, not the target version. Real plugins
    commonly name an older number (1.13, 1.17) and run fine on 1.21.10 — a warning would be wrong
    in almost every case and would bury the real findings."""
    srv = _srv(tmp_path, version="1.21.10")
    _paper_plugin(os.path.join(foreign.mods_dir(srv), "old.jar"), api="1.13")
    assert asyncio.run(foreign.check(srv, {"old.jar"})).findings == []


def test_a_plugin_demanding_a_newer_server_warns(tmp_path):
    """The reverse, actually risky case: the mod demands more than the server offers."""
    srv = _srv(tmp_path, version="1.20.4")
    _paper_plugin(os.path.join(foreign.mods_dir(srv), "new.jar"), api="1.21")
    result = asyncio.run(foreign.check(srv, {"new.jar"}))
    assert result.ok                                    # does NOT block
    assert "MOD_VERSION_MISMATCH" in {f.code for f in result.warnings}


def test_matching_version_is_silent(tmp_path):
    srv = _srv(tmp_path, version="1.21.10")
    _paper_plugin(os.path.join(foreign.mods_dir(srv), "good.jar"), api="1.21")
    assert asyncio.run(foreign.check(srv, {"good.jar"})).findings == []


def test_a_mod_folder_is_found_like_a_file(tmp_path):
    """Vintage Story also loads mods as an UNPACKED folder — the RCON mod is installed exactly like
    that. Looking at files only would make it invisible."""
    srv = _srv(tmp_path, game=GameType.vintagestory, server_type="")
    folder = os.path.join(foreign.mods_dir(srv), "VintageRCon")
    os.makedirs(folder)
    with open(os.path.join(folder, "VintageRCon.dll"), "wb") as f:
        f.write(b"x" * 100)

    found = {m.filename: m for m in foreign.scan(srv)}
    assert "VintageRCon" in found
    assert found["VintageRCon"].mod_id == "VintageRCon"     # folder name as identifier
    assert found["VintageRCon"].size == 100
    assert asyncio.run(foreign.check(srv, {"VintageRCon"})).ok


def test_disabled_files_are_not_checked(tmp_path):
    """What is not active cannot knock the server over."""
    srv = _srv(tmp_path, server_type="PAPER")
    _fabric_mod(os.path.join(foreign.disabled_dir(srv), "sodium.jar"))
    assert asyncio.run(foreign.check(srv, set())).ok


# --- Moving and uploading ----------------------------------------------------------------------
def test_enabling_and_disabling_moves_files(tmp_path):
    srv = _srv(tmp_path)
    _paper_plugin(os.path.join(foreign.mods_dir(srv), "on.jar"), name="On")
    _paper_plugin(os.path.join(foreign.disabled_dir(srv), "off.jar"), name="Off")

    changed = foreign.set_enabled(srv, {"off.jar"})
    assert changed == {"enabled": ["off.jar"], "disabled": ["on.jar"], "failed": []}
    assert os.path.isfile(os.path.join(foreign.mods_dir(srv), "off.jar"))
    assert os.path.isfile(os.path.join(foreign.disabled_dir(srv), "on.jar"))


def _upload(srv, source: str, target_name: str, max_bytes: int = 100 * 1024 * 1024):
    """Like the route: chunk by chunk from a file, limit enforced while writing."""
    def _chunks():
        with open(source, "rb") as f:
            while True:
                block = f.read(1 << 16)
                if not block:
                    return
                yield block

    return foreign.store_upload_stream(srv, target_name, _chunks(), max_bytes)


def test_upload_lands_disabled(tmp_path):
    """Uploading must not change the running server — otherwise it would be an immediate
    intervention again."""
    srv = _srv(tmp_path)
    buf = os.path.join(str(tmp_path), "source.jar")
    _paper_plugin(buf, name="New", version="1.0")

    mod = _upload(srv, buf, "New.jar")
    assert mod.enabled is False and mod.mod_id == "New"
    assert os.path.isfile(os.path.join(foreign.disabled_dir(srv), "New.jar"))
    assert not os.path.exists(os.path.join(foreign.mods_dir(srv), "New.jar"))


def test_upload_rejects_wrong_extension(tmp_path):
    srv = _srv(tmp_path)
    with pytest.raises(ValueError):
        foreign.check_filename(srv, "evil.sh")


def test_upload_strips_any_path_component(tmp_path):
    """A directory part in the file name must never get through — otherwise the file lands
    somewhere in the filesystem instead of in the mod folder."""
    srv = _srv(tmp_path)
    buf = os.path.join(str(tmp_path), "source.jar")
    _paper_plugin(buf, name="New")

    mod = _upload(srv, buf, "../../evil.jar")
    assert mod.filename == "evil.jar"                                  # path cut off
    assert os.path.isfile(os.path.join(foreign.disabled_dir(srv), "evil.jar"))
    assert not os.path.exists(os.path.join(str(tmp_path), "..", "..", "evil.jar"))


def test_oversized_uploads_never_land_fully_on_disk(tmp_path):
    """The limit takes hold WHILE writing, not afterwards — and leaves no half `.incoming`
    behind, otherwise the atomic swap would be worthless."""
    srv = _srv(tmp_path)
    buf = os.path.join(str(tmp_path), "big.jar")
    _paper_plugin(buf, name="Big")

    with pytest.raises(ValueError, match="too large"):
        _upload(srv, buf, "Big.jar", max_bytes=10)

    assert not os.path.exists(os.path.join(foreign.disabled_dir(srv), "Big.jar"))
    assert not os.path.exists(os.path.join(foreign.disabled_dir(srv), "Big.jar.incoming"))


def test_a_failed_move_is_reported_not_concealed(tmp_path, monkeypatch):
    """`_move` returns False and `set_enabled` checks it — but a failure must show up in
    `failed`, not just as a MISSING list entry the caller never compares against."""
    srv = _srv(tmp_path)
    _paper_plugin(os.path.join(foreign.mods_dir(srv), "stuck.jar"), name="Stuck")

    def _broken(src, dst):
        raise OSError("Read-only file system")

    monkeypatch.setattr(foreign.shutil, "move", _broken)
    changed = foreign.set_enabled(srv, set())
    assert changed["failed"] == ["stuck.jar"]
    assert changed["disabled"] == []


def test_plugin_config_folders_are_not_mods(tmp_path):
    """Paper creates a CONFIGURATION folder with the same name next to every jar. Counting those
    as mods doubles the list and produces a loader blocker for every folder (observed for real:
    13 plugins became 32 entries with 32 blockers)."""
    srv = _srv(tmp_path, server_type="PAPER")
    d = foreign.mods_dir(srv)
    _paper_plugin(os.path.join(d, "Chunky-Bukkit-1.4.40.jar"), name="Chunky")
    os.makedirs(os.path.join(d, "Chunky"), exist_ok=True)          # configuration folder
    open(os.path.join(d, "Chunky", "config.yml"), "w").close()

    found = [m.filename for m in foreign.scan(srv)]
    assert found == ["Chunky-Bukkit-1.4.40.jar"]
    assert asyncio.run(foreign.check(srv, set(found))).ok


def test_a_same_named_file_at_the_target_is_not_silently_replaced(tmp_path):
    """The obvious way out must not destroy the new version.

    On Valheim, BepInEx plugins usually carry no version in the file name. Upload the new version
    as `Foo.dll` (it lands disabled) and then deselect the old one: a plain `shutil.move` pushes
    the old one over the new one — `os.rename` replaces silently. No quarantine, no backup, no
    rollback; `set_enabled` reports success. Selecting the new one instead makes the preflight
    block with "active twice" — so deselecting is exactly the button one presses then.
    """
    from app.services.mods import foreign

    active = tmp_path / "active"
    out = tmp_path / "off"
    active.mkdir()
    out.mkdir()
    (active / "Foo.dll").write_bytes(b"OLD")
    (out / "Foo.dll").write_bytes(b"NEW-uploaded")

    assert foreign._move(str(active / "Foo.dll"), str(out), "Foo.dll")
    assert (out / "Foo.dll").read_bytes() == b"OLD", "the moved file is at the target"
    set_aside = list(out.glob("Foo.dll.displaced*"))
    assert len(set_aside) == 1, "the version found there must be kept"
    assert set_aside[0].read_bytes() == b"NEW-uploaded"
