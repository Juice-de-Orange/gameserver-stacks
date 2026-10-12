"""`bridge_in` sends, `bridge_out` receives — pinned to the code, not to the comment.

The model comments once had both flags the wrong way round. `cross_server/bridge.ingest_once`
skips servers with `not server.bridge_in` — so it READS the chat of the `bridge_in` servers.
`fanout_once` builds its target list from `s.bridge_out` — so it SENDS to the `bridge_out` servers.

Both defaults are `True`, so a swap only shows with exactly the setup the flags exist for
("this server reads along but does not write"). This test checks the meaning on the real
functions: a swapped comment cannot break it, a swapped IMPLEMENTATION does — the costlier error.
"""
from __future__ import annotations

import asyncio

import pytest

from app.models.server import GameServer, GameType
from app.services import servers as servers_registry
from app.services.cross_server import bridge


def _srv(key, *, inbound: bool, out: bool) -> GameServer:
    s = GameServer(key=key, game_type=GameType.pz, container_name=key, data_dir="/x",
                   display_name=key, bridge_in=inbound, bridge_out=out)
    s.id = abs(hash(key)) % 1000 + 1
    return s


class _Drv:
    def __init__(self) -> None:
        self.announced: list[str] = []
        self.reads = 0

    def chat_log_path(self, server):
        self.reads += 1
        return None            # no source → ingest stops after this, enough as a measuring point

    async def announce(self, server, text):
        self.announced.append(text)


def test_bridge_in_is_the_SOURCE(monkeypatch, mem_engine):
    """Only `bridge_in` servers are read."""
    source, silent = _srv("reads-along", inbound=True, out=False), _srv("does-not-read", inbound=False, out=True)
    d1, d2 = _Drv(), _Drv()
    monkeypatch.setattr(servers_registry, "list_enabled_with_driver", lambda: [(source, d1), (silent, d2)])

    class _Docker:
        async def state(self, name):
            return {"running": True}

    monkeypatch.setattr(bridge, "docker", _Docker())
    monkeypatch.setattr(bridge, "engine", mem_engine)
    monkeypatch.setattr(bridge.bus, "get_settings", lambda s: type("S", (), {"chat_bridge_enabled": True})())

    asyncio.run(bridge.ingest_once())

    assert d1.reads >= 1, "the bridge_in server was not treated as a source"
    assert d2.reads == 0, "a server without bridge_in was read anyway"


def test_bridge_out_is_the_TARGET():
    """The fanout's target list depends on `bridge_out` — checked in the source.

    Deliberately a source assertion and not a run: `fanout_once` needs the bus, loop guard,
    cursor and settings, and the point here is a single line.
    """
    import inspect

    source = inspect.getsource(bridge.fanout_once)
    assert "if s.bridge_out" in source or "s.bridge_out]" in source, (
        "the fanout's target list no longer depends on bridge_out"
    )
    assert "bridge_in" not in source, "the fanout must not know the source side at all"


def test_the_ingest_does_not_know_the_target_side():
    """The opposite direction — otherwise the two flags would be two names for the same thing."""
    import inspect

    source = inspect.getsource(bridge.ingest_once)
    assert "bridge_in" in source
    assert "bridge_out" not in source
