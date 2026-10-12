"""Derived player stats — computed purely from player_sessions, no extra data source.

Naive UTC throughout (consistent with the column convention in the poller).
"""
from __future__ import annotations

import datetime as dt
from collections.abc import Iterable

from ..models import PlayerSession
from ..models.player import utcnow_naive


def _session_seconds(s: PlayerSession, now: dt.datetime) -> int:
    if s.duration_seconds is not None:
        return max(s.duration_seconds, 0)
    return max(int((now - s.joined_at).total_seconds()), 0)


def canonical_names(sessions: Iterable[PlayerSession]) -> dict[str, str]:
    """Map all names of the same game account ID to the most recently used name.

    Players are identified primarily by their ID, not by name — names can change and are
    ambiguous. The ID in `player_sessions.steam_id` comes from the join log line.

    Sessions without an ID stay under their own name — no guessing.
    """
    newest: dict[str, tuple] = {}
    for s in sessions:
        if not s.steam_id:
            continue
        seen = s.left_at or s.joined_at
        before = newest.get(s.steam_id)
        if before is None or seen > before[0]:
            newest[s.steam_id] = (seen, s.username)
    return {s.username: newest[s.steam_id][1] for s in sessions if s.steam_id in newest}


def summarize(sessions: Iterable[PlayerSession], alias: dict[str, str] | None = None) -> list[dict]:
    """Per player: playtime, sessions, longest/average session, first/last seen, active days.

    `alias` (from `canonical_names`) merges names that belong to the same game account ID —
    without it a name change would count as a new person and the leaderboard would list both.
    """
    now = utcnow_naive()
    agg: dict[str, dict] = {}
    days: dict[str, set] = {}
    alias = alias or {}
    for s in sessions:
        name = alias.get(s.username, s.username)
        secs = _session_seconds(s, now)
        a = agg.setdefault(
            name,
            {
                "username": name,
                "total_seconds": 0,
                "sessions": 0,
                "longest_session": 0,
                "avg_session": 0,
                "first_seen": None,
                "last_seen": None,
                "days_active": 0,
                "online": False,
                "current_online_seconds": None,
            },
        )
        days.setdefault(name, set()).add(s.joined_at.date())
        a["total_seconds"] += secs
        a["sessions"] += 1
        a["longest_session"] = max(a["longest_session"], secs)
        if a["first_seen"] is None or s.joined_at < a["first_seen"]:
            a["first_seen"] = s.joined_at
        last = s.left_at or s.joined_at
        if a["last_seen"] is None or last > a["last_seen"]:
            a["last_seen"] = last
        if s.left_at is None:
            a["online"] = True
            a["current_online_seconds"] = secs
    for name, a in agg.items():
        a["days_active"] = len(days.get(name, ()))
        a["avg_session"] = round(a["total_seconds"] / a["sessions"]) if a["sessions"] else 0
    return sorted(agg.values(), key=lambda x: -x["total_seconds"])


def heatmap(sessions: Iterable[PlayerSession], name: str | None = None) -> dict:
    """7×24 grid (weekday 0=Mon × hour) of presence seconds, weighted by duration."""
    grid = [[0] * 24 for _ in range(7)]
    now = utcnow_naive()
    days: set = set()
    for s in sessions:
        if name and s.username != name:
            continue
        start = s.joined_at
        end = s.left_at or now
        if end <= start:
            continue
        days.add(start.date())
        cur = start
        guard = 0
        while cur < end and guard < 24 * 400:  # safety cap against an endless loop
            next_hour = cur.replace(minute=0, second=0, microsecond=0) + dt.timedelta(hours=1)
            seg_end = min(end, next_hour)
            grid[cur.weekday()][cur.hour] += int((seg_end - cur).total_seconds())
            cur = seg_end
            guard += 1
    peak = None
    peak_val = 0
    for wd in range(7):
        for h in range(24):
            if grid[wd][h] > peak_val:
                peak_val = grid[wd][h]
                peak = {"weekday": wd, "hour": h, "seconds": grid[wd][h]}
    return {"grid": grid, "peak": peak, "total_distinct_days": len(days)}
