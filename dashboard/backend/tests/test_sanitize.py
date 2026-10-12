from app.routers.actions import _COORDS_RE, _ITEM_RE, _TOKEN_RE
from app.routers.moderation import _clean


def test_clean_removes_injection():
    assert _clean('bob"; quit') == "bob quit"   # quote + semicolon removed
    assert _clean("a\x00b\nc") == "abc"          # control characters removed
    assert _clean("  spaced  ") == "spaced"      # trimmed


def test_action_regexes():
    assert _ITEM_RE.match("Base.Axe")
    assert not _ITEM_RE.match("Base Axe")
    assert _COORDS_RE.match("100,200")
    assert _COORDS_RE.match("100,200,300")
    assert not _COORDS_RE.match("x,y")
    assert _TOKEN_RE.match("Strength")
    assert not _TOKEN_RE.match('a"b')
    assert not _TOKEN_RE.match("a;b")


def test_ticket_link_must_be_a_web_link():
    """The requester types `workshop_url` — the admin reads it.

    The value ends up in an `href`. React sets `javascript:…` without complaint, the click would
    run in the admin's session, and the access token is in the browser's `localStorage`.
    Both sides check: the frontend protects the reader (`lib/url.safeUrl`), the server
    prevents such a value from being stored at all.
    """
    import pytest
    from fastapi import HTTPException

    from app.routers.tickets import _check_weblink

    assert _check_weblink("https://steamcommunity.com/sharedfiles/filedetails/?id=1") \
        == "https://steamcommunity.com/sharedfiles/filedetails/?id=1"
    assert _check_weblink("  http://example.org/x  ") == "http://example.org/x"
    assert _check_weblink("") == ""          # empty is allowed, the field is optional

    for bad in ["javascript:alert(1)", "JaVaScRiPt:alert(1)",
                  "data:text/html,<script>alert(1)</script>", "file:///etc/passwd",
                  "//other-host/x", "steamcommunity.com/x"]:
        with pytest.raises(HTTPException) as e:
            _check_weblink(bad)
        assert e.value.status_code == 400, bad
