"""Driver resolution: `get_driver(server)` returns the GameDriver for the server's game type.

Drivers are stateless, so one cached singleton per `GameType` is enough. The concrete driver
modules are imported lazily inside the function. That is no longer required (importing a driver
module pulls in nothing heavy and causes no cycle); the lazy form stays because it costs nothing.

**Game activation does not apply here.** Every driver stays resolvable whether its game is
active or not (`services/game_settings.py`): a server row of an inactive game must still render
instead of crashing. Inactive games are filtered where lists are built — the presets, the version
catalogue refresh, the workshop check and download.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from ...models.server import GameType
from .base import (  # re-export
    AutoUpdate, BackupSpec, ConfigField, ConfigReload, DriverError, DriverUnsupported, GameDriver,
    ServerEvent, VersionSource,
)

if TYPE_CHECKING:
    from ...models.server import GameServer

__all__ = [
    "get_driver",
    "GameDriver",
    "DriverError",
    "DriverUnsupported",
    "ServerEvent",
    "ConfigField",
    "AutoUpdate",
    "BackupSpec",
    "ConfigReload",
    "VersionSource",
    "running_version",
    "version_source",
    "versioned_by_image_tag",
]

_INSTANCES: dict[GameType, GameDriver] = {}


def get_driver(server: GameServer) -> GameDriver:
    """Return the (cached) GameDriver for this server row's game type."""
    gt = server.game_type
    drv = _INSTANCES.get(gt)
    if drv is None:
        if gt == GameType.pz:
            from .pz import PZDriver  # lazy: see module docstring

            drv = PZDriver()
        elif gt == GameType.minecraft:
            from .minecraft import MinecraftDriver

            drv = MinecraftDriver()
        elif gt == GameType.vintagestory:
            from .vintagestory import VintageStoryDriver

            drv = VintageStoryDriver()
        elif gt == GameType.arma_reforger:
            from .reforger import ReforgerDriver

            drv = ReforgerDriver()
        elif gt == GameType.factorio:
            from .factorio import FactorioDriver

            drv = FactorioDriver()
        elif gt == GameType.avorion:
            from .avorion import AvorionDriver

            drv = AvorionDriver()
        elif gt == GameType.valheim:
            from .valheim import ValheimDriver

            drv = ValheimDriver()
        else:
            raise NotImplementedError(f"No driver for game_type={gt!r}")
        _INSTANCES[gt] = drv
    return drv


def stop_grace(server) -> int:
    """Stop grace period in seconds for this server, taken from the driver; never raises.

    Passed on to `docker.action(..., "stop", stop_timeout=…)`. Without it Docker's default of
    10 s applies and a game server gets killed in the middle of saving its world.
    """
    try:
        return get_driver(server).stop_timeout(server)
    except Exception:
        return 60


def running_version(server) -> str:
    """The game version that is running, read from the source that is authoritative for the game.

    The registry has two columns, `image_tag` and `game_version`. For games with
    `version_source = image_tag` (PZ, Factorio, Avorion) the tag IS the version, and the two
    columns can drift apart: after a version change that fails its boot check the tag is rolled
    back, the `game_version` column may not be. Consumers such as the mod portal search filter
    by the running game line, so a stale value offers mods built for the wrong version.

    For those games `game_version` is therefore only a fallback. For `env` and `runtime` games it
    remains the source, because there the tag says nothing about the game version.
    """
    if version_source(server) is VersionSource.image_tag:
        return server.image_tag or server.game_version or ""
    return server.game_version or ""


def version_source(server) -> VersionSource:
    """Where this server's version comes from; the single place that answers the question.

    Deliberately without `try`: the version path writes to the world, and silently falling back
    to a default would swallow the error. If the registry holds a game type without a driver,
    `get_driver` raises `NotImplementedError`; `version_manager` and `routers/versions` catch
    exactly that and answer 501.
    """
    return get_driver(server).version_source


def versioned_by_image_tag(server) -> bool:
    """`True` when the image tag IS the game version (PZ, Factorio, Avorion)."""
    return version_source(server) is VersionSource.image_tag
