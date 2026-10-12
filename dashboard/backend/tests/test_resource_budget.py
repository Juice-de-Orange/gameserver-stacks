"""Resource budget: validator + can_start (candidates/deficit)."""
import asyncio

from app.models import ResourceBudget
from app.models.server import GameServer, GameType
from app.services import resource_budget as rb

# **The seam is `list_all`.** The budget does not build its candidates from `list_enabled()`:
# `enabled=False` is the DELETE gesture and does not touch the container, which keeps running
# under `unless-stopped` and occupies its RAM, so it must be visible to `usage()` and
# `can_start()`. A `getattr` fallback in the seam would silently keep these dummies on the old
# behaviour.


def _srv(key, sid, ram_mb=0, cpu=0.0, container=None):
    s = GameServer(key=key, game_type=GameType.pz, container_name=container or key, ram_mb=ram_mb, cpu_cores=cpu)
    s.id = sid
    return s


class _Docker:
    """Has `stats`, so the estimation path for servers without a cgroup cap in
    `_running_servers` is reachable by tests."""

    def __init__(self, running_names, rss_bytes=0):
        self.running = set(running_names)
        self.rss_bytes = rss_bytes

    async def state(self, name):
        return {"running": name in self.running}

    async def stats(self, name):
        return {"mem_bytes": self.rss_bytes}


def test_validate_allocation():
    assert rb.validate_allocation(0, 8192, 25) == (True, "")            # 0 = no cap
    assert rb.validate_allocation(10240, 8192, 25)[0] is True           # 10240 == 8192*1.25
    ok, msg = rb.validate_allocation(9000, 8192, 25)                    # < Headroom
    assert ok is False and "OOM" in msg
    assert rb.validate_allocation(2048, None, 25) == (True, "")         # VS: no heap


def test_can_start_advisory_when_budget_zero(monkeypatch):
    monkeypatch.setattr(rb, "get_budget", lambda: ResourceBudget(total_ram_mb=0))
    monkeypatch.setattr(rb.servers_registry, "list_all", lambda: [])
    monkeypatch.setattr(rb, "docker", _Docker([]))
    ok, deficit, cands = asyncio.run(rb.can_start(_srv("x", 1, ram_mb=8192)))
    assert ok and deficit == 0 and cands == []


def test_can_start_fits(monkeypatch):
    a = _srv("a", 1, ram_mb=8192, container="a")
    x = _srv("x", 2, ram_mb=4096, container="x")
    monkeypatch.setattr(rb, "get_budget", lambda: ResourceBudget(total_ram_mb=16384))
    monkeypatch.setattr(rb.servers_registry, "list_all", lambda: [a, x])
    monkeypatch.setattr(rb, "docker", _Docker(["a"]))  # a running (8192) + x wants 4096 → 12288 ≤ 16384
    ok, deficit, _ = asyncio.run(rb.can_start(x))
    assert ok and deficit == 0


def test_can_start_exceeds_gives_candidates(monkeypatch):
    a = _srv("a", 1, ram_mb=8192, container="a")
    b = _srv("b", 2, ram_mb=6144, container="b")
    x = _srv("x", 3, ram_mb=8192, container="x")
    monkeypatch.setattr(rb, "get_budget", lambda: ResourceBudget(total_ram_mb=16384))
    monkeypatch.setattr(rb.servers_registry, "list_all", lambda: [a, b, x])
    monkeypatch.setattr(rb, "docker", _Docker(["a", "b"]))  # a+b running (14336) + x wants 8192 → 22528 > 16384
    ok, deficit, cands = asyncio.run(rb.can_start(x))
    assert ok is False
    assert deficit == 14336 + 8192 - 16384  # 6144
    assert [c["key"] for c in cands] == ["a", "b"]  # largest RAM reservation first


# --- Servers without a cgroup cap -------------------------------------------------------------

def test_a_server_without_a_cap_does_not_cost_zero_at_start(monkeypatch):
    """This hits exactly the largest server.

    A PZ server typically runs with `ram_mb=0, heap_max_mb=8192`, deliberately without a hard
    memory limit, because an OOM kill in the middle of a world save would be worse. Counting it
    as `server.ram_mb or 0`, i.e. 0, means the lock that should protect the host never applies:
    with MC+VS+RF together at 14336 MB of 16384, pz-1 (really ~10 GB) would still start.
    """
    # A realistic allocation: cap 16384, pz-1 uncapped.
    prod = _srv("pz-1", 1, ram_mb=0)
    prod.heap_max_mb = 8192                     # 8192 * 1.25 = 10240
    mc = _srv("mc", 2, ram_mb=5120)
    vs = _srv("vs", 3, ram_mb=3072)
    rf = _srv("rf-1", 4, ram_mb=6144)           # together 14336 of 16384

    monkeypatch.setattr(rb, "docker", _Docker({"mc", "vs", "rf-1"}))
    monkeypatch.setattr(rb.servers_registry, "list_all", lambda: [prod, mc, vs, rf])
    monkeypatch.setattr(rb, "get_budget", lambda: ResourceBudget(total_ram_mb=16384, mem_headroom_pct=25))

    ok, deficit, candidates = asyncio.run(rb.can_start(prod))
    assert ok is False, "pz-1 starts despite a full budget, booked at 0 MB"
    assert deficit == 14336 + 10240 - 16384      # = 8192
    assert [k["key"] for k in candidates] == ["rf-1", "mc", "vs"]   # largest first


def test_a_server_with_a_cap_is_still_counted_with_its_cap(monkeypatch):
    """Cross-check: a server with a cgroup limit is booked with it, not estimated."""
    target = _srv("pz-test", 1, ram_mb=5120)
    target.heap_max_mb = 4096
    monkeypatch.setattr(rb, "docker", _Docker(set()))
    monkeypatch.setattr(rb.servers_registry, "list_all", lambda: [target])
    monkeypatch.setattr(rb, "get_budget", lambda: ResourceBudget(total_ram_mb=16384, mem_headroom_pct=25))

    assert asyncio.run(rb.can_start(target)) == (True, 0, [])


def test_a_running_server_without_a_cap_is_counted_by_rss(monkeypatch):
    """Pins the estimation path for RUNNING servers."""
    running = _srv("pz-1", 1, ram_mb=0)
    running.heap_max_mb = 1024                   # ceiling 1280 …
    monkeypatch.setattr(rb, "docker", _Docker({"pz-1"}, rss_bytes=7000 * 1024 * 1024))
    monkeypatch.setattr(rb.servers_registry, "list_all", lambda: [running])
    monkeypatch.setattr(rb, "get_budget", lambda: ResourceBudget(total_ram_mb=16384, mem_headroom_pct=25))

    rows = asyncio.run(rb._running_servers())
    assert rows[0]["effective_ram_mb"] == 7000     # … the real RSS is larger and wins
    assert rows[0]["uncapped"] is True


def test_an_unqueryable_server_counts_as_running(monkeypatch):
    """No fail-open when COUNTING: the RAM lock must not let through a start it should hold.

    Skipping a server on a `docker.state` error drops it from the sum: `used_ram` is too small
    and `can_start` gives a green light. The trigger is a timeout or 5xx of the socket proxy for
    a single container; the result is a start into a host OOM.

    The direction is the same as elsewhere in the project (`minecraft._running`: "when in
    doubt, it is running").
    """
    import asyncio

    from app.services import resource_budget as rb

    srv = _srv("mc", 1, ram_mb=5120)

    class _DockerBlind:
        async def state(self, name):
            raise RuntimeError("dockerproxy unreachable")

        async def stats(self, name):
            raise RuntimeError("dockerproxy unreachable")

    monkeypatch.setattr(rb, "get_budget", lambda: ResourceBudget(total_ram_mb=16384))
    monkeypatch.setattr(rb, "docker", _DockerBlind())
    monkeypatch.setattr(rb.servers_registry, "list_all", lambda: [srv])

    running = asyncio.run(rb._running_servers())
    assert [s["key"] for s in running] == ["mc"], (
        "A server whose state cannot be queried right now drops out of the sum; the lock then "
        "lets through a start it should hold."
    )
    assert running[0]["effective_ram_mb"] == 5120
