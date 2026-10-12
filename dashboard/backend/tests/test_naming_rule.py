"""`pz` means Project Zomboid, and nothing else — a structural guard.

The dashboard started as a single-game tool for Project Zomboid, and "pz" used to sit in every
place where "any game server" was meant: containers, the network, the stack itself. That is how
such names creep back in — with the next compose file, the next helper container.

The rule:

    `pz` names the game Project Zomboid only (driver, game type, a server key, its passwords).
    Everything that means the platform carries the `console-` prefix (containers, networks) or
    the `GSS_` prefix (environment variables read by the platform's own tooling).

**Honest limit:** the guard checks *infrastructure names*, not every occurrence of "pz". A
comment that talks about "PZ" is fine — the driver is called that, after all.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent   # dashboard/

# Platform names that once carried the game's prefix. Built from parts on purpose: the guard
# scans this file too, and a literal list would make it report itself.
_PZ = "pz"
_SUFFIXES = {
    "-api": "console-api",
    "-web": "console-web",
    "-db": "console-db",
    "-dockerproxy": "console-dockerproxy",
    "-internal": "console-internal",
    "-backup": "console-backup",
    "-dashboard": "stacks-console",
    "-graceful": "the systemd example unit",
    "net": "gameserver-net",
    "dash": "stacks",
}
FORBIDDEN = {_PZ + suffix: new for suffix, new in _SUFFIXES.items()}
# Not only the container carries the name, the variable that overrides it does too.
# `PZ_SERVER_PW` stays correct — that is the password of the game server.
FORBIDDEN[_PZ.upper() + "_API_CONTAINER"] = "GSS_API_CONTAINER"
FORBIDDEN[_PZ.upper() + "_GRACEFUL_RECORD"] = "GSS_GRACEFUL_RECORD"


def finds(name: str, text: str) -> bool:
    r"""The ONE place that decides whether an old name occurs in `text`.

    The left boundary is plain `(?<!\w)`: anything glued to a hyphen on the left counts, so the
    shell default form `${VAR:-name}` and a mixed form like `something-<name>` are both found.
    The right boundary excludes longer names (`<name>-x`, `<name>x`).
    """
    return re.search(rf"(?<!\w){re.escape(name)}(?![\w-])", text) is not None


FILE_SUFFIXES = {".py", ".ts", ".tsx", ".yml", ".yaml", ".sh", ".md", ".service", ".example", ".json", ".html"}
# Files WITHOUT a suffix that still carry names — a Caddyfile that proxies `/api` to a container
# that no longer exists answers 502 on every API route while `/` still returns 200.
SUFFIXLESS_NAMES = {"Caddyfile", "Dockerfile", "Makefile"}
SKIP = {".git", "node_modules", ".venv-test", "__pycache__", ".ruff_cache", ".pytest_cache", "dist"}


def _files():
    for p in sorted(ROOT.rglob("*")):
        if not p.is_file() or any(t in p.parts for t in SKIP):
            continue
        if p.suffix not in FILE_SUFFIXES and p.name not in SUFFIXLESS_NAMES:
            continue
        if p.name == "package-lock.json":
            continue
        yield p.relative_to(ROOT).as_posix(), p


def test_the_guard_sees_the_files_that_matter():
    """Without this, a wrong root would turn the guard into a test that checks nothing."""
    seen = {rel for rel, _ in _files()}
    for required in ("docker-compose.yml", ".env.example", "frontend/Caddyfile", "backend/Dockerfile",
                 "backend/app/cli.py", "ops/backup-loop.sh"):
        assert required in seen, f"{required} is not checked"


def test_no_platform_name_still_carries_pz():
    violations: list[str] = []
    for rel, p in _files():
        try:
            text = p.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for line_no, line in enumerate(text.splitlines(), 1):
            for old, new in FORBIDDEN.items():
                if finds(old, line):
                    violations.append(f"{rel}:{line_no} → '{old}' (use '{new}')")
    assert not violations, (
        "These places still carry a platform name with the game's prefix. 'pz' names the game\n"
        "Project Zomboid only; platform names use the `console-` / `GSS_` prefix:\n  "
        + "\n  ".join(violations)
    )


def test_the_real_pz_names_stay_untouched():
    """Counter-check: the guard must NOT report everything with 'pz'.

    `pz-1` is a server key of a Project Zomboid server, `PZDriver` and `drivers/pz.py` are the
    driver, `PZ_SERVER_PW` is the password of THAT server. Renaming those would break the rule
    instead of enforcing it.
    """
    examples = [
        "server = servers_registry.get_by_key('pz-1')",
        "from app.services.drivers.pz import PZDriver",
        "if server.game_type == GameType.pz:",
        "password_env = 'PZ_SERVER_PW'",
        "rcon_password_env = 'PZ_RCON_PW'",
        "key = 'pz-test'",
    ]
    for line in examples:
        for old in FORBIDDEN:
            assert not finds(old, line), (
                f"The guard reports '{old}' in a legitimate Project Zomboid line: {line!r}"
            )


def test_the_guard_would_find_the_old_name():
    """Without this proof it would not be shown that the guard checks anything at all."""
    api, net = _PZ + "-api", _PZ + "net"
    variable = _PZ.upper() + "_API_CONTAINER"
    lines = [
        f"    image: {api}",                       # hit
        f"    networks: [{net}]",                  # hit
        "  stacks-console/backend",                # NO hit
        "  key: pz-1",                             # NO hit (legitimate)
        f'CONTAINER="${{{variable}:-{api}}}"',     # two hits — the shell default form, where the
                                                   # `-` of `:-` sits directly before the name
        f"  image: stacks-console-{api}",          # hit — a mixed form of new prefix and old core
    ]
    hits = [(nr, old) for nr, line in enumerate(lines, 1) for old in FORBIDDEN if finds(old, line)]
    assert sorted(hits) == [
        (1, api),
        (2, net),
        (5, variable),
        (5, api),
        (6, api),
    ], f"unexpected: {hits}"


def _compose() -> str:
    return (ROOT / "docker-compose.yml").read_text(encoding="utf-8")


def test_every_platform_service_is_called_console():
    """The positive half of the rule: services carry the `console-` prefix.

    No service pins a container name: a fixed `container_name` exists once per host, so a second
    installation could not start next to the first. The containers get `<project>-<service>-1`,
    and everything in the stack addresses the others by service name."""
    text = _compose()
    block = text[text.index("\nservices:"):text.index("\nnetworks:")]
    services = re.findall(r"^  ([A-Za-z0-9_-]+):\s*$", block, flags=re.M)
    assert services, "no service found — the compose file is not being read correctly"
    assert all(d.startswith("console-") for d in services), services
    container = re.findall(r"^\s+container_name:\s*(\S+)", block, flags=re.M)
    assert container == [], "a fixed container name keeps two installations off one host"


def test_the_reverse_proxy_knows_the_api_service():
    """The Caddyfile must proxy to a service that exists in the compose file."""
    caddy = (ROOT / "frontend" / "Caddyfile").read_text(encoding="utf-8")
    targets = re.findall(r"reverse_proxy\s+([A-Za-z0-9_-]+):\d+", caddy)
    assert targets == ["console-api"], targets
    assert re.search(r"^  console-api:\s*$", _compose(), flags=re.M)


def test_the_platform_environment_variables_are_called_gss():
    """`app.cli` reads its own settings from `GSS_*`, never from a game-prefixed variable."""
    cli = (ROOT / "backend" / "app" / "cli.py").read_text(encoding="utf-8")
    names = set(re.findall(r"os\.environ\.get\(\s*\"([A-Z0-9_]+)\"", cli))
    assert "GSS_GRACEFUL_RECORD" in names
    assert all(n.startswith("GSS_") for n in names), names
