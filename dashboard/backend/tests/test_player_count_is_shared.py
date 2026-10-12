"""One RCON player list per server for everything that only shows or samples it.

Every RCON command is a connection of its own, and Paper/Minecraft logs each one ("Thread RCON
Client /… started" / "shutting down"). The fleet page polls every 6 s, the status strip every
12 s in every open tab, plus the minute jobs: with a few tabs open the game log got two lines
about once a second. The display paths now share one answer per server for `MAX_AGE_S`;
decisions (watchdog liveness, idle auto-stop, auto-update, boot proof) still ask fresh.
"""
import ast
import asyncio
from pathlib import Path

import pytest

from app.services import player_count
from app.services.rcon import RconError

APP = Path(__file__).resolve().parents[1] / "app"


class _Srv:
    def __init__(self, key="mc-1"):
        self.key = key


class _Drv:
    def __init__(self, fail=False, delay=0.0):
        self.calls = 0
        self.fail = fail
        self.delay = delay

    async def players(self, server):
        self.calls += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.fail:
            raise RconError("rcon down")
        return {"count": self.calls, "names": []}


@pytest.fixture()
def cached(monkeypatch):
    monkeypatch.setattr(player_count, "MAX_AGE_S", 15.0)


def test_a_second_ask_within_the_window_reuses_the_answer(cached, monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(player_count, "_now", lambda: clock[0])
    drv = _Drv()

    async def scenario():
        first, _ = await player_count.players(_Srv(), drv)
        clock[0] += 10
        second, _ = await player_count.players(_Srv(), drv)
        clock[0] += 6
        third, _ = await player_count.players(_Srv(), drv)
        return first, second, third

    first, second, third = asyncio.run(scenario())
    assert drv.calls == 2
    assert first == second == {"count": 1, "names": []}
    assert third == {"count": 2, "names": []}


def test_concurrent_asks_share_one_connection(cached):
    drv = _Drv(delay=0.05)

    async def scenario():
        return await asyncio.gather(*(player_count.players(_Srv(), drv) for _ in range(5)))

    answers = asyncio.run(scenario())
    assert drv.calls == 1
    assert all(a[0] == {"count": 1, "names": []} for a in answers)


def test_servers_are_kept_apart(cached):
    a, b = _Drv(), _Drv()
    asyncio.run(player_count.players(_Srv("mc-1"), a))
    asyncio.run(player_count.players(_Srv("mc-2"), b))
    assert (a.calls, b.calls) == (1, 1)


def test_an_error_is_not_kept(cached):
    drv = _Drv(fail=True)
    for _ in range(2):
        with pytest.raises(RconError):
            asyncio.run(player_count.players(_Srv(), drv))
    assert drv.calls == 2, "a failed ask must not stand in for the next one"


# The display and sampling paths go through the shared answer; the deciding paths must not.
SHARED = ["services/fleet.py", "routers/server.py", "routers/join.py", "services/metrics.py",
          "services/stats_poller.py"]
FRESH = ["services/watchdog.py", "services/idle_autostop.py", "services/auto_update.py",
         "services/version_manager.py"]


def _calls(rel: str) -> tuple[int, int]:
    """(direct `….players(server)` driver calls, `player_count.players(...)` calls)."""
    tree = ast.parse((APP / rel).read_text(encoding="utf-8"))
    direct = shared = 0
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "players":
            owner = node.func.value
            if isinstance(owner, ast.Name) and owner.id == "player_count":
                shared += 1
            else:
                direct += 1
    return direct, shared


@pytest.mark.parametrize("rel", SHARED)
def test_display_paths_share_the_answer(rel):
    direct, shared = _calls(rel)
    assert direct == 0 and shared >= 1, f"{rel}: {direct} direct RCON player call(s)"


@pytest.mark.parametrize("rel", FRESH)
def test_deciding_paths_still_ask_fresh(rel):
    direct, shared = _calls(rel)
    assert shared == 0 and direct >= 1, rel
