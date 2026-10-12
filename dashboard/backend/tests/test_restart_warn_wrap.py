"""A warning broadcast that crosses midnight must fire on the previous day.

Restart Mon 00:05, warning 10 min before = 23:55. Without the day shift the warning job would be
scheduled for Mon 23:55 — almost 24 h AFTER the restart. Correct is Sun 23:55 (the day before).
"""
from sqlmodel import Session

from app.models import RestartSchedule
from app.models.server import GameServer, GameType


def _capture_jobs(monkeypatch):
    from app.services import restarts

    jobs = []

    def _add_job(func, trigger, **kw):
        jobs.append(kw)

    monkeypatch.setattr(restarts.scheduler, "add_job", _add_job)
    monkeypatch.setattr(restarts, "_clear_jobs", lambda: None)
    # `reload_jobs` asks whether the game can send a broadcast at all — otherwise it creates no
    # warning job. This is about the midnight arithmetic, so use a server that can (Project Zomboid).
    target = GameServer(key="pz-1", game_type=GameType.pz, container_name="pz-1", data_dir="/x")
    target.id = 1
    monkeypatch.setattr(restarts.servers_registry, "resolve_server", lambda sid: target)
    return restarts, jobs


def _warn_jobs(jobs):
    return [j for j in jobs if "_warn_" in j["id"]]


def _restart_job(jobs):
    return next(j for j in jobs if "_warn_" not in j["id"])


def test_warn_wraps_to_previous_day(monkeypatch, mem_engine):
    restarts, jobs = _capture_jobs(monkeypatch)
    monkeypatch.setattr(restarts, "engine", mem_engine)
    with Session(mem_engine) as s:
        s.add(RestartSchedule(time_hhmm="00:05", days="mon", warn_minutes="10", enabled=True))
        s.commit()

    restarts.reload_jobs()

    assert _restart_job(jobs)["day_of_week"] == "mon"  # restart on Monday
    warn = _warn_jobs(jobs)
    assert len(warn) == 1
    w = warn[0]
    assert (w["hour"], w["minute"]) == (23, 55)  # 10 min before 00:05
    assert w["day_of_week"] == "sun"  # the day before — NOT Monday


def test_warn_no_wrap_keeps_same_day(monkeypatch, mem_engine):
    restarts, jobs = _capture_jobs(monkeypatch)
    monkeypatch.setattr(restarts, "engine", mem_engine)
    with Session(mem_engine) as s:
        s.add(RestartSchedule(time_hhmm="05:00", days="wed", warn_minutes="10", enabled=True))
        s.commit()

    restarts.reload_jobs()

    w = _warn_jobs(jobs)[0]
    assert (w["hour"], w["minute"]) == (4, 50)
    assert w["day_of_week"] == "wed"  # no wrap → same day


def test_warn_wrap_multiple_days(monkeypatch, mem_engine):
    restarts, jobs = _capture_jobs(monkeypatch)
    monkeypatch.setattr(restarts, "engine", mem_engine)
    with Session(mem_engine) as s:
        s.add(RestartSchedule(time_hhmm="00:30", days="mon,thu", warn_minutes="45", enabled=True))
        s.commit()

    restarts.reload_jobs()

    w = _warn_jobs(jobs)[0]
    assert (w["hour"], w["minute"]) == (23, 45)  # 45 min before 00:30 = 23:45 the day before
    assert w["day_of_week"] == "sun,wed"  # the day before each


def test_warn_daily_no_shift(monkeypatch, mem_engine):
    restarts, jobs = _capture_jobs(monkeypatch)
    monkeypatch.setattr(restarts, "engine", mem_engine)
    with Session(mem_engine) as s:
        s.add(RestartSchedule(time_hhmm="00:05", days="daily", warn_minutes="10", enabled=True))
        s.commit()

    restarts.reload_jobs()

    w = _warn_jobs(jobs)[0]
    assert (w["hour"], w["minute"]) == (23, 55)
    assert w["day_of_week"] == "*"  # daily stays daily (fires every day at 23:55, acceptable)
