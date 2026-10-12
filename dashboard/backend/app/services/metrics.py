"""Periodic metric samples (CPU/RAM/players) for the history graphs.

Multi-game fan-out: ONE scheduler job (`max_instances=1`) writes one row per enabled server
every 60 s; a daily prune keeps the table small. Errors are swallowed per server (no crash while
a server is restarting).
"""
from __future__ import annotations

import datetime as dt
import logging
from typing import TYPE_CHECKING

from sqlalchemy import delete as sa_delete
from sqlmodel import Session

from ..config import settings
from ..db import engine
from ..models import MetricSample
from ..models.player import utcnow_naive
from ..services import player_count
from ..services import servers as servers_registry
from ..services.docker_client import docker

if TYPE_CHECKING:
    from ..models.server import GameServer
    from ..services.drivers import GameDriver

log = logging.getLogger("uvicorn.error")


async def sample_once() -> None:
    """ONE job: write one metric row per enabled server."""
    for server, drv in servers_registry.list_enabled_with_driver():
        try:
            await _sample_server(server, drv)
        except Exception:
            log.warning("metrics[%s]: sample failed", server.key, exc_info=False)


async def _sample_server(server: GameServer, drv: GameDriver) -> None:
    state = await docker.state(server.container_name)
    running = bool(state.get("running"))
    cpu, mem, mem_pct, players = 0.0, 0, None, 0
    # Exception to the "skip non-running servers" pattern: stopped servers deliberately get a
    # running=False row, so downtime stays visible in the history graph.
    if running:
        try:
            st = await docker.stats(server.container_name)
            cpu = float(st.get("cpu_percent") or 0.0)
            mem = int(st.get("mem_bytes") or 0)
            heap = state.get("max_heap_bytes")
            mem_pct = round(mem / heap * 100, 1) if heap and mem else None
        except Exception:
            # A sample that could not be taken is not a measurement of zero. Committing the
            # initial `cpu=0.0, mem=0` as a `running=True` row would be indistinguishable from
            # an idle container, and the history graph and the overview sparkline would show a
            # drop to 0 % / 0 MB that never happened.
            #
            # A gap in the graph is the correct statement (unlike the player count below, where
            # 0 is the only available fallback). It also matters for `resource_budget`, which
            # falls back to the newest metric row when the live reading fails: an invented 0
            # would understate memory use there and wave a start through into a host OOM.
            log.warning("metrics[%s]: stats cannot be read — NO row is written "
                        "(a gap is more honest than an invented zero)", server.key)
            return
        try:
            players = (await player_count.players(server, drv))[0]["count"]
        except Exception:
            # Not just RconError: the RCON reads can also raise asyncio.TimeoutError/
            # IncompleteReadError (hanging server). Catching less would abort the whole sample
            # and leave a GAP in the history graph instead of a running=True/players=0 row.
            players = 0
    with Session(engine) as session:
        session.add(
            MetricSample(
                running=running, players=players, cpu_percent=cpu,
                mem_bytes=mem, mem_pct=mem_pct, server_id=server.id,
            )
        )
        session.commit()


def prune_metrics() -> None:
    cutoff = utcnow_naive() - dt.timedelta(days=settings.metrics_retention_days)
    try:
        with Session(engine) as session:
            session.execute(sa_delete(MetricSample).where(MetricSample.at < cutoff))
            session.commit()
    except Exception:
        log.warning("metrics: prune failed", exc_info=False)
