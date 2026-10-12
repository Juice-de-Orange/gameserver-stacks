"""Player stats + account linking."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlmodel import Session, select

from ..auth.deps import get_current_user, require_admin, require_flag
from ..db import get_session
from ..errors import err
from ..models import PlayerEvent, PlayerLink, PlayerSession, User
from ..models.server import GameServer, GameType
from ..services import achievements, servers as servers_registry, stats_calc

router = APIRouter(tags=["stats"])

_stats = require_flag("can_view_player_stats")

# Derived from the enum, not copied. A hand-written list is a place where a new game gets
# forgotten: the scope would silently fall back to "pz" and account linking would assign the
# new server's players to the PZ servers.
_GAME_TYPES = {g.value: g for g in GameType}


def _clamp(limit: int, hi: int = 1000) -> int:
    """Clamp limit to [1, hi] — a negative value would otherwise cause a 500 on Postgres."""
    return max(1, min(limit, hi))


def _game_scope(session: Session, game: str, col):
    """WHERE clause for a server_id column that restricts a PlayerLink `game` to its servers.

    Prevents a verified name from seeing the data of a different person with the same name on
    ANOTHER game/server.
    """
    gt = _GAME_TYPES.get(game)
    if gt is None:
        return col.in_([-1])  # unknown game → match nothing
    ids = [s.id for s in session.exec(select(GameServer).where(GameServer.game_type == gt)).all() if s.id is not None]
    if not ids:
        return col.in_([-1])  # game without a registered server → match nothing
    return col.in_(ids)


def _server_scope(session: Session, server_key: str | None, col):
    """Optional server scope for a server_id column: `server_key` set → only that server's rows
    (via `scope_clause`); None → no filter (deliberate cross-game view). Unknown key → 404.
    Prevents players with the same name on different servers/games from being mixed up in
    leaderboards/feeds/summary."""
    if not server_key:
        return None
    srv = session.exec(select(GameServer).where(GameServer.key == server_key)).first()
    if srv is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            detail=err("server.not_found", "Server '{key}' not found", key=server_key),
        )
    return servers_registry.scope_clause(col, srv)


def _scoped(query, scope):
    """`query.where(scope)` only if a scope is active (None = no filter)."""
    return query.where(scope) if scope is not None else query


# ---------- Stats ----------
@router.get("/api/stats/online")
def online(server: str | None = None, session: Session = Depends(get_session), _: User = Depends(_stats)):
    sc = _server_scope(session, server, PlayerSession.server_id)
    q = _scoped(select(PlayerSession).where(PlayerSession.left_at == None), sc)  # noqa: E711
    sessions = session.exec(q).all()
    return [{"username": s.username, "since": s.joined_at} for s in sessions]


@router.get("/api/stats/sessions")
def sessions(
    server: str | None = None, limit: int = 100,
    session: Session = Depends(get_session), _: User = Depends(_stats),
):
    sc = _server_scope(session, server, PlayerSession.server_id)
    rows = session.exec(
        _scoped(select(PlayerSession), sc).order_by(PlayerSession.id.desc()).limit(_clamp(limit))
    ).all()
    return [
        {
            "username": s.username,
            "joined_at": s.joined_at,
            "left_at": s.left_at,
            "duration_seconds": s.duration_seconds,
        }
        for s in rows
    ]


@router.get("/api/stats/summary")
def summary(server: str | None = None, session: Session = Depends(get_session), _: User = Depends(_stats)):
    sc = _server_scope(session, server, PlayerSession.server_id)
    rows = session.exec(_scoped(select(PlayerSession), sc)).all()
    return stats_calc.summarize(rows)


@router.get("/api/stats/player/{name}")
def player_detail(
    name: str, server: str | None = None,
    session: Session = Depends(get_session), _: User = Depends(_stats),
):
    s_sc = _server_scope(session, server, PlayerSession.server_id)
    e_sc = _server_scope(session, server, PlayerEvent.server_id)
    rows = session.exec(
        _scoped(select(PlayerSession).where(PlayerSession.username == name), s_sc)
    ).all()
    agg = stats_calc.summarize(rows)
    recent = session.exec(
        _scoped(select(PlayerSession).where(PlayerSession.username == name), s_sc)
        .order_by(PlayerSession.id.desc()).limit(50)
    ).all()
    events = session.exec(
        _scoped(select(PlayerEvent).where(PlayerEvent.username == name), e_sc)
        .order_by(PlayerEvent.id.desc()).limit(50)
    ).all()
    all_events = session.exec(
        _scoped(select(PlayerEvent).where(PlayerEvent.username == name), e_sc)
    ).all()
    kills = sum(1 for e in all_events if e.kind == "kill")
    deaths = sum(1 for e in all_events if e.kind == "death")
    death_times = [e.at for e in all_events if e.kind == "death"]
    return {
        "summary": agg[0] if agg else None,
        "heatmap": stats_calc.heatmap(rows, name),
        "achievements": achievements.for_player(agg[0] if agg else None, kills, deaths, death_times),
        "sessions": [
            {
                "joined_at": s.joined_at,
                "left_at": s.left_at,
                "duration_seconds": s.duration_seconds,
            }
            for s in recent
        ],
        "events": [{"kind": e.kind, "at": e.at, "detail": e.detail} for e in events],
    }


@router.get("/api/stats/heatmap")
def heatmap(
    name: str | None = None, server: str | None = None,
    session: Session = Depends(get_session), _: User = Depends(_stats),
):
    sc = _server_scope(session, server, PlayerSession.server_id)
    rows = session.exec(_scoped(select(PlayerSession), sc)).all()
    return stats_calc.heatmap(rows, name)


@router.get("/api/stats/events")
def events(
    name: str | None = None,
    server: str | None = None,
    limit: int = 100,
    session: Session = Depends(get_session),
    _: User = Depends(_stats),
):
    sc = _server_scope(session, server, PlayerEvent.server_id)
    query = _scoped(select(PlayerEvent), sc).order_by(PlayerEvent.id.desc()).limit(_clamp(limit))
    if name:
        query = query.where(PlayerEvent.username == name)
    rows = session.exec(query).all()
    return [{"username": e.username, "kind": e.kind, "at": e.at, "detail": e.detail} for e in rows]


@router.get("/api/stats/deaths")
def deaths_map(
    server: str | None = None,
    limit: int = 500,
    session: Session = Depends(get_session),
    _: User = Depends(_stats),
):
    """Death locations with world coordinates — the data behind the map.

    The coordinates come from `data/Logs/*_user.txt` (`user <name> died at (x,y,z) (non pvp)`).
    Events without coordinates (other games, old data) are left out instead of being placed at
    (0,0) — a wrong point is worse than no point.
    """
    sc = _server_scope(session, server, PlayerEvent.server_id)
    rows = session.exec(
        _scoped(
            select(PlayerEvent).where(PlayerEvent.kind == "death", PlayerEvent.x != None),  # noqa: E711
            sc,
        ).order_by(PlayerEvent.at.desc()).limit(_clamp(limit))
    ).all()
    return {
        "deaths": [
            {"username": e.username, "at": e.at, "x": e.x, "y": e.y, "z": e.z,
             "pvp": e.detail == "PvP"}
            for e in rows
        ],
        "total": len(rows),
    }


@router.get("/api/stats/leaderboards")
def leaderboards(server: str | None = None, session: Session = Depends(get_session), _: User = Depends(_stats)):
    s_sc = _server_scope(session, server, PlayerSession.server_id)
    e_sc = _server_scope(session, server, PlayerEvent.server_id)
    rows = session.exec(_scoped(select(PlayerSession), s_sc)).all()
    # Merge names of the same game account ID (assignment is primarily by ID).
    alias = stats_calc.canonical_names(rows)
    summ = stats_calc.summarize(rows, alias=alias)
    deaths: dict[str, int] = {}
    for e in session.exec(_scoped(select(PlayerEvent).where(PlayerEvent.kind == "death"), e_sc)).all():
        name = alias.get(e.username, e.username)
        deaths[name] = deaths.get(name, 0) + 1
    return {
        "playtime": [{"username": s["username"], "value": s["total_seconds"]} for s in summ[:10]],
        "longest_session": [
            {"username": s["username"], "value": s["longest_session"]}
            for s in sorted(summ, key=lambda x: -x["longest_session"])[:10]
        ],
        "active_days": [
            {"username": s["username"], "value": s["days_active"]}
            for s in sorted(summ, key=lambda x: -x["days_active"])[:10]
        ],
        "deaths": [
            {"username": u, "value": c} for u, c in sorted(deaths.items(), key=lambda x: -x[1])[:10]
        ],
    }


@router.get("/api/stats/feed")
def feed(
    server: str | None = None, limit: int = 50,
    session: Session = Depends(get_session), _: User = Depends(_stats),
):
    sc = _server_scope(session, server, PlayerEvent.server_id)
    rows = session.exec(
        _scoped(select(PlayerEvent), sc).order_by(PlayerEvent.id.desc()).limit(_clamp(limit))
    ).all()
    return [{"username": e.username, "kind": e.kind, "at": e.at, "detail": e.detail} for e in rows]


# ---------- player portal: own stats (no full stats permission needed) ----------
@router.get("/api/me/stats")
def my_stats(session: Session = Depends(get_session), user: User = Depends(get_current_user)):
    """Own stats for linked players — filtered to the confirmed in-game names."""
    links = [
        link for link in session.exec(
            select(PlayerLink).where(PlayerLink.user_id == user.id, PlayerLink.verified == True)  # noqa: E712
        ).all()
        if link.ingame_username
    ]
    if not links:
        return {"linked": False, "players": []}
    players = []
    for link in links:
        name = link.ingame_username
        # Restrict to the servers of the linked game — otherwise a namesake on another server
        # (e.g. PZ "Steve" vs. MC "Steve") would expose someone else's player data.
        s_scope = _game_scope(session, link.game, PlayerSession.server_id)
        e_scope = _game_scope(session, link.game, PlayerEvent.server_id)
        rows = session.exec(select(PlayerSession).where(PlayerSession.username == name, s_scope)).all()
        agg = stats_calc.summarize(rows)
        events = session.exec(
            select(PlayerEvent).where(PlayerEvent.username == name, e_scope)
            .order_by(PlayerEvent.id.desc()).limit(50)
        ).all()
        all_events = session.exec(
            select(PlayerEvent).where(PlayerEvent.username == name, e_scope)
        ).all()
        kills = sum(1 for e in all_events if e.kind == "kill")
        deaths = sum(1 for e in all_events if e.kind == "death")
        death_times = [e.at for e in all_events if e.kind == "death"]
        players.append(
            {
                "name": name,
                "summary": agg[0] if agg else None,
                "heatmap": stats_calc.heatmap(rows, name),
                "achievements": achievements.for_player(agg[0] if agg else None, kills, deaths, death_times),
                "events": [{"kind": e.kind, "at": e.at, "detail": e.detail} for e in events],
            }
        )
    return {"linked": True, "players": players}


# ---------- account linking (multi-game: game/external_id) ----------
_VALID_GAMES = frozenset(_GAME_TYPES)


class ClaimIn(BaseModel):
    ingame_username: str
    game: str = "pz"                 # one of the GameType values; unknown → falls back to "pz"
    external_id: str = ""            # SteamID / MC UUID (optional)


@router.get("/api/links/me")
def my_links(session: Session = Depends(get_session), user: User = Depends(get_current_user)):
    return session.exec(select(PlayerLink).where(PlayerLink.user_id == user.id)).all()


@router.post("/api/links/claim")
def claim(body: ClaimIn, session: Session = Depends(get_session), user: User = Depends(get_current_user)):
    game = body.game if body.game in _VALID_GAMES else "pz"
    link = PlayerLink(
        user_id=user.id, ingame_username=body.ingame_username, verified=False,
        game=game, external_id=body.external_id or "",
    )
    session.add(link)
    session.commit()
    session.refresh(link)
    return link


def _conflict(session: Session, link: PlayerLink) -> PlayerLink | None:
    """A CONFIRMED claim by ANOTHER account on the same name in the same game."""
    return session.exec(
        select(PlayerLink).where(
            PlayerLink.ingame_username == link.ingame_username,
            PlayerLink.game == link.game,
            PlayerLink.verified == True,      # noqa: E712 — SQLAlchemy needs the comparison
            PlayerLink.user_id != link.user_id,
        )
    ).first()


@router.get("/api/links")
def all_links(session: Session = Depends(get_session), _: User = Depends(require_admin)):
    """All claims — WITH the details needed to decide on them.

    The approval here is the ONLY control in the whole flow: `claim` accepts any name and
    `verify` only sets a flag. A wrong approval opens `GET /api/me/stats` — session history,
    activity heatmap, achievements and the latest events with timestamps — to someone else's
    account, reachable without any permission.

    Hence the display name, the game in plain text and above all a hint when the same name is
    already confirmed for another account, rather than just a raw foreign key (`user_id`) and
    the claimed name.
    """
    links = session.exec(select(PlayerLink)).all()
    names = {
        u.id: {"display_name": u.display_name, "username": u.username}
        for u in session.exec(select(User)).all()
    }
    out = []
    for link in links:
        entry = link.model_dump()
        entry["user"] = names.get(link.user_id)
        other = None if link.verified else _conflict(session, link)
        entry["conflict"] = (
            None if other is None
            else {"user_id": other.user_id, "user": names.get(other.user_id)}
        )
        out.append(entry)
    return out


@router.post("/api/links/{link_id}/verify")
def verify_link(link_id: int, session: Session = Depends(get_session), _: User = Depends(require_admin)):
    link = session.get(PlayerLink, link_id)
    if link is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=err("link.not_found", "Link not found"))
    # Fail closed: if the same name in the same game is already CONFIRMED for another account,
    # one of the two assignments is wrong — and the consequence of a wrong one is insight into
    # someone else's playtime, heatmap and death history. That must not be decided by a click
    # the UI offers as harmless. It is resolved by deleting the old claim (DELETE below).
    if (other := _conflict(session, link)) is not None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail=err(
                "link.conflict",
                "“{username}” is already assigned to account #{user_id} in the same game. "
                "Delete the old claim first.",
                username=link.ingame_username, user_id=other.user_id,
            ),
        )
    link.verified = True
    session.add(link)
    session.commit()
    session.refresh(link)
    return link


@router.delete("/api/links/{link_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_link(link_id: int, session: Session = Depends(get_session), _: User = Depends(require_admin)):
    """Remove a claim — rejected, wrongly assigned or created with the wrong game.

    Without this the conflict check above would be a dead end: the admin would see that
    something is wrong and could do nothing about it. It also covers old entries that were
    created as `game="pz"` regardless of the actual game.
    """
    link = session.get(PlayerLink, link_id)
    if link is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=err("link.not_found", "Link not found"))
    session.delete(link)
    session.commit()
