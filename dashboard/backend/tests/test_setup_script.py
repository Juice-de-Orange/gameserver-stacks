"""`dashboard/setup` turns a fresh checkout into one that `docker compose up -d` starts.

The quick start used to be a `sed`/`openssl` block for four secrets plus
`sudo install -d -o 1000 -g 1000 /srv/gameservers`. The script replaces both, so it must be safe
to run on a checkout that is already in use: an existing `.env` is never replaced, a value that
is set is never changed, and the database password is not touched once the database exists.

These tests run the real script in a copy of `setup` + `.env.example` in a temporary directory.
"""
from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

from app.models.server import GameType

DASHBOARD = Path(__file__).resolve().parents[2]
SCRIPT = DASHBOARD / "setup"
SECRETS = {"POSTGRES_PASSWORD": 48, "JWT_SECRET": 64, "GSS_SECRET_KEY": 64, "SEED_ADMIN_PASSWORD": 24}

pytestmark = pytest.mark.skipif(
    not all(shutil.which(tool) for tool in ("sh", "awk", "sed", "od", "mktemp")),
    reason="needs a POSIX shell with awk, sed, od and mktemp",
)

MY_UID = os.getuid()
# As root the script keeps APP_UID and hands DATA_ROOT to it; otherwise APP_UID becomes the uid
# that owns the directory it created.
EXPECTED_UID = 1000 if MY_UID == 0 else MY_UID


@pytest.fixture
def checkout(tmp_path: Path) -> Path:
    d = tmp_path / "dashboard"
    d.mkdir()
    shutil.copy2(SCRIPT, d / "setup")
    shutil.copy2(DASHBOARD / ".env.example", d / ".env.example")
    return d


def _run(d: Path, *args: str, path: str | None = None, cwd: Path | None = None):
    return subprocess.run(
        ["sh", str(d / "setup"), *args],
        cwd=cwd or d, capture_output=True, text=True, timeout=60,
        env={"PATH": path or os.environ["PATH"], "HOME": str(d)},
    )


def _env(d: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in (d / ".env").read_text(encoding="utf-8").splitlines():
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            out[key] = value
    return out


def test_a_fresh_checkout_gets_every_secret_and_a_data_root(checkout):
    result = _run(checkout)
    assert result.returncode == 0, result.stderr

    env = _env(checkout)
    assert stat.S_IMODE((checkout / ".env").stat().st_mode) == 0o600
    for key, length in SECRETS.items():
        assert re.fullmatch(rf"[0-9a-f]{{{length}}}", env[key]), key
    assert len({env[k] for k in SECRETS}) == len(SECRETS), "two secrets are the same"

    data_root = checkout / "data" / "servers"
    assert env["DATA_ROOT"] == str(data_root.resolve())
    assert data_root.is_dir() and data_root.stat().st_uid == EXPECTED_UID
    assert env["APP_UID"] == str(EXPECTED_UID)

    # The one-time admin password is shown exactly once; no other secret is shown at all.
    assert result.stdout.count(env["SEED_ADMIN_PASSWORD"]) == 1
    for key in ("POSTGRES_PASSWORD", "JWT_SECRET", "GSS_SECRET_KEY"):
        assert env[key] not in result.stdout + result.stderr, key
    assert "http://127.0.0.1:8080" in result.stdout
    assert 'sign in as "admin"' in result.stdout


def test_everything_else_in_the_template_stays_as_it_was(checkout):
    assert _run(checkout).returncode == 0
    changed = {"DATA_ROOT", *SECRETS}
    if EXPECTED_UID != 1000:
        changed |= {"APP_UID", "DB_BACKUP_OWNER"}
    template = (checkout / ".env.example").read_text(encoding="utf-8").splitlines()
    written = (checkout / ".env").read_text(encoding="utf-8").splitlines()
    keep = lambda lines: [ln for ln in lines if ln.split("=", 1)[0].removeprefix("# ") not in changed]  # noqa: E731
    assert keep(written) == keep(template)


def test_a_second_run_changes_nothing_and_shows_no_password(checkout):
    assert _run(checkout).returncode == 0
    before = (checkout / ".env").read_bytes()
    password = _env(checkout)["SEED_ADMIN_PASSWORD"]

    again = _run(checkout)
    assert again.returncode == 0, again.stderr
    assert (checkout / ".env").read_bytes() == before
    assert password not in again.stdout
    assert "grep '^SEED_ADMIN_PASSWORD=' .env" in again.stdout
    assert not list(checkout.glob(".env.setup.*")), "a temporary file was left behind"


def test_an_existing_env_is_kept_and_only_the_placeholders_are_filled(checkout):
    template = (checkout / ".env.example").read_text(encoding="utf-8")
    mine = "my-own-jwt-secret-that-is-long-enough-0123456789"
    text = template.replace("JWT_SECRET=change-me-long-random", f"JWT_SECRET={mine}")
    text = text.replace("SEED_ADMIN_PASSWORD=change-me-shown-once", "SEED_ADMIN_PASSWORD=")
    (checkout / ".env").write_text(text + "MY_OWN_LINE=1\n", encoding="utf-8")
    (checkout / ".env").chmod(0o644)
    (checkout / "srv").mkdir()

    result = _run(checkout, "--data-root", str(checkout / "srv"))
    assert result.returncode == 0, result.stderr

    env = _env(checkout)
    assert env["JWT_SECRET"] == mine
    assert env["MY_OWN_LINE"] == "1"
    for key in ("POSTGRES_PASSWORD", "GSS_SECRET_KEY", "SEED_ADMIN_PASSWORD"):
        assert re.fullmatch(rf"[0-9a-f]{{{SECRETS[key]}}}", env[key]), key
    assert "JWT_SECRET" not in result.stdout.split("secrets", 1)[1].splitlines()[0]
    assert stat.S_IMODE((checkout / ".env").stat().st_mode) == 0o600, "the existing .env was left readable"


def test_the_database_password_is_left_alone_once_the_database_exists(checkout):
    (checkout / "data" / "db").mkdir(parents=True)

    result = _run(checkout)
    assert result.returncode == 0, result.stderr
    assert _env(checkout)["POSTGRES_PASSWORD"] == "change-me"
    assert "POSTGRES_PASSWORD is not set, but the database" in result.stderr


def test_data_root_games_and_port_from_the_options(checkout, tmp_path):
    result = _run(checkout, "--data-root", "games-data", "--games", "Minecraft, factorio",
                  "--port", "25110", cwd=tmp_path)
    assert result.returncode == 0, result.stderr

    env = _env(checkout)
    # A relative path is taken from where the script was called, and stored absolute.
    assert env["DATA_ROOT"] == str((tmp_path / "games-data").resolve())
    assert (tmp_path / "games-data").is_dir()
    assert env["GSS_GAMES"] == "minecraft,factorio"
    assert env["DASHBOARD_PORT"] == "25110"
    assert "http://127.0.0.1:25110" in result.stdout
    # Called from elsewhere, the start command says where to run it.
    assert f'cd "{checkout.resolve()}" && docker compose up -d' in result.stdout


@pytest.mark.parametrize("args, message", [
    (["--games", "minecraft,minecarft"], "'minecarft' is not a game"),
    (["--port", "80a"], "is not a port number"),
    (["--port", "70000"], "is not between 1 and 65535"),
    (["--data-root", "/srv/game servers"], "contains a character Docker Compose cannot take"),
    (["--data-root", "/srv/a:b"], "contains a character Docker Compose cannot take"),
    (["--force"], "unknown option: --force"),
])
def test_a_wrong_option_stops_before_anything_is_written(checkout, args, message):
    result = _run(checkout, *args)
    assert result.returncode != 0
    assert message in result.stderr
    assert not (checkout / ".env").exists()


def test_the_game_list_is_the_backends(checkout):
    text = (checkout / "setup").read_text(encoding="utf-8")
    known = re.search(r'^KNOWN_GAMES="([^"]*)"', text, re.M).group(1).split()
    assert sorted(known) == sorted(g.value for g in GameType)

    result = _run(checkout, "--games", "all")
    assert result.returncode == 0, result.stderr
    assert _env(checkout)["GSS_GAMES"] == "all"


def test_without_openssl_the_secrets_come_from_urandom(checkout, tmp_path):
    tools = tmp_path / "bin"
    tools.mkdir()
    (tools / "openssl").write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    (tools / "openssl").chmod(stat.S_IRWXU)

    result = _run(checkout, path=f"{tools}{os.pathsep}{os.environ['PATH']}")
    assert result.returncode == 0, result.stderr
    env = _env(checkout)
    for key, length in SECRETS.items():
        assert re.fullmatch(rf"[0-9a-f]{{{length}}}", env[key]), key


@pytest.mark.skipif(MY_UID == 0, reason="as root the script hands DATA_ROOT to APP_UID instead")
def test_another_uid_than_the_images_takes_over_app_uid_and_asks_for_a_build(checkout):
    # The prebuilt images run as 1000. Simulated with an APP_UID that is not ours.
    other = 4242 if MY_UID != 4242 else 4343
    template = (checkout / ".env.example").read_text(encoding="utf-8")
    (checkout / ".env").write_text(template.replace("APP_UID=1000", f"APP_UID={other}"), encoding="utf-8")

    result = _run(checkout, "--data-root", str(checkout / "servers"))
    assert result.returncode == 0, result.stderr
    env = _env(checkout)
    assert env["APP_UID"] == str(MY_UID)
    if MY_UID != 1000:
        assert "docker compose up -d --build" in result.stdout
        assert env["DB_BACKUP_OWNER"] == f"{MY_UID}:{os.getgid()}"


@pytest.mark.skipif(MY_UID == 0, reason="root can create any directory")
def test_a_data_root_that_cannot_be_created_is_a_clear_error(checkout, tmp_path):
    locked = tmp_path / "locked"
    locked.mkdir()
    locked.chmod(0o500)
    try:
        result = _run(checkout, "--data-root", str(locked / "servers"))
    finally:
        locked.chmod(0o700)
    assert result.returncode != 0
    assert "cannot create" in result.stderr and "--data-root" in result.stderr
