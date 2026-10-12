"""Scheduled times have a zone, and a missed nightly run is caught up.

Both rest on the same root: the scheduler must know about time.

*The zone must be a decision.* An `AsyncIOScheduler` built without `timezone=` makes APScheduler
take `tzlocal.get_localzone()`, and inside the container no zone is set — so UTC. A nightly
restart entered as "05:00" would then run at 07:00 local time, and because UTC has no daylight
saving time it would shift by an hour twice a year without anyone changing anything.

*And the memory must survive.* All jobs live in the `MemoryJobStore` and are registered anew on
every process start; `add_job` counts from that moment. For the minute in which the process was
not running no job exists that could misfire — `misfire_grace_time` only protects runs that a
RUNNING scheduler picks up late. A deploy on a Sunday at 04:00 would drop the weekly run without
replacement: no archive for any server, no log line, no incident.
"""
from __future__ import annotations

import datetime as dt

import pytest
from apscheduler.schedulers.background import BackgroundScheduler

from app.models.player import utcnow_naive
from app.services import jobs


def test_the_timezone_comes_from_the_setting_not_from_the_environment():
    """`scheduler.timezone is not None` would be no test — APScheduler ALWAYS sets one, it may
    just come from `tzlocal` instead of a decision. So the origin is checked: the zone must be the
    one from the setting."""
    from app.config import settings
    from app.scheduler import scheduler
    assert str(scheduler.timezone) == (settings.scheduler_tz or "UTC")
    # And every trigger inherits it — that is what `add_job` does (`trigger_args.setdefault`).
    job = scheduler.add_job(lambda: None, "cron", hour=5, id="_probe", replace_existing=True)
    try:
        assert str(job.trigger.timezone) == str(scheduler.timezone)
    finally:
        scheduler.remove_job("_probe")


def test_an_unknown_zone_does_not_abort_the_start(monkeypatch):
    """A typo in `.env` must not take all nightly jobs down with it."""
    import app.scheduler as sch
    monkeypatch.setattr(sch.settings, "scheduler_tz", "Europe/Vienna-typo")
    assert str(sch._zone()) == "UTC"


def test_the_zone_is_in_the_response_of_the_schedules_page():
    """The UI can only say it if it is told."""
    import inspect

    from app.routers import schedules
    source = inspect.getsource(schedules.list_schedules_scoped)
    assert '"timezone"' in source


class _FakeScheduler:
    def __init__(self):
        self.scheduled: list[tuple] = []

    def add_job(self, fn, trigger, **kw):
        self.scheduled.append((kw.get("id"), trigger, kw.get("run_date")))


@pytest.fixture
def memory(monkeypatch):
    """Replace the table with a dict — this is about the rule, not about SQL."""
    runs: dict[str, dt.datetime] = {}
    monkeypatch.setattr(jobs, "last_run", lambda jid: runs.get(jid))
    monkeypatch.setattr(jobs, "remember", lambda jid, now=None: runs.__setitem__(jid, now or utcnow_naive()))
    return runs


def test_a_missed_weekly_run_is_caught_up(memory):
    memory["weekly_backup"] = utcnow_naive() - dt.timedelta(days=9)
    s = _FakeScheduler()
    assert jobs.schedule_catchup(s, "weekly_backup", lambda: None, max_gap_h=7 * 24 + 12) is True
    assert [job[0] for job in s.scheduled] == ["weekly_backup__catchup"]
    assert s.scheduled[0][1] == "date", "exactly ONE catch-up run, not a new cadence"


def test_a_run_from_yesterday_is_no_omission(memory):
    """A few hours of delay caused by the restart itself must not trigger anything."""
    memory["weekly_backup"] = utcnow_naive() - dt.timedelta(days=6)
    s = _FakeScheduler()
    assert jobs.schedule_catchup(s, "weekly_backup", lambda: None, max_gap_h=7 * 24 + 12) is False
    assert s.scheduled == []


def test_nothing_is_caught_up_on_the_very_first_start(memory):
    """"Never ran" is UNKNOWN, not "overdue".

    Otherwise the first deploy with this rule would immediately pull a full backup of every server.
    """
    s = _FakeScheduler()
    assert jobs.schedule_catchup(s, "weekly_backup", lambda: None, max_gap_h=7 * 24 + 12) is False
    assert s.scheduled == []
    assert "weekly_backup" in memory, "the start time is recorded anyway"


def test_every_nightly_job_has_a_memory():
    """Guard against regression: registering a cron job without a record brings the gap back."""
    import ast
    import pathlib

    source = pathlib.Path(__file__).resolve().parents[1] / "app" / "main.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    unrecorded: list[str] = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "add_job"):
            continue
        trigger = node.args[1] if len(node.args) > 1 else None
        if not (isinstance(trigger, ast.Constant) and trigger.value == "cron"):
            continue
        job_id = next((k.value.value for k in node.keywords
                       if k.arg == "id" and isinstance(k.value, ast.Constant)), "?")
        first = ast.unparse(node.args[0])
        if "jobs.logged" not in first:
            unrecorded.append(job_id)
    assert unrecorded == [], (
        f"Cron jobs without memory: {unrecorded}. A backend restart across their minute drops "
        f"them without replacement — silently, because the job does not exist during that time."
    )
