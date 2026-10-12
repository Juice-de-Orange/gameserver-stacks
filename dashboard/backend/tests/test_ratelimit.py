from types import SimpleNamespace

from app.auth import ratelimit


def test_throttle_after_threshold():
    ratelimit.reset(["k"])
    for _ in range(ratelimit.MAX_FAILURES):
        assert ratelimit.retry_after(["k"]) == 0
        ratelimit.record_failure(["k"])
    assert ratelimit.retry_after(["k"]) > 0
    ratelimit.reset(["k"])
    assert ratelimit.retry_after(["k"]) == 0


def test_empty_keys_do_not_stay_forever():
    """Expired keys of `_failures` must be removed, not just emptied.

    `reset()` runs only on a **successful** login, so exactly not while someone hammers the
    endpoint. The key is the username from the form; `/api/auth/login` is the only publicly
    reachable write path.
    """
    ratelimit._failures.clear()
    ratelimit.record_failure(["user:alice"])
    assert "user:alice" in ratelimit._failures

    # Window over: the entry is worthless and must disappear, not just become empty.
    ratelimit._failures["user:alice"][0] -= ratelimit.WINDOW_SEC + 1
    ratelimit.retry_after(["user:alice"])
    assert "user:alice" not in ratelimit._failures


def test_the_number_of_keys_is_capped():
    """Even without expiry the dict must not grow without bound."""
    ratelimit._failures.clear()
    for i in range(ratelimit.MAX_KEYS + 50):
        ratelimit.record_failure([f"user:{i}"])
    assert len(ratelimit._failures) <= ratelimit.MAX_KEYS


def test_the_username_goes_into_the_key_truncated():
    """Neither FastAPI nor Caddy limits the login form fields:
    `OAuth2PasswordRequestForm` declares `username: str = Form()` without `max_length`, and
    Starlette's urlencoded parser, unlike the multipart parser, has no limit."""
    from app.routers.auth import _throttle_keys

    huge = "a" * 5000
    keys = _throttle_keys(SimpleNamespace(headers={}, client=SimpleNamespace(host="9.9.9.9")), huge)
    assert all(len(k) <= 128 for k in keys), keys


def _req(headers=None, peer="9.9.9.9"):
    return SimpleNamespace(headers=headers or {}, client=SimpleNamespace(host=peer))


def test_forwarding_headers_from_an_untrusted_peer_are_ignored(monkeypatch):
    """Without TRUSTED_PROXIES (the default) the TCP peer is the client, whatever the headers say.

    Both headers can be set freely by any caller that reaches the API directly — in the compose
    layout every game container shares a network with it. Believing them would hand such a
    caller a fresh throttle key per request.
    """
    monkeypatch.setattr(ratelimit.settings, "trusted_proxies", [])
    assert ratelimit.client_ip(_req({"cf-connecting-ip": "203.0.113.7"})) == "9.9.9.9"
    assert ratelimit.client_ip(_req({"x-forwarded-for": "203.0.113.7, 1.1.1.1"})) == "9.9.9.9"
    assert ratelimit.client_ip(_req()) == "9.9.9.9"

    # A proxy list that does not contain this peer changes nothing.
    monkeypatch.setattr(ratelimit.settings, "trusted_proxies", ["10.0.0.0/8"])
    assert ratelimit.client_ip(_req({"cf-connecting-ip": "203.0.113.7",
                                     "x-forwarded-for": "203.0.113.8"})) == "9.9.9.9"


def test_forwarding_headers_from_a_trusted_peer_are_used(monkeypatch):
    monkeypatch.setattr(ratelimit.settings, "trusted_proxies", ["10.0.0.0/8", "192.0.2.5"])
    # Single address and CIDR entries both match.
    assert ratelimit.client_ip(_req({"x-forwarded-for": "203.0.113.7"}, peer="10.1.2.3")) == "203.0.113.7"
    assert ratelimit.client_ip(_req({"x-forwarded-for": "203.0.113.7"}, peer="192.0.2.5")) == "203.0.113.7"
    # CF-Connecting-IP wins over X-Forwarded-For.
    assert ratelimit.client_ip(_req({"cf-connecting-ip": "198.51.100.4",
                                     "x-forwarded-for": "203.0.113.7"}, peer="10.1.2.3")) == "198.51.100.4"
    # No header at all: the proxy itself is the client.
    assert ratelimit.client_ip(_req(peer="10.1.2.3")) == "10.1.2.3"
    # An IPv4-mapped IPv6 peer is the same peer.
    assert ratelimit.client_ip(_req({"x-forwarded-for": "203.0.113.7"}, peer="::ffff:10.1.2.3")) == "203.0.113.7"


def test_x_forwarded_for_is_read_from_the_right(monkeypatch):
    """The leftmost entries are whatever the client sent; only the hops appended by trusted
    proxies count. The client is the first address from the right that is not a trusted proxy."""
    monkeypatch.setattr(ratelimit.settings, "trusted_proxies", ["10.0.0.0/8"])
    xff = "1.2.3.4, 203.0.113.7, 10.0.0.2"   # "1.2.3.4" is spoofed by the client 203.0.113.7
    assert ratelimit.client_ip(_req({"x-forwarded-for": xff}, peer="10.0.0.1")) == "203.0.113.7"
    # Every hop trusted: the leftmost one is the origin.
    assert ratelimit.client_ip(_req({"x-forwarded-for": "10.0.0.3, 10.0.0.2"}, peer="10.0.0.1")) == "10.0.0.3"


def test_free_text_never_becomes_a_throttle_key(monkeypatch):
    """A header value that is not an address is discarded rather than used as a key — otherwise
    an attacker sends fresh free text per attempt and bypasses the IP throttle entirely."""
    monkeypatch.setattr(ratelimit.settings, "trusted_proxies", ["10.0.0.0/8"])
    assert ratelimit.client_ip(_req({"cf-connecting-ip": "not-an-ip"}, peer="10.0.0.9")) == "10.0.0.9"
    long = ratelimit.client_ip(_req({"cf-connecting-ip": "A" * 5000}, peer="10.0.0.9"))
    assert long == "10.0.0.9"
    assert ratelimit.client_ip(_req({"x-forwarded-for": "A" * 5000}, peer="10.0.0.9")) == "10.0.0.9"
    # A malformed hop ends the walk; what was established to its right stays.
    assert ratelimit.client_ip(_req({"x-forwarded-for": "junk, 203.0.113.7"}, peer="10.0.0.9")) == "203.0.113.7"
    assert ratelimit.client_ip(_req({"cf-connecting-ip": "junk",
                                     "x-forwarded-for": "198.51.100.4, 10.0.0.1"}, peer="10.0.0.9")) == "198.51.100.4"
    # A peer without an address (unix socket, test client) gets one shared key.
    assert ratelimit.client_ip(SimpleNamespace(headers={}, client=None)) == "unknown"


def test_a_trusted_proxy_can_be_named_by_host_name(monkeypatch):
    """The compose file trusts the bundled Caddy by its service name, because Docker assigns
    its address. The name is resolved at runtime; a name that does not resolve trusts nobody."""
    ratelimit._resolved.clear()
    calls = []

    def fake_getaddrinfo(name, *a, **kw):
        calls.append(name)
        if name == "console-web":
            return [(2, 1, 6, "", ("172.18.0.5", 0))]
        raise OSError("unknown host")

    monkeypatch.setattr(ratelimit.socket, "getaddrinfo", fake_getaddrinfo)
    monkeypatch.setattr(ratelimit.settings, "trusted_proxies", ["console-web", "gone-host"])
    hdr = {"x-forwarded-for": "203.0.113.7"}
    assert ratelimit.client_ip(_req(hdr, peer="172.18.0.5")) == "203.0.113.7"
    assert ratelimit.client_ip(_req(hdr, peer="172.18.0.6")) == "172.18.0.6"
    # Resolved once per name within the cache lifetime, not once per request.
    ratelimit.client_ip(_req(hdr, peer="172.18.0.5"))
    assert calls.count("console-web") == 1
    ratelimit._resolved.clear()
