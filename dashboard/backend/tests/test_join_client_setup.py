"""The join page tells the PLAYER what they need on their own computer.

Proximity chat on a Minecraft server can fail on the client side: a vanilla client cannot do
Simple Voice Chat and only notices because everyone is silent. Then there is the version trap: a
client on 26.2 cannot get onto a 26.1.2 server and only reads "Outdated client" in the game.
"""
import json
import zipfile

import pytest

from app.models.server import GameServer, GameType
from app.routers import join


class _Driver:
    """Minimal driver stand-in — `_client_setup` only asks `list_mods`."""

    def __init__(self, plugins=None, raises=False):
        self._plugins = plugins or []
        self._raises = raises

    def list_mods(self, server):
        if self._raises:
            raise RuntimeError("folder not readable")
        return {"plugins": [{"name": n} for n in self._plugins], "mods": []}


def _mc(**kw) -> GameServer:
    kw.setdefault("game_version", "26.1.2")
    return GameServer(key="mc", game_type=GameType.minecraft, **kw)


# ------------------------------------------------------- only where there is something to do

def test_no_client_setup_for_other_games():
    """On Project Zomboid Steam fetches the mods by itself — the card has no business there."""
    srv = GameServer(key="pz-1", game_type=GameType.pz, game_version="42.20.4")
    assert join._client_setup(srv, _Driver()) is None


def test_minecraft_names_edition_and_version():
    cs = join._client_setup(_mc(), _Driver())
    assert cs is not None
    assert "Java" in cs["edition"]
    assert cs["version"] == "26.1.2"


# ------------------------------------------------------- Voice chat: three states

def test_voice_chat_detected():
    cs = join._client_setup(_mc(), _Driver(plugins=["voicechat-bukkit-2.6.21.jar", "EssentialsX.jar"]))
    assert cs["voice"] is True


def test_without_a_voice_chat_plugin_it_is_a_clear_no():
    cs = join._client_setup(_mc(), _Driver(plugins=["EssentialsX.jar"]))
    assert cs["voice"] is False


def test_an_unreadable_plugin_folder_is_unknown_not_no():
    """`None`, not `False` — the page then stays silent instead of claiming "no voice chat".

    This exact mix-up happened here before: `_other_mods` read a key no driver delivers, and the
    page reported "Active mods (0) — the server runs without add-on content." for three of four
    games.
    """
    assert join._client_setup(_mc(), _Driver(raises=True))["voice"] is None


# ------------------------------------------------------- the join pack
#
# No pack ships with the repository, so the tests build a synthetic one in a temp directory.

@pytest.fixture
def pack_dir(tmp_path, monkeypatch):
    """A modpack directory with one synthetic `stacks-26.1.2.mrpack` (a zip with the index)."""
    index = {"formatVersion": 1, "game": "minecraft", "versionId": "1", "name": "test",
             "files": [], "dependencies": {"minecraft": "26.1.2", "fabric-loader": "0.0.0"}}
    with zipfile.ZipFile(tmp_path / "stacks-26.1.2.mrpack", "w") as z:
        z.writestr("modrinth.index.json", json.dumps(index))
    (tmp_path / "secret").write_text("x", encoding="utf-8")
    monkeypatch.setattr(join, "MODPACK_DIR", tmp_path)
    return tmp_path


def test_pack_is_found_for_the_running_version(pack_dir):
    """There is a pack for 26.1.2 in the folder — the button may appear."""
    assert join._modpack(_mc(game_version="26.1.2")) == pack_dir / "stacks-26.1.2.mrpack"
    assert join._client_setup(_mc(game_version="26.1.2"), _Driver())["modpack"] is True


def test_without_a_matching_pack_no_button(pack_dir):
    """After a version jump without a new pack the button disappears instead of handing out a 404."""
    assert join._modpack(_mc(game_version="27.9.9")) is None
    assert join._client_setup(_mc(game_version="27.9.9"), _Driver())["modpack"] is False


def test_pack_lookup_cannot_be_lured_out_of_the_folder(pack_dir):
    """`game_version` is input from the registry and is inserted into a path."""
    for evil in ("../../etc/passwd", "26.1.2/../../secret", "", "  ", "26.1.2 26.2", "./26.1.2"):
        assert join._modpack(_mc(game_version=evil)) is None, evil

    # Surrounding whitespace is NOT an attack but a sloppy registry line — it is normalised, not
    # rejected.
    assert join._modpack(_mc(game_version=" 26.1.2\n")) is not None


def test_pack_is_found_only_under_the_naming_scheme(pack_dir):
    """Only `stacks-<version>.mrpack` counts — and only for Minecraft servers."""
    (pack_dir / "stacks-26.1.2.mrpack").rename(pack_dir / "other-26.1.2.mrpack")
    assert join._modpack(_mc(game_version="26.1.2")) is None

    (pack_dir / "other-26.1.2.mrpack").rename(pack_dir / "stacks-26.1.2.mrpack")
    assert join._modpack(_mc(game_version="26.1.2")) is not None
    pz = GameServer(key="pz-1", game_type=GameType.pz, image_tag="26.1.2", game_version="26.1.2")
    assert join._modpack(pz) is None
