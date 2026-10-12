"""The Content-Security-Policy and the files it depends on must not drift apart.

The policy in `frontend/Caddyfile` forbids inline scripts, inlined fonts and credentials in
WebSocket URLs only as long as the frontend does not need them. Each test here reads both sides,
so that re-adding an inline script (or loosening the policy to make one work) fails in the
standard run rather than in a browser.
"""
import re
from pathlib import Path

FRONTEND = Path(__file__).resolve().parents[2] / "frontend"


def _csp() -> dict[str, list[str]]:
    caddy = (FRONTEND / "Caddyfile").read_text(encoding="utf-8")
    found = re.search(r'Content-Security-Policy\s+"([^"]+)"', caddy)
    assert found, "the Caddyfile sets no Content-Security-Policy"
    return {
        parts[0]: parts[1:]
        for parts in (directive.split() for directive in found.group(1).split(";"))
        if parts
    }


def test_the_policy_allows_no_inline_code_and_no_foreign_origin():
    csp = _csp()
    assert csp["default-src"] == ["'self'"]
    assert csp["script-src"] == ["'self'"]
    assert csp["frame-ancestors"] == ["'none'"]
    assert csp["object-src"] == ["'none'"]
    for directive, sources in csp.items():
        for source in sources:
            assert source in ("'self'", "'none'", "data:"), f"{directive} allows {source}"
    assert [d for d, sources in csp.items() if "data:" in sources] == ["img-src"]


def test_the_other_security_headers_are_set():
    caddy = (FRONTEND / "Caddyfile").read_text(encoding="utf-8")
    assert re.search(r"^\s*X-Content-Type-Options\s+nosniff\s*$", caddy, flags=re.M)
    assert re.search(r"^\s*Referrer-Policy\s+no-referrer\s*$", caddy, flags=re.M)
    assert re.search(r"^\s*X-Frame-Options\s+DENY\s*$", caddy, flags=re.M)


def test_index_html_needs_nothing_the_policy_forbids():
    html = (FRONTEND / "index.html").read_text(encoding="utf-8")
    scripts = re.findall(r"<script\b([^>]*)>", html)
    assert scripts, "no script tag found — is this still the entry page?"
    for attrs in scripts:
        assert "src=" in attrs, f"inline <script{attrs}> — `script-src 'self'` blocks it"
    assert not re.search(r"\son[a-z]+\s*=", html), "inline event handler"
    assert "<style" not in html and not re.search(r"\sstyle\s*=", html), "inline style"
    assert not re.findall(r"""(?:src|href)=["']https?://""", html), "resource from another origin"
    # The theme bootstrap that used to be inline must exist as a file.
    assert (FRONTEND / "public" / "theme-init.js").is_file()


def test_fonts_are_not_inlined_into_the_stylesheet():
    """Vite inlines small assets as `data:` URIs by default; `font-src 'self'` blocks those."""
    config = (FRONTEND / "vite.config.ts").read_text(encoding="utf-8")
    assert re.search(r"assetsInlineLimit:\s*0\b", config)


def test_the_proxy_hands_the_api_one_validated_client_address():
    caddy = (FRONTEND / "Caddyfile").read_text(encoding="utf-8")
    assert re.search(r"header_up\s+X-Forwarded-For\s+\{client_ip\}", caddy)
    assert re.search(r"header_up\s+-CF-Connecting-IP", caddy)
    assert "trusted_proxies_strict" in caddy


def test_websocket_urls_carry_a_ticket_not_the_access_token():
    ws = (FRONTEND / "src" / "lib" / "ws.ts").read_text(encoding="utf-8")
    assert "/auth/ws-ticket" in ws and "ticket=" in ws
    assert "getAccess" not in ws and "token=" not in ws.replace("ticket=", "")
