"""The chat bridge and the broadcasts report their failures.

Both modules have the same job — deliver something to players — and neither may book its
failures as success.

**The bridge:**
* The 12-second fan-out asks for the running state per target, not per (message x target). With
  five servers, ten chat lines in one tick would otherwise take longer than the job's interval.
* The cursor does not move past targets that were not reached — the next round only selects
  `id > cursor`, so the line would be lost for good. The ingest path of the same file does it
  right at exactly this place and explains why.
* A too small read window must be distinguishable from "nothing new".
* Switching off and on again must not replay the whole backlog as fresh chat.

**The broadcasts:**
* Sent to a game without broadcast (Valheim, Reforger), they must not end in a silent no-op — no
  error, no log, the job booking every run as done. `broadcasts.py` mirrors
  `services/restarts.py`, which closes the same class.
* They must not keep firing on a "deleted" server, nor must the restart warning.
* Their countdown must not restart on every change to ANY other broadcast.
"""
from __future__ import annotations

import asyncio
import datetime as dt
from types import SimpleNamespace

import pytest

from app.services import broadcasts as bc
from app.services.cross_server import bridge


def _srv(key, *, sid=1, enabled=True, bridge_out=True):
    return SimpleNamespace(id=sid, key=key, container_name=key, display_name=key,
                           enabled=enabled, bridge_out=bridge_out, bridge_in=True)


class _Drv:
    def capabilities(self): return {"announce"}
    async def announce(self, server, text): self.last_run = text


@pytest.fixture(autouse=True)
def _clean():
    bridge.reset_state()
    yield
    bridge.reset_state()


def _fanout_setup(monkeypatch, targets, states, msgs):
    monkeypatch.setattr(bridge.servers_registry, "list_enabled_with_driver", lambda: targets)

    class _Dk:
        def __init__(self): self.calls = []
        async def state(self, name):
            self.calls.append(name)
            running = states[name]
            if isinstance(running, Exception):
                raise running
            return {"running": running}
    dk = _Dk()
    monkeypatch.setattr(bridge, "docker", dk)

    class _Sess:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def exec(self, stmt):
            # `fanout_once` does `list(session.exec(...))` — the result must be iterable.
            class _Result(list):
                def one(self_i): return 0
                def all(self_i): return list(self_i)
            return _Result(msgs)
    monkeypatch.setattr(bridge, "Session", lambda engine: _Sess())
    monkeypatch.setattr(bridge.bus, "get_settings",
                        lambda s: SimpleNamespace(chat_bridge_enabled=True, chat_format="{msg}"))
    bridge._fanout_cursor["id"] = 0        # baseline already set, otherwise the round returns early
    return dk


def _msg(mid, key="pz-1"):
    return SimpleNamespace(id=mid, server_id=99, server_key=key, player="A", message=f"m{mid}")


def test_the_running_state_is_fetched_per_target_not_per_message(monkeypatch):
    targets = [(_srv("mc", sid=2), _Drv()), (_srv("vs", sid=3), _Drv())]
    dk = _fanout_setup(monkeypatch, targets, {"mc": True, "vs": True},
                          [_msg(1), _msg(2), _msg(3), _msg(4), _msg(5)])
    asyncio.run(bridge.fanout_once())
    assert len(dk.calls) == 2, (
        f"{len(dk.calls)} proxy round trips for 5 messages x 2 targets — the answer depends only "
        f"on the target and does not change within a round"
    )


def test_an_unreachable_target_holds_the_cursor(monkeypatch):
    targets = [(_srv("mc", sid=2), _Drv())]
    _fanout_setup(monkeypatch, targets, {"mc": RuntimeError("proxy dead")}, [_msg(1)])
    asyncio.run(bridge.fanout_once())
    assert bridge._fanout_cursor["id"] == 0, \
        "the line would be lost for good — the next round only takes id > cursor"


def test_a_stopped_target_does_NOT_hold_the_cursor(monkeypatch):
    """"Not running" is a result, not an unknown — otherwise the bridge stalls forever."""
    targets = [(_srv("mc", sid=2), _Drv())]
    _fanout_setup(monkeypatch, targets, {"mc": False}, [_msg(1)])
    asyncio.run(bridge.fanout_once())
    assert bridge._fanout_cursor["id"] == 1


def test_the_pause_discards_the_watermark(monkeypatch):
    bridge._last_at["pz-1"] = (dt.datetime(2026, 8, 20, 12, 0), set())

    class _Sess:
        def __enter__(self): return self
        def __exit__(self, *a): return False
    monkeypatch.setattr(bridge, "Session", lambda engine: _Sess())
    monkeypatch.setattr(bridge.bus, "get_settings",
                        lambda s: SimpleNamespace(chat_bridge_enabled=False))

    asyncio.run(bridge.ingest_once())
    assert bridge._last_at == {}, \
        "otherwise switching back on feeds the whole pause in as fresh chat"


def test_a_broadcast_to_a_game_without_broadcast_is_reported(monkeypatch):
    reported: list[str] = []
    monkeypatch.setattr(bc.log, "info", lambda *a, **k: reported.append(a[0]))
    monkeypatch.setattr(bc, "can_announce", lambda s: (False, "Valheim has no console"))
    monkeypatch.setattr(bc.servers_registry, "resolve_server", lambda sid: _srv("vh"))

    sent = []

    class _Silent:
        async def announce(self, server, text): sent.append(text)
    monkeypatch.setattr(bc, "get_driver", lambda s: _Silent())

    class _Sess:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def get(self, m, i):
            return SimpleNamespace(enabled=True, message="hello", target_all=False, server_id=1)
    monkeypatch.setattr(bc, "Session", lambda engine: _Sess())

    asyncio.run(bc._send(bid=1))
    assert sent == []
    assert reported, "a no-op without a log line is a failure that looks like success"


def test_a_broadcast_to_a_disabled_server_is_skipped(monkeypatch):
    sent = []

    class _Silent:
        async def announce(self, server, text): sent.append(text)
    monkeypatch.setattr(bc, "get_driver", lambda s: _Silent())
    monkeypatch.setattr(bc, "can_announce", lambda s: (True, ""))
    monkeypatch.setattr(bc.servers_registry, "resolve_server",
                        lambda sid: _srv("mc", enabled=False))

    class _Sess:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def get(self, m, i):
            return SimpleNamespace(enabled=True, message="hello", target_all=False, server_id=1)
    monkeypatch.setattr(bc, "Session", lambda engine: _Sess())

    asyncio.run(bc._send(bid=1))
    assert sent == [], "`enabled=False` is the delete gesture — the `target_all` branch checks it too"


def test_a_reload_does_not_reset_the_countdown(monkeypatch):
    """The routers reload GLOBALLY — on every change to any other broadcast."""
    added: list[dict] = []
    upcoming = dt.datetime(2026, 8, 25, 13, 0, tzinfo=dt.timezone.utc)

    class _Sch:
        def get_jobs(self):
            return [SimpleNamespace(id="bcast_7", next_run_time=upcoming)]
        def remove_job(self, jid): pass
        def add_job(self, fn, trigger, **kw): added.append(kw)
    monkeypatch.setattr(bc, "scheduler", _Sch())

    class _Sess:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def exec(self, stmt):
            return SimpleNamespace(all=lambda: [SimpleNamespace(
                id=7, interval_minutes=60, time_hhmm=None, days="daily", enabled=True)])
    monkeypatch.setattr(bc, "Session", lambda engine: _Sess())

    bc.reload_jobs()
    assert added and added[0].get("next_run_time") == upcoming, \
        "otherwise the countdown of every broadcast restarts on every unrelated change"
