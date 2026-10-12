"""Grace period of the scheduler jobs.

APScheduler discards every run that starts later than `misfire_grace_time`, and **without a value
that is one second** (`apscheduler/schedulers/base.py::_configure`:
`asint(job_defaults.get("misfire_grace_time", 1))`). Without `job_defaults` a `weekly_backup`
that is one second late is dropped without replacement and the next archive comes a week later —
visible only as a WARNING on `apscheduler.executors.default` at four in the morning.

The loop missing that one second is not an edge case: **all** relevant jobs are coroutines in the
single uvicorn loop (hard constraint), `AsyncIOExecutor` puts them into exactly that loop via
`create_task`, and the deadline is checked only **inside** the task. Alongside run
`bridge_fanout` (12 s), `bridge_ingest` (25 s), stats, metrics and the watchdog (60 s each) —
plus every HTTP request.

The reverse is just as wrong: a generous grace period for a **broadcast** turns "restart in 15
minutes" into a lie when it arrives an hour late. So this guard checks both directions.
"""
from __future__ import annotations

import ast
from pathlib import Path

APP = Path(__file__).parent.parent / "app"

# Jobs whose value dies with the delay: a broadcast that comes too late says something wrong.
# They must set their OWN, tight grace period instead of relying on the generous default. The key
# is the called function, because the job ids are built dynamically.
TIME_CRITICAL = {"_send", "_broadcast"}

# Floor against a silent failure of the collection itself.
#
# It is meant to be a COUNTED number, not an estimate kept just below it: with a floor below the
# real count a job could be removed without the guard saying anything — the very guard built
# against a job silently disappearing. When the number rises on purpose, raise it here too.
MIN_EXPECTED = 22


def _add_job_calls(tree: ast.AST) -> list[tuple[int, str, set[str]]]:
    """(line, name of the target function, keywords set) per `…add_job(…)`.

    Matching is by the attribute name, not the object — so `s = scheduler; s.add_job(…)` counts
    as well. If the first argument is not a simple name (lambda, attribute access), the target is
    `"unclear"`: unreadable counts as a hit, not as an acquittal.
    """
    hits: list[tuple[int, str, set[str]]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Attribute) and func.attr == "add_job"):
            continue
        target = "unclear"
        first = node.args[0] if node.args else None
        # **The nightly run sits in a wrapper**: `add_job` gets
        # `jobs.logged("weekly_backup", scheduled_backup)`, so the run leaves a trace in the
        # database and a backend restart across the scheduled minute can be caught up. Without
        # unwrapping, every such job shows up as `unclear` — and this guard would no longer find
        # its own targets. The cross-check below catches exactly that.
        if isinstance(first, ast.Call) and len(first.args) >= 2:
            first = first.args[1]
        if isinstance(first, ast.Name):
            target = first.id
        elif isinstance(first, ast.Attribute):
            target = first.attr
        keys = {kw.arg for kw in node.keywords if kw.arg}
        hits.append((node.lineno, target, keys))
    return hits


def _all_call_sites() -> list[tuple[str, int, str, set[str]]]:
    out: list[tuple[str, int, str, set[str]]] = []
    for path in sorted(APP.rglob("*.py")):
        for lineno, target, keys in _add_job_calls(ast.parse(path.read_text(encoding="utf-8"))):
            out.append((path.relative_to(APP.parent).as_posix(), lineno, target, keys))
    return out


def test_the_scheduler_sets_its_own_grace_period():
    """Without `job_defaults` the 1-second deadline applies — also to the weekly backup.

    The value is set when the instance is built and so applies to all jobs at once, instead of
    being copied to every `add_job` call.
    """
    from app.scheduler import scheduler

    deadline = scheduler._job_defaults.get("misfire_grace_time")
    assert deadline is not None, (
        "The scheduler has no job_defaults — APScheduler's default of 1 second applies, "
        "and a weekly_backup that is one second late is dropped without replacement."
    )
    assert deadline > 60, (
        f"misfire_grace_time={deadline}s is too tight for a nightly run queued behind a "
        "busy event loop."
    )
    assert scheduler._job_defaults.get("coalesce") is True, (
        "Without coalesce a caught-up interval job would fire its whole backlog "
        "at once."
    )


def test_time_critical_broadcasts_set_their_own_tight_grace_period():
    """A broadcast that comes too late says something wrong.

    The generous default is right for `weekly_backup` and wrong for "restart in 15 minutes".
    These jobs must therefore override it explicitly.
    """
    missing = [
        f"{file}:{line} ({target})"
        for file, line, target, keys in _all_call_sites()
        if target in TIME_CRITICAL and "misfire_grace_time" not in keys
    ]
    assert not missing, (
        "These broadcast jobs inherit the generous grace period and would still claim, hours "
        "late, that the restart is imminent:\n  " + "\n  ".join(missing)
    )


def test_the_collection_finds_all_call_sites_at_all():
    """Cross-check against a silent failure of the guard itself."""
    sites = _all_call_sites()
    assert len(sites) >= MIN_EXPECTED, (
        f"Only {len(sites)} add_job calls found, expected at least "
        f"{MIN_EXPECTED}. Either jobs were removed — or the collection is broken."
    )
    assert {target for _, _, target, _ in sites} >= {"scheduled_backup", "_send", "_broadcast"}, (
        "The known target jobs do not show up — the AST walk matches nothing."
    )


def test_the_guard_would_find_the_old_bug(tmp_path):
    """Without this evidence it would not be shown that the guard checks anything at all."""
    p = tmp_path / "old.py"
    p.write_text(
        "scheduler.add_job(_send, 'interval', minutes=5, id='x')\n"
        "s = scheduler\n"
        "s.add_job(_broadcast, 'cron', hour=4, misfire_grace_time=60)\n"
        "scheduler.add_job(lambda: None, 'cron', hour=1)\n",
        encoding="utf-8",
    )
    found = _add_job_calls(ast.parse(p.read_text(encoding="utf-8")))
    assert [(ln, target) for ln, target, _ in found] == [(1, "_send"), (3, "_broadcast"), (4, "unclear")]
    # The alias `s = scheduler` does not escape, and the lambda form is reported as "unclear"
    # instead of being skipped.
    without_deadline = [target for _, target, keys in found if "misfire_grace_time" not in keys]
    assert without_deadline == ["_send", "unclear"]
