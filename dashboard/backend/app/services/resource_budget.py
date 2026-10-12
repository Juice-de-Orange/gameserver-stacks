"""Resource budget logic (multi-game).

The only reservation unit is `game_servers.ram_mb` (cgroup `HostConfig.Memory`) — the same unit
for ALL games. `resource_budget.total_ram_mb == 0` means "advisory only" (starts are not
blocked) until an admin sets the shared limit.

Safety: putting the first cgroup cap on a running, previously unlimited server is the most
dangerous operation — `validate_allocation` enforces `ram_mb >= heap_max_mb * (1 + headroom)`,
and the router additionally reads the live RSS before it lowers a memory limit.
"""
from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from sqlmodel import Session, select

from ..db import engine
from ..models import ResourceBudget
from ..services import servers as servers_registry
from ..services import world_lock
from ..services.docker_client import docker

log = logging.getLogger("uvicorn.error")

if TYPE_CHECKING:
    from ..models.server import GameServer


def get_budget() -> ResourceBudget:
    """Budget singleton; if not seeded (yet): an unpersisted default (advisory only)."""
    with Session(engine) as s:
        return s.exec(select(ResourceBudget)).first() or ResourceBudget()


def validate_allocation(ram_mb: int, heap_max_mb: int | None, headroom_pct: int) -> tuple[bool, str]:
    """Whether `ram_mb` is a safe cgroup limit for a server with JVM heap `heap_max_mb`.

    `ram_mb == 0` → no cap (always ok). JVM games need enough headroom above the heap
    (metaspace/threads/off-heap/native), otherwise the kernel OOM-kills the process. Games
    without a heap setting (heap None, e.g. Vintage Story) have no JVM condition.
    """
    if ram_mb <= 0:
        return True, ""
    if heap_max_mb:
        factor = 1 + headroom_pct / 100
        need = int(heap_max_mb * factor)
        if ram_mb < need:
            return False, (
                f"ram_mb={ram_mb} MB < heap_max_mb×{factor:.2f} = {need} MB — risk of OOM "
                f"(heap {heap_max_mb} MB needs {headroom_pct}% headroom)"
            )
    return True, ""


async def _effective_demand(
    server: GameServer, budget, *, running: bool, stats_hint: dict[str, dict] | None = None
) -> int:
    """How much RAM does this server really use (or will it use)?

    A server without a cgroup cap (`ram_mb=0`) still uses RAM. Booking it as 0 would allow
    overcommit. That applies to the server about to START just as much as to the running ones:
    a PZ server typically runs with `ram_mb=0, heap_max_mb=8192` on purpose (a hard memory limit
    risks an OOM kill in the middle of a world save), so `server.ram_mb or 0` would make the
    largest server cost nothing.

    A server that is not running yet has no RSS — the JVM ceiling is the only honest estimate
    for it.

    Returns `None` when a running server's usage is unknown (measurement failed, no fallback).
    """
    if (server.ram_mb or 0) > 0:
        return server.ram_mb
    estimated = 0
    if running:
        # `stats_hint` is a sample already taken in the same pass, not a cache: the overview
        # fetches `docker.stats` per server anyway, and `docker stats?stream=false` costs about a
        # second per container (Docker takes two CPU samples). A SILENT cache would be wrong
        # here — `routers/resources` reads the same number before it LOWERS a memory limit and
        # must not be handed an older value. So the caller has to pass the sample explicitly.
        known = (stats_hint or {}).get(server.container_name)
        try:
            st = known if known is not None else await docker.stats(server.container_name)
            estimated = int((st.get("mem_bytes") or 0) / 1024 / 1024)
            _last_measured[server.container_name] = estimated
        except Exception:
            # An unreadable measurement does not mean "uses nothing". Booking 0 here would undo
            # what `_running_servers` does for an unreadable container state ("when in doubt it
            # IS running"): the server would be counted, but with 0 MB. The JVM floor below only
            # helps when `heap_max_mb` is set.
            #
            # The fallback is the most recent measurement of the SAME container (from this
            # process or from the metrics table). If there is none either, the usage is
            # **unknown** — `None` means unknown, not zero. What that means for the start button
            # is decided by `can_start`, not here.
            estimated = _last_measured.get(server.container_name) or _from_metrics(server)
            log.warning("budget[%s]: RAM measurement cannot be read — %s", server.key,
                        f"last known measurement: {estimated} MB" if estimated
                        else "no fallback value, demand UNKNOWN")
    if server.heap_max_mb:
        floor = int(server.heap_max_mb * (1 + budget.mem_headroom_pct / 100))
        estimated = max(estimated or 0, floor)
    return estimated if estimated else (0 if not running else None)


# The most recent successful measurement per container, process-local (single-worker
# invariant). Not a cache: it is read only when the live measurement has just FAILED.
_last_measured: dict[str, int] = {}


def _from_metrics(server) -> int:
    """The last measured RAM value from the history — 0 if there is none.

    Reliable only because `metrics._sample_server` writes no row at all for a failed sample; an
    invented `mem_bytes = 0` there would make this fallback wrong.
    """
    from ..models import MetricSample

    try:
        with Session(engine) as session:
            line = session.exec(
                select(MetricSample)
                .where(MetricSample.server_id == server.id, MetricSample.running == True)  # noqa: E712
                .order_by(MetricSample.at.desc())
                .limit(1)
            ).first()
        return int((line.mem_bytes or 0) / 1024 / 1024) if line else 0
    except Exception:
        log.warning("budget[%s]: metric fallback cannot be read", server.key, exc_info=False)
        return 0


async def _running_servers(
    exclude_id: int | None = None, *, stats_hint: dict[str, dict] | None = None
) -> list[dict]:
    """Servers currently RUNNING (or held down by a world operation), largest effective RAM first.

    Not filtered by driver: the budget counts containers, not driver availability.

    `effective_ram_mb`: a running **unlimited** server (ram_mb=0) still uses real RAM — counting
    it as 0 would allow overcommit. For ram_mb=0 the live RSS is used instead, but at least
    `heap_max_mb×(1+headroom)` (JVM ceiling).
    """
    budget = get_budget()
    # `list_all()`, not `list_enabled()`. `enabled=False` is how a server is "deleted" from the
    # registry, and it does not touch the container: it keeps running under `unless-stopped`
    # and keeps its RAM. Leaving it out would make `used_ram_mb` too small and let `can_start`
    # report free memory that does not exist — "no longer managed" is not "uses nothing".
    candidates = [
        s for s in servers_registry.list_all()
        if exclude_id is None or s.id != exclude_id
    ]

    # In parallel, not one after another: this function runs BEFORE EVERY server start
    # (`can_start`), i.e. exactly when someone has pressed a button and is waiting, and serial
    # proxy round trips add up to seconds.
    states = await asyncio.gather(
        *(docker.state(s.container_name) for s in candidates), return_exceptions=True
    )

    # The second, more expensive half runs in parallel as well (the `gather` further down):
    # `_effective_demand` calls `docker.stats` for every server without a cgroup cap, at about
    # 1 s per container, and `can_start` passes no `stats_hint`.
    prepared: list[tuple] = []
    for server, st in zip(candidates, states, strict=True):
        if isinstance(st, BaseException):
            # When in doubt it IS running. A `continue` here would drop the server from the sum,
            # `used_ram` would be too small and `can_start` would give a green light — the start
            # would run into a host OOM, triggered by a socket-proxy timeout for a single
            # container. Same direction as `minecraft._running`.
            log.warning(
                "budget[%s]: container state cannot be read — counts as running", server.key,
            )
            st = {"running": True}
        # A server that a world operation is currently holding down keeps its place. A version
        # change or restore leaves the container stopped across pull, mandatory backup and
        # rebuild; without this, its RAM would drop out of `used_ram` and `can_start` would
        # allow a start whose memory the running operation needs again in a moment.
        reserved = None if st.get("running") else world_lock.held_by(server.key)
        if not st.get("running") and not reserved:
            continue
        prepared.append((server, st, reserved))

    demands = await asyncio.gather(*(
        _effective_demand(server, budget, running=bool(st.get("running")), stats_hint=stats_hint)
        for server, st, _ in prepared
    ), return_exceptions=True)

    out: list[dict] = []
    for (server, _st, reserved), effective in zip(prepared, demands, strict=True):
        if isinstance(effective, BaseException):
            log.warning("budget[%s]: demand cannot be determined (%s) — treated as UNKNOWN",
                        server.key, effective)
            effective = None
        out.append({
            "id": server.id, "key": server.key, "display_name": server.display_name,
            "container_name": server.container_name, "ram_mb": server.ram_mb or 0,
            "effective_ram_mb": effective, "cpu_cores": server.cpu_cores or 0.0,
            "uncapped": (server.ram_mb or 0) <= 0,
            # `None` means UNKNOWN, not zero — the caller decides what that means for it. The
            # display may estimate, the start button may not.
            "demand_unknown": effective is None,
            "reserved_for": reserved,
        })
    out.sort(key=lambda s: s["effective_ram_mb"] or 0, reverse=True)
    return out


async def usage(exclude_id: int | None = None, *, stats_hint: dict[str, dict] | None = None) -> dict:
    """Current budget usage: sum of the running servers' effective RAM use, plus the cap.

    `stats_hint` (container name -> `docker.stats()` result) saves a second measurement when the
    caller already has one. See `_effective_demand`.
    """
    running = await _running_servers(exclude_id=exclude_id, stats_hint=stats_hint)
    budget = get_budget()
    used_ram = sum(s["effective_ram_mb"] or 0 for s in running)
    used_cpu = sum(s["cpu_cores"] for s in running)
    unknown = [s["key"] for s in running if s["demand_unknown"]]
    return {
        "total_ram_mb": budget.total_ram_mb,
        "total_cpu_cores": budget.total_cpu_cores,
        "used_ram_mb": used_ram,
        "used_cpu_cores": round(used_cpu, 2),
        "free_ram_mb": (budget.total_ram_mb - used_ram) if budget.total_ram_mb else None,
        "running": running,
        # Servers whose usage could not be measured. `used_ram_mb` is then a LOWER BOUND, and the
        # display must be able to say so instead of claiming too much free memory.
        "demand_unknown": unknown,
    }


async def can_start(server: GameServer) -> tuple[bool, int, list[dict]]:
    """Does `server` (with its ram_mb reservation) still fit into the budget?

    Returns `(ok, deficit_mb, stop_candidates)`. With `total_ram_mb == 0` (advisory) ALWAYS ok.
    `stop_candidates` = running servers (largest first) whose stop would free the memory needed.
    """
    budget = get_budget()
    if not budget.total_ram_mb or budget.total_ram_mb <= 0:
        return True, 0, []
    running = await _running_servers(exclude_id=server.id)
    # Unknown usage is not free space. If a running server's usage cannot be measured right
    # now, this function cannot answer the question — and "yes" would be the more expensive of
    # the two answers: the start could run into a host OOM, and the kernel's biggest victim is
    # a server that deliberately runs without a cgroup cap. The admin `force` remains the
    # escape hatch.
    if any(s["demand_unknown"] for s in running):
        blind = [s["key"] for s in running if s["demand_unknown"]]
        log.warning("budget: start rejected — demand cannot be measured for %s", ", ".join(blind))
        return False, 0, running
    used_ram = sum(s["effective_ram_mb"] or 0 for s in running)
    # Same calculation as for the running servers: with `server.ram_mb or 0` a server without
    # a cgroup cap would cost nothing at start and get through even with the budget full.
    need = await _effective_demand(server, budget, running=False)
    if used_ram + need <= budget.total_ram_mb:
        return True, 0, []
    deficit = used_ram + need - budget.total_ram_mb
    return False, deficit, running
