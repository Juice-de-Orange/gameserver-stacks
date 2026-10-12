"""What a stranger may need to change in docker-compose.yml is a `.env` setting, not a literal.

Read as text on purpose (no YAML parser in the test dependencies); each assertion names the line
it expects, so a failure says what changed.
"""
from __future__ import annotations

import re
from pathlib import Path

DASHBOARD = Path(__file__).resolve().parents[2]
COMPOSE = (DASHBOARD / "docker-compose.yml").read_text(encoding="utf-8")


def _service(name: str) -> str:
    """The text of one service block (up to the next service or top-level key)."""
    start = COMPOSE.index(f"\n  {name}:\n")
    nxt = re.search(r"\n  [a-z][a-z0-9-]*:\n|\n[a-z]", COMPOSE[start + 1:])
    return COMPOSE[start:start + 1 + nxt.start()] if nxt else COMPOSE[start:]


def test_the_console_images_are_pulled_and_can_still_be_built():
    """`docker compose up -d` pulls the release images; `--build` builds the same names here."""
    for service, context in (("console-api", "./backend"), ("console-web", "./frontend")):
        block = _service(service)
        suffix = service.removeprefix("console-")
        assert f"image: ghcr.io/juice-de-orange/stacks-console-{suffix}:${{GSS_VERSION:-latest}}" in block
        assert re.search(rf"build:\s*(\n\s+context:\s*)?{re.escape(context)}\b", block), block


def test_the_dashboard_address_and_port_are_settings():
    assert '- "${DASHBOARD_BIND:-127.0.0.1}:${DASHBOARD_PORT:-8080}:80"' in _service("console-web")


def test_the_stack_reaches_its_services_by_service_name():
    """Defaults that name another container use the service name, which Docker resolves in any
    compose project (`container_name` is gone, see `test_naming_rule`)."""
    api = _service("console-api")
    assert "POSTGRES_HOST: console-db" in api
    assert "DOCKER_PROXY_URL: http://console-dockerproxy:2375" in api
    assert "TRUSTED_PROXIES: ${TRUSTED_PROXIES-console-web}" in api
    assert "PGHOST: console-db" in _service("console-backup")
    caddy = (DASHBOARD / "frontend" / "Caddyfile").read_text(encoding="utf-8")
    assert re.findall(r"reverse_proxy\s+(\S+)", caddy) == ["console-api:8000"]


def test_the_override_example_adds_a_root_on_both_sides():
    """An extra data root needs a same-path bind mount AND an allowlist entry that keeps
    DATA_ROOT — otherwise servers under DATA_ROOT would be refused."""
    override = (DASHBOARD / "docker-compose.override.example.yml").read_text(encoding="utf-8")
    assert "- /mnt/games:/mnt/games" in override
    assert "ALLOWED_DATA_ROOTS: ${DATA_ROOT:-/srv/gameservers},/mnt/games" in override


def test_caddy_stays_off_the_game_network():
    """GAME_NETWORK has a fixed name. Two installations that share it by mistake (Compose only
    warns) would each find two `console-api`s there; Caddy reaches the API on the project's own
    `console-front` network instead."""
    assert "networks: [console-front]" in _service("console-web")
    assert "networks: [console-internal, console-front, gameserver-net]" in _service("console-api")


def test_the_api_has_a_healthcheck_on_its_health_route():
    """`docker compose up -d --wait` (systemd unit, migration runbook) waits for the API's start-up
    migrations only if console-api has a healthcheck; without one the CLI ran against an old schema."""
    api = _service("console-api")
    assert "healthcheck:" in api
    assert "http://127.0.0.1:8000/api/health" in api
    assert ".get('db')" in api
    # start_interval needs Docker Engine 25; the stack supports older engines.
    assert "start_interval" not in api
