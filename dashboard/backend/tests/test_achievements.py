import datetime as dt

from app.services import achievements


def _summary(total_h=0.0, days=0, longest_h=0.0, first_seen=None):
    return {
        "total_seconds": int(total_h * 3600),
        "days_active": days,
        "longest_session": int(longest_h * 3600),
        "first_seen": first_seen,
    }


def test_playtime_and_kill_tiers():
    s = _summary(total_h=60, days=10, longest_h=5, first_seen=dt.datetime(2026, 1, 1))
    ach = {a["key"]: a for a in achievements.for_player(s, kills=300, deaths=2, death_times=[])}
    assert ach["playtime_10"]["achieved"] is True
    assert ach["playtime_50"]["achieved"] is True
    assert ach["playtime_100"]["achieved"] is False
    assert 0 < ach["playtime_100"]["progress"] < 1
    assert ach["slayer_250"]["achieved"] is True
    assert ach["slayer_1000"]["achieved"] is False
    assert ach["doomed_1"]["achieved"] is True


def test_no_summary_returns_empty():
    assert achievements.for_player(None, 0, 0, []) == []


def test_longest_survival_days():
    base = dt.datetime(2026, 1, 1)
    deaths = [base + dt.timedelta(days=5), base + dt.timedelta(days=7)]
    d = achievements._longest_survival_days(base, deaths, base + dt.timedelta(days=8))
    assert d == 5.0  # longest gap first_seen → first death
