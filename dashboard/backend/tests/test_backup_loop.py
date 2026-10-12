"""The nightly database dump (`ops/backup-loop.sh`) keeps a dump of a fresh install.

The script once told a worthless dump from a real one by size alone (50 kB of gzip). That floor
was sized for a database in use: on a fresh install the dump is a few kB, so every dump was
discarded until the database had grown — the first weeks had no database backup at all.

What makes a dump worthless is not its size but a database without the application's tables,
so the script now looks for one of them (`users`) in the dump.

These tests run the real script with a fake `pg_dump` and a `sleep` that ends the loop: the
script takes its catch-up dump at start, reaches the `sleep` before the next run and stops there.
"""
from __future__ import annotations

import gzip
import hashlib
import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "ops" / "backup-loop.sh"

pytestmark = pytest.mark.skipif(
    not all(shutil.which(tool) for tool in ("sh", "gzip", "find", "grep", "sed")),
    reason="needs a POSIX shell with gzip, find, grep and sed",
)

# What `pg_dump --clean --if-exists` writes for a database nobody has created a table in.
WITHOUT_SCHEMA = """\
--
-- PostgreSQL database dump
--

SET statement_timeout = 0;
SET client_encoding = 'UTF8';
SELECT pg_catalog.set_config('search_path', '', false);

--
-- PostgreSQL database dump complete
--
"""

USERS = """\
CREATE TABLE public.users (
    id integer NOT NULL,
    username character varying NOT NULL
);

COPY public.users (id, username) FROM stdin;
1\tadmin
\\.

"""


def _with_schema() -> str:
    """A dump with the application's tables — a few kB of gzip, like the one of a fresh install
    (measured against PostgreSQL 16 with the schema `init_db()` creates: about 7 kB)."""
    other = "".join(
        f"CREATE TABLE public.table_{n} (\n    id integer NOT NULL,\n"
        f"    value_{hashlib.sha256(str(n).encode()).hexdigest()} text\n);\n\n"
        for n in range(40)
    )
    return WITHOUT_SCHEMA.replace("--\n-- PostgreSQL database dump complete", USERS + other + "--\n-- PostgreSQL database dump complete")


def _run(tmp_path: Path, dump: str, *, pg_dump_status: int = 0, env: dict | None = None,
          sleep_script: str = "#!/bin/sh\nexit 3\n"):
    """Run the script once. Returns `(dump files kept, output)`."""
    tool_dir = tmp_path / "bin"
    tool_dir.mkdir()
    (tmp_path / "dump.sql").write_text(dump, encoding="utf-8")
    (tool_dir / "pg_dump").write_text(
        f'#!/bin/sh\ncat "{tmp_path}/dump.sql"\nexit {pg_dump_status}\n', encoding="utf-8"
    )
    # The script sleeps until the next scheduled run; a failing `sleep` ends it there (`set -e`).
    (tool_dir / "sleep").write_text(sleep_script, encoding="utf-8")
    for name in ("pg_dump", "sleep"):
        (tool_dir / name).chmod(stat.S_IRWXU)
    target = tmp_path / "backups"
    result = subprocess.run(
        ["sh", str(SCRIPT)],
        env={"PATH": f"{tool_dir}{os.pathsep}{os.environ['PATH']}", "BACKUP_DIR": str(target), **(env or {})},
        capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 3, f"the script did not reach the sleep:\n{result.stdout}\n{result.stderr}"
    assert not list(target.glob("*.part")), "a working file was left behind"
    return sorted(target.glob("*.sql.gz")), result.stdout


def test_the_dump_of_a_fresh_install_is_kept(tmp_path):
    dump = _with_schema()
    assert 1_000 < len(gzip.compress(dump.encode())) < 50_000, "the example must be smaller than the old floor"

    files, output = _run(tmp_path, dump)

    assert len(files) == 1, output
    assert gzip.decompress(files[0].read_bytes()).decode() == dump
    assert "ok: stacks-console-" in output


def test_a_dump_without_the_schema_is_discarded_and_says_why(tmp_path):
    # Padded far beyond any size floor: the size must not be what decides.
    dump = WITHOUT_SCHEMA + "".join(f"-- {n} {os.urandom(24).hex()}\n" for n in range(4000))
    assert len(gzip.compress(dump.encode())) > 50_000

    files, output = _run(tmp_path, dump)

    assert files == []
    assert "no table 'users'" in output


def test_a_table_that_only_contains_the_name_does_not_count(tmp_path):
    dump = _with_schema().replace("CREATE TABLE public.users (", "CREATE TABLE public.users_archive (")

    files, output = _run(tmp_path, dump)

    assert files == []
    assert "no table 'users'" in output


def test_the_required_table_can_be_changed_or_switched_off(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    dump = _with_schema()

    files, _ = _run(tmp_path / "a", dump, env={"BACKUP_REQUIRE_TABLE": "table_7"})
    assert len(files) == 1

    without_users = dump.replace("public.users", "public.people")
    files, _ = _run(tmp_path / "b", without_users, env={"BACKUP_REQUIRE_TABLE": ""})
    assert len(files) == 1


def test_a_tiny_file_is_discarded_even_without_the_table_check(tmp_path):
    files, output = _run(tmp_path, WITHOUT_SCHEMA, env={"BACKUP_REQUIRE_TABLE": ""})

    assert files == []
    assert "is not a database dump" in output


def test_a_failed_pg_dump_leaves_no_file(tmp_path):
    files, output = _run(tmp_path, _with_schema(), pg_dump_status=1)

    assert files == []
    assert "ERROR pg_dump" in output


def test_the_dump_at_start_waits_for_the_backend_to_create_its_tables(tmp_path):
    """On a first start the sidecar is up before the schema exists. It tries again shortly
    instead of leaving the installation without a dump until the scheduled time."""
    (tmp_path / "later.sql").write_text(_with_schema(), encoding="utf-8")
    # The wait between two attempts (7 s here) is when the backend creates the schema;
    # any other sleep is the one before the scheduled run and ends the script.
    fake_sleep = (
        "#!/bin/sh\n"
        f'if [ "$1" = 7 ]; then cp "{tmp_path}/later.sql" "{tmp_path}/dump.sql"; exit 0; fi\n'
        "exit 3\n"
    )

    files, output = _run(tmp_path, WITHOUT_SCHEMA, env={"BACKUP_CATCHUP_WAIT": "7"}, sleep_script=fake_sleep)

    assert "attempt 1 of 10" in output
    assert len(files) == 1, output
    assert "CREATE TABLE public.users (" in gzip.decompress(files[0].read_bytes()).decode()


def test_the_dump_at_start_gives_up_after_the_configured_attempts(tmp_path):
    fake_sleep = '#!/bin/sh\nif [ "$1" = 7 ]; then exit 0; fi\nexit 3\n'

    files, output = _run(
        tmp_path, WITHOUT_SCHEMA, env={"BACKUP_CATCHUP_WAIT": "7", "BACKUP_CATCHUP_TRIES": "3"}, sleep_script=fake_sleep
    )

    assert files == []
    assert "attempt 2 of 3" in output and "attempt 3 of 3" not in output
    assert "trying again at the scheduled time" in output


# --- Schedule, target and owner come from the .env --------------------------------------------

COMPOSE = SCRIPT.parents[1] / "docker-compose.yml"


def test_the_schedule_and_retention_come_from_the_environment(tmp_path):
    files, output = _run(tmp_path, _with_schema(),
                         env={"BACKUP_HOUR": "5", "BACKUP_MINUTE": "7", "BACKUP_KEEP_DAYS": "3"})

    assert len(files) == 1, output
    assert "daily at 5:07" in output and "kept for 3 days" in output


def test_the_compose_file_takes_schedule_paths_and_owner_from_the_env_file():
    """The sidecar's settings are `.env` settings (`DB_BACKUP_*`, `DB_DATA_DIR`) with the old
    fixed values as defaults — not literals in the compose file."""
    compose = COMPOSE.read_text(encoding="utf-8")
    for line in (
        "BACKUP_HOUR: ${DB_BACKUP_HOUR:-2}",
        "BACKUP_MINUTE: ${DB_BACKUP_MINUTE:-30}",
        "BACKUP_KEEP_DAYS: ${DB_BACKUP_KEEP_DAYS:-30}",
        "BACKUP_OWNER: ${DB_BACKUP_OWNER:-1000:1000}",
        "- ${DB_BACKUP_DIR:-./db-backups}:/backups",
        "- ${DB_DATA_DIR:-./data/db}:/var/lib/postgresql/data",
    ):
        assert line in compose, line


def _tools(tmp_path: Path, scripts: dict[str, str]) -> Path:
    tool_dir = tmp_path / "bin"
    tool_dir.mkdir()
    for name, body in scripts.items():
        (tool_dir / name).write_text(body, encoding="utf-8")
        (tool_dir / name).chmod(stat.S_IRWXU)
    return tool_dir


def test_started_as_root_it_hands_the_directory_to_the_owner_and_drops_to_it(tmp_path):
    """Docker creates a missing `./db-backups` as root. The script, started as root, gives that
    directory to BACKUP_OWNER and re-executes itself as that owner, so neither the directory nor
    the dumps end up root-owned. Root is faked here (`id`, `stat`), the tools record their calls."""
    log = tmp_path / "calls"
    tool_dir = _tools(tmp_path, {
        "id": '#!/bin/sh\n[ "$1" = -u ] && echo 0 || echo 0\n',
        "stat": "#!/bin/sh\necho 0\n",
        "chown": f'#!/bin/sh\necho "chown $*" >>"{log}"\n',
        "su-exec": f'#!/bin/sh\necho "su-exec $1 $2 $3 AS_OWNER=$BACKUP_AS_OWNER" >>"{log}"\nexit 0\n',
    })
    target = tmp_path / "backups"
    result = subprocess.run(
        ["sh", str(SCRIPT)],
        env={"PATH": f"{tool_dir}{os.pathsep}{os.environ['PATH']}", "BACKUP_DIR": str(target),
             "BACKUP_OWNER": "1234:5678"},
        capture_output=True, text=True, timeout=60,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert log.read_text(encoding="utf-8").splitlines() == [
        f"chown 1234:5678 {target}",
        f"su-exec 1234:5678 /bin/sh {SCRIPT} AS_OWNER=1",
    ]
    assert target.is_dir()


def test_a_directory_root_does_not_own_is_not_taken_over(tmp_path):
    log = tmp_path / "calls"
    tool_dir = _tools(tmp_path, {
        "id": "#!/bin/sh\necho 0\n",
        "stat": "#!/bin/sh\necho 1000\n",
        "chown": f'#!/bin/sh\necho "chown $*" >>"{log}"\n',
        "su-exec": f'#!/bin/sh\necho "su-exec $1" >>"{log}"\nexit 0\n',
    })
    result = subprocess.run(
        ["sh", str(SCRIPT)],
        env={"PATH": f"{tool_dir}{os.pathsep}{os.environ['PATH']}",
             "BACKUP_DIR": str(tmp_path / "backups"), "BACKUP_OWNER": "1234:5678"},
        capture_output=True, text=True, timeout=60,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert log.read_text(encoding="utf-8").splitlines() == ["su-exec 1234:5678"]


@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0, reason="root can write anywhere")
def test_an_unwritable_directory_stops_the_script_with_the_fix(tmp_path):
    target = tmp_path / "backups"
    target.mkdir()
    target.chmod(stat.S_IRUSR | stat.S_IXUSR)
    try:
        result = subprocess.run(
            ["sh", str(SCRIPT)],
            env={"PATH": os.environ["PATH"], "BACKUP_DIR": str(target)},
            capture_output=True, text=True, timeout=60,
        )
    finally:
        target.chmod(stat.S_IRWXU)

    assert result.returncode == 1
    assert f"cannot write to {target}" in result.stdout
    assert "sudo chown" in result.stdout
