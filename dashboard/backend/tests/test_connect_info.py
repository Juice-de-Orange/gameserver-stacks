"""Join info per game.

Address, port and server name come from the registry for every game, not from literals in
`routers/join.py`.

The core of these tests is the **honesty of the URI**: a one-click join exists only where the
game really has a protocol for it. An invented `minecraft://` link would look like a button and
do nothing — worse than none at all.
"""
import json

import pytest

from app.config import settings
from app.models.server import GameServer, GameType
from app.services.drivers import get_driver
from app.services.drivers.base import game_port, game_proto, public_host


def _srv(game, ports, extra=None, key="s1"):
    return GameServer(
        key=key, game_type=game, container_name=key, data_dir="/nowhere",
        game_ports=json.dumps(ports), extra=json.dumps(extra or {}), display_name="My Server",
    )


@pytest.fixture(autouse=True)
def _host(monkeypatch):
    monkeypatch.setattr(settings, "public_host", "game.example.org", raising=False)


# ---------------------------------------------------------------- shared derivation

def test_port_and_protocol_come_from_the_registry():
    s = _srv(GameType.pz, {"game": "16261/udp", "direct": "16262/udp"})
    assert game_port(s) == 16261
    assert game_proto(s) == "udp"


def test_tcp_is_detected():
    s = _srv(GameType.minecraft, {"game": "25565/tcp"})
    assert game_proto(s) == "tcp"


def test_without_a_game_port_there_is_no_way_in_from_outside():
    s = _srv(GameType.pz, {"aux": "9999/tcp"})
    assert game_port(s) == 0


def test_a_broken_port_spec_does_not_raise():
    s = _srv(GameType.pz, {})
    s.game_ports = "{not json"
    assert game_port(s) == 0 and game_proto(s) == "udp"


def test_the_server_own_address_beats_the_global_one():
    s = _srv(GameType.pz, {"game": "1/udp"}, {"public_host": "own.example.org"})
    assert public_host(s) == "own.example.org"


def test_without_a_stored_address_it_stays_empty(monkeypatch):
    """Fail-closed: the UI says "not set" instead of showing a wrong address."""
    monkeypatch.setattr(settings, "public_host", "", raising=False)
    assert public_host(_srv(GameType.pz, {"game": "1/udp"})) == ""


# ---------------------------------------------------------------- per game

def test_pz_is_the_only_one_with_a_one_click_join():
    s = _srv(GameType.pz, {"game": "16261/udp"}, {"servername": "servertest"})
    info = get_driver(s).connect_info(s)
    assert info.uri == "steam://connect/game.example.org:16261"
    assert info.browser_name == "servertest"
    assert "Direct" in info.hint or "Favorites" in info.hint


def test_minecraft_gets_no_invented_uri():
    s = _srv(GameType.minecraft, {"game": "25565/tcp"})
    info = get_driver(s).connect_info(s)
    assert info.uri == ""                       # Minecraft Java has no URL scheme
    assert info.port == 25565 and info.protocol == "tcp"
    assert "Multiplayer" in info.hint
    assert info.password_required is False      # MC has no server password, but a whitelist


def test_vintage_story_gets_no_invented_uri():
    s = _srv(GameType.vintagestory, {"game": "42420/tcp"})
    info = get_driver(s).connect_info(s)
    assert info.uri == ""
    assert info.port == 42420
    assert "Multiplayer" in info.hint


def test_reforger_points_to_the_server_browser():
    """Enfusion has no `steam://connect` — the server name is the useful information."""
    s = _srv(GameType.arma_reforger, {"game": "2001/udp", "a2s": "17777/udp"})
    info = get_driver(s).connect_info(s)
    assert info.uri == ""
    assert info.port == 2001
    assert "server browser" in info.hint
    assert info.browser_name == "My Server"


def test_factorio_names_the_address_instead_of_a_click():
    """Factorio has no URL scheme — the in-game path is "connect to address"."""
    s = _srv(GameType.factorio, {"game": "34197/udp"})
    info = get_driver(s).connect_info(s)
    assert info.uri == ""
    assert info.port == 34197 and info.protocol == "udp"
    assert "address" in info.hint


def test_avorion_names_the_address_instead_of_a_click():
    s = _srv(GameType.avorion, {"game": "27000/tcp"})
    info = get_driver(s).connect_info(s)
    assert info.uri == "" and info.port == 27000 and info.protocol == "tcp"
    assert "Connect directly" in info.hint


def test_valheim_names_the_address_with_port():
    """Valheim needs the address WITH the port — without it the join fails."""
    s = _srv(GameType.valheim, {"game": "2456/udp"})
    info = get_driver(s).connect_info(s)
    assert info.uri == "" and info.port == 2456
    assert "port" in info.hint


def test_pz_reports_a_required_join_password():
    s = _srv(GameType.pz, {"game": "16261/udp"}, {"password": "secret"})
    assert get_driver(s).connect_info(s).password_required is True


def test_pz_without_a_password_says_so_too():
    s = _srv(GameType.pz, {"game": "16261/udp"})
    assert get_driver(s).connect_info(s).password_required is False


def test_without_an_address_the_uri_stays_empty_too(monkeypatch):
    """Otherwise `steam://connect/:16261` would result — a button that leads nowhere."""
    monkeypatch.setattr(settings, "public_host", "", raising=False)
    s = _srv(GameType.pz, {"game": "16261/udp"})
    assert get_driver(s).connect_info(s).uri == ""


# --- Join page: the mod list of the non-PZ games ---------------------------------------------

def test_join_shows_the_mods_the_drivers_really_return():
    """`_other_mods` must read the keys the drivers really return.

    Minecraft returns `{"plugins": [...], "mods": [...]}`, Vintage Story `{"mods": [...]}`,
    Reforger `{"mods": [{modId,name,version}], "scenarioId": ...}`. Reading a key no driver
    returns gives `out == []`, and the UI then shows a positive false statement ("0 active mods,
    the server runs without extra content") — no error, no log.
    """
    from app.routers.join import _other_mods

    class _MC:
        def list_mods(self, s):
            return {"plugins": ["EssentialsX.jar"], "mods": ["fabric-api.jar"]}

    class _VS:
        def list_mods(self, s):
            return {"mods": ["carryon.zip"]}

    class _RF:
        def list_mods(self, s):
            return {"mods": [{"modId": "ABC123", "name": "Fire Brigade", "version": "1.2"}],
                    "scenarioId": "any"}

    class _RF_without_name:
        """The normal case when adding by workshop GUID: `Mods.tsx` sends {modId} without a name,
        and `_dedup_ordered` normalises that to name="". The title must fall back to the ID, not
        to the raw Python dict notation."""

        def list_mods(self, s):
            return {"mods": [{"modId": "5EABC1234567890A", "name": "", "version": ""}]}

    srv = GameServer(key="x", game_type=GameType.minecraft)

    # ONLY `mods`, explicitly NOT `plugins`: this page is open to every signed-in user (no
    # permission flag), while the same list in `GET /{key}/mods` sits behind `can_manage_mods`.
    # `data/plugins` are server-side Paper plugins with the version in the file name — admin,
    # anticheat and moderation tools no joining player needs. Returning them here would bypass
    # the lock.
    assert [m["title"] for m in _other_mods(srv, _MC())] == ["fabric-api.jar"]
    assert [m["title"] for m in _other_mods(srv, _VS())] == ["carryon.zip"]
    assert [m["title"] for m in _other_mods(srv, _RF())] == ["Fire Brigade"]
    assert [m["title"] for m in _other_mods(srv, _RF_without_name())] == ["5EABC1234567890A"]


def test_join_stays_empty_with_a_broken_driver():
    """A display may come out empty — it just must not be STRUCTURALLY always empty."""
    from app.routers.join import _other_mods

    class _Broken:
        def list_mods(self, s):
            raise RuntimeError("mount gone")

    assert _other_mods(GameServer(key="x", game_type=GameType.minecraft), _Broken()) == []
