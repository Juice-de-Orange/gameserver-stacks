"""PZ stop: a `quit` whose reply never arrives is a clean shutdown when the container goes down.

Before, every such stop made `lifecycle` log the WARNING "the game could not be shut down through
the console — falling back to the signal path", the same false alarm Factorio had (A12 F2).
"""
import asyncio

from app.models.server import GameServer, GameType
from app.services.drivers.pz import PZDriver


def _srv():
    return GameServer(key="pz-1", game_type=GameType.pz, container_name="gs-pz-1")


def _quit_fails(monkeypatch, states):
    """`quit` without a reply, and the container's run state over time (last one repeats)."""
    d = PZDriver()
    sent: list[str] = []

    async def console(server, command, *, timeout=8.0):
        sent.append(command)
        if command == "quit":
            raise RuntimeError("RCON error while sending/reading the response")
        return "World saved"

    async def state(name):
        return {"running": states.pop(0) if len(states) > 1 else states[0]}

    async def no_wait(_seconds):
        return None

    d.console = console
    monkeypatch.setattr("app.services.docker_client.docker.state", state)
    monkeypatch.setattr("asyncio.sleep", no_wait)
    return d, sent


def test_a_quit_without_reply_followed_by_the_exit_is_a_clean_shutdown(monkeypatch):
    # PZ writes its world after `quit` and exits about 22 s later.
    d, sent = _quit_fails(monkeypatch, [True] * 22 + [False])
    assert asyncio.run(d.graceful_shutdown(_srv())) is True
    assert sent == ["save", "quit"]


def test_a_server_that_keeps_running_is_still_reported(monkeypatch):
    """The console did not cooperate and the game is still up: the warning must stay."""
    d, _ = _quit_fails(monkeypatch, [True])
    assert asyncio.run(d.graceful_shutdown(_srv())) is False


def test_an_unreadable_state_is_no_proof_of_a_clean_end(monkeypatch):
    d, _ = _quit_fails(monkeypatch, [True])

    async def broken(name):
        raise OSError("socket proxy gone")

    monkeypatch.setattr("app.services.docker_client.docker.state", broken)
    assert asyncio.run(d.graceful_shutdown(_srv())) is False


def test_a_quit_with_reply_needs_no_wait(monkeypatch):
    d = PZDriver()

    async def console(server, command, *, timeout=8.0):
        return "Quit" if command == "quit" else "World saved"

    async def must_not_ask(name):
        raise AssertionError("the run state is only needed when `quit` got no reply")

    d.console = console
    monkeypatch.setattr("app.services.docker_client.docker.state", must_not_ask)
    assert asyncio.run(d.graceful_shutdown(_srv())) is True
