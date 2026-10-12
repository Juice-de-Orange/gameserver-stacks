"""Minimal async Steam server query client (A2S) for games without a console.

**Why it exists.** Valheim's dedicated server has no remote console: no RCON, no stdin commands.
The only information it offers from outside is the Steam query on the port next to the game
port. That yields player count, player names and the running game version, which is exactly
what `players()`, `idle_autostop`, `watchdog` and the versions tab need.

**Why hand-written.** `services/rcon.py` speaks Source RCON itself as well; A2S is two UDP
packets with a challenge handshake. A dependency would be out of proportion for a deliberately
lean `requirements.txt`, and the error handling, which is the decisive part here, would not be
ours.

**The rule this module rests on: an unclear answer is an exception, never a zero.** Whoever
catches `A2sError` knows "unknown" and touches nothing, which is how `idle_autostop` and
`auto_update` already treat an error. A silently reported 0 would mean "nobody is here", and
then a server gets stopped or updated while someone is playing on it.

Protocol: https://developer.valvesoftware.com/wiki/Server_queries
"""
from __future__ import annotations

import asyncio
import struct

from .rcon import RconError

_HEADER = b"\xff\xff\xff\xff"
_INFO_REQUEST = _HEADER + b"TSource Engine Query\x00"
_PLAYER_REQUEST = _HEADER + b"U"
# A2S_PLAYER requires a challenge field from the start; -1 means "I do not have one yet".
_NO_CHALLENGE = b"\xff\xff\xff\xff"
_CHALLENGE = 0x41            # 'A': "ask again, this time with this number"
_INFO_RESPONSE = 0x49         # 'I'
_PLAYER_RESPONSE = 0x44       # 'D'

# An A2S packet is normally <1400 B; 4096 leaves ample headroom for a single packet. SPLIT
# responses (several packets, header 0xFFFFFFFE) are deliberately NOT reassembled: they only
# occur with very many players, and a half-understood reassembly would be exactly the kind of
# guess this module is meant to avoid. They fail as `A2sError`, i.e. as "unknown".
_MAX_PACKET = 4096


class A2sError(RconError):
    """The query was not answered or not understood: state UNKNOWN.

    **Inherits from `RconError` on purpose.** The name is historical: throughout the project
    `RconError` means "this game server's query channel does not answer", and every consumer
    treats it that way. `routers/server.status_payload` catches `(RconError, NotImplementedError)`
    and returns `null` instead of a 500, `watchdog._check_server` counts it and opens an incident
    after N rounds, `stats_poller` ends its round cleanly.

    With a plain `Exception` base, Valheim's outage (which happens on EVERY start while SteamCMD
    is running) reached the browser as a 500 and the watchdog was effectively off for that
    server: `fail_count` never increased, so there was neither an incident nor self-healing
    while the UI showed "monitored".

    A new shared base type would have meant changing every `except` line and eventually missing
    one; inheritance puts the rule at the single choke point instead.
    """


class _Reader:
    """Read bytes in order; if the buffer runs out, the response is unusable."""

    def __init__(self, data: bytes):
        self.data = data
        self.pos = 0

    def byte(self) -> int:
        return self._take(1)[0]

    def short(self) -> int:
        return struct.unpack("<h", self._take(2))[0]

    def long(self) -> int:
        return struct.unpack("<i", self._take(4))[0]

    def float(self) -> float:
        return struct.unpack("<f", self._take(4))[0]

    def string(self) -> str:
        end = self.data.find(b"\x00", self.pos)
        if end < 0:
            raise A2sError("A2S: null-terminated string runs past the end of the packet")
        raw = self.data[self.pos:end]
        self.pos = end + 1
        return raw.decode("utf-8", errors="replace")

    def raw(self, n: int) -> bytes:
        return self._take(n)

    def _take(self, n: int) -> bytes:
        if self.pos + n > len(self.data):
            raise A2sError("A2S: response is shorter than the protocol requires")
        raw = self.data[self.pos:self.pos + n]
        self.pos += n
        return raw


class _Protocol(asyncio.DatagramProtocol):
    def __init__(self, finished: asyncio.Future):
        self.finished = finished

    def datagram_received(self, data: bytes, addr) -> None:
        if not self.finished.done():
            self.finished.set_result(data)

    def error_received(self, exc: Exception) -> None:
        if not self.finished.done():
            self.finished.set_exception(A2sError(f"A2S: {exc}"))


async def _query(host: str, port: int, payload: bytes, timeout: float) -> bytes:
    """Send one UDP packet and wait for exactly one response."""
    loop = asyncio.get_running_loop()
    finished: asyncio.Future = loop.create_future()
    try:
        transport, _ = await asyncio.wait_for(
            loop.create_datagram_endpoint(lambda: _Protocol(finished), remote_addr=(host, port)),
            timeout,
        )
    except (OSError, asyncio.TimeoutError) as e:
        raise A2sError(f"A2S: {host}:{port} unreachable ({e})") from e
    try:
        transport.sendto(payload)
        data = await asyncio.wait_for(finished, timeout)
    except asyncio.TimeoutError as e:
        raise A2sError(f"A2S: no response from {host}:{port} within {timeout:g} s") from e
    finally:
        transport.close()
    if len(data) > _MAX_PACKET:
        raise A2sError("A2S: response larger than one packet — not evaluated")
    if not data.startswith(_HEADER):
        # 0xFFFFFFFE is the header of a SPLIT response. See the module docstring: unknown.
        raise A2sError("A2S: unknown packet header (split response?)")
    return data[4:]


async def _with_challenge(
    host: str, port: int, prefix: bytes, expected: int, timeout: float, *, suffix: bytes = b""
) -> _Reader:
    """Send the request; if the server demands a challenge, repeat with `prefix + challenge`.

    Steam servers usually answer the first request with `0x41` and a number. Exactly ONE retry:
    if the server demands another challenge after that, something is wrong, and a loop would
    turn it into a flood against a foreign host.
    """
    request = prefix + suffix
    for attempt in range(2):
        reader = _Reader(await _query(host, port, request, timeout))
        kind = reader.byte()
        if kind == expected:
            return reader
        if kind != _CHALLENGE:
            raise A2sError(f"A2S: unexpected response type {kind:#x}")
        if attempt:
            raise A2sError("A2S: server demands another challenge after the second request")
        request = prefix + reader.raw(4)
    raise A2sError("A2S: challenge exchange not completed")   # pragma: no cover


async def info(host: str, port: int, timeout: float = 5.0) -> dict:
    """A2S_INFO → {"name", "map", "game", "version", "players", "max_players", "password"}.

    `version` is the game version reported by the SERVER. For Valheim that is the only place
    it exists at all, because the image fetches it fresh from Steam on every start.
    """
    reader = await _with_challenge(host, port, _INFO_REQUEST, _INFO_RESPONSE, timeout)
    reader.byte()                       # protocol version
    name = reader.string()
    map_name = reader.string()
    folder = reader.string()
    game = reader.string()
    reader.short()                      # Steam app ID (truncated)
    players = reader.byte()
    max_players = reader.byte()
    reader.byte()                       # bots
    reader.byte()                       # server type
    reader.byte()                       # operating system
    password = reader.byte()
    reader.byte()                       # VAC
    version = reader.string()
    return {
        "name": name, "map": map_name, "folder": folder, "game": game,
        "version": version, "players": players, "max_players": max_players,
        "password": bool(password),
    }


async def players(host: str, port: int, timeout: float = 5.0) -> list[dict]:
    """A2S_PLAYER → [{"name", "score", "duration"}, …].

    Empty names occur, since not every game fills the field. They are returned rather than
    filtered out: the NUMBER of entries is still meaningful, and a caller that only needs
    names can see for itself that there are none.
    """
    reader = await _with_challenge(
        host, port, _PLAYER_REQUEST, _PLAYER_RESPONSE, timeout, suffix=_NO_CHALLENGE
    )
    count = reader.byte()
    out: list[dict] = []
    for _ in range(count):
        reader.byte()                   # index (not reliably filled by the server)
        out.append({
            "name": reader.string(),
            "score": reader.long(),
            "duration": round(reader.float(), 1),
        })
    return out
