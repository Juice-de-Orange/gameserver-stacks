"""A *hanging* or dropping RCON server must surface as `RconError`, not as a bare
`asyncio.TimeoutError`/`IncompleteReadError`.

Otherwise `watchdog._check_server` (`except RconError`) does not catch the hang → `fail_count`
never rises → no auto-restart; and `stats_poller` leaves open sessions behind. `bercon.py`
(Reforger) does the same; this test pins the behaviour for Source RCON.
"""
import asyncio

import pytest

from app.services import rcon


class _FakeWriter:
    """Minimal StreamWriter stand-in: swallows writes, closes immediately."""

    def write(self, *_a):  # noqa: D401
        pass

    async def drain(self):
        pass

    def close(self):
        pass

    async def wait_closed(self):
        pass


def _patch_open(monkeypatch, make_reader):
    async def _fake_open_connection(host, port):  # same signature as asyncio.open_connection
        return make_reader(), _FakeWriter()

    monkeypatch.setattr(rcon.asyncio, "open_connection", _fake_open_connection)


def test_hung_server_after_connect_raises_rconerror(monkeypatch):
    """TCP is up but the server never answers → read timeout → RconError (not TimeoutError)."""
    _patch_open(monkeypatch, asyncio.StreamReader)  # reader is never fed → read blocks
    with pytest.raises(rcon.RconError):
        asyncio.run(rcon.rcon_command("host", 27015, "pw", "players", timeout=0.05))


def test_connection_drop_mid_handshake_raises_rconerror(monkeypatch):
    """Connection drops right after connect (EOF) → IncompleteReadError → RconError."""

    def _eof_reader():
        r = asyncio.StreamReader()
        r.feed_eof()  # readexactly raises IncompleteReadError at once
        return r

    _patch_open(monkeypatch, _eof_reader)
    with pytest.raises(rcon.RconError):
        asyncio.run(rcon.rcon_command("host", 27015, "pw", "players", timeout=0.5))


def test_multi_read_hang_also_raises_rconerror(monkeypatch):
    """The multi=True path (Minecraft) too may only raise RconError on a hang."""

    def _auth_ok_then_hang():
        r = asyncio.StreamReader()
        # valid AUTH_RESPONSE (req_id != -1) → handshake ok; then nothing more arrives → hang
        r.feed_data(rcon._encode(1, rcon._AUTH_RESPONSE, ""))
        return r

    _patch_open(monkeypatch, _auth_ok_then_hang)
    with pytest.raises(rcon.RconError):
        asyncio.run(rcon.rcon_command("host", 27015, "pw", "list", timeout=0.05, multi=True))
