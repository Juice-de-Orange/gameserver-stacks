"""Growth without a limit, and a report without an expiry date.

**Every job table needs a retention rule** — and comments claiming completeness are not proof:
`server_incidents` (six writing modules, and the watchdog records one per cycle in a boot loop),
`player_events` (the parser runs every 30 seconds) and `operations` (one row per operation with up
to 5000 characters of payload) all grow.

**And the workshop report needs an expiry date.** It carries no timestamp itself and decides the
mod traffic light via `cached()` — after a mod apply, i.e. after exactly the change it is supposed
to judge, a stale report would show the finding of the OLD mod list for up to six hours. Three
more on the same file: an unreadable appworkshop file must not become "no updates", the 6-hour
refresh must ask the world lock, and its cross-check against the workshop page must be capped —
exactly in the situation it was built for.
"""
from __future__ import annotations

import asyncio
import ast
import inspect
import pathlib
from types import SimpleNamespace

import pytest

from app.services import mod_check


def test_every_job_table_has_a_retention_rule():
    """The guard against creeping back — and against the next wrong comment."""
    from app.services import cleanup

    source = inspect.getsource(cleanup.prune_history)
    for table in ("ServerIncident", "PlayerEvent", "Operation"):
        assert table in source, f"{table} has no retention rule"

    # And the job is registered too — a rule nobody runs is no rule.
    main_py = pathlib.Path(__file__).resolve().parents[1] / "app" / "main.py"
    ids = {
        getattr(k.value, "value", None)
        for node in ast.walk(ast.parse(main_py.read_text(encoding="utf-8")))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        and node.func.attr == "add_job"
        for k in node.keywords if k.arg == "id"
    }
    assert "history_prune" in ids


def test_player_sessions_is_deliberately_not_pruned():
    """It is a record table, not a log table — leaderboard and heatmap come from it."""
    from app.services import cleanup

    source = inspect.getsource(cleanup)
    assert "PlayerSession" not in source.split('"""', 2)[2], \
        "deleting play times is the operator's decision, not cleanup"


def test_an_unreadable_appworkshop_file_is_not_a_green_report(tmp_path, monkeypatch):
    """The two sister sources of the same report already carry a flag for exactly this."""
    srv = SimpleNamespace(key="pz-1", data_dir=str(tmp_path))
    folder = tmp_path / "workshop-mods"
    folder.mkdir()
    file = folder / f"appworkshop_{mod_check.PZ_APP_ID}.acf"
    file.write_text('"123" { "timeupdated" "1700000000" }', encoding="utf-8")

    stamps, readable = mod_check.read_acf(srv)
    assert stamps == {"123": 1700000000} and readable is True

    # Not readable (permissions, mount gone) -> UNKNOWN, not "no timestamps"
    def _broken(*a, **k):
        raise PermissionError("permission denied")
    monkeypatch.setattr("builtins.open", _broken)
    stamps, readable = mod_check.read_acf(srv)
    assert stamps == {} and readable is False


def test_a_missing_file_is_a_result(tmp_path):
    """A never-installed collection has no file — that is not a read error."""
    srv = SimpleNamespace(key="pz-1", data_dir=str(tmp_path))
    stamps, readable = mod_check.read_acf(srv)
    assert stamps == {} and readable is True


def test_the_report_expires(monkeypatch):
    """Without an expiry nobody would ever discard it."""
    mod_check._cache.clear()
    runs = []

    async def _check(server):
        runs.append(server.key)
        return {"items": [], "issues": [], "summary": {}}
    monkeypatch.setattr(mod_check, "run_check", _check)

    srv = SimpleNamespace(key="pz-1")
    clock = [1000.0]
    monkeypatch.setattr(mod_check.time, "monotonic", lambda: clock[0])

    asyncio.run(mod_check.get_report(srv))
    asyncio.run(mod_check.get_report(srv))
    assert len(runs) == 1, "no new check within the validity period"

    clock[0] += mod_check.REPORT_VALID_S + 1
    assert mod_check.cached("pz-1") is None, \
        "expired: better \"still pending\" than a false assurance"
    asyncio.run(mod_check.get_report(srv))
    assert len(runs) == 2


def test_a_mod_apply_discards_the_report():
    """Structural: both apply paths must call `mod_check.discard` — also on failure."""
    from app.services.mods import apply as mod_apply, foreign_apply

    for module in (mod_apply, foreign_apply):
        source = inspect.getsource(module)
        assert "mod_check.discard" in source, (
            f"{module.__name__} does not discard the workshop report — the traffic light then "
            f"shows the finding of the old mod list for up to six hours, exactly when someone "
            f"looks because they just changed something"
        )


def test_the_refresh_asks_the_world_lock():
    source = inspect.getsource(mod_check.scheduled_refresh)
    assert "world_lock.held_by" in source, (
        "`run_check` reads exactly the folders a running mod operation is moving — "
        "`backup.scheduled_backup` asks exactly this question for the same folders"
    )


def test_the_cross_check_is_capped(monkeypatch):
    """In exactly the situation it was built for, it affects ALL items."""
    concurrent = {"max": 0, "now": 0}

    async def _probe(wid):
        concurrent["now"] += 1
        concurrent["max"] = max(concurrent["max"], concurrent["now"])
        await asyncio.sleep(0)
        concurrent["now"] -= 1
        return mod_check.AVAILABLE
    monkeypatch.setattr(mod_check, "probe_workshop_page", _probe)

    ids = [str(i) for i in range(30)]
    steam = {w: {"result": 9} for w in ids}          # web API disturbed: ALL are suspect
    asyncio.run(mod_check.check_availability(ids, steam))
    assert concurrent["max"] <= mod_check.CROSS_CHECKS_CONCURRENT, (
        f"{concurrent['max']} HTML pages at once in the memory of the ONE worker"
    )


def test_the_mod_refresh_runs_shortly_after_startup():
    """Otherwise the traffic light shows yellow for six hours after every deploy."""
    main_py = pathlib.Path(__file__).resolve().parents[1] / "app" / "main.py"
    for node in ast.walk(ast.parse(main_py.read_text(encoding="utf-8"))):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "add_job"):
            continue
        kw = {k.arg: k.value for k in node.keywords}
        if getattr(kw.get("id"), "value", None) == "mod_check":
            assert "next_run_time" in kw, (
                "an interval job without `next_run_time` starts a FULL interval after "
                "registration. The only store of the result is a process-local cache that "
                "every deploy throws away with the container"
            )
            return
    pytest.fail("job `mod_check` not found — the guard would check nothing")


def test_the_demand_is_measured_in_parallel():
    """`can_start` passes no `stats_hint` — the round runs BEFORE EVERY server start."""
    from app.services import resource_budget as rb

    source = inspect.getsource(rb._running_servers)
    assert source.count("asyncio.gather") >= 2, (
        "only the container states are fetched in parallel; `docker.stats` (~1.0 s per container) "
        "still runs one after another"
    )
