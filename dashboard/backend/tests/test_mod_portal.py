"""The Factorio mod portal — and the three assumptions that fell when measured.

Project Zomboid fetches its mods via SteamCMD; Factorio has an open portal. On this game an
outdated mod weighs more than elsewhere: the client compares the mod set exactly, and whoever
does not have the same version does not get in at all.

**What measuring the real API disproved:**

1. **There is no full-text search.** `?q=` and `?search=` are silently ignored — they return the
   same 22 924 hits as a request without parameters. Using them yields a response that looks
   like a search result and starts alphabetically at `----`. Exactly this kind of false answer —
   plausible, sorted, completely wrong — is the most expensive one.
2. **`sort=downloads_count` does not sort.** No error either, not even with nonsense.
3. **`namelist=a,b,c` is exact and cheap.** The right way for the update check.

Filtering and sorting therefore happen locally, and this test pins down the rules that matter
there — above all those where a mistake would be expensive.
"""
from __future__ import annotations

import pytest

from app.services import mod_portal


# ------------------------------------------------------------------ Version comparison

@pytest.mark.parametrize(
    ("new", "old", "expected"),
    [
        ("1.2.3", "1.2.2", True),
        ("1.10.0", "1.9.9", True),      # not alphabetical: 10 > 9
        ("2.0.0", "1.99.99", True),
        ("1.2.3", "1.2.3", False),
        ("1.2.2", "1.2.3", False),
        ("0.18.47", "0.18.9", True),
    ],
)
def test_version_comparison_uses_numbers(new, old, expected):
    assert mod_portal.is_newer(new, old) is expected


@pytest.mark.parametrize("broken", ["", "  ", "latest", None])
def test_unreadable_versions_report_NO_update(broken):
    """Better to report no update than one based on an unreadable version string.

    On Factorio a falsely reported update makes someone change their mod set — afterwards nobody
    whose client has the old version gets in anymore.
    """
    assert mod_portal.is_newer(broken, "1.0.0") is False
    assert mod_portal.is_newer("1.0.0", broken) is False


# ------------------------------------------------------------------ Game version

def test_the_mod_series_must_match_the_game_series():
    """`factorio_version` is the SERIES (`2.0`), not the version (`2.0.77`)."""
    assert mod_portal._matches_game_version("2.0", "2.0.77") is True
    assert mod_portal._matches_game_version("1.1", "2.0.77") is False


def test_a_missing_value_does_not_exclude():
    """Otherwise a hit would be concealed because of a missing value — again an unknown passing
    as a result."""
    assert mod_portal._matches_game_version("", "2.0.77") is True
    assert mod_portal._matches_game_version("2.0", "") is True


# ------------------------------------------------------------------ Credentials

def test_without_account_it_says_what_is_missing():
    reason = mod_portal.credentials_missing("", "")
    assert "factorio.com" in reason and "Searching" in reason, (
        "the sentence must also say what still works WITHOUT an account"
    )


def test_with_account_there_is_nothing_to_say():
    assert mod_portal.credentials_missing("alice", "abc123") == ""


def test_the_download_url_carries_the_credentials():
    """Without them the portal redirects to the login page — the result would be an HTML page
    with the extension `.zip` in the mod folder, and the server would not come up afterwards."""
    url = mod_portal.download_url("/download/x/abc", username="alice", token="secret")
    assert url.startswith("https://mods.factorio.com/download/x/abc?")
    assert "username=alice" in url and "token=secret" in url


# ------------------------------------------------------------------ Search

def test_a_too_short_term_is_rejected():
    """Otherwise half the catalogue comes back — 22 924 entries, 13 MB."""
    import asyncio

    with pytest.raises(ValueError, match="two characters"):
        asyncio.run(mod_portal.search("a"))


def test_the_search_filters_and_sorts_itself(monkeypatch):
    """The portal can do neither — so it has to happen here, and correctly."""
    import asyncio

    response = {"results": [
        {"name": "belt-a", "title": "Belt A", "summary": "", "downloads_count": 5,
         "latest_release": {"version": "1.0.0", "info_json": {"factorio_version": "2.0"}}},
        {"name": "belt-b", "title": "Belt B", "summary": "", "downloads_count": 500,
         "latest_release": {"version": "2.0.0", "info_json": {"factorio_version": "2.0"}}},
        {"name": "whatever", "title": "Nothing to do with it", "summary": "", "downloads_count": 9999,
         "latest_release": {"version": "1.0.0", "info_json": {"factorio_version": "2.0"}}},
        {"name": "belt-old", "title": "Belt Old", "summary": "", "downloads_count": 800,
         "latest_release": {"version": "1.0.0", "info_json": {"factorio_version": "1.1"}}},
    ]}

    class _Response:
        status_code = 200

        def json(self):
            return response

        def raise_for_status(self):
            return None

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, params=None):
            # Proof that we do NOT trust a portal search: the full list is fetched, without
            # a search term.
            assert params == {"page_size": "max"}
            return _Response()

    monkeypatch.setattr(mod_portal.httpx, "AsyncClient", lambda **kw: _Client())

    hits = asyncio.run(mod_portal.search("belt", game_version="2.0.77"))
    assert [t["name"] for t in hits] == ["belt-b", "belt-a"], (
        "either not sorted by downloads, or the wrong game series got through"
    )


def test_an_unreachable_portal_is_not_an_empty_result(monkeypatch):
    """"No hits" and "I could not look" are two different answers."""
    import asyncio

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, *a, **kw):
            raise OSError("network down")

    monkeypatch.setattr(mod_portal.httpx, "AsyncClient", lambda **kw: _Client())
    with pytest.raises(mod_portal.PortalError):
        asyncio.run(mod_portal.search("belt"))


# ------------------------------------------------------------------ Updates

def test_the_two_response_shapes_of_the_portal():
    """The same route returns two shapes.

    Without `namelist` the portal attaches a `latest_release` field; WITH `namelist=` it returns
    `releases` instead — the full list, without `latest_release`. The update check queries via
    `namelist`; reading `latest_release` there leaves `available` always empty and
    `is_newer("", x)` always `False` — the check would **never report an update** and look like
    "everything up to date".

    A dummy that mimics the shape of the OTHER query does not catch this, so BOTH shapes are
    here, with the real keys.
    """
    with_latest = {"latest_release": {"version": "2.0.0"}}
    assert mod_portal._latest_release(with_latest)["version"] == "2.0.0"

    # This is how `namelist=` really answers (measured against the real API):
    with_releases = {"releases": [{"version": "1.0.0"}, {"version": "2.0.0"}]}
    assert mod_portal._latest_release(with_releases)["version"] == "2.0.0"

    # And the highest, not the last row: the portal's order is guaranteed nowhere, and a
    # sorting error there would be a false update here.
    unsorted = {"releases": [{"version": "2.0.0"}, {"version": "1.9.0"}]}
    assert mod_portal._latest_release(unsorted)["version"] == "2.0.0"

    assert mod_portal._latest_release({}) == {}


def test_updates_query_only_the_installed_names(monkeypatch):
    """One request with `namelist=` instead of a 13 MB dump.

    The dummy uses the REAL `namelist` shape (`releases`), not the one of the other query — that
    difference is exactly what a wrong dummy would hide.
    """
    import asyncio

    seen = {}

    class _Response:
        def json(self):
            return {"results": [
                {"name": "flib", "title": "flib", "downloads_count": 1,
                 "releases": [
                     {"version": "0.15.0", "info_json": {"factorio_version": "2.0"}},
                     {"version": "0.16.0", "info_json": {"factorio_version": "2.0"}},
                 ]},
            ]}

        def raise_for_status(self):
            return None

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, params=None):
            seen.update(params or {})
            return _Response()

    monkeypatch.setattr(mod_portal.httpx, "AsyncClient", lambda **kw: _Client())
    lines = asyncio.run(mod_portal.updates(
        {"flib": "0.15.0", "homemade": "1.0.0"}, game_version="2.0.77"))

    assert seen.get("namelist") == "flib,homemade"
    by_name = {row["name"]: row for row in lines}
    assert by_name["flib"]["update"] is True
    assert by_name["flib"]["available"] == "0.16.0"


def test_a_mod_the_portal_does_not_know_does_not_disappear(monkeypatch):
    """Home-made and renamed mods must not silently drop off the list — otherwise "everything up
    to date" looks the same even when half of them were never checked."""
    import asyncio

    class _Response:
        def json(self):
            return {"results": []}

        def raise_for_status(self):
            return None

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, *a, **kw):
            return _Response()

    monkeypatch.setattr(mod_portal.httpx, "AsyncClient", lambda **kw: _Client())
    lines = asyncio.run(mod_portal.updates({"homemade": "1.0.0"}))
    assert lines[0]["unknown"] is True
    assert lines[0]["update"] is False
    assert "does not know" in lines[0]["note"]


def test_a_newer_version_for_the_wrong_game_series_is_not_offered(monkeypatch):
    """It would be an update that makes the server unstartable."""
    import asyncio

    class _Response:
        def json(self):
            return {"results": [
                {"name": "old", "title": "Old", "downloads_count": 1,
                 "releases": [{"version": "3.0.0", "info_json": {"factorio_version": "1.1"}}]},
            ]}

        def raise_for_status(self):
            return None

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, *a, **kw):
            return _Response()

    monkeypatch.setattr(mod_portal.httpx, "AsyncClient", lambda **kw: _Client())
    line = asyncio.run(mod_portal.updates({"old": "2.0.0"}, game_version="2.0.77"))[0]
    assert line["update"] is False
    assert "1.1" in line["note"] and "2.0.77" in line["note"]
