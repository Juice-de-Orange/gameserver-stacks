"""Cross-server features without loop risk: presence, leaderboard, come-over, announce.

These four are harmless (no chat feedback) and may run unattended; only the chat bridge
(`bridge.py`) depends on the `chat_bridge_enabled` switch. Every cross-server output names
the origin server. Presence/leaderboard are read-only (from `PlayerSession`/`PlayerEvent`);
come-over/announce send server messages via `driver.announce`.
"""
from __future__ import annotations

import time

from sqlmodel import Session, select

from ...models.player import PlayerEvent, PlayerSession, utcnow_naive
from ...models.server import GameServer
from ...services import servers as servers_registry
from ...services.docker_client import docker
from ..drivers.base import sanitize_message
from . import bus

# Process-local come-over cooldown per origin server (single worker).
_come_over_last: dict[str, float] = {}


def _servers_by_id(session: Session) -> dict[int, GameServer]:
    return {s.id: s for s in session.exec(select(GameServer)).all() if s.id is not None}


def presence(session: Session) -> list[dict]:
    """Who is online where: from open `PlayerSession`s, grouped by server (read-only)."""
    if not bus.get_settings(session).presence_enabled:
        return []
    smap = _servers_by_id(session)
    open_rows = session.exec(
        select(PlayerSession).where(PlayerSession.left_at == None)  # noqa: E711 → IS NULL
    ).all()
    grouped: dict[int | None, set[str]] = {}
    for ps in open_rows:
        grouped.setdefault(ps.server_id, set()).add(ps.username)
    out: list[dict] = []
    for sid, names in grouped.items():
        srv = smap.get(sid) if sid is not None else None
        out.append({
            "server_id": sid,
            "server_key": srv.key if srv else None,
            "display_name": srv.display_name if srv else "?",
            "game": srv.game_type.value if srv else None,
            "count": len(names),
            "players": sorted(names),
        })
    out.sort(key=lambda x: (x["display_name"] or ""))
    return out


def leaderboard(session: Session, limit: int = 20) -> list[dict]:
    """Cross-game leaderboard: playtime (from sessions) plus kills/deaths (from events),
    aggregated per player, with a per-game breakdown. Read-only."""
    if not bus.get_settings(session).leaderboard_enabled:
        return []
    now = utcnow_naive()
    smap = _servers_by_id(session)
    playtime: dict[str, float] = {}
    per_game: dict[str, dict[str, float]] = {}
    for ps in session.exec(select(PlayerSession)).all():
        end = ps.left_at or now
        secs = max(0.0, (end - ps.joined_at).total_seconds())
        playtime[ps.username] = playtime.get(ps.username, 0.0) + secs
        srv = smap.get(ps.server_id) if ps.server_id is not None else None
        game = srv.game_type.value if srv else "pz"
        per_game.setdefault(ps.username, {})
        per_game[ps.username][game] = per_game[ps.username].get(game, 0.0) + secs
    kills: dict[str, int] = {}
    deaths: dict[str, int] = {}
    for e in session.exec(select(PlayerEvent)).all():
        if e.kind == "kill":
            kills[e.username] = kills.get(e.username, 0) + 1
        elif e.kind == "death":
            deaths[e.username] = deaths.get(e.username, 0) + 1
    rows = [
        {
            "player": name,
            "playtime_hours": round(secs / 3600, 1),
            "kills": kills.get(name, 0),
            "deaths": deaths.get(name, 0),
            "games": {g: round(s / 3600, 1) for g, s in per_game.get(name, {}).items()},
        }
        for name, secs in playtime.items()
    ]
    rows.sort(key=lambda r: r["playtime_hours"], reverse=True)
    return rows[: max(1, min(limit, 100))]


async def come_over(session: Session, origin_key: str, player: str) -> dict:
    """Send the come-over message ("{player} is playing on {server}") to all OTHER running
    servers.

    Cooldown per origin server (process-local). The origin is always named."""
    settings = bus.get_settings(session)
    if not settings.presence_enabled:
        return {"sent": False, "reason": "presence_disabled"}
    now = time.time()
    last = _come_over_last.get(origin_key, 0.0)
    cooldown = max(0, settings.come_over_cooldown_sec)
    if now - last < cooldown:
        return {"sent": False, "reason": "cooldown", "retry_in": int(cooldown - (now - last))}

    smap = {s.key: s for s in session.exec(select(GameServer)).all()}
    origin = smap.get(origin_key)
    origin_name = origin.display_name if origin else origin_key
    try:
        text = settings.come_over_format.format(player=sanitize_message(player), server=origin_name)
    except Exception:
        text = f"{sanitize_message(player)} is playing on {origin_name} right now – come over!"

    _come_over_last[origin_key] = now  # set the cooldown even on partial failure (no spam)
    sent_to: list[str] = []
    for srv, drv in servers_registry.list_enabled_with_driver():
        if srv.key == origin_key:
            continue
        if "announce" not in drv.capabilities():
            continue  # game without a broadcast verb (e.g. Reforger): do not count as "sent"
        try:
            if not (await docker.state(srv.container_name)).get("running"):
                continue
            await drv.announce(srv, text)
            sent_to.append(srv.key)
        except Exception:
            continue
    return {"sent": True, "servers": sent_to, "message": text}


async def announce_all(session: Session, message: str) -> dict:
    """Shared announcement to all running `announce_target` servers. Output only."""
    settings = bus.get_settings(session)
    if not settings.announcements_enabled:
        return {"sent": [], "reason": "announcements_disabled"}
    message = sanitize_message(message)
    if not message:
        return {"sent": [], "reason": "empty"}
    sent: list[str] = []
    for srv, drv in servers_registry.list_enabled_with_driver():
        if not srv.announce_target:
            continue
        if "announce" not in drv.capabilities():
            continue  # no broadcast verb (Reforger): skip instead of "sending" into the void
        try:
            if not (await docker.state(srv.container_name)).get("running"):
                continue
            await drv.announce(srv, message)
            sent.append(srv.key)
        except Exception:
            continue
    return {"sent": sent}


def reset_state() -> None:
    """Tests only."""
    _come_over_last.clear()
