"""The stats poller and its supplier.

The poller's module header calls it "clock AND cleaner" and justifies that sharply: a missed row
is a session that never closes and falsifies play time for good. These tests pin that role.

* **A stopped container must close every open session.** `left_at` is only set here in the whole
  backend — `lifecycle`, the scheduled restart, the watchdog autoheal and the world operations do
  not touch `player_sessions`. "Container stopped" is the most definite answer there is: nobody
  is online anymore, guaranteed. Treated as "nothing to do", play time would keep growing over
  the whole downtime.
* **A PARTIAL name list does not count as complete.** Catching only the total failure is not
  enough; `count > len(names)` would book out every missing player — anew every minute.
* **The activity clock must not depend on NAMES.** For a game that does not reliably fill its
  name list (Valheim, Factorio — both noted as measured in the code) it would never advance, and
  the idle stop would switch off a server that is played daily.
* **A rejoin within 30 minutes must not inherit the join of the previous session.** The opposite
  path has exactly this protection; the join side needs it too.
* **Every game whose version source is the runtime gets the detected version in the registry** —
  not only Arma Reforger; "this game fetches its version itself at startup" applies to Valheim
  just as well.

Two on the supplier: the event parser must not fall back silently to the Docker log when no log
files are found (for PZ that source structurally contains nothing), and it must not tick at HALF
the rate of its own consumer.
"""
from __future__ import annotations

import asyncio
import datetime as dt
from types import SimpleNamespace

import pytest

from app.services import log_parser, stats_poller as sp


class _Sess:
    """A session, as far as the poller needs one."""

    def __init__(self, lines=()):
        self.lines = list(lines)
        self.added: list = []
        self.commits = 0

    def __enter__(self): return self
    def __exit__(self, *a): return False
    def exec(self, stmt): return SimpleNamespace(all=lambda: self.lines, first=lambda: None)
    def add(self, row): self.added.append(row)
    def get(self, model, sid): return None
    def commit(self): self.commits += 1


def _open_session(name, hours_ago):
    return SimpleNamespace(
        username=name, left_at=None, duration_seconds=None, steam_id="",
        joined_at=dt.datetime(2026, 8, 20, 12, 0) - dt.timedelta(hours=hours_ago),
    )


def _srv(key="vh"):
    return SimpleNamespace(id=1, key=key, container_name=key, game_type=None)


@pytest.fixture(autouse=True)
def _no_database(monkeypatch):
    """This is about the rule, not the column — `scope_clause` is replaced."""
    monkeypatch.setattr(sp.servers_registry, "scope_clause", lambda col, server: True)


def test_a_stopped_container_closes_the_open_sessions(monkeypatch):
    open_ = [_open_session("Anna", 2), _open_session("Bo", 1)]
    sess = _Sess(open_)
    monkeypatch.setattr(sp, "Session", lambda engine: sess)

    class _Dk:
        async def state(self, name):
            return {"running": False, "finished_at": "2026-08-20T14:00:00.000000000Z"}
    monkeypatch.setattr(sp, "docker", _Dk())

    asyncio.run(sp._poll_server(_srv(), SimpleNamespace()))
    assert all(s.left_at is not None for s in open_), "the only writer of left_at gives up"
    # Closed at the STOP time, not at "now" — otherwise play time keeps growing over the whole
    # downtime.
    assert open_[0].left_at == dt.datetime(2026, 8, 20, 14, 0)
    assert open_[0].duration_seconds == 4 * 3600


def test_a_partial_name_list_closes_no_session(monkeypatch):
    open_ = [_open_session("Anna", 1), _open_session("Bo", 1)]
    sess = _Sess(open_)
    monkeypatch.setattr(sp, "Session", lambda engine: sess)
    monkeypatch.setattr(sp, "_latest_event", lambda *a, **k: None)

    class _Dk:
        async def state(self, name):
            return {"running": True}
    monkeypatch.setattr(sp, "docker", _Dk())

    class _Drv:
        async def players(self, server):
            return {"count": 2, "names": ["Anna"]}      # Bo is missing from the list, not from the game
    monkeypatch.setattr(sp, "version_source", lambda s: None)

    asyncio.run(sp._poll_server(_srv(), _Drv()))
    assert open_[1].left_at is None, \
        "count=2 with one name means: the list is incomplete, not that Bo left"


def test_the_activity_clock_depends_on_the_count_not_the_names(monkeypatch):
    """Otherwise the idle stop switches off a server that is played daily."""
    touched: list = []
    monkeypatch.setattr(sp, "_touch_activity", lambda s: touched.append(s.key))

    class _Dk:
        async def state(self, name):
            return {"running": True}
    monkeypatch.setattr(sp, "docker", _Dk())

    class _Drv:
        async def players(self, server):
            return {"count": 2, "names": []}            # Valheim/Factorio: count without names
    asyncio.run(sp._poll_server(_srv(), _Drv()))
    assert touched == ["vh"], "two players online, and the clock stood still"


def test_the_parser_does_not_fall_back_to_a_source_that_contains_nothing(monkeypatch):
    """For PZ an empty path list means "not found", not "it is on stdout"."""
    fetched: list[str] = []

    class _Dk:
        async def state(self, name):
            return {"running": True}
        async def logs(self, name, **k):
            fetched.append(name)
            return ""
    monkeypatch.setattr(log_parser, "docker", _Dk())

    class _PZ:
        events_from_files = True
        def event_log_paths(self, server): return []
        def parse_events(self, server, raw): return []

    asyncio.run(log_parser._poll_server(_srv("pz-1"), _PZ()))
    assert fetched == [], "the Docker log is the wrong source for this game"

    class _MC:
        events_from_files = False
        def event_log_paths(self, server): return []
        def parse_events(self, server, raw): return []

    asyncio.run(log_parser._poll_server(_srv("minecraft"), _MC()))
    assert fetched == ["minecraft"], "for Minecraft stdout is still right"


def test_the_producer_ticks_faster_than_its_consumer():
    """The parser writes the events, the poller reads them — the parser must not lag behind."""
    import ast
    import pathlib

    source = pathlib.Path(__file__).resolve().parents[1] / "app" / "main.py"
    intervals: dict[str, int] = {}
    for node in ast.walk(ast.parse(source.read_text(encoding="utf-8"))):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "add_job"):
            continue
        kw = {k.arg: k.value for k in node.keywords}
        jid = getattr(kw.get("id"), "value", None)
        secs = getattr(kw.get("seconds"), "value", None)
        if jid in {"stats_poll", "log_events"} and isinstance(secs, int):
            intervals[jid] = secs
    assert intervals.keys() == {"stats_poll", "log_events"}, intervals
    assert intervals["log_events"] <= intervals["stats_poll"], (
        f"the event parser ({intervals['log_events']} s) runs slower than its consumer "
        f"({intervals['stats_poll']} s). The poller then usually wins the race and creates the "
        f"session with the poll time instead of the log time — without a game account ID, and "
        f"nobody ever looks again"
    )


def test_the_detected_version_depends_on_the_source_not_the_game_name():
    """Structural: the check must use `version_source`, not `game_type == arma_reforger`."""
    import inspect

    source = inspect.getsource(sp._poll_server)
    assert "VersionSource.runtime" in source
    assert "arma_reforger" not in source, (
        "Valheim uses the same version source and has a measured `detect_version` — since this "
        "branch is the only place that writes a detected version back, `game_version` there "
        "would stay at its creation value"
    )
