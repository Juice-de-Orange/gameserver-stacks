"""The version cache invalidates itself when the container starts.

Vintage Story and Reforger read the running game version from the start banner — a line that is
hundreds of thousands of characters back after a longer uptime, which is why the result is
remembered. A cache keyed only by `server.key` is **never** invalidated; since `get_driver()`
caches one singleton per game type, the dict lives for the whole process lifetime.

"Changes only on restart" — and a restart is exactly what such a cache does not notice. After a
version change the dashboard would keep reporting the old version: the nastiest kind of bug,
because the display stays plausible.
"""
import asyncio

import pytest

from app.models.server import GameServer, GameType
from app.services.drivers.factorio import FactorioDriver
from app.services.drivers.reforger import ReforgerDriver
from app.services.drivers.vintagestory import VintageStoryDriver

BANNER = {
    "vs": "5.7.2026 20:42:59 [Server Notification] Game Version: v{v} (Stable)",
    "rf": "INIT : Creating game instance(ArmaReforgerScripted), version {v} built 2026-06-14",
    "fac": "   0.123 Factorio {v} (build 82345, linux64, headless)",
}


class _Docker:
    """Docker stub: `started_at` and log text can be switched from outside."""

    def __init__(self, started_at, text):
        self.started_at = started_at
        self.text = text
        self.log_calls = 0

    async def state(self, name):
        return {"running": True, "started_at": self.started_at}

    async def logs(self, name, tail=100):
        self.log_calls += 1
        return self.text


def _srv(key, container):
    return GameServer(key=key, game_type=GameType.pz, container_name=container)


_DRIVERS = {"vs": VintageStoryDriver, "rf": ReforgerDriver, "fac": FactorioDriver}


@pytest.fixture(params=sorted(_DRIVERS))
def driver(request, monkeypatch):
    """Run every driver with banner detection against the same assertions.

    The code behind it (`_version_from_log`) is shared — the only per-driver difference is the
    regex. That is exactly what nobody notices when it does not match: the display stays
    plausible and simply shows nothing.
    """
    from app.services import docker_client

    kind = request.param
    drv = _DRIVERS[kind]()
    type(drv)._version_cache.clear()
    d = _Docker("2026-08-02T10:00:00.000000000Z", BANNER[kind].format(v="1.21.6"))
    monkeypatch.setattr(docker_client, "docker", d)
    return drv, d, kind


def test_the_version_is_detected(driver):
    drv, d, _ = driver
    assert asyncio.run(drv.detect_version(_srv("x", "c"))) == "1.21.6"
    assert d.log_calls == 1


def test_the_second_call_does_not_fetch_the_log_again(driver):
    """The reason for the cache: `tail=1_000_000` against a log of several hundred KB."""
    drv, d, _ = driver
    srv = _srv("x", "c")
    asyncio.run(drv.detect_version(srv))
    asyncio.run(drv.detect_version(srv))
    assert d.log_calls == 1


def test_a_restart_invalidates_the_cache(driver):
    """The core case: without invalidation the second call here would still return "1.21.6"."""
    drv, d, kind = driver
    srv = _srv("x", "c")
    assert asyncio.run(drv.detect_version(srv)) == "1.21.6"

    d.started_at = "2026-08-02T12:00:00.000000000Z"   # container restarted
    d.text = BANNER[kind].format(v="1.21.7")           # new version in the banner

    assert asyncio.run(drv.detect_version(srv)) == "1.21.7"
    assert d.log_calls == 2


def test_two_servers_do_not_share_an_entry(driver):
    drv, d, kind = driver
    assert asyncio.run(drv.detect_version(_srv("a", "ca"))) == "1.21.6"
    d.text = BANNER[kind].format(v="1.22.0")
    assert asyncio.run(drv.detect_version(_srv("b", "cb"))) == "1.22.0"


def test_an_empty_result_is_not_cached(driver):
    """"Nothing in the log yet" is not a result — the next call must search again.

    Otherwise a server whose banner was not yet written at the first look would stay without a
    version for the whole process lifetime.
    """
    drv, d, kind = driver
    d.text = "nothing of the sort"
    srv = _srv("x", "c")
    assert asyncio.run(drv.detect_version(srv)) == ""

    d.text = BANNER[kind].format(v="1.21.6")
    assert asyncio.run(drv.detect_version(srv)) == "1.21.6"


def test_a_docker_error_does_not_raise(driver):
    drv, d, _ = driver

    async def _broken(name, tail=100):
        raise RuntimeError("proxy gone")

    d.logs = _broken
    assert asyncio.run(drv.detect_version(_srv("x", "c"))) == ""


def test_the_drivers_do_not_share_a_cache_dict():
    """Otherwise a Vintage Story server could overwrite the version of a Reforger server."""
    assert VintageStoryDriver._version_cache is not ReforgerDriver._version_cache
