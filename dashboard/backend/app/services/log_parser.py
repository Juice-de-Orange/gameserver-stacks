"""Reads player events (join, leave, death, level-up) and stores them as PlayerEvent.

**The source differs per game.** Project Zomboid writes player events exclusively to files
under `data/Logs/` (`*_user.txt`, `*_PerkLog.txt`), **never** to stdout, so the Docker log
structurally cannot contain them, however correct the patterns are.

`driver.event_log_paths(server)` decides: empty list → Docker log (Minecraft), otherwise these
files are tailed.

Dedup: a bounded section per run; an event is only created if (username, kind, at, server_id)
does not exist yet. That makes the job idempotent and lets it survive restarts and the daily
rollover of the log files without a watermark.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
import os
import re
from typing import TYPE_CHECKING

from sqlmodel import Session, select

from ..db import engine
from ..models import PlayerEvent
from ..services import servers as servers_registry
from ..services.docker_client import docker

if TYPE_CHECKING:
    from ..models.server import GameServer
    from ..services.drivers import GameDriver

log = logging.getLogger("uvicorn.error")

# With timestamps=1 Docker prefixes every line with an RFC3339Nano timestamp.
_TS_RE = re.compile(r"^(?P<ts>\d{4}-\d{2}-\d{2}T[\d:.]+Z)\s?(?P<rest>.*)$")

# How much is read per round. Generous, because the level-up derivation must see two
# consecutive skill snapshots in the same section.
TAIL_BYTES = 400_000


def _parse_ts(raw: str) -> dt.datetime:
    """RFC3339(Nano) → naive UTC (consistent with the column convention)."""
    s = re.sub(r"(\.\d{6})\d+", r"\1", raw.replace("Z", "+00:00"))
    try:
        parsed = dt.datetime.fromisoformat(s)
    except ValueError:
        return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
    return parsed.astimezone(dt.timezone.utc).replace(tzinfo=None)


def _read_tail(path: str, limit: int = TAIL_BYTES) -> str:
    """Read the end of a log file without pulling all of it into memory."""
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            start = max(0, f.tell() - limit)
            f.seek(start)
            raw = f.read()
        text = raw.decode("utf-8", errors="replace")
        return text.split("\n", 1)[1] if start and "\n" in text else text
    except OSError:
        return ""


async def poll_log_events() -> None:
    """Scheduler job (ONE job): parse the tail per enabled server, store new events."""
    for server, drv in servers_registry.list_enabled_with_driver():
        try:
            await _poll_server(server, drv)
        except Exception:
            log.warning("log_parser[%s]: round failed", server.key, exc_info=False)


async def _poll_server(server: GameServer, drv: GameDriver) -> None:
    try:
        state = await docker.state(server.container_name)
        if not state.get("running"):
            return
        # Source per game: PZ reads files (the events are there and ONLY there),
        # Minecraft the Docker log.
        paths = drv.event_log_paths(server)
        if not paths and drv.events_from_files:
            # **An empty list does NOT mean "it is on stdout" here.** For a game whose events
            # exist only in files it means "not found": a permission problem, a missing mount,
            # a renamed directory. Falling back to the Docker log would query the source that
            # structurally contains nothing for this game, find nothing and end the round
            # without a log line, an incident or any trace.
            #
            # **But "not found" is not the same as "not reachable".** PZ only creates
            # `<date>_user.txt` once someone PLAYS. On a running, empty server the file
            # regularly does not exist, and warning about that normal case every 30 seconds
            # would flood the log and push every real message out of the rotation window.
            #
            # So the driver says whether there IS an obstacle. Only that is worth a warning.
            obstacle = drv.event_log_obstacle(server)
            if obstacle:
                log.warning(
                    "log_parser[%s]: no event log files found, and there is a reason "
                    "for it: %s This game does NOT write them to stdout — so there is "
                    "NO fallback to the Docker log.", server.key, obstacle,
                )
            else:
                log.debug(
                    "log_parser[%s]: no event log file — the directory is reachable, "
                    "nobody has played yet.", server.key,
                )
            return
        if paths:
            parts = [await asyncio.to_thread(_read_tail, p) for p in paths]
            raw = "\n".join(p for p in parts if p)
            if not raw.strip():
                # Found but nothing read: `_read_tail` returns "" on `OSError`, so an
                # unreadable file cannot be told apart from an empty one. For a game that
                # writes its events only here, that is worth a message.
                log.info("log_parser[%s]: event log files found, but empty or unreadable "
                         "(%s)", server.key, ", ".join(paths))
        else:
            raw = await docker.logs(server.container_name, tail=1500, timestamps=True)
    except Exception:
        return

    events = drv.parse_events(server, raw)  # the driver encapsulates the game-specific log format
    if not events:
        return

    try:
        with Session(engine) as session:
            # Dedup scoped by server_id: two servers can deliver identical (username, kind, at).
            cond = servers_registry.scope_clause(PlayerEvent.server_id, server)
            for ev in events:
                exists = session.exec(
                    select(PlayerEvent).where(
                        PlayerEvent.username == ev.username,
                        PlayerEvent.kind == ev.kind,
                        PlayerEvent.at == ev.at,
                        cond,
                    )
                ).first()
                if exists:
                    continue
                session.add(PlayerEvent(
                    username=ev.username, kind=ev.kind, at=ev.at, server_id=server.id,
                    detail=ev.detail[:500], game_id=ev.game_id, x=ev.x, y=ev.y, z=ev.z,
                ))
            session.commit()
    except Exception:
        log.warning("log_parser: events could not be saved", exc_info=False)
