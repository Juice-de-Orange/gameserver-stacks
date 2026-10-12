"""A misspelt setting is reported at start instead of silently falling back to its default.

`extra="ignore"` keeps an unknown variable from aborting the start, and that stays so. But
`JWT_ACCESS_TTL_MN=…` used to vanish without a trace while the empty default took over. Names with one
of the project's prefixes (`config.CHECKED_ENV_PREFIXES`) that nobody reads now get one warning
each, with the closest known name when there is one.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings

from app import config
from app.config import Settings, setting_env_names, unknown_env_warnings

DASHBOARD = Path(__file__).resolve().parents[2]


def test_a_misspelt_variable_is_named_with_the_closest_known_one():
    assert unknown_env_warnings(["GSS_VERSOIN"]) == [
        "Unknown setting GSS_VERSOIN in the environment — it is ignored. Did you mean GSS_VERSION?"
    ]
    (ttl,) = unknown_env_warnings(["JWT_ACCESS_TTL_MN"])
    assert ttl.endswith("Did you mean JWT_ACCESS_TTL_MIN?")
    (backup,) = unknown_env_warnings(["DB_BACKUP_MINUTES"])
    assert backup.endswith("Did you mean DB_BACKUP_MINUTE?")


def test_a_name_far_from_every_known_one_gets_no_guess():
    assert unknown_env_warnings(["GSS_SOMETHING_ELSE_ENTIRELY"]) == [
        "Unknown setting GSS_SOMETHING_ELSE_ENTIRELY in the environment — it is ignored."
    ]


def test_known_names_and_other_prefixes_are_not_reported():
    """Settings (in any case, the settings are case-insensitive), names read elsewhere, and
    everything without a project prefix: image variables, game passwords named by the operator."""
    present = ["POSTGRES_PASSWORD", "seed_admin_username", "Jwt_Secret", "GSS_VERSION", "DB_BACKUP_HOUR",
               "PATH", "HOME", "PYTHON_VERSION", "PZ_RCON_PW", "MY_OWN_PASSWORD", "TZ"]
    assert unknown_env_warnings(present) == []


def test_aliased_fields_count_under_their_environment_name():
    """A field that reads `GSS_*` through an alias is known by that name, not by its field name."""
    class Aliased(BaseSettings):
        games: str = Field("", validation_alias=AliasChoices("GSS_GAMES", "games"))
        plain: int = 0
        single: str = Field("", validation_alias="GSS_SINGLE")

    assert setting_env_names(Aliased) == {"GSS_GAMES", "GAMES", "PLAIN", "GSS_SINGLE"}


def test_every_documented_name_is_known():
    """Nothing the compose file reads or `.env.example` shows may trigger the warning — otherwise
    every correct installation would log it on each start."""
    compose = (DASHBOARD / "docker-compose.yml").read_text(encoding="utf-8")
    override = (DASHBOARD / "docker-compose.override.example.yml").read_text(encoding="utf-8")
    example = (DASHBOARD / ".env.example").read_text(encoding="utf-8")
    names = set(re.findall(r"\$\{([A-Z_][A-Z0-9_]*)", compose + override))
    names |= set(re.findall(r"^#?\s*([A-Z][A-Z0-9_]*)=", example, flags=re.M))
    assert {"GSS_VERSION", "DB_BACKUP_HOUR", "DASHBOARD_BIND", "POSTGRES_PASSWORD"} <= names
    assert unknown_env_warnings(names) == []


def test_the_start_logs_a_warning_and_still_starts(monkeypatch, tmp_path, caplog):
    """From the environment and from a `.env` file the settings read; neither aborts the start."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("JWT_ACCESS_TTL_MN=5\n", encoding="utf-8")
    monkeypatch.setenv("GSS_VERSOIN", "1.0.0")
    config.get_settings.cache_clear()
    try:
        with caplog.at_level(logging.WARNING, logger="uvicorn.error"):
            loaded = config.get_settings()
    finally:
        config.get_settings.cache_clear()

    assert isinstance(loaded, Settings)
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("GSS_VERSOIN" in w and "GSS_VERSION?" in w for w in warnings), warnings
    assert any("JWT_ACCESS_TTL_MN" in w and "JWT_ACCESS_TTL_MIN?" in w for w in warnings), warnings


def test_retired_mail_settings_are_named_as_unused():
    """Sign-in by mail was never built, yet DASHBOARD_BASE_URL used to force SMTP settings at
    start. The settings are gone; whoever still sets them is told they do nothing."""
    messages = unknown_env_warnings(["DASHBOARD_BASE_URL", "SMTP_PASSWORD", "MAIL_FROM"])
    assert len(messages) == 3
    assert all("no longer used" in m and "never built" in m for m in messages), messages
    assert not {"DASHBOARD_BASE_URL", "SMTP_HOST", "MAIL_FROM"} & setting_env_names()


def test_the_start_no_longer_demands_mail_settings():
    """`main.lifespan` refused to start with DASHBOARD_BASE_URL set and SMTP incomplete — for a
    sign-in path that had no caller."""
    import inspect

    from app import main

    source = inspect.getsource(main.lifespan)
    assert "DASHBOARD_BASE_URL" not in source and "mailer" not in source
