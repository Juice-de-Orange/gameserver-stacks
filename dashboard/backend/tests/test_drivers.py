"""get_driver resolution and PZDriver game specifics (no DB, no Docker)."""
import pytest

from app.models.server import GameServer, GameType
from app.services.drivers import get_driver
from app.services.drivers.pz import PZDriver, _parse_players


def test_get_driver_pz():
    drv = get_driver(GameServer(key="pz", game_type=GameType.pz))
    assert isinstance(drv, PZDriver)
    assert "console" in drv.capabilities()
    assert "versions" in drv.capabilities()


def test_get_driver_cached_singleton():
    a = get_driver(GameServer(key="a", game_type=GameType.pz))
    b = get_driver(GameServer(key="b", game_type=GameType.pz))
    assert a is b  # stateless → one singleton per game type


def test_get_driver_mc_vs_resolve():
    from app.services.drivers.minecraft import MinecraftDriver
    from app.services.drivers.vintagestory import VintageStoryDriver

    assert isinstance(get_driver(GameServer(key="mc", game_type=GameType.minecraft)), MinecraftDriver)
    assert isinstance(get_driver(GameServer(key="vs", game_type=GameType.vintagestory)), VintageStoryDriver)


def test_pz_container_binds_and_env():
    drv = PZDriver()
    srv = GameServer(
        key="pz-test",
        game_type=GameType.pz,
        data_dir="/srv/gameservers/pz-test",
        heap_max_mb=4096,
        heap_min_mb=2048,
        presence_visible=False,
    )
    assert drv.container_binds(srv) == [
        "/srv/gameservers/pz-test/data:/home/steam/Zomboid",
        "/srv/gameservers/pz-test/workshop-mods:/home/steam/pz-dedicated/steamapps/workshop",
    ]
    env = drv.container_env(srv, "topsecret")
    assert env["ADMINPASSWORD"] == "topsecret"
    assert env["RCONPASSWORD"] == "topsecret"
    assert env["MAX_MEMORY"] == "4096m"
    assert env["SELF_MANAGED_MODS"] == "true"
    assert env["PUBLIC"] == "false"  # presence_visible=False


def test_pz_parse_players():
    p = _parse_players("Players connected (2):\n-Alice\n-Bob")
    assert p == {"count": 2, "names": ["Alice", "Bob"]}
    assert _parse_players("Players connected (0):") == {"count": 0, "names": []}
