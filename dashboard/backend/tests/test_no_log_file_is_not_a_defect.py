""""Nobody played today" is not a configuration error.

Project Zomboid creates `<date>_user.txt` only **when someone plays**, and moves it to
`logs_<date>/` at the day change. On a server nobody is on, the file simply does not exist —
reporting that normal case as a defect every 30 seconds floods the API log, and with
`max-size 10m, max-file 3` pushes every real message out of the rotation window within days:
the false alarm makes the log useless as a diagnostic tool.

**The root:** "nothing there" and "could not get there" must stay distinguishable.
`PZDriver._newest_log` returns `None` both for an empty glob and on `except OSError`, so the
driver says explicitly whether there IS an obstacle (`event_log_obstacle`) instead of letting it
be guessed from an empty list.
"""
from __future__ import annotations

import asyncio
import logging
import os

import pytest

from app.models.server import GameServer
from app.services.drivers.pz import PZDriver


def _server(data_dir: str) -> GameServer:
    return GameServer(
        key="pz-1", game_type="pz", display_name="PZ", container_name="pz-1",
        data_dir=data_dir, enabled=True,
    )


def test_an_empty_log_directory_is_no_obstacle(tmp_path):
    """The normal case: everything is fine, nobody has played."""
    os.makedirs(tmp_path / "data" / "Logs")
    assert PZDriver().event_log_obstacle(_server(str(tmp_path))) is None


def test_a_missing_log_directory_IS_an_obstacle(tmp_path):
    """The case the warning is meant for — missing mount, wrong path."""
    obstacle = PZDriver().event_log_obstacle(_server(str(tmp_path)))
    assert obstacle, "a missing Logs directory must still be noticed"
    assert "Logs" in obstacle, "the message must name the path, otherwise it helps nobody"


def test_an_unreadable_log_directory_IS_an_obstacle(tmp_path):
    """Permission problem. Skipped when the test runs as root — then no mode applies."""
    directory = tmp_path / "data" / "Logs"
    os.makedirs(directory)
    os.chmod(directory, 0o000)
    try:
        if os.access(directory, os.R_OK):
            pytest.skip("running as root — file permissions do not apply")
        assert PZDriver().event_log_obstacle(_server(str(tmp_path)))
    finally:
        os.chmod(directory, 0o755)


def test_a_game_without_file_events_never_has_an_obstacle():
    """Minecraft reads stdout — there this question must not even arise."""
    from app.services.drivers.minecraft import MinecraftDriver
    assert MinecraftDriver().event_log_obstacle(_server("/does/not/exist")) is None


def test_the_parser_warns_only_on_a_real_obstacle(tmp_path, monkeypatch, caplog):
    """No warning just because nobody played.

    The level is checked, not the wording: one WARNING every 30 seconds is what makes the log
    useless.
    """
    from app.services import log_parser

    class _FakeDocker:
        async def state(self, name):
            return {"running": True}

        async def logs(self, *a, **k):        # pragma: no cover — must not be called here
            raise AssertionError("with events_from_files the Docker log must not be fetched")

    monkeypatch.setattr(log_parser, "docker", _FakeDocker())
    os.makedirs(tmp_path / "data" / "Logs")
    server = _server(str(tmp_path))

    caplog.set_level(logging.DEBUG, logger="uvicorn.error")
    asyncio.run(log_parser._poll_server(server, PZDriver()))
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert warnings == [], (
        "An empty but readable log directory must not produce a warning — every poll would "
        f"repeat it. Got: {[r.getMessage() for r in warnings]}"
    )

    # And the other direction: without a directory it must still be loud.
    caplog.clear()
    asyncio.run(log_parser._poll_server(_server(str(tmp_path / "gone")), PZDriver()))
    assert [r for r in caplog.records if r.levelno >= logging.WARNING], (
        "A missing log directory is a real problem and must be reported."
    )
