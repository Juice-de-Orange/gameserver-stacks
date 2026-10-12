"""Vintage Story without the optional "Vintage RCON" mod is a fully managed server.

The mod is third-party and the game docs call it optional. Before, every restore, version change
and recreate of such a server waited 900 s for an RCON answer that could never come and then
rolled back a healthy boot (measured: "Dedicated Server now running" 32 s after the switch,
then `UP/ROLLED_BACK` after 15 min). The console said "RCON unreachable" and the watchdog read
the silent port as a hung game.

Now: the server's own ready line, written since the CURRENT start, plus several stable rounds
prove the boot; an RCON answer still wins at once. The console names the missing mod, and the
watchdog does not probe a port nobody listens on.
"""
from __future__ import annotations

import asyncio
import datetime as dt
from types import SimpleNamespace

import pytest

from app.models.server import ConsoleKind, GameServer, GameType
from app.services import version_manager as vm
from app.services.drivers import vintagestory as vsd
from app.services.drivers.vintagestory import VintageStoryDriver
from app.services.rcon import RconError

READY = "12.10.2026 11:37:42 [Server Event] Dedicated Server now running on Port 42420 and all ips!"
STARTED = "2026-10-12T11:36:37.123456789Z"
STARTED_EPOCH = int(dt.datetime(2026, 10, 12, 11, 36, 37, tzinfo=dt.timezone.utc).timestamp())


class _Docker:
    """A running, stable container whose log holds `lines` as `(epoch, text)`."""

    def __init__(self, lines, started_at=STARTED):
        self.lines = lines
        self.started_at = started_at
        self.states = 0
        self.since: list[int | None] = []

    async def state(self, name):
        self.states += 1
        return {"exists": True, "running": True, "restarting": False, "restarts": 0,
                "started_at": self.started_at}

    async def logs(self, name, tail=200, timestamps=False, since=None):
        self.since.append(since)
        return "\n".join(text for at, text in self.lines if since is None or at >= since)


class _SilentRcon(VintageStoryDriver):
    """The real VS driver, with RCON that never answers (no mod)."""

    async def players(self, server):
        raise RconError("RCON unreachable: [Errno 111] Connection refused")


class _AnsweringRcon(VintageStoryDriver):
    async def players(self, server):
        return {"count": 0, "names": []}


def _srv(tmp_path=None):
    return SimpleNamespace(key="vs-1", container_name="vs-1", console_kind=ConsoleKind.source_rcon,
                           data_dir=str(tmp_path or "/nonexistent"), extra="{}")


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    async def _no_sleep(_):
        return None
    monkeypatch.setattr(vm.asyncio, "sleep", _no_sleep)
    monkeypatch.setattr(vm.watchdog, "suppress", lambda k, m: None)


# --- the boot proof ------------------------------------------------------------------------------

def test_the_ready_line_proves_the_boot_when_rcon_stays_silent(monkeypatch):
    dk = _Docker([(STARTED_EPOCH + 32, "loading…"), (STARTED_EPOCH + 33, READY)])
    monkeypatch.setattr(vm, "docker", dk)

    assert asyncio.run(vm.verify_boot(_srv(), _SilentRcon(), timeout=900)) is True
    assert dk.states >= 3, "accepted before the container had been stable for several rounds"
    assert dk.since and all(s == STARTED_EPOCH for s in dk.since), (
        f"the log must be read from the container's current start on, got since={dk.since}"
    )


def test_a_ready_line_from_an_earlier_run_is_no_proof(monkeypatch):
    """A restore restarts the SAME container; its log still holds the line of the last run."""
    dk = _Docker([(STARTED_EPOCH - 600, READY), (STARTED_EPOCH + 5, "loading…")])
    monkeypatch.setattr(vm, "docker", dk)

    assert asyncio.run(vm.verify_boot(_srv(), _SilentRcon(), timeout=60)) is False


def test_without_a_readable_start_time_the_line_proves_nothing(monkeypatch):
    dk = _Docker([(STARTED_EPOCH + 33, READY)], started_at="0001-01-01T00:00:00Z")
    monkeypatch.setattr(vm, "docker", dk)

    assert asyncio.run(vm.verify_boot(_srv(), _SilentRcon(), timeout=60)) is False


def test_no_answer_and_no_ready_line_still_fails(monkeypatch):
    dk = _Docker([(STARTED_EPOCH + 5, "[vs-runner] FATAL: download of 1.22.7 failed.")])
    monkeypatch.setattr(vm, "docker", dk)

    assert asyncio.run(vm.verify_boot(_srv(), _SilentRcon(), timeout=60)) is False


def test_an_rcon_answer_is_still_the_proof_and_comes_first(monkeypatch):
    dk = _Docker([])
    monkeypatch.setattr(vm, "docker", dk)

    assert asyncio.run(vm.verify_boot(_srv(), _AnsweringRcon(), timeout=60)) is True
    assert dk.states == 1 and dk.since == [], "the RCON answer may take effect at once, no log read"


def test_a_crash_after_the_ready_line_is_still_caught(monkeypatch):
    """The restart counter keeps working on the ready-line path."""
    class _Crashing(_Docker):
        async def state(self, name):
            st = await super().state(name)
            st["restarts"] = 0 if self.states == 1 else 1
            return st

    dk = _Crashing([(STARTED_EPOCH + 33, READY)])
    monkeypatch.setattr(vm, "docker", dk)
    assert asyncio.run(vm.verify_boot(_srv(), _SilentRcon(), timeout=60)) is False


# --- is the mod there? ---------------------------------------------------------------------------

def _vs(tmp_path) -> GameServer:
    return GameServer(key="vs-1", game_type=GameType.vintagestory, data_dir=str(tmp_path),
                      rcon_host="vs-1", rcon_port=42425)


def test_mod_detection(tmp_path):
    drv = VintageStoryDriver()
    srv = _vs(tmp_path)
    assert drv.rcon_mod_installed(srv) is None, "no data mount means: cannot tell"

    (tmp_path / "gamedata" / "vs" / "Mods").mkdir(parents=True)
    assert drv.rcon_mod_installed(srv) is False
    (tmp_path / "gamedata" / "vs" / "Mods" / "someothermod_1.0.zip").write_bytes(b"x")
    assert drv.rcon_mod_installed(srv) is False
    (tmp_path / "gamedata" / "vs" / "Mods" / "VintageRCon-2.0.1.zip").write_bytes(b"x")
    assert drv.rcon_mod_installed(srv) is True


def test_the_config_the_mod_writes_counts_as_installed(tmp_path):
    (tmp_path / "gamedata" / "vs" / "ModConfig").mkdir(parents=True)
    (tmp_path / "gamedata" / "vs" / "ModConfig" / "vsrcon.json").write_text("{}")
    assert VintageStoryDriver().rcon_mod_installed(_vs(tmp_path)) is True


def test_the_watchdog_does_not_probe_a_port_nobody_listens_on(tmp_path):
    drv = VintageStoryDriver()
    srv = _vs(tmp_path)
    assert drv.players_is_queryable(srv) is True, "unknown must not switch the watchdog off"
    (tmp_path / "gamedata" / "vs").mkdir(parents=True)
    assert drv.players_is_queryable(srv) is False


def test_the_console_names_the_missing_mod(tmp_path, monkeypatch):
    from app.services import rcon

    async def _refused(*a, **kw):
        raise RconError("RCON unreachable: [Errno 111] Connection refused")

    monkeypatch.setattr(rcon, "rcon_command", _refused)
    monkeypatch.setattr("app.services.servers.resolve_secret", lambda s: "x")
    (tmp_path / "gamedata" / "vs").mkdir(parents=True)
    drv = VintageStoryDriver()

    with pytest.raises(vsd.VsRconModMissing) as e:
        asyncio.run(drv.console(_vs(tmp_path), "list clients"))
    assert e.value.detail["code"] == "console.vintagestory_rcon_mod_missing"
    assert isinstance(e.value, RconError), "callers that treat RCON errors as 'unknown' must still do so"
    assert drv.console_notice(_vs(tmp_path))["code"] == "console.vintagestory_rcon_mod_missing"

    # With the mod there, the real error stays: a wrong password is not a missing mod.
    (tmp_path / "gamedata" / "vs" / "Mods").mkdir()
    (tmp_path / "gamedata" / "vs" / "Mods" / "VintageRCon").mkdir()
    with pytest.raises(RconError) as e2:
        asyncio.run(drv.console(_vs(tmp_path), "list clients"))
    assert not isinstance(e2.value, vsd.VsRconModMissing)
    assert drv.console_notice(_vs(tmp_path)) is None


def test_the_console_socket_says_why_before_the_first_command(tmp_path, monkeypatch, api_client):
    """The page shows the reason when it opens, not after a command went nowhere."""
    from app.routers import console as console_router

    (tmp_path / "gamedata" / "vs").mkdir(parents=True)
    srv = _vs(tmp_path)
    srv.enabled = True
    user = SimpleNamespace(id=1, username="admin")
    monkeypatch.setattr(console_router, "user_from_ws_ticket", lambda t, s: user)
    monkeypatch.setattr(console_router, "has_permission", lambda u, p: True)
    monkeypatch.setattr(console_router.servers_registry, "get_by_key", lambda k: srv)

    with api_client.websocket_connect("/ws/console?server=vs-1&ticket=t") as ws:
        first = ws.receive_json()
    assert first["type"] == "error"
    assert first["message"]["code"] == "console.vintagestory_rcon_mod_missing"
