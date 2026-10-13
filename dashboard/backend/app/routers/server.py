"""Server status helpers (parametrised by server) — NO routes.

`status_payload`/`overview_payload`/`perform_action`/`_bucket` are the core helpers that the
scoped router `routers/servers.py` reuses per registry row. `_parse_players` is additionally
imported by `routers/join.py`.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import re
import shutil
from math import ceil

from fastapi import HTTPException, status
from sqlalchemy import func
from sqlmodel import Session, select

from ..errors import err
from ..models import ServerIncident, Ticket, TicketStatus, User, UserStatus
from ..models.server import GameServer, GameType
from ..models.player import utcnow_naive
from ..scheduler import scheduler
from ..services import lifecycle
from ..services import mod_check, player_count
from ..services import ports
from ..services import resource_budget as budget_svc
from ..services.mods import state as mod_state
from ..services import restarts
from ..services import servers as servers_registry
from ..services import watchdog
from ..services.audit import record
from ..services.backup import list_backups
from ..services.docker_client import docker, missing_network
from ..services.drivers import get_driver
from ..services.fleet import connect_of
from ..services.rcon import RconError


def _uptime_seconds(started_at: str | None) -> int | None:
    """Docker StartedAt (ISO, possibly nanoseconds + 'Z') → seconds since start."""
    if not started_at:
        return None
    s = re.sub(r"(\.\d{6})\d+", r"\1", started_at.replace("Z", "+00:00"))
    try:
        started = dt.datetime.fromisoformat(s)
    except ValueError:
        return None
    if started.tzinfo is None:
        started = started.replace(tzinfo=dt.timezone.utc)
    return max(int((dt.datetime.now(dt.timezone.utc) - started).total_seconds()), 0)


def _next_backup() -> str | None:
    """Next scheduled backup run (from the APScheduler)."""
    try:
        job = scheduler.get_job("weekly_backup")
        return job.next_run_time.isoformat() if job and job.next_run_time else None
    except Exception:
        return None


def _bucket(rows: list, target: int) -> list[dict]:
    """Condense metric samples to ~target points (averages per bucket)."""
    if not rows:
        return []

    def pt(at, players, cpu, mem, running) -> dict:
        return {"t": at.isoformat(), "players": players, "cpu": cpu, "mem_pct": mem, "running": running}

    if len(rows) <= target:
        return [pt(r.at, r.players, r.cpu_percent, r.mem_pct, r.running) for r in rows]
    size = ceil(len(rows) / target)
    out: list[dict] = []
    for i in range(0, len(rows), size):
        chunk = rows[i : i + size]
        n = len(chunk)
        out.append(
            pt(
                chunk[n // 2].at,
                round(sum(c.players for c in chunk) / n, 1),
                round(sum(c.cpu_percent for c in chunk) / n, 1),
                round(sum((c.mem_pct or 0) for c in chunk) / n, 1),
                any(c.running for c in chunk),
            )
        )
    return out


# What `/status` and `/overview` tell about the container. A list of what may go out, not of what
# must stay in: `docker.state()` also returns the container's FULL environment (RCON_PASSWORD,
# PZ's ADMINPASSWORD, Valheim's SERVER_PASS), and both routes are open to every user with
# `can_view_status` — every member by default, polled by the status strip.
_PUBLIC_CONTAINER = ("exists", "running", "restarting", "restarts", "status", "started_at",
                     "finished_at", "health")


def _public_container(state: dict) -> dict:
    return {k: state[k] for k in _PUBLIC_CONTAINER if k in state}


async def status_payload(server: GameServer) -> dict:
    """Container state + stats + online players (for the status bar)."""
    state = await docker.state(server.container_name)
    stats = await docker.stats(server.container_name) if state.get("running") else {}
    players = None
    if state.get("running"):
        try:
            # Go through the driver (PZ "players", MC "list", VS "list clients") — a raw PZ
            # command via console() would always yield 0 players for MC/VS.
            players, _ = await player_count.players(server, get_driver(server))
        except (RconError, NotImplementedError):
            players = None
    return {"container": _public_container(state), "stats": stats, "players": players}


async def overview_payload(server: GameServer, session: Session) -> dict:
    """One polled aggregation for the status bar. Every source is guarded individually.

    Player count and mod count are the driver's business and therefore work for every server.
    """
    container = server.container_name
    out: dict = {}

    try:
        state = await docker.state(container)
    except Exception:
        state = {"exists": False, "running": False, "status": "unknown", "health": None}
    out["container"] = _public_container(state)
    running = bool(state.get("running"))
    out["uptime_seconds"] = _uptime_seconds(state.get("started_at")) if running else None

    out["stats"] = {}
    if running:
        try:
            out["stats"] = await docker.stats(container)
            # Heap ceiling (-Xmx) from the container state — shown as information only.
            out["stats"]["mem_max_heap"] = state.get("max_heap_bytes")
            # The container's memory limit (`None` = none): what the container's memory is
            # measured against (`services/fleet.py`, same field).
            out["stats"]["mem_container_limit"] = state.get("memory") or None
        except Exception:
            out["stats"] = {}

    players, rcon_ok, latency = None, False, None
    if running:
        try:
            # Driver abstraction (MC/VS have other player-list commands than PZ) instead of a raw
            # "players"; shared with the other display paths (`services/player_count.py`).
            players, latency = await player_count.players(server, get_driver(server))
            rcon_ok = True
        except (RconError, NotImplementedError):
            rcon_ok = False
    out["players"] = players
    out["rcon"] = {"reachable": rcon_ok, "latency_ms": latency}

    # Through the driver for ALL servers, so that a second server of the same game gets its
    # own values instead of a "—".
    try:
        stats = get_driver(server).overview_stats(server)
    except Exception:
        stats = {}
    out["max_players"] = stats.get("max_players")
    out["mods_count"] = stats.get("mods_count")
    # Where players connect — the same source as the fleet and the join page.
    out["connect"] = connect_of(server)
    # Mod health from the cache (no live check here) → update hint on the overview page.
    # Server-scoped: the cache is kept per server key.
    out["mod_health"] = None
    if server.game_type == GameType.pz:
        try:
            rep = mod_check.cached(server.key)
            if rep:
                updates = sum(1 for it in rep.get("items", []) if "update_available" in it.get("status", []))
                s = rep.get("summary", {})
                out["mod_health"] = {
                    "updates": updates,
                    "errors": s.get("errors", 0),
                    "warnings": s.get("warnings", 0),
                    "ok": s.get("ok", True),
                }
        except Exception:
            out["mod_health"] = None

    try:
        spec = get_driver(server).backup_spec(server)
        backups = await asyncio.to_thread(list_backups, data_dir=spec.data_dir)  # dir listing/stat → thread
        out["last_backup"] = backups[0] if backups else None
    except Exception:
        out["last_backup"] = None
    # The weekly run backs up EVERY enabled server, so the date applies to all of them.
    out["next_backup"] = _next_backup()
    # Always server-scoped — a global next_restart() would show the earliest restart of ALL servers.
    out["next_restart"] = restarts.next_restart(server_id=server.id)

    try:
        du = await asyncio.to_thread(shutil.disk_usage, server.data_dir)
        out["disk"] = {
            "free": du.free,
            "total": du.total,
            "used": du.used,
            "percent": round(du.used / du.total * 100, 1) if du.total else None,
        }
    except Exception:
        out["disk"] = None

    try:
        out["pending_registrations"] = session.exec(
            select(func.count()).select_from(User).where(User.status == UserStatus.pending)
        ).one()
        out["open_tickets"] = session.exec(
            select(func.count()).select_from(Ticket).where(Ticket.status == TicketStatus.open)
        ).one()
    except Exception:
        out["pending_registrations"] = None
        out["open_tickets"] = None

    # Latest watchdog incident (only if < 24h old) — for the banner on the overview page. Scoped by server_id.
    try:
        inc = session.exec(
            select(ServerIncident)
            .where(servers_registry.scope_clause(ServerIncident.server_id, server))
            .order_by(ServerIncident.id.desc())
            .limit(1)
        ).first()
        out["last_incident"] = (
            {"at": inc.at, "kind": inc.kind, "detail": inc.detail}
            if inc and (utcnow_naive() - inc.at) < dt.timedelta(hours=24)
            else None
        )
    except Exception:
        out["last_incident"] = None

    return out


async def perform_action(
    server: GameServer, action: str, session: Session, user: User, *, force: bool = False
) -> dict:
    """Apply start/stop/restart to a server (Docker action + audit).

    For `start` the shared RAM budget is checked first (`can_start`): if the server does not fit,
    the start is refused with **HTTP 409** + deficit + stop candidates (the frontend turns that
    into "free up & start"). `force=True` (admin only, enforced by the router) overrides the
    block and additionally writes `server.start.forced` to the audit log. `stop`/`restart` are
    never blocked by the budget (restarting a running server is net zero; `can_start` excludes
    the server itself via exclude_id, `total_ram_mb=0` means "advisory only" → always ok).
    """
    if action not in {"start", "stop", "restart"}:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=err("server.action_invalid", "Invalid action"))
    forced_over_budget = False
    if action == "start":
        ok, deficit, candidates = await budget_svc.can_start(server)
        if not ok:
            if not force:
                raise HTTPException(
                    status.HTTP_409_CONFLICT,
                    detail={
                        **err(
                            "server.start_over_budget",
                            "Starting would exceed the shared RAM budget by {deficit} MB.",
                            deficit=deficit,
                        ),
                        "deficit_mb": deficit,
                        "stop_candidates": candidates,
                    },
                )
            forced_over_budget = True
    # Mod gate: a configuration with known crash causes must not be booted in the first place.
    # A running server is better than one that dies when someone joins — which is why `restart`
    # is refused as well. `stop` is always allowed.
    if action in {"start", "restart"}:
        blockers = await mod_state.guard_boot(server)
        if blockers:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                detail={
                    "message": mod_state.guard_message(blockers),
                    "blockers": [vars(b) for b in blockers],
                },
            )
    # A host port held by a running container: a clear answer instead of Docker's "port is
    # already allocated" (`services/ports.py`). Best effort — if Docker cannot be asked, the
    # start itself reports it.
    if action == "start":
        await ports.check_containers(server, strict=False)
    # Stop/restart go through `lifecycle` so that the game is shut down the way it needs
    # (announcement → save → quit) instead of being killed after the grace period.
    try:
        if action == "stop":
            # Time budget, because this runs inside an HTTP request and a proxy/CDN in front may
            # cut it off (commonly after ~100 s). A hanging game server needs the full grace
            # period (120 s); after 75 s we return and the stop finishes in the background,
            # instead of the browser seeing a gateway timeout and the user believing nothing
            # happened.
            await lifecycle.stop_server(
                server, announce_text="The server is shutting down now…", wait_timeout=75.0
            )
        elif action == "restart":
            # Restart deliberately waits in full: the start may only run once the server is
            # really down. With a HANGING server that can exceed the proxy timeout — the
            # browser then reports an error although the restart continues. The case is rare
            # (that is what the watchdog is for) and the alternative would be a start into the
            # unknown.
            await lifecycle.restart_server(server, announce_text="The server is restarting now…")
        else:  # only "start" is left (validated above)
            # Through `lifecycle` so that the start runs under the same lock as stop and
            # restart. Without it a click on "start" could run into a stop still in progress:
            # `docker start` clears the "manually stopped" mark, and the stop's subsequent
            # `quit` then kills the freshly booted server — reported as `{"ok": true}`. The
            # idle auto-stop holds the lock for up to 120 s with PZ.
            await lifecycle.start_server(server)
    except Exception as e:
        if (in_use := ports.docker_error(server, e)) is not None:
            raise in_use from None
        if missing_network(e) is not None:
            # A stopped server after `docker compose down` + `up -d`: its container still points
            # at the removed game network. The socket proxy grants no `/networks/…`, so it
            # cannot be reattached from here; a new container can (`missing_network`).
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                detail=err(
                    "server.network_missing",
                    "The container of '{key}' is attached to a Docker network that no longer "
                    "exists, usually after docker compose down while the server was stopped. "
                    "Recreate the container (Version page → Recreate) to attach it to the current "
                    "game network; the world data stays as it is.",
                    key=server.key,
                ),
            ) from None
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            detail=err("server.docker_action_failed", "Docker action failed: {error}", error=str(e)),
        )
    if action in {"start", "restart"}:
        watchdog.suppress(server.key, 5)  # do not count the boot as a hang
        # A human pressing start IS activity — the idle clock starts over.
        #
        # Without this `last_active_at` has only one source: players seen. Once the clock is
        # past the timeout, the next hourly round would stop every manually started server
        # again after the grace period. "I'll start it now, we play tonight" would be
        # impossible, permanently: without a real join the clock never gets back below the
        # timeout.
        #
        # Why exactly HERE and nowhere else: `perform_action` is the only start path with a
        # `user`. The scheduled nightly restart, the watchdog auto-heal, the restart after a
        # host reboot and the create-then-swap of a version change all bypass it — they
        # deliberately do NOT count as activity. If they did, the clock of a server with a daily
        # restart would never exceed 24 h and the idle stop would be dead for exactly the
        # servers it was built for. So no "automatic" marker is needed: the presence of `user`
        # is the distinction. That is also why it is the condition below:
        # `perform_action(..., session=None, user=None)` is the call form without a human (see
        # tests/test_budget_enforcement.py) and must not touch the clock.
        if user is not None and session is not None:
            row = session.get(GameServer, server.id)
            if row is not None:
                row.last_active_at = utcnow_naive()
                session.add(row)
                session.commit()
    record(session, user, f"server.{action}", server.container_name, server_id=server.id)
    if forced_over_budget:
        record(session, user, "server.start.forced", server.container_name,
               payload=f"deficit={deficit}MB", server_id=server.id)
    return {"ok": True, "action": action}
