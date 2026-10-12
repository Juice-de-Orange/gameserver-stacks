"""A dead source must not look like a result.

The catalog is the basis of two things: the dropdown from which a person picks a version, and the
nightly `auto_update`. Both must be able to tell "there is nothing new" from "we did not look":

1. **A PaperMC outage must not be masked with the vanilla list** — stored under the PAPER variant
   with a fresh `fetched_at`, which would also disarm the staleness brake.
2. **An empty catalog plus an active refill brake must not yield `source_ok: true`** — an empty
   dropdown next to the assurance that the sync runs daily. For 30 minutes, i.e. the normal case:
   only the FIRST call would be honest.
3. **An outage of the notes source must not delete all stored changelog notes** — for good, the
   table is the only store — while the run counts as successful.
4. **A game with a changed source format must not stall the whole round.** Every target behind it
   would go untouched; the trace must say which game it was.
5. **An undeterminable RUNNING version must not become "no update available".** That is the
   state `current_ref_detected` names in its docstring as the error it was built against — only
   without any source marked as dead.
"""
from __future__ import annotations

import asyncio
import datetime as dt
from types import SimpleNamespace

import pytest

from app.services import auto_update, version_catalog as vc


def test_an_empty_catalog_is_never_ok(monkeypatch):
    monkeypatch.setattr(vc, "_rows_for", lambda k, v: [])
    monkeypatch.setattr(vc, "_lazy_refresh_due", lambda k, v: False)   # brake applies
    monkeypatch.setattr(vc, "variant_for", lambda s: "")
    monkeypatch.setattr(vc, "current_ref", lambda s: "1.0")

    async def _detected(s):
        return "1.0"
    monkeypatch.setattr(vc, "current_ref_detected", _detected)

    srv = SimpleNamespace(game_type=next(iter(vc.FETCHERS)), image_repo="r", image_tag="t")
    data = asyncio.run(vc.entries_for(srv))
    assert data["entries"] == []
    assert data["source_ok"] is False, \
        "\"did not look because I may not right now\" is not \"checked, there is nothing\""


def test_an_unknown_running_version_is_not_up_to_date(monkeypatch):
    line = SimpleNamespace(version="2.0.5", ref="2.0.5", channel="stable", released_at=None,
                            changelog_title="", changelog_url="", changelog_summary="",
                            first_seen_at=vc.utcnow_naive(), fetched_at=vc.utcnow_naive())
    monkeypatch.setattr(vc, "_rows_for", lambda k, v: [line])
    monkeypatch.setattr(vc, "variant_for", lambda s: "")
    monkeypatch.setattr(vc, "current_ref", lambda s: "")

    async def _unknown(s):
        return ""                       # every detection failed
    monkeypatch.setattr(vc, "current_ref_detected", _unknown)

    srv = SimpleNamespace(game_type=next(iter(vc.FETCHERS)), image_repo="r", image_tag="t")
    data = asyncio.run(vc.entries_for(srv))
    assert data["update_available"] is None
    assert data["running_version_known"] is False, \
        "otherwise `update_available: None` means two things — up to date OR clueless"


def test_auto_update_stops_on_an_unknown_running_version(monkeypatch):
    """Otherwise the hotfix check is silently off — exactly the state it exists to prevent."""
    incidents: list[str] = []
    monkeypatch.setattr(auto_update, "_incident", lambda sid, text: incidents.append(text))

    async def _data(server):
        return {"supported": True, "source_ok": True, "running_version_known": False,
                "entries": [{"ref": "2.0.99", "version": "2.0.99", "channel": "stable",
                             "newer": True}],
                "current": ""}
    monkeypatch.setattr(auto_update.version_catalog, "entries_for", _data)

    class _Drv:
        auto_update_limit = auto_update.AutoUpdate.patch

    monkeypatch.setattr(auto_update, "get_driver", lambda s: _Drv())
    # `version_source` in turn asks the driver — the dummy must apply here too, otherwise the
    # test falls into the "deliberately without a pinned version" branch.
    monkeypatch.setattr(auto_update, "version_source",
                        lambda s: auto_update.VersionSource.image_tag)

    async def _still_running(server):
        return False        # not "safely stopped" -> the check continues
    monkeypatch.setattr(auto_update, "_safely_stopped", _still_running)

    srv = SimpleNamespace(id=1, key="fac", game_type=None, enabled=True, image_tag="2.0.77",
                          game_version="2.0.77")
    assert asyncio.run(auto_update.check_server(srv)) is None
    assert any("cannot be determined" in t for t in incidents), incidents


def test_empty_notes_do_not_overwrite_existing_ones():
    """Structural: `_store` may only SET title/link/excerpt when there is something."""
    import ast
    import inspect

    import textwrap

    tree = ast.parse(textwrap.dedent(inspect.getsource(vc._store)))
    assignments = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Attribute) and t.attr.startswith("changelog_") for t in node.targets)
    ]
    assert assignments, "the assignments were renamed — the guard would check nothing"
    for assign in assignments:
        parents = [node for node in ast.walk(tree)
                   if isinstance(node, ast.If) and assign in ast.walk(node)]
        assert parents, (
            "a changelog note is overwritten unconditionally. If the (second, independent) "
            "notes source fails, the daily run deletes all stored notes — for good, and the run "
            "still counts as successful"
        )


def test_a_broken_game_does_not_stall_the_round(monkeypatch):
    seen: list[str] = []

    async def _refresh(gt, variant):
        seen.append(str(gt))
        if str(gt) == "broken":
            raise AttributeError("'list' object has no attribute 'items'")
        return {"game_type": str(gt), "variant": variant, "ok": True, "total": 1, "new": 0}

    monkeypatch.setattr(vc, "refresh_for", _refresh)
    monkeypatch.setattr(vc, "_registered_targets", lambda: [("broken", ""), ("after", "")])

    results = asyncio.run(vc.refresh_all())
    assert seen == ["broken", "after"], "the target AFTER the broken one was skipped"
    assert results[0]["ok"] is False and "unreadable" in results[0]["detail"]
    assert results[1]["ok"] is True
