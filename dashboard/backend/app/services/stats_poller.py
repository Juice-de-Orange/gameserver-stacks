"""Stats poller: periodically queries the player list and maintains player_sessions (presence).

Multi-game fan-out: ONE scheduler job (`max_instances=1`) iterates over all enabled servers.
Reconciliation of open sessions is STRICTLY scoped by `server_id` — otherwise the poll of
server A would close the still-open sessions of server B. `scope_clause` filters to exactly
this server.

**Hybrid of sampling and log lines.** The poll alone knows times only to the poll interval
(±60 s) and no game account ID. The log knows both exactly — but a purely log-driven writer
would fail worse: **one missed line = one session that never closes** and distorts playtime
permanently. So the poll remains the clock and the cleaner; the log line supplies, where
available, the exact time and the ID.
"""
from __future__ import annotations

import datetime as dt
import logging
from typing import TYPE_CHECKING

from sqlmodel import Session, select

from ..db import engine
from ..models import PlayerEvent, PlayerSession
from ..models.player import utcnow_naive
from ..models.server import GameServer  # needed at runtime (session.get), not only for types
from ..services import player_count
from ..services import servers as servers_registry
from ..services.docker_client import docker, start_time as docker_stop_time
from ..services.drivers import VersionSource, version_source
from ..services.rcon import RconError

# How far back a log line may lie and still match a join that was just observed. More generous
# than the poll interval because the event parser runs less often than the stats poller.
LOG_WINDOW = dt.timedelta(minutes=30)

if TYPE_CHECKING:
    from ..services.drivers import GameDriver

log = logging.getLogger("uvicorn.error")


async def poll_once() -> None:
    """One poll round over all enabled servers (isolated per server)."""
    for server, drv in servers_registry.list_enabled_with_driver():
        try:
            await _poll_server(server, drv)
        except Exception:
            log.warning("stats_poller[%s]: round failed", server.key, exc_info=False)


def _latest_event(session, scope, username: str, kind: str, *, since: dt.datetime):
    """Newest `join`/`leave` event of this player since `since` — or None.

    Without a hit the plain poll behaviour applies; the log line is an improvement, not a
    requirement (Minecraft/VS/Reforger currently deliver no such events).
    """
    return session.exec(
        select(PlayerEvent).where(
            PlayerEvent.username == username,
            PlayerEvent.kind == kind,
            PlayerEvent.at >= since,
            scope,
        ).order_by(PlayerEvent.at.desc()).limit(1)
    ).first()


def _close_open(server: GameServer, *, until: dt.datetime) -> None:
    """Close all open sessions of this server at `until`.

    Called when the container is NOT running. That is not an unclear answer but the most
    definite one: nobody can be online any more.
    """
    with Session(engine) as session:
        cond = servers_registry.scope_clause(PlayerSession.server_id, server)
        open_ = session.exec(
            select(PlayerSession).where(PlayerSession.left_at == None, cond)  # noqa: E711
        ).all()
        if not open_:
            return
        for s in open_:
            s.left_at = max(until, s.joined_at)
            s.duration_seconds = max(int((s.left_at - s.joined_at).total_seconds()), 0)
            session.add(s)
        session.commit()
        log.info("stats_poller[%s]: container is down — closed %d open session(s) at %s",
                 server.key, len(open_), until.isoformat(timespec="seconds"))


def _last_end(session, cond, username: str):
    """When this player last ENDED a session — or None."""
    line = session.exec(
        select(PlayerSession)
        .where(PlayerSession.username == username, PlayerSession.left_at != None, cond)  # noqa: E711
        .order_by(PlayerSession.left_at.desc()).limit(1)
    ).first()
    return line.left_at if line else None


def _touch_activity(server: GameServer) -> None:
    """Set `last_active_at` without touching the sessions (player count without names)."""
    with Session(engine) as session:
        row = session.get(GameServer, server.id)
        if row:
            row.last_active_at = utcnow_naive()
            session.add(row)
            session.commit()


async def _poll_server(server: GameServer, drv: GameDriver) -> None:
    state = await docker.state(server.container_name)
    if not state.get("running"):
        # "Container stopped" is the most definite answer there is, not "nothing to do": open
        # sessions must be closed here. There is no second path — `left_at` is set only in this
        # module; `lifecycle`, the scheduled restart, the watchdog autoheal and the world
        # operations do not touch `player_sessions`. Otherwise the player would stay listed as
        # online, and leaderboard and heatmap (`end = ps.left_at or now`) would keep counting
        # playtime for the whole downtime.
        #
        # Sessions are closed at the container's STOP time, not at "now": for a server that
        # was down for three days, that is the difference between a two-hour session and a
        # three-day one.
        _close_open(server, until=docker_stop_time(state.get("finished_at")) or utcnow_naive())
        return
    try:
        response, _ = await player_count.players(server, drv)
    except RconError:
        return
    online = response.get("names") or []

    # An empty name list while players are reported means UNKNOWN, not "nobody here".
    #
    # The contract of `base.GameDriver.players` returns both, and `count` is the reliable one.
    # For Valheim the count comes from A2S_INFO and the names from A2S_PLAYER, which this game
    # does not always fill; Factorio's name list can be truncated.
    #
    # Reading `names == []` as "everyone left" would close every open session, create no new
    # one and leave `last_active_at` unset — playtimes and leaderboard would stay empty without
    # an error anywhere, and nothing would correct it (for Valheim `parse_events` deliberately
    # produces no `leave`).
    number = response.get("count")
    # A PARTIAL name list must not close a session either. When `count` is greater than
    # `len(names)` (truncated Factorio list, empty names filtered out by `ValheimDriver.players`),
    # the loop further down would close every open session whose name is missing from the
    # partial list — the player would be checked out and in again every minute, turning one
    # session into dozens of fragments.
    incomplete = isinstance(number, int) and number > len(online)
    if not online and isinstance(number, int) and number > 0:
        log.debug("stats_poller: %s reports %d players without names — sessions untouched",
                  server.key, number)
        _touch_activity(server)
        return

    now = utcnow_naive()
    with Session(engine) as session:
        cond = servers_registry.scope_clause(PlayerSession.server_id, server)
        open_sessions = session.exec(
            select(PlayerSession).where(PlayerSession.left_at == None, cond)  # noqa: E711
        ).all()
        open_names = {s.username for s in open_sessions}
        e_cond = servers_registry.scope_clause(PlayerEvent.server_id, server)

        for name in online:
            if name not in open_names:
                # The poll only sees "is here now" — not exactly since when (± poll interval).
                # The log knows it to the millisecond and also knows the game account ID.
                #
                # But only if the event belongs to THIS session. The window is 30 minutes wide;
                # a player who leaves and returns within half an hour would otherwise inherit
                # the join of the previous, already closed session, and the break in between
                # would be booked as playtime. The leave path below has the matching guard
                # (`since=s.joined_at` plus `ev.at >= s.joined_at`).
                ev = _latest_event(session, e_cond, name, "join", since=now - LOG_WINDOW)
                if ev is not None and (last := _last_end(session, cond, name)) is not None:
                    if ev.at <= last:
                        ev = None
                session.add(PlayerSession(
                    username=name,
                    joined_at=ev.at if ev else now,
                    steam_id=ev.game_id if ev else "",
                    server_id=server.id,
                ))

        online_set = set(online)
        for s in (() if incomplete else open_sessions):
            if s.username not in online_set:
                ev = _latest_event(session, e_cond, s.username, "leave", since=s.joined_at)
                s.left_at = ev.at if ev and ev.at >= s.joined_at else now
                s.duration_seconds = max(int((s.left_at - s.joined_at).total_seconds()), 0)
                if not s.steam_id and ev:
                    s.steam_id = ev.game_id
                session.add(s)

        # Record activity where it is observed. `last_active_at` drives the idle auto-stop,
        # which only checks hourly: someone who plays a short round and leaves before the next
        # check would otherwise leave no trace, and the server would count as empty for weeks.
        # The poll sees the players every minute anyway.
        #
        # Activity means PLAYER COUNT, not name list. With `if online:` alone the clock would
        # NEVER advance for a game that does not reliably fill its name list (Valheim,
        # Factorio), no matter how much is played — and the first round that happens to see 0
        # players after `default_idle_timeout_minutes` would stop a server in daily use.
        if online or (isinstance(number, int) and number > 0):
            row = session.get(GameServer, server.id)
            if row:
                row.last_active_at = now
                session.add(row)

        session.commit()

    # Record the running game version where the server reports it.
    #
    # For games that fetch themselves at start (e.g. Arma Reforger: the image downloads the
    # game via SteamCMD on every start, so a tag says nothing) this is the only source, and the
    # overview shows exactly this `game_version` field.
    #
    # Deliberately ONLY for games without a version list: for PZ the image tag is the truth,
    # and for Vintage Story a set `VERSION` would move into the env on the next rebuild and
    # could trigger a world migration (reasoning in the VS driver). `game_version` is display
    # only here — `container_env` of the affected drivers does not read it.
    #
    # The condition is `version_source == runtime`, not a game name: this branch is the ONLY
    # place in the backend that writes a detected version to the registry (`change_version`
    # never runs for a game without a version list).
    if version_source(server) is VersionSource.runtime:
        try:
            detected = await drv.detect_version(server)
        except Exception:
            detected = ""
        if detected and detected != server.game_version:
            with Session(engine) as session:
                row = session.get(GameServer, server.id)
                if row and row.game_version != detected:
                    row.game_version = detected
                    session.add(row)
                    session.commit()
