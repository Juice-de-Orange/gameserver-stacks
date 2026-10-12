"""A script must not address a container that does not exist.

The failure this guards against: a host-side script (for example the graceful stop/start unit)
keeps calling `docker exec <old name> …` after a container was renamed. Its effect is silent:

* `stop`: the readiness check fails, the script exits with 0 **without stopping a single game
  server** — dockerd then kills them with its own 15 s, and Project Zomboid falls back to the
  last autosave. Exactly the damage such a unit is built to prevent.
* `start`: after waiting, "nothing is started" — the servers do not come back after a reboot.

Nobody notices, because such a script only runs on a host reboot.

The naming guard (`test_naming_rule.py`) asks whether a name is *old*; this one asks whether
it **exists**. A future rename without an old name only shows up here.

**Scope:** every shell script of the repository, not only `dashboard/ops/`.

**What it does NOT see, stated explicitly:** a container name that only exists at runtime (from
the registry, from a `.env` on the host), and `docker exec` with options before the name
(`docker exec -e X=1 name …`).

The truth about the container names is the repository's `docker-compose*.yml` files
(`container_name:`) — the same files the containers are really created from. Since no service
pins a name any more (the dashboard's containers are `<project>-<service>-1`), that set is empty
and every `docker exec <name>` with a fixed name is reported: a host-side script addresses a
stack service with `docker compose exec <service>` in the stack's directory.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).parent.parent.parent.parent      # repo root
SKIP = {".git", "node_modules", ".venv-test", "__pycache__", ".ruff_cache", ".pytest_cache", "dist"}

# `NAME="${OTHER:-value}"` and the same form without quotes.
ASSIGNMENT = re.compile(r'^\s*([A-Z_][A-Z0-9_]*)="?\$\{[A-Z_][A-Z0-9_]*:-([^}"]*)\}"?\s*$')
# The TARGET of a docker call: `docker exec "$VAR" …` or `docker stop name …`.
# Deliberately the target and not the line: `"$SSH" "$HOST" "docker compose up"` names two
# variables on a line with `docker`, and neither is a container.
DOCKER_TARGET = re.compile(
    r'docker\s+(?:exec|stop|start|restart|kill|inspect)\s+"?(\$\{?[A-Za-z_]\w*\}?|[A-Za-z][\w.-]*)"?'
)


def _files(pattern: str):
    for p in sorted(ROOT.rglob(pattern)):
        if p.is_file() and not any(t in p.parts for t in SKIP):
            yield p


def container_names() -> set[str]:
    names: set[str] = set()
    for p in _files("docker-compose*.yml"):
        names |= set(re.findall(r"^\s*container_name:\s*(\S+)\s*$", p.read_text(encoding="utf-8"), re.M))
    return names


def _missing(text: str, known: set[str]) -> list[tuple[int, str, str]]:
    """Every container name attached to a docker call that is not a container."""
    lines = text.splitlines()

    # Collect all assignments — deliberately a LIST, not a dict: if a script sets the same name in
    # two branches, both must be checked, otherwise the one read last covers the other.
    assignments: list[tuple[int, str, str]] = []
    for nr, text_line in enumerate(lines, 1):
        if m := ASSIGNMENT.match(text_line):
            assignments.append((nr, m.group(1), m.group(2)))

    hits: list[tuple[int, str, str]] = []
    seen: set[tuple[int, str, str]] = set()
    for line_no, text_line in enumerate(lines, 1):
        for target in DOCKER_TARGET.findall(text_line):
            if target.startswith("$"):
                var = target.strip("${}")
                for nr, name, value in assignments:
                    if name == var and value not in known and (nr, name, value) not in seen:
                        seen.add((nr, name, value))
                        hits.append((nr, name, value))
            elif target not in known and (line_no, "docker", target) not in seen:
                seen.add((line_no, "docker", target))
                hits.append((line_no, "docker", target))
    return sorted(hits)


def test_every_container_in_the_scripts_exists():
    assert list(_files("docker-compose*.yml")), "no docker-compose*.yml found — the root is wrong"
    assert list(_files("*.sh")), "no shell script found — then this guard checks nothing"
    known = container_names()

    violations: list[str] = []
    for p in _files("*.sh"):
        rel = p.relative_to(ROOT).as_posix()
        for nr, who, value in _missing(p.read_text(encoding="utf-8"), known):
            violations.append(f"{rel}:{nr} → {who} points to '{value}', which does not exist")

    assert not violations, (
        "These scripts address a container that no docker-compose.yml creates.\n"
        "They fail silently — an ops script runs only on reboot, and then nobody is watching:\n  "
        + "\n  ".join(violations)
        + "\n\nKnown containers: " + ", ".join(sorted(known))
    )


def test_the_guard_would_have_found_the_defect():
    """Cross-check against the real text before the fix — without it, it is not shown that it bites."""
    before = (
        'set -u\n'
        'CONTAINER="${OLD_API_CONTAINER:-old-api}"\n'
        'api_ready() {\n'
        '  docker exec "$CONTAINER" python -c "import app.services.lifecycle"\n'
        '}\n'
    )
    assert _missing(before, {"console-api", "console-db"}) == [(2, "CONTAINER", "old-api")]

    after = before.replace("${OLD_API_CONTAINER:-old-api}", "${GSS_API_CONTAINER:-console-api}")
    assert _missing(after, {"console-api", "console-db"}) == []


def test_the_form_without_quotes_counts_too():
    unquoted = 'CONTAINER=${API:-old-api}\ndocker exec $CONTAINER true\n'
    assert _missing(unquoted, {"console-api"}) == [(1, "CONTAINER", "old-api")]


def test_a_literally_written_name_counts_too():
    literal = 'docker exec old-api python -m app.cli stop-all\n'
    assert _missing(literal, {"console-api"}) == [(1, "docker", "old-api")]
    assert _missing(literal.replace("old-api", "console-api"), {"console-api"}) == []


def test_only_the_TARGET_of_a_docker_call_counts():
    """Two false alarms a line-based check would produce.

    Asking "is the variable on a line with `docker`?" reports the SSH alias and the ssh program
    in a deploy script as dead containers, because `"$SSH" "$HOST" "docker compose up -d"` names
    both on a docker line. Without a right boundary `$DIRECTORY` would also count as a use of
    `$DIR`. So the TARGET of the call is checked, not the line."""
    confusable = (
        'DIR="${D:-/opt/backups}"\n'
        'docker exec "$DIRECTORY" true\n'
        'HOST="${DEPLOY_HOST:-myhost}"\n'
        'SSH="${SSH_BIN:-ssh}"\n'
        '"$SSH" "$HOST" "docker compose up -d --build"\n'
    )
    assert _missing(confusable, {"console-api"}) == []


def test_both_branches_are_checked():
    """A dict would swallow the first branch — only the last one would be checked."""
    two_branches = (
        'if [ "$X" ]; then\n'
        'CONTAINER="${A:-old-api}"\n'
        'else\n'
        'CONTAINER="${A:-console-api}"\n'
        'fi\n'
        'docker exec "$CONTAINER" true\n'
    )
    assert _missing(two_branches, {"console-api"}) == [(2, "CONTAINER", "old-api")]


def test_the_guard_reports_nothing_without_a_docker_call():
    """Cross-check the other way round: a default that is no container at all is no violation."""
    harmless = 'TARGET="${BACKUP_DIR:-/opt/backups}"\nmkdir -p "$TARGET"\n'
    assert _missing(harmless, {"console-api"}) == []
