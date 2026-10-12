"""The fleet picture: all servers, their key figures and the host load, in ONE response.

**Why this service exists.** Calling `GET /api/servers/{key}/overview` per server means one
request per server every twelve seconds, each doing container state, `docker stats`, an RCON
round trip, a directory listing of the backups, a disk query and three database queries.
`docker stats?stream=false` alone takes about a second per container (Docker takes two CPU
samples), and this backend runs with **one** uvicorn worker. Here everything is collected once
in parallel, the disk is queried once for the host instead of once per server, and the
sparklines come from **one** query over `metric_samples`.

**Two rules this service must follow:**

1. *An unclear answer is an exception, never a zero.* If RCON does not answer, the result is
   `players: null` and `players_source: "unknown"`, **not** `0`. `metric_samples` is
   explicitly NOT a fallback: `metrics._sample_server` deliberately writes `players=0` on an
   RCON error so the history graph has no gap. That is a defensible choice for a graph; as the
   statement "nobody is here" it would turn an unknown into a result.
2. *The display reads the same source as the decision.* The idle countdown comes from
   `idle_autostop.idle_status`, the same function the hourly job takes its stop decision
   from.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
import shutil
import time

from sqlmodel import Session, select

from ..config import settings
from ..db import engine
from ..models import MetricSample
from ..models.player import utcnow_naive
from ..models.server import GameServer
from ..services import idle_autostop, player_count
from ..services import resource_budget as budget_svc
from ..services import restarts
from ..services import servers as servers_registry
from ..services.backup import list_backups
from ..services.docker_client import docker, start_time as docker_start_time
from ..services.drivers import GameDriver, get_driver, running_version

log = logging.getLogger("uvicorn.error")

# How long a finished fleet response is reused. Deliberately SHORT and deliberately only here:
# a cache on `docker.state()` would be dangerous because `lifecycle.stop_and_confirm` and
# `version_manager.verify_boot` depend on it; a stale "not running" would confirm a stop that
# never happened. This cache sits above that and serves exactly one purpose: several open
# browser tabs share one round instead of each running its own.
CACHE_TTL_S = 3.0

# Time budget for the player count per server. `driver.players()` depends on RCON or the Steam
# query; a hanging server must not hold up the whole fleet view.
RCON_BUDGET_S = 3.0

# Number of points in a sparkline (last hour, 60 s samples -> ~60 rows).
SPARK_POINTS = 12

_cache: tuple[float, dict] | None = None
_cache_lock = asyncio.Lock()


async def fleet_status(*, force: bool = False) -> dict:
    """The whole fleet in one response (cached briefly)."""
    global _cache
    async with _cache_lock:
        if not force and _cache is not None and (time.monotonic() - _cache[0]) < CACHE_TTL_S:
            return _cache[1]
        data = await _collect()
        _cache = (time.monotonic(), data)
        return data


def clear_cache() -> None:
    """Discard the cache after an action (start/stop/restart).

    Otherwise the row would keep showing the old state for up to `CACHE_TTL_S`, right after the
    click, i.e. exactly when someone is looking.
    """
    global _cache
    _cache = None


async def _collect() -> dict:
    pairs = servers_registry.list_enabled_with_driver()
    default_timeout = budget_svc.get_budget().default_idle_timeout_minutes
    sparks = _sparklines([s.id for s, _ in pairs])

    # First the servers, all in parallel. The most expensive item is `docker stats?stream=false`
    # at about one second PER RUNNING container (Docker takes two CPU samples); sequentially
    # that adds up to one second per running server.
    results = await asyncio.gather(
        *(_one_server(s, d, default_timeout, sparks.get(s.id, [])) for s, d in pairs),
        return_exceptions=True,
    )

    server: list[dict] = []
    stats_hint: dict[str, dict] = {}
    for (s, _), e in zip(pairs, results, strict=True):
        if isinstance(e, BaseException):
            # A server that cannot be queried does not drop out of the list: it is listed and
            # says that it could not be queried. Omitting it would mean "does not exist".
            log.warning("fleet[%s]: row failed", s.key, exc_info=False)
            server.append(_unqueryable(s, str(e)))
        else:
            server.append(e)
            if e.get("stats"):
                stats_hint[s.container_name] = e["stats"]

    # THEN the host, deliberately afterwards and not alongside: `budget.usage()` needs the
    # memory samples of the running servers, and those have just been taken. Measuring them a
    # second time costs about 1 s per container without a cgroup limit. The rest of `_host`
    # takes milliseconds.
    host = await _host(pairs, stats_hint)

    server.sort(key=lambda r: (r["sort_order"], r["id"] or 0))
    return {
        "host": host,
        "job": {"idle_interval_min": idle_autostop.JOB_INTERVAL_MIN},
        "generated_at": utcnow_naive().isoformat(),
        "servers": server,
    }


def connect_of(s: GameServer, drv: GameDriver | None = None) -> dict | None:
    """Where players connect: `{host, port, protocol}` from the driver — `None` if it cannot say.

    The same source as the join page (`driver.connect_info`), so the fleet, the server page and
    the join page cannot show three different addresses. `host` is empty when no public address
    is configured; the UI then says so instead of showing an address nobody can reach.
    """
    try:
        info = (drv or get_driver(s)).connect_info(s)
    except Exception:  # noqa: BLE001 — an unreadable config must not break the fleet
        return None
    return {"host": info.host, "port": info.port, "protocol": info.protocol}


def _unqueryable(s: GameServer, error: str) -> dict:
    return {
        "id": s.id, "key": s.key, "game_type": s.game_type, "display_name": s.display_name,
        "sort_order": s.sort_order, "game_version": running_version(s), "connect": connect_of(s),
        "container": {"exists": None, "running": None, "status": "unknown"},
        "uptime_seconds": None, "stats": {}, "spark": [],
        "players": None, "players_source": "unknown", "max_players": None,
        "rcon": {"reachable": False, "latency_ms": None},
        "idle": None, "next_restart": None, "last_backup_at": None,
        "error": error,
    }


async def _one_server(s: GameServer, drv: GameDriver, default_timeout: int, spark: list) -> dict:
    st = await docker.state(s.container_name)
    running = bool(st.get("running"))
    started_at = docker_start_time(st.get("started_at"))

    stats: dict = {}
    if running:
        try:
            stats = await docker.stats(s.container_name)
            stats["mem_max_heap"] = st.get("max_heap_bytes")
            # What `mem_bytes` (the whole container: heap + JVM + native) is measured against:
            # the container's own memory limit, `None` without one. Docker's `mem_limit` is the
            # host's RAM then, and the heap ceiling is a different quantity.
            stats["mem_container_limit"] = st.get("memory") or None
        except Exception:
            stats = {}

    players, source, latency, rcon_ok = None, "off", None, False
    if running:
        try:
            # Shared with the other display paths (`services/player_count.py`).
            players, latency = await asyncio.wait_for(player_count.players(s, drv), timeout=RCON_BUDGET_S)
            source, rcon_ok = "live", True
        except Exception:
            # NO fallback to the last metric sample: that one writes `players=0` on an RCON
            # error (see module docstring). Unknown stays unknown.
            players, source = None, "unknown"

    status = idle_autostop.idle_status(
        s, running=running, started_at=started_at,
        default_timeout=default_timeout, now=utcnow_naive(),
    )

    try:
        game_stats = drv.overview_stats(s)
    except Exception:
        game_stats = {}

    return {
        "id": s.id,
        "key": s.key,
        "game_type": s.game_type,
        "display_name": s.display_name,
        "sort_order": s.sort_order,
        # The source that applies to this game, not the column (see `running_version`).
        "game_version": running_version(s),
        "connect": connect_of(s, drv),
        "container": {
            "exists": st.get("exists"),
            "running": running,
            "restarting": st.get("restarting"),
            "restarts": st.get("restarts"),
            "status": st.get("status"),
            "health": st.get("health"),
            "started_at": st.get("started_at"),
        },
        "uptime_seconds": _uptime(started_at),
        "stats": stats,
        "spark": spark,
        "players": players,
        # "live" (freshly queried) | "unknown" (queried, no answer) | "off" (not queried
        # because the container is stopped). The UI must render the three differently.
        "players_source": source,
        "max_players": game_stats.get("max_players"),
        "rcon": {"reachable": rcon_ok, "latency_ms": latency},
        "idle": {
            "active": status.active,
            "reason": status.reason,
            "timeout_min": status.timeout_min,
            "last_active_at": status.last_active_at.isoformat() if status.last_active_at else None,
            "earliest_stop_at": status.earliest_stop_at.isoformat() if status.earliest_stop_at else None,
            "remaining_min": status.remaining_min,
        },
        "next_restart": restarts.next_restart(server_id=s.id),
        "last_backup_at": await _last_backup(s, drv),
    }


def _uptime(started_at: dt.datetime | None) -> int | None:
    if started_at is None:
        return None
    return max(int((utcnow_naive() - started_at).total_seconds()), 0)


async def _last_backup(s: GameServer, drv: GameDriver) -> str | None:
    try:
        spec = drv.backup_spec(s)
        entries = await asyncio.to_thread(list_backups, data_dir=spec.data_dir)  # listing -> thread
        return entries[0]["created"] if entries else None
    except Exception:
        return None


def _sparklines(server_ids: list[int | None]) -> dict[int, list[dict]]:
    """CPU and player count of the last hour per server, ONE query for all.

    Querying per server would mean one query per server every few seconds, and the table grows
    with 60 s samples per server.
    """
    ids = [i for i in server_ids if i is not None]
    if not ids:
        return {}
    since = utcnow_naive() - dt.timedelta(hours=1)
    raw: dict[int, list[MetricSample]] = {i: [] for i in ids}
    try:
        with Session(engine) as session:
            lines = session.exec(
                select(MetricSample)
                .where(MetricSample.server_id.in_(ids), MetricSample.at >= since)
                .order_by(MetricSample.at)
            ).all()
        for z in lines:
            raw.setdefault(z.server_id, []).append(z)
    except Exception:
        log.warning("fleet: sparklines cannot be read", exc_info=False)
        return {}
    return {sid: _condense(zs) for sid, zs in raw.items()}


def _condense(lines: list[MetricSample]) -> list[dict]:
    """Reduce samples to `SPARK_POINTS` averages."""
    if not lines:
        return []
    n = len(lines)
    size = max(1, -(-n // SPARK_POINTS))  # ceiling division
    out: list[dict] = []
    for i in range(0, n, size):
        block = lines[i : i + size]
        k = len(block)
        out.append({
            "cpu": round(sum(float(b.cpu_percent or 0) for b in block) / k, 1),
            "players": round(sum(int(b.players or 0) for b in block) / k, 1),
            "running": any(b.running for b in block),
        })
    return out


async def _host(pairs: list[tuple[GameServer, GameDriver]], stats_hint: dict[str, dict]) -> dict:
    """Host totals, and an honest statement of where they come from.

    `GET /info` on the socket proxy returns `NCPU` and `MemTotal` of the HOST. If the endpoint
    is not enabled, only the budget set by the admin remains, which is an upper bound someone
    entered, not a measurement. The difference belongs in the response, not in a footnote: a
    bar reading "71 % used" means something entirely different depending on the source.
    """
    async def _disk():
        # The servers' own directories first, then DATA_ROOT: on a fresh install there is no
        # server yet, and "not readable" would be wrong — DATA_ROOT is mounted into the API at
        # the same path and is where every server will live.
        for path in [s.data_dir for s, _ in pairs] + [settings.data_root]:
            try:
                du = await asyncio.to_thread(shutil.disk_usage, path)
                return {
                    "free": du.free, "total": du.total, "used": du.used,
                    "percent": round(du.used / du.total * 100, 1) if du.total else None,
                }
            except Exception:
                continue
        return None

    info, budget, disk = await asyncio.gather(
        docker.host_info(), budget_svc.usage(stats_hint=stats_hint), _disk()
    )

    return {
        "cpu_count": info.get("cpu_count"),
        "mem_total_bytes": info.get("mem_total_bytes"),
        "source": "docker_info" if info.get("cpu_count") else "budget",
        "disk": disk,
        "budget": budget,
    }
