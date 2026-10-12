"""Achievements / milestones, derived purely from player_sessions (summarize) + player_events.

No new data source: tiered badges from playtime, active days, longest session, kills, deaths
and the longest survival streak (gap between death events).
"""
from __future__ import annotations

import datetime as dt

from ..models.player import utcnow_naive


def _tiered(key: str, label: str, desc_tmpl: str, value: float, thresholds: list[int], unit: str = "") -> list[dict]:
    out: list[dict] = []
    for t in thresholds:
        out.append(
            {
                "key": f"{key}_{t}",
                "group": label,
                "label": f"{label} {t}{unit}",
                "desc": desc_tmpl.format(t=t),
                "achieved": value >= t,
                "value": value,
                "target": t,
                "progress": min(1.0, round(value / t, 3)) if t else 1.0,
            }
        )
    return out


def _longest_survival_days(first_seen, death_times: list[dt.datetime], now: dt.datetime) -> float:
    """Longest streak without a death (in days): first_seen→1st death, death→death, last death→now."""
    if not first_seen:
        return 0.0
    best = 0.0
    prev = first_seen
    for d in sorted(death_times):
        best = max(best, (d - prev).total_seconds())
        prev = d
    best = max(best, (now - prev).total_seconds())
    return round(best / 86400, 1)


def for_player(summary: dict | None, kills: int, deaths: int, death_times: list[dt.datetime]) -> list[dict]:
    if not summary:
        return []
    playtime_h = round(summary.get("total_seconds", 0) / 3600, 1)
    days_active = summary.get("days_active", 0)
    longest_h = round(summary.get("longest_session", 0) / 3600, 1)
    first_seen = summary.get("first_seen")
    veteran_days = max(0, (utcnow_naive() - first_seen).days) if first_seen else 0
    survive_days = _longest_survival_days(first_seen, death_times, utcnow_naive())

    ach: list[dict] = []
    ach += _tiered("playtime", "Playtime", "{t} hours played", playtime_h, [10, 50, 100, 250], " h")
    ach += _tiered("days", "Active days", "Played on {t} different days", days_active, [7, 30, 100])
    ach += _tiered("marathon", "Marathon", "A single session of {t} h", longest_h, [4, 8, 12], " h")
    ach += _tiered("slayer", "Slayer", "{t} zombie kills", kills, [50, 250, 1000])
    ach += _tiered("survivor", "Survivor", "Survived {t} days in a row", survive_days, [3, 7, 30], " d")
    ach += _tiered("veteran", "Veteran", "Member for {t} days", veteran_days, [30, 90], " d")
    ach += _tiered("doomed", "Doomed", "Died {t}×", deaths, [1, 10, 50])
    return ach
