"""The firewall helper in docker-compose.yml: opt-in, and with exactly the rights it needs.

`console-firewall` is the only service in the host's network namespace and the only one with an
added capability. If one of these lines changes, the trust boundary in `docs/security.md`
changes with it. Read as text like `test_https_front` (no YAML parser in the test dependencies).
"""
from __future__ import annotations

import re
from pathlib import Path

DASHBOARD = Path(__file__).resolve().parents[2]
COMPOSE = (DASHBOARD / "docker-compose.yml").read_text(encoding="utf-8")
SECURITY = (DASHBOARD / "docs" / "security.md").read_text(encoding="utf-8")


def _service(name: str) -> str:
    start = COMPOSE.index(f"\n  {name}:\n")
    nxt = re.search(r"\n  [a-z][a-z0-9-]*:\n|\n[a-z]", COMPOSE[start + 1:])
    return COMPOSE[start:start + 1 + nxt.start()] if nxt else COMPOSE[start:]


def test_the_helper_exists_only_with_its_profile():
    assert "    profiles: [firewall]\n" in _service("console-firewall")


def test_the_helper_gets_net_admin_and_nothing_else():
    fw = _service("console-firewall")
    assert "    network_mode: host\n" in fw
    assert "    cap_drop: [ALL]\n" in fw and "    cap_add: [NET_ADMIN]\n" in fw
    assert 'security_opt: ["no-new-privileges:true"]' in fw and "    read_only: true\n" in fw
    assert "privileged" not in fw and "docker.sock" not in fw
    assert "ports:" not in fw, "the helper listens on a Unix socket only"


def test_no_other_service_is_in_the_host_network_or_has_added_capabilities():
    assert COMPOSE.count("network_mode:") == 1
    assert COMPOSE.count("cap_add:") == 1
    assert "privileged: true" not in COMPOSE


def test_the_api_and_only_the_api_shares_the_socket_volume():
    users = [name for name in re.findall(r"^  ([a-z][a-z0-9-]*):\n", COMPOSE, re.M)
             if "firewall_socket:" in _service(name)]
    assert users == ["console-api", "console-firewall"]


def test_the_token_is_optional_for_compose_and_mandatory_for_the_helper():
    """`${…:?}` would break `docker compose config` for everybody without the profile; the
    helper itself refuses to start without a token of 32 characters."""
    assert "GSS_FIREWALL_TOKEN: ${GSS_FIREWALL_TOKEN:-}" in _service("console-firewall")


def test_the_trust_boundary_is_written_down():
    section = SECURITY.split("## Firewall helper", 1)[1].split("\n## ", 1)[0]
    for fact in ("network_mode: host", "NET_ADMIN", "GSS-ALLOW", "DOCKER-USER", "GSS_FIREWALL_TOKEN",
                 "GSS_FIREWALL_PORT_RANGE", "plan"):
        assert fact in section, fact
