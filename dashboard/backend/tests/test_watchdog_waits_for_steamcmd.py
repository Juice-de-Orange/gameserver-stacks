"""The watchdog does not restart a server whose image is still downloading the game.

Found on a real stack: a first Valheim start whose SteamCMD download (with one "Missing
configuration" retry) took longer than the 15-minute boot window was restarted by the watchdog at
99.95 % verification, which starts the download over. The window now counts from SteamCMD's last
progress line in the container log.
"""
import asyncio
import datetime as dt
import time

from sqlmodel import Session, select

from app.config import settings
from app.models import ServerIncident
from app.models.server import GameServer, GameType
from app.services import watchdog

# Recorded from ghcr.io/community-valheim-tools/valheim-server:1.4.0 on a first start.
DOWNLOADING = ("Oct 12 13:39:08 supervisord: valheim-updater  Update state (0x61) downloading, "
               "progress: 57.32 (1253443568 / 2186650630)")
VERIFYING = ("Oct 12 13:40:37 supervisord: valheim-updater  Update state (0x81) verifying update, "
             "progress: 86.35 (1888082551 / 2186650630)")
COMMITTING = ("Oct 12 13:40:39 supervisord: valheim-updater  Update state (0x101) committing, "
              "progress: 98.00 (2143025004 / 2186650630)")
NOT_PROGRESS = (
    "Oct 12 13:40:41 supervisord: valheim-updater Success! App '896660' fully installed.",
    "Oct 12 13:37:18 supervisord: valheim-updater INFO - Downloading/updating/validating Valheim "
    "server from Steam",
    "Oct 12 13:41:34 supervisord: valheim-server 10/12/2026 13:41:34: Game server connected",
)


def _srv():
    s = GameServer(key="valheim-1", game_type=GameType.valheim, container_name="gs-valheim-1")
    s.id = 1
    return s


class _Docker:
    """A container that has run for 20 minutes (past the boot window), with a given log."""

    def __init__(self, log_text="", *, fail_logs=False):
        self.log_text = log_text
        self.fail_logs = fail_logs
        self.actions: list[tuple[str, str]] = []
        self.log_calls: list[dict] = []

    async def state(self, name):
        started = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None) - dt.timedelta(minutes=20)
        return {"running": True, "id": "c-1", "started_at": started.isoformat() + "Z"}

    async def logs(self, name, tail=200, timestamps=False, since=None):
        self.log_calls.append({"name": name, "tail": tail, "since": since})
        if self.fail_logs:
            raise RuntimeError("dockerproxy unreachable")
        return self.log_text

    async def action(self, name, action, **kw):
        self.actions.append((name, action))


def _rounds(monkeypatch, mem_engine, fake_driver_cls, wire_lifecycle, fake, n=5):
    watchdog._states.clear()
    monkeypatch.setattr(watchdog, "docker", fake)
    wire_lifecycle(fake)
    monkeypatch.setattr(watchdog, "engine", mem_engine)
    monkeypatch.setattr(settings, "watchdog_fail_threshold", 3, raising=False)
    monkeypatch.setattr(settings, "watchdog_cooldown_min", 0, raising=False)
    server = _srv()
    dead = fake_driver_cls(fail_rcon=True)
    for _ in range(n):
        asyncio.run(watchdog._check_server(server, dead))
    with Session(mem_engine) as s:
        kinds = {i.kind for i in s.exec(select(ServerIncident)).all()}
    return server, kinds


def test_a_server_still_verifying_its_download_is_not_restarted(
        monkeypatch, mem_engine, fake_driver_cls, wire_lifecycle):
    fake = _Docker("\n".join([DOWNLOADING, VERIFYING]))
    server, kinds = _rounds(monkeypatch, mem_engine, fake_driver_cls, wire_lifecycle, fake)
    assert fake.actions == [], "the watchdog restarted a server in the middle of its SteamCMD download"
    assert "rcon_hang" not in kinds and "auto_restart" not in kinds
    assert watchdog._state(server.key).fail_count == 0
    # The log is read for exactly the boot window, counted back from now.
    call = fake.log_calls[-1]
    assert call["name"] == "gs-valheim-1"
    assert abs(call["since"] - (time.time() - watchdog.BOOT_WINDOW_MIN * 60)) < 30


def test_a_hang_after_the_download_is_still_healed(
        monkeypatch, mem_engine, fake_driver_cls, wire_lifecycle):
    """No progress line within the window: the game is installed and silent, so it counts."""
    fake = _Docker("\n".join(NOT_PROGRESS))
    _, kinds = _rounds(monkeypatch, mem_engine, fake_driver_cls, wire_lifecycle, fake, n=3)
    assert fake.actions == [("gs-valheim-1", "stop"), ("gs-valheim-1", "start")]
    assert {"rcon_hang", "auto_restart"} <= kinds


def test_an_unreadable_log_keeps_the_old_rule(
        monkeypatch, mem_engine, fake_driver_cls, wire_lifecycle):
    """Best effort: a log that cannot be read must not switch the watchdog off."""
    fake = _Docker(fail_logs=True)
    _, kinds = _rounds(monkeypatch, mem_engine, fake_driver_cls, wire_lifecycle, fake, n=3)
    assert fake.actions == [("gs-valheim-1", "stop"), ("gs-valheim-1", "start")]
    assert "rcon_hang" in kinds


def test_the_pattern_matches_steamcmd_progress_and_nothing_else():
    for line in (DOWNLOADING, VERIFYING, COMMITTING,
                 " Update state (0x5) verifying install, progress: 99.95 (2185557299 / 2186650630)"):
        assert watchdog._STEAMCMD_PROGRESS.search(line), line
    for line in NOT_PROGRESS:
        assert not watchdog._STEAMCMD_PROGRESS.search(line), line
