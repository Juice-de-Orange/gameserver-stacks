"""An unverified Workshop item is not a removed one, and `result != 1` is no proof.

Two mistakes that both look like a measurement once led to an intact mod being removed from a
running configuration:

1. `ISteamRemoteStorage/GetPublishedFileDetails` returned `result=9` for item 2688809268, which
   was read as "removed". In fact the page was alive with 900,363 subscribers, size and
   `timeupdated` matched the local `.acf`, and the server had downloaded the item in full the day
   before over the same SteamUGC path. Cross-check: a really removed item (2846459045) ALSO
   returns `result=9`. The value does not separate the two.
2. The sentence "This item has been removed from the community because it violates Steam
   Community & Content Guidelines" is in the HTML of EVERY Workshop page, in a
   `<div class="bannedNotification" style="display: none">`, including the page of an undisputedly
   healthy item. A `grep` over the HTML always finds it.

What `mod_check` makes of this: three states instead of two. `AVAILABLE`, `REMOVED` and,
decisively, `UNVERIFIABLE`. The third must pass neither as green nor as a finding, because it is a
statement about the CHECK, not about the mod.

The other failure direction of the same class was in `run_check` itself: an unreadable mod list
gave an empty item list, and `steam_ok` was then even True via `not workshop_items`, "nothing to
object to" for a server about whose mods nothing was known. Cf.
`test_a_blind_check_is_not_ok.py`, the same direction elsewhere.
"""
import asyncio
import json

import pytest

from app.models.server import GameServer, GameType, ManagementMode
from app.services import mod_check


def _pz(root) -> GameServer:
    s = GameServer(key="pz-test", game_type=GameType.pz,
                   management_mode=ManagementMode.dashboard, container_name="servertest",
                   data_dir=str(root), enabled=True,
                   extra=json.dumps({"servername": "servertest"}))
    s.id = 1
    return s


# --------------------------------------------------------------- the page cross-check

class _Response:
    def __init__(self, text: str) -> None:
        self.text = text

    def raise_for_status(self) -> None:
        pass


class _Client:
    """Dummy for httpx.AsyncClient: returns a stored page per Workshop ID."""

    def __init__(self, pages: dict[str, str], error: bool = False) -> None:
        self._pages, self._error = pages, error

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url: str):
        if self._error:
            raise RuntimeError("network gone")
        wid = url.rsplit("=", 1)[-1]
        return _Response(self._pages.get(wid, ""))


LIVE_PAGE = (
    "<html><head><title>Steam Workshop::TCCacheMP</title></head><body>"
    '<div class="bannedNotification" id="bannedNotification" style="display: none">'
    "This item has been removed from the community because it violates Steam Community &amp; "
    "Content Guidelines.</div><div>900,363 Current Subscribers</div></body></html>"
)
ERROR_PAGE = "<html><head><title>Steam Community :: Error</title></head><body>Error</body></html>"


def _with_pages(monkeypatch, pages, error=False):
    monkeypatch.setattr(mod_check.httpx, "AsyncClient",
                        lambda *a, **k: _Client(pages, error))


def test_a_live_page_counts_as_available(monkeypatch):
    _with_pages(monkeypatch, {"111": LIVE_PAGE})
    assert asyncio.run(mod_check.probe_workshop_page("111")) == mod_check.AVAILABLE


def test_an_error_page_counts_as_removed(monkeypatch):
    _with_pages(monkeypatch, {"222": ERROR_PAGE})
    assert asyncio.run(mod_check.probe_workshop_page("222")) == mod_check.REMOVED


def test_the_hidden_ban_sentence_does_not_turn_a_live_page_into_a_removed_one(monkeypatch):
    """The actual trap: the sentence is on every page, hidden."""
    assert "violates Steam Community" in LIVE_PAGE      # it IS in the HTML …
    _with_pages(monkeypatch, {"111": LIVE_PAGE})
    assert asyncio.run(mod_check.probe_workshop_page("111")) == mod_check.AVAILABLE  # … but does not count

def test_a_network_error_on_the_page_fetch_is_no_verdict(monkeypatch):
    _with_pages(monkeypatch, {}, error=True)
    assert asyncio.run(mod_check.probe_workshop_page("333")) == mod_check.UNVERIFIABLE


# --------------------------------------------------------------- the availability rule

def test_result_9_with_a_live_page_is_available(monkeypatch):
    """The core case. Exactly here an intact mod was taken for removed."""
    _with_pages(monkeypatch, {"2688809268": LIVE_PAGE})
    steam = {"2688809268": {"result": 9, "banned": False}}
    verdict = asyncio.run(mod_check.check_availability(["2688809268"], steam))
    assert verdict["2688809268"] == mod_check.AVAILABLE


def test_result_9_with_an_error_page_is_removed(monkeypatch):
    _with_pages(monkeypatch, {"2846459045": ERROR_PAGE})
    steam = {"2846459045": {"result": 9, "banned": False}}
    verdict = asyncio.run(mod_check.check_availability(["2846459045"], steam))
    assert verdict["2846459045"] == mod_check.REMOVED


def test_a_banned_item_needs_no_cross_check(monkeypatch):
    """Steam states `banned` explicitly; that is reliable without fetching the page."""
    def _no_page(*a, **k):
        raise AssertionError("the page fetch should not have happened")
    monkeypatch.setattr(mod_check, "probe_workshop_page", _no_page)
    steam = {"444": {"result": 1, "banned": True}}
    verdict = asyncio.run(mod_check.check_availability(["444"], steam))
    assert verdict["444"] == mod_check.REMOVED


def test_without_a_steam_response_it_stays_unverifiable(monkeypatch):
    def _no_page(*a, **k):
        raise AssertionError("without a Steam response there is nothing to cross-check")
    monkeypatch.setattr(mod_check, "probe_workshop_page", _no_page)
    verdict = asyncio.run(mod_check.check_availability(["555"], {}))
    assert verdict["555"] == mod_check.UNVERIFIABLE


def test_result_1_triggers_no_page_fetch(monkeypatch):
    """29 fetches per report would be load without insight."""
    def _no_page(*a, **k):
        raise AssertionError("result=1 is undisputed")
    monkeypatch.setattr(mod_check, "probe_workshop_page", _no_page)
    steam = {"666": {"result": 1, "banned": False}}
    verdict = asyncio.run(mod_check.check_availability(["666"], steam))
    assert verdict["666"] == mod_check.AVAILABLE


# --------------------------------------------------------------- the report as a whole

def _report(monkeypatch, tmp_path, *, list_readable: bool, items: list[str], verdicts: dict[str, str]):
    from app.services.mods import inventory, state as mod_state

    class _Plan:
        readable = list_readable
        detail = "" if list_readable else "servertest.ini: Permission denied"
        workshop_items = tuple(items)

    class _Inv:
        readable = True
        items: dict = {}

    monkeypatch.setattr(mod_state, "live_plan", lambda s: _Plan())
    monkeypatch.setattr(inventory, "scan", lambda s: _Inv())
    # `read_acf` also returns whether the file was READABLE: an `OSError` used to give an empty
    # dict, and an empty dict structurally switches off update detection, so the report stayed
    # green for a server whose install state was unknown. Here: readable, but empty.
    monkeypatch.setattr(mod_check, "read_acf", lambda s: ({}, True))
    monkeypatch.setattr(mod_check, "fetch_steam", lambda ids: _done({}))
    monkeypatch.setattr(mod_check, "check_availability", lambda ids, steam: _done(verdicts))
    return asyncio.run(mod_check.run_check(_pz(tmp_path)))


async def _done(value):
    return value


def test_an_unreadable_mod_list_is_not_fine(monkeypatch, tmp_path):
    """The fail-open fix: empty list + `not workshop_items` used to give `steam_ok=True`."""
    rep = _report(monkeypatch, tmp_path, list_readable=False, items=[], verdicts={})
    codes = [i["code"] for i in rep["issues"]]
    assert "MOD_LIST_UNREADABLE" in codes
    assert rep["summary"]["ok"] is False
    assert rep["summary"]["steam_ok"] is False
    assert rep["summary"]["list_readable"] is False


def test_an_unverifiable_item_makes_the_comparison_incomplete(monkeypatch, tmp_path):
    rep = _report(monkeypatch, tmp_path, list_readable=True, items=["777"],
                  verdicts={"777": mod_check.UNVERIFIABLE})
    codes = [i["code"] for i in rep["issues"]]
    assert "WORKSHOP_ITEM_UNVERIFIABLE" in codes
    assert rep["summary"]["steam_ok"] is False
    # … but it is NOT a finding against the mod: it does not show up under `removed`.
    assert rep["summary"]["removed"] == []
    assert rep["summary"]["errors"] == 0


def test_a_removed_item_is_named_in_the_report(monkeypatch, tmp_path):
    rep = _report(monkeypatch, tmp_path, list_readable=True, items=["888"],
                  verdicts={"888": mod_check.REMOVED})
    codes = [i["code"] for i in rep["issues"]]
    assert "WORKSHOP_ITEM_REMOVED" in codes
    assert rep["summary"]["removed"] == ["888"]
    assert rep["summary"]["ok"] is False


def test_a_fully_checked_set_is_green(monkeypatch, tmp_path):
    rep = _report(monkeypatch, tmp_path, list_readable=True, items=["999"],
                  verdicts={"999": mod_check.AVAILABLE})
    assert rep["summary"]["ok"] is True
    assert rep["summary"]["steam_ok"] is True
    assert rep["summary"]["removed"] == []
