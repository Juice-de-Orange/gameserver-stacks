"""The shared stop/restart path (`services/lifecycle.py`).

Background: PZ ignores SIGTERM (PID 1 is a bash without a handler), so the game is shut down via
its own console. The properties checked here are exactly the ones that could go wrong — above all:
**the stop happens even when the game console is dead**, and **`docker stop` goes out BEFORE the
`quit`** (otherwise `unless-stopped` pulls the server right back up).
"""
import asyncio

import pytest

from app.models.server import GameServer, GameType
from app.services import lifecycle


def _srv(key="pz-1", game=GameType.pz, container="cont"):
    s = GameServer(key=key, game_type=game, container_name=container)
    s.id = 1
    return s


class _Docker:
    """Fake Docker that writes every action into a shared trace."""

    def __init__(self, trace, running=True):
        self.trace = trace
        self.running = running
        self.stop_timeouts: list[int | None] = []

    async def state(self, name):
        return {"running": self.running, "exists": True}

    async def action(self, name, action, *, stop_timeout=None):
        self.trace.append(("docker", action))
        if action == "stop":
            self.stop_timeouts.append(stop_timeout)
            self.running = False


class _Drv:
    def __init__(self, trace, graceful=True, boom=False):
        self.trace = trace
        self._graceful = graceful
        self._boom = boom
        self.announced: list[str] = []

    async def announce(self, server, text):
        self.announced.append(text)
        self.trace.append(("driver", "announce"))

    async def graceful_shutdown(self, server):
        self.trace.append(("driver", "graceful"))
        if self._boom:
            raise RuntimeError("RCON dead")
        return self._graceful


@pytest.fixture()
def wired(monkeypatch):
    """Wire lifecycle with fakes; returns (trace, docker, driver)."""
    trace: list[tuple[str, str]] = []

    def _make(running=True, graceful=True, boom=False, grace=120):
        dk = _Docker(trace, running=running)
        drv = _Drv(trace, graceful=graceful, boom=boom)
        monkeypatch.setattr(lifecycle, "docker", dk)
        monkeypatch.setattr(lifecycle, "_driver", lambda s: drv)
        monkeypatch.setattr(lifecycle, "stop_grace", lambda s: grace)
        monkeypatch.setattr(lifecycle, "_suppress", lambda *a, **k: None)
        monkeypatch.setattr(lifecycle, "_STOP_MARK_DELAY", 0)
        monkeypatch.setattr(lifecycle, "_locks", {})  # each test has its own event loop
        return trace, dk, drv

    return _make


def test_docker_stop_goes_out_before_the_game_quit(wired):
    """The order is the core: first Docker marks the container as manually stopped, then the
    game may shut itself down. The other way round `unless-stopped` would start the server
    again immediately."""
    trace, dk, drv = wired()
    assert asyncio.run(lifecycle.stop_server(_srv())) is True
    assert trace == [("docker", "stop"), ("driver", "graceful")]
    assert dk.stop_timeouts == [120]


def test_broadcast_goes_out_before_the_stop(wired):
    trace, _dk, drv = wired()
    asyncio.run(lifecycle.stop_server(_srv(), announce_text="Shutting down soon"))
    assert trace[0] == ("driver", "announce")
    assert drv.announced == ["Shutting down soon"]


def test_stop_happens_even_with_a_dead_console(wired):
    """The most important case: if the game's own shutdown fails, the signal path remains —
    the server must still go down (otherwise the dashboard would hang)."""
    trace, dk, _drv = wired(boom=True)
    assert asyncio.run(lifecycle.stop_server(_srv())) is True
    assert ("docker", "stop") in trace
    assert dk.stop_timeouts == [120]


def test_a_stopped_server_is_not_shut_down_again(wired):
    trace, dk, _drv = wired(running=False)
    assert asyncio.run(lifecycle.stop_server(_srv())) is False
    # The stop still goes out (Docker answers 304) — but without console chatter.
    assert trace == [("docker", "stop")]


def test_grace_period_comes_from_the_driver(wired):
    _trace, dk, _drv = wired(grace=45)
    asyncio.run(lifecycle.stop_server(_srv()))
    assert dk.stop_timeouts == [45]


def test_no_quit_when_docker_does_not_accept_the_stop(monkeypatch):
    """If Docker does not accept the stop (proxy error, container gone), the container is NOT
    marked as manually stopped. A `quit` would end the game process and `unless-stopped` would
    pull it right back up — worse than doing nothing."""
    trace: list[tuple[str, str]] = []

    class _BrokenDocker(_Docker):
        async def action(self, name, action, *, stop_timeout=None):
            self.trace.append(("docker", f"{action}-error"))
            raise RuntimeError("proxy 404")

    dk = _BrokenDocker(trace)
    drv = _Drv(trace)
    monkeypatch.setattr(lifecycle, "docker", dk)
    monkeypatch.setattr(lifecycle, "_driver", lambda s: drv)
    monkeypatch.setattr(lifecycle, "stop_grace", lambda s: 120)
    monkeypatch.setattr(lifecycle, "_suppress", lambda *a, **k: None)
    monkeypatch.setattr(lifecycle, "_STOP_MARK_DELAY", 0)

    with pytest.raises(RuntimeError):
        asyncio.run(lifecycle.stop_server(_srv()))
    assert ("driver", "graceful") not in trace  # no quit on an unmarked container


def test_restart_starts_even_when_the_stop_fails(monkeypatch):
    """`docker restart` was one step, stop-then-start are two — a failure in the first must not
    leave a server down."""
    trace: list[tuple[str, str]] = []

    class _StopFails(_Docker):
        async def action(self, name, action, *, stop_timeout=None):
            self.trace.append(("docker", action))
            if action == "stop":
                raise RuntimeError("stop rejected")

    dk = _StopFails(trace)
    monkeypatch.setattr(lifecycle, "docker", dk)
    monkeypatch.setattr(lifecycle, "_driver", lambda s: _Drv(trace))
    monkeypatch.setattr(lifecycle, "stop_grace", lambda s: 120)
    monkeypatch.setattr(lifecycle, "_suppress", lambda *a, **k: None)
    monkeypatch.setattr(lifecycle, "_STOP_MARK_DELAY", 0)

    with pytest.raises(RuntimeError):
        asyncio.run(lifecycle.restart_server(_srv()))
    assert ("docker", "start") in trace  # the start was attempted despite the error


def test_driver_without_own_shutdown_is_not_asked(monkeypatch):
    """Reforger inherits the base default (no shutdown verb over RCON). Such games get neither
    the head start nor a console attempt — the signal path is right there."""
    from app.services.drivers.base import GameDriver

    trace: list[tuple[str, str]] = []

    class _Inherits:
        graceful_shutdown = GameDriver.graceful_shutdown  # unchanged base default

        async def announce(self, server, text):
            trace.append(("driver", "announce"))

    dk = _Docker(trace)
    monkeypatch.setattr(lifecycle, "docker", dk)
    monkeypatch.setattr(lifecycle, "_driver", lambda s: _Inherits())
    monkeypatch.setattr(lifecycle, "stop_grace", lambda s: 60)
    monkeypatch.setattr(lifecycle, "_suppress", lambda *a, **k: None)
    monkeypatch.setattr(lifecycle, "_locks", {})
    # No `_STOP_MARK_DELAY = 0`: if the head start ran anyway, the test would hang for 2 s.

    asyncio.run(lifecycle.stop_server(_srv(key="rf-1", game=GameType.arma_reforger)))
    assert trace == [("docker", "stop")]


def test_time_budget_releases_the_request_and_stops_in_the_background(monkeypatch):
    """Behind a reverse proxy with a ~100 s cap a hanging server must not hold up the request."""
    trace: list[tuple[str, str]] = []

    class _Slow(_Docker):
        async def action(self, name, action, *, stop_timeout=None):
            if action == "stop":
                await asyncio.sleep(5)  # container takes forever
            self.trace.append(("docker", action))

    monkeypatch.setattr(lifecycle, "docker", _Slow(trace))
    monkeypatch.setattr(lifecycle, "_driver", lambda s: _Drv(trace))
    monkeypatch.setattr(lifecycle, "stop_grace", lambda s: 120)
    monkeypatch.setattr(lifecycle, "_suppress", lambda *a, **k: None)
    monkeypatch.setattr(lifecycle, "_STOP_MARK_DELAY", 0)
    monkeypatch.setattr(lifecycle, "_locks", {})

    async def run():
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        await lifecycle.stop_server(_srv(), wait_timeout=0.05)
        return loop.time() - t0

    duration = asyncio.run(run())
    assert duration < 2.0                   # returns immediately
    assert ("docker", "stop") not in trace  # the stop was still running when we returned


def test_a_stop_during_a_restart_is_not_overtaken(monkeypatch):
    """Otherwise the stop reports "ok" and the restart undoes it right after with its start."""
    trace: list[tuple[str, str]] = []

    class _Delayed(_Docker):
        async def action(self, name, action, *, stop_timeout=None):
            if action == "stop":
                await asyncio.sleep(0.05)
            self.trace.append(("docker", action))
            self.running = action == "start"

    monkeypatch.setattr(lifecycle, "docker", _Delayed(trace))
    monkeypatch.setattr(lifecycle, "_driver", lambda s: _Drv(trace))
    monkeypatch.setattr(lifecycle, "stop_grace", lambda s: 120)
    monkeypatch.setattr(lifecycle, "_suppress", lambda *a, **k: None)
    monkeypatch.setattr(lifecycle, "_STOP_MARK_DELAY", 0)
    monkeypatch.setattr(lifecycle, "_locks", {})
    srv = _srv()

    async def both():
        restart = asyncio.create_task(lifecycle.restart_server(srv))
        await asyncio.sleep(0)  # the restart holds the lock
        await asyncio.gather(restart, lifecycle.stop_server(srv))

    asyncio.run(both())
    actions = [a for w, a in trace if w == "docker"]
    # The restart runs through completely (stop, start), only then the stop takes effect.
    assert actions[:2] == ["stop", "start"] and actions[-1] == "stop"


def test_restart_is_stop_then_start(wired):
    """`docker restart` would send the same ineffective SIGTERM."""
    trace, _dk, _drv = wired()
    asyncio.run(lifecycle.restart_server(_srv()))
    assert [t for t in trace if t[0] == "docker"] == [("docker", "stop"), ("docker", "start")]


def test_two_concurrent_stops_run_one_after_the_other(wired):
    """Idle auto-stop and an admin click must not overlap."""
    trace, dk, _drv = wired()
    srv = _srv()

    async def both():
        await asyncio.gather(lifecycle.stop_server(srv), lifecycle.stop_server(srv))

    asyncio.run(both())
    # The second run sees an already stopped server → only the Docker stop remains.
    assert trace.count(("driver", "graceful")) == 1
    assert trace.count(("docker", "stop")) == 2


# --------------------------------------------------------- game-specific commands per driver

def _record_console(driver, sent):
    async def console(server, command, *, timeout=8.0):
        sent.append(command)
        return ""

    driver.console = console
    return driver


def test_pz_sends_save_then_quit():
    """Per the game files `quit` is "Save and quit the server"; the preceding `save` is
    deliberate redundancy."""
    from app.services.drivers.pz import PZDriver

    sent: list[str] = []
    drv = _record_console(PZDriver(), sent)
    assert asyncio.run(drv.graceful_shutdown(_srv())) is True
    assert sent == ["save", "quit"]


def test_minecraft_stops_through_the_images_sigterm_handler(monkeypatch, caplog):
    """Measured against a real `itzg/minecraft-server` (Paper 26.2): on `docker stop` the
    image's `mc-server-runner` sends `stop` to the server console itself; the server saved all
    chunks and exited with code 0 within about a second. The RCON `save-all flush` + `stop` that
    `lifecycle` sent 2 s after the stop call found the container already gone ("RCON
    unreachable") and logged "falling back to the signal path" on EVERY stop — although that
    path WAS the clean stop. Sending `stop` over RCON before `docker stop` is no option: the
    game would exit before Docker marks the container as stopped, and `unless-stopped` starts it
    again (module docstring of `lifecycle`).

    So Minecraft, like Avorion, Valheim and Reforger, relies on the signal path: no console
    attempt, no head start, no misleading warning.
    """
    import logging

    from app.services.drivers.minecraft import MinecraftDriver
    from app.services.rcon import RconError

    trace: list[tuple[str, str]] = []
    dk = _Docker(trace)
    sent: list[str] = []

    async def console(self, server, command, *, timeout=8.0):
        sent.append(command)
        if not dk.running:
            raise RconError("RCON unreachable: Temporary failure in name resolution")
        return ""

    monkeypatch.setattr(MinecraftDriver, "console", console)
    monkeypatch.setattr(lifecycle, "docker", dk)
    monkeypatch.setattr(lifecycle, "_driver", lambda s: MinecraftDriver())
    monkeypatch.setattr(lifecycle, "stop_grace", lambda s: 90)
    monkeypatch.setattr(lifecycle, "_suppress", lambda *a, **k: None)
    monkeypatch.setattr(lifecycle, "_STOP_MARK_DELAY", 0)
    monkeypatch.setattr(lifecycle, "_locks", {})

    with caplog.at_level(logging.WARNING, logger="uvicorn.error"):
        assert asyncio.run(lifecycle.stop_server(_srv(key="mc", game=GameType.minecraft))) is True

    assert trace == [("docker", "stop")] and dk.stop_timeouts == [90]
    assert sent == [], f"console commands sent at a server that is already stopping: {sent}"
    assert not [r for r in caplog.records if "signal path" in r.getMessage()]


def test_reforger_sends_nothing():
    """Enfusion RCON has no shutdown verb — the signal path is the right one here."""
    from app.services.drivers.reforger import ReforgerDriver

    sent: list[str] = []
    drv = _record_console(ReforgerDriver(), sent)
    srv = _srv(key="rf-1", game=GameType.arma_reforger)
    assert asyncio.run(drv.graceful_shutdown(srv)) is False
    assert sent == []


def test_pz_reports_false_when_rcon_is_dead():
    from app.services.drivers.pz import PZDriver
    from app.services.rcon import RconError

    async def dead(server, command, *, timeout=8.0):
        raise RconError("down")

    drv = PZDriver()
    drv.console = dead
    assert asyncio.run(drv.graceful_shutdown(_srv())) is False


@pytest.mark.parametrize(
    "game", [GameType.arma_reforger, GameType.avorion, GameType.valheim, GameType.vintagestory],
)
def test_signal_path_games_get_no_console_attempt_and_no_warning(monkeypatch, caplog, game):
    """Arma Reforger, Avorion, Valheim and Vintage Story stop cleanly on the signal (`docker stop`).

    With a `graceful_shutdown` of their own that only ever returned `False`, every stop waited
    the 2 s head start, sent nothing useful (Vintage Story: an unverified `stop` over the RCON
    mod) and then logged "falling back to the signal path" — a warning on every stop about the
    path that WAS the right one.
    """
    import logging

    from app.services.drivers import get_driver

    trace: list[tuple[str, str]] = []
    dk = _Docker(trace)
    srv = _srv(key="sig-1", game=game)
    drv = get_driver(srv)
    sent: list[str] = []

    async def console(server, command, *, timeout=8.0):
        sent.append(command)
        return ""

    drv.console = console
    monkeypatch.setattr(lifecycle, "docker", dk)
    monkeypatch.setattr(lifecycle, "_driver", lambda s: drv)
    monkeypatch.setattr(lifecycle, "stop_grace", lambda s: 60)
    monkeypatch.setattr(lifecycle, "_suppress", lambda *a, **k: None)
    monkeypatch.setattr(lifecycle, "_locks", {})
    # No `_STOP_MARK_DELAY = 0`: a head start would show up as a 2 s delay, not as a failure.

    with caplog.at_level(logging.WARNING, logger="uvicorn.error"):
        assert asyncio.run(lifecycle.stop_server(srv)) is True

    assert trace == [("docker", "stop")]
    assert sent == [], f"console commands sent although the signal is the clean stop: {sent}"
    assert not [r for r in caplog.records if "signal path" in r.getMessage()]
