"""HTTPS by the bundled Caddy is opt-in, and switching it on changes nothing else.

The compose profile `https` adds `console-tls`, a Caddy that terminates TLS for DASHBOARD_DOMAIN
and hands every request to `console-web`. Without the profile the stack is the plain HTTP setup:
`console-web` on DASHBOARD_BIND:DASHBOARD_PORT, no port 80/443, no HSTS.

Read as text like `test_compose_settings` (no YAML parser in the test dependencies). The start
check of `console-tls` is real shell logic and runs here with `/bin/sh`.
"""
from __future__ import annotations

import os
import re
import subprocess
import textwrap
from pathlib import Path

from app.config import unknown_env_warnings

DASHBOARD = Path(__file__).resolve().parents[2]
COMPOSE = (DASHBOARD / "docker-compose.yml").read_text(encoding="utf-8")
EDGE = (DASHBOARD / "frontend" / "Caddyfile.https").read_text(encoding="utf-8")
PLAIN = (DASHBOARD / "frontend" / "Caddyfile").read_text(encoding="utf-8")
EXAMPLE = (DASHBOARD / ".env.example").read_text(encoding="utf-8")


def _service(name: str) -> str:
    """The text of one service block (up to the next service or top-level key)."""
    start = COMPOSE.index(f"\n  {name}:\n")
    nxt = re.search(r"\n  [a-z][a-z0-9-]*:\n|\n[a-z]", COMPOSE[start + 1:])
    return COMPOSE[start:start + 1 + nxt.start()] if nxt else COMPOSE[start:]


def _start_script() -> str:
    """The `sh -c` script of console-tls, as the container's shell receives it."""
    block = _service("console-tls")
    body = block.split("      - |\n", 1)[1].split("\n    volumes:", 1)[0]
    return textwrap.dedent(body).replace("$$", "$")


def _run_start_check(**env: str) -> subprocess.CompletedProcess[str]:
    script = _start_script().replace("exec caddy run ", "echo would run caddy #")
    clean = {k: v for k, v in os.environ.items()
             if k not in {"DASHBOARD_DOMAIN", "ACME_EMAIL", "ACME_CA"}}
    return subprocess.run(["/bin/sh", "-c", script], env={**clean, **env},
                          capture_output=True, text=True, timeout=10, check=False)


def test_the_https_front_exists_only_with_its_profile():
    tls = _service("console-tls")
    assert "    profiles: [https]\n" in tls
    # Every other service runs without a profile, so the plain setup is complete without it
    # (the one other optional service is the firewall helper, profile `firewall`).
    assert re.findall(r"^    profiles: \[(\w+)\]$", COMPOSE, re.M) == ["https", "firewall"]


def test_the_plain_front_is_unchanged():
    """Without the profile nothing but console-web is published, on the address it always had."""
    web = _service("console-web")
    assert re.findall(r'^      - "(.+)"$', web.split("    ports:\n", 1)[1].split("\n    depends_on")[0],
                      re.M) == ["${DASHBOARD_BIND:-127.0.0.1}:${DASHBOARD_PORT:-8080}:80"]
    assert "Strict-Transport-Security" not in PLAIN


def test_the_https_front_publishes_80_and_443_with_settable_host_ports():
    tls = _service("console-tls")
    assert '- "${DASHBOARD_HTTP_PORT:-80}:80"' in tls
    assert '- "${DASHBOARD_HTTPS_PORT:-443}:443"' in tls
    assert '- "${DASHBOARD_HTTPS_PORT:-443}:443/udp"' in tls
    assert "- ./frontend/Caddyfile.https:/etc/caddy/Caddyfile:ro" in tls
    # Certificates survive a re-creation of the container.
    assert "- tls_data:/data" in tls
    assert "networks: [console-front]" in tls


def test_the_edge_serves_the_domain_and_hands_everything_to_console_web():
    assert re.search(r"^\{\$DASHBOARD_DOMAIN\} \{$", EDGE, re.M)
    assert re.findall(r"reverse_proxy\s+(\S+)", EDGE) == ["console-web:80"]
    assert 'header Strict-Transport-Security "max-age=31536000"' in EDGE


def test_the_edge_believes_no_forwarded_header():
    """It faces the clients directly: a client's own X-Forwarded-For must not become its
    address. console-web believes the edge (private address), the API believes console-web."""
    assert not re.search(r"^\s*trusted_proxies\b", EDGE, re.M)
    assert re.search(r"^\s*trusted_proxies static", PLAIN, re.M)
    assert "TRUSTED_PROXIES: ${TRUSTED_PROXIES-console-web}" in _service("console-api")


def test_every_accepted_ca_has_a_tls_snippet():
    """`import tls_{$ACME_CA}` needs a snippet for each value the start check lets through."""
    snippets = set(re.findall(r"^\(tls_([a-z]+)\) \{$", EDGE, re.M))
    accepted = set(re.findall(r"^\s+([a-z]+)\)", _start_script(), re.M))
    assert snippets == accepted == {"acme", "internal"}
    assert "import tls_{$ACME_CA}" in EDGE
    assert "ACME_CA: ${ACME_CA:-acme}" in _service("console-tls")


def test_the_start_check_names_a_missing_domain():
    result = _run_start_check(ACME_EMAIL="admin@example.com", ACME_CA="acme")
    assert result.returncode == 1
    assert "DASHBOARD_DOMAIN is not set" in result.stderr


def test_the_start_check_needs_a_contact_address_for_a_public_ca_only():
    missing = _run_start_check(DASHBOARD_DOMAIN="dashboard.example.com", ACME_EMAIL="",
                               ACME_CA="acme")
    assert missing.returncode == 1
    assert "ACME_EMAIL is not set" in missing.stderr
    internal = _run_start_check(DASHBOARD_DOMAIN="console.test", ACME_EMAIL="",
                                ACME_CA="internal")
    assert (internal.returncode, internal.stdout.strip()) == (0, "would run caddy")
    public = _run_start_check(DASHBOARD_DOMAIN="dashboard.example.com",
                              ACME_EMAIL="admin@example.com", ACME_CA="acme")
    assert (public.returncode, public.stdout.strip()) == (0, "would run caddy")


def test_the_start_check_rejects_an_unknown_ca():
    result = _run_start_check(DASHBOARD_DOMAIN="dashboard.example.com",
                              ACME_EMAIL="admin@example.com", ACME_CA="letsencrypt")
    assert result.returncode == 1
    assert "ACME_CA must be acme or internal, not 'letsencrypt'" in result.stderr


def test_the_https_settings_are_documented_and_known():
    """The block in .env.example names every switch, and none of them is reported as a typo."""
    documented = set(re.findall(r"^#?\s*([A-Z][A-Z0-9_]*)=", EXAMPLE, flags=re.M))
    names = {"COMPOSE_PROFILES", "DASHBOARD_DOMAIN", "ACME_EMAIL", "ACME_CA",
             "DASHBOARD_HTTP_PORT", "DASHBOARD_HTTPS_PORT"}
    assert names <= documented
    assert "# COMPOSE_PROFILES=https\n" in EXAMPLE
    assert unknown_env_warnings(names) == []
