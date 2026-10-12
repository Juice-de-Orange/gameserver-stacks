"""The module meant to protect the host must not count itself rich.

Four places with the same error shape: unknown turned into zero. They hit not the display but
the **start button**: the budget is enforced.

1. **An unreadable RAM measurement must not become 0 MB demand.** The JVM floor only catches
   this where `heap_max_mb` is set. `_running_servers` deliberately treats an unreadable
   container state as "running when in doubt"; asking the same, just provably unreachable proxy
   again and booking the server at 0 MB would be exactly the `continue` that comment rejects.
2. **A disabled but still running server must count.** `enabled=False` is the DELETE gesture
   and does not touch the container — it keeps running under `unless-stopped` and occupies its
   memory.
3. **A server held down by a world operation must not vanish.** For a large PZ server that is
   about 9.4 GB that would count as "free" during a version change — and the room is needed
   again shortly.
4. **And the fallback source must not lie either:** `metrics` must not write a "CPU 0 %, RAM 0
   bytes" row into the history on a failed stats probe, indistinguishable from a real
   measurement. Only when that row is absent can the history serve as a fallback.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from app.services import metrics, resource_budget as rb


def _srv(key, *, ram_mb=0, heap=None, enabled=True, sid=1):
    return SimpleNamespace(id=sid, key=key, container_name=key, display_name=key,
                           ram_mb=ram_mb, heap_max_mb=heap, cpu_cores=0.0, enabled=enabled)


def _budget(monkeypatch, total=20480):
    monkeypatch.setattr(rb, "get_budget", lambda: SimpleNamespace(
        total_ram_mb=total, total_cpu_cores=0, mem_headroom_pct=25))


class _Dk:
    def __init__(self, *, running=True, stats_broken=False, mem=1024 ** 3):
        self.running, self.stats_broken, self.mem = running, stats_broken, mem

    async def state(self, name):
        return {"running": self.running}

    async def stats(self, name):
        if self.stats_broken:
            raise RuntimeError("socket proxy not answering")
        return {"mem_bytes": self.mem}


@pytest.fixture(autouse=True)
def _no_memory():
    rb._last_measured.clear()
    yield
    rb._last_measured.clear()


def test_an_unreadable_measurement_without_a_jvm_floor_is_unknown_not_zero(monkeypatch):
    _budget(monkeypatch)
    monkeypatch.setattr(rb, "docker", _Dk(stats_broken=True))
    monkeypatch.setattr(rb, "_from_metrics", lambda s: 0)
    monkeypatch.setattr(rb.servers_registry, "list_all", lambda: [_srv("fac")])

    running_rows = asyncio.run(rb._running_servers())
    assert running_rows[0]["demand_unknown"] is True
    assert running_rows[0]["effective_ram_mb"] is None, "0 would be indistinguishable from \"uses nothing\""


def test_an_unknown_demand_gives_no_green_light(monkeypatch):
    """The most expensive of the two answers is "yes"."""
    _budget(monkeypatch)
    monkeypatch.setattr(rb, "docker", _Dk(stats_broken=True))
    monkeypatch.setattr(rb, "_from_metrics", lambda s: 0)
    monkeypatch.setattr(rb.servers_registry, "list_all", lambda: [_srv("fac", sid=1)])

    ok, deficit, candidates = asyncio.run(rb.can_start(_srv("new", sid=99, heap=2048)))
    assert ok is False


def test_the_last_good_measurement_carries_over_an_outage(monkeypatch):
    """A single proxy hiccup should not lock the start button right away."""
    _budget(monkeypatch)
    dk = _Dk(mem=4 * 1024 ** 3)
    monkeypatch.setattr(rb, "docker", dk)
    monkeypatch.setattr(rb.servers_registry, "list_all", lambda: [_srv("fac")])

    assert asyncio.run(rb._running_servers())[0]["effective_ram_mb"] == 4096
    dk.stats_broken = True
    second = asyncio.run(rb._running_servers())[0]
    assert second["effective_ram_mb"] == 4096, "the last known measurement is the fallback"
    assert second["demand_unknown"] is False


def test_the_budget_asks_the_registry_for_ALL_rows():
    """**Structural, not via a dummy** — and that is the point here.

    A dummy that replaces the candidate list bypasses exactly the filter in question: it simply
    passes the disabled server through and proves nothing. The defect would sit in the choice of
    the registry function, so that is what gets checked.

    `enabled=False` is the DELETE gesture here (`routers/servers` has no DELETE; deleting means
    `enabled=False`) and does not touch the container — it keeps running under `unless-stopped`
    and occupies its RAM. Built from `list_enabled()`, it would be invisible to `usage()` and
    `can_start()`: "no longer managed" would be booked as "uses nothing", and `can_start` would
    report free memory that does not exist.
    """
    import ast
    import inspect

    source = inspect.getsource(rb._running_servers)
    calls = {
        node.func.attr
        for node in ast.walk(ast.parse(inspect.cleandoc(source)))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    assert "list_all" in calls
    assert "list_enabled" not in calls, (
        "the budget must not build its candidates from the ENABLED ones — a disabled "
        "container keeps running and occupies memory"
    )


def test_a_world_operation_holds_the_room_of_the_stopped_server(monkeypatch):
    _budget(monkeypatch)
    monkeypatch.setattr(rb, "docker", _Dk(running=False))
    monkeypatch.setattr(rb.servers_registry, "list_all", lambda: [_srv("pz-1", heap=8192)])
    monkeypatch.setattr(rb.world_lock, "held_by", lambda k: "version.change")

    status = asyncio.run(rb.usage())
    assert status["used_ram_mb"] == int(8192 * 1.25), \
        "the room is needed again shortly — it must not count as free"
    assert status["running"][0]["reserved_for"] == "version.change"

    # Without a running operation a stopped server still does NOT count.
    monkeypatch.setattr(rb.world_lock, "held_by", lambda k: None)
    assert asyncio.run(rb.usage())["used_ram_mb"] == 0


def test_a_failed_probe_writes_no_invented_zero(monkeypatch):
    """The budget's fallback is only useful as long as the history itself does not lie."""
    written: list = []

    class _Sess:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def add(self, line): written.append(line)
        def commit(self): pass

    class _Dk2:
        async def state(self, name):
            return {"running": True, "max_heap_bytes": 8 * 1024 ** 3}
        async def stats(self, name):
            raise RuntimeError("deadline missed")

    class _Drv:
        async def players(self, server):
            return {"count": 2}

    monkeypatch.setattr(metrics, "docker", _Dk2())
    monkeypatch.setattr(metrics, "Session", lambda engine: _Sess())
    asyncio.run(metrics._sample_server(_srv("pz-1"), _Drv()))
    assert written == [], \
        "a gap in the graph is more honest than a drop to 0 % that never happened"
