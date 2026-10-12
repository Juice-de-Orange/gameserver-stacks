import datetime as dt

from app.models import PlayerSession
from app.services import stats_calc


def _sess(name: str, start: dt.datetime, dur: int) -> PlayerSession:
    return PlayerSession(
        username=name, joined_at=start, left_at=start + dt.timedelta(seconds=dur), duration_seconds=dur
    )


def test_summarize_totals():
    base = dt.datetime(2026, 6, 1, 12, 0, 0)
    rows = [
        _sess("a", base, 3600),
        _sess("a", base + dt.timedelta(days=1), 1800),
        _sess("b", base, 600),
    ]
    summ = {s["username"]: s for s in stats_calc.summarize(rows)}
    assert summ["a"]["total_seconds"] == 5400
    assert summ["a"]["sessions"] == 2
    assert summ["a"]["longest_session"] == 3600
    assert summ["a"]["days_active"] == 2
    assert summ["b"]["total_seconds"] == 600


def test_heatmap_shape_and_total():
    base = dt.datetime(2026, 6, 1, 12, 0, 0)
    hm = stats_calc.heatmap([_sess("a", base, 3600)], "a")
    assert len(hm["grid"]) == 7
    assert all(len(row) == 24 for row in hm["grid"])
    assert sum(sum(row) for row in hm["grid"]) == 3600
