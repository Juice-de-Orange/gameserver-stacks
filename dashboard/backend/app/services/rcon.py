"""Minimal async Source RCON client (Project Zomboid speaks Source RCON)."""
from __future__ import annotations

import asyncio
import struct


_AUTH = 3
_AUTH_RESPONSE = 2
_EXECCOMMAND = 2
_RESPONSE_VALUE = 0
# Sentinel request ID of the multi-packet terminator: an empty RESPONSE_VALUE that a server
# echoes AFTER all response packets. It is still recognised but no longer sent (see
# `_read_command_response`). Max int32 (fits "<i"), guaranteed ≠ the IDs in use, 1 (auth) and
# 2 (command).
_SENTINEL = 0x7FFFFFFF
# Idle window for FOLLOW-UP packets of a multi-packet response. Short, because it only has to
# bridge the gap between two parts of the same response (same TCP connection, same Docker
# network) — and because every single-packet response pays it as waiting time.
_IDLE_S = 0.3


class RconError(Exception):
    pass


def _encode(req_id: int, ptype: int, body: str) -> bytes:
    payload = struct.pack("<ii", req_id, ptype) + body.encode("utf-8") + b"\x00\x00"
    return struct.pack("<i", len(payload)) + payload


async def _read_packet(reader: asyncio.StreamReader) -> tuple[int, int, str]:
    header = await reader.readexactly(4)
    (length,) = struct.unpack("<i", header)
    data = await reader.readexactly(length)
    req_id, ptype = struct.unpack("<ii", data[:8])
    body = data[8:-2].decode("utf-8", errors="replace")
    return req_id, ptype, body


async def _read_command_response(
    reader: asyncio.StreamReader, timeout: float, idle: float = _IDLE_S
) -> str:
    """Accumulate RESPONSE_VALUE packets until the connection goes idle (for `multi=True`).

    Source RCON splits responses >4096 B across several packets, and the protocol does not say
    how many follow. The usual trick is an empty RESPONSE_VALUE as a terminator, which the
    server echoes after all response packets.

    **Paper does not tolerate that packet.** Measured against Paper 26.1.2: without the
    terminator packet every call succeeds, with it most calls fail — the server closes the
    connection as soon as the client sends a packet of type RESPONSE_VALUE, before it sends the
    actual response. The symptom is flakiness rather than a clean error.

    So the terminator is not SENT. It is still recognised — a server that echoes one on its own
    ends the loop immediately. Otherwise the FIRST packet may take up to `timeout`, every further
    one only `idle`; when none arrives, the response is complete.

    Only a timeout WITHOUT any chunk raises — then there was no response at all.
    """
    chunks: list[str] = []
    while True:
        try:
            # The first packet is the response itself; anything after it is a continuation.
            deadline = timeout if not chunks else idle
            req_id, _ptype, body = await asyncio.wait_for(_read_packet(reader), deadline)
        except (asyncio.TimeoutError, asyncio.IncompleteReadError):
            if chunks:
                return "".join(chunks).strip()
            raise RconError("RCON timed out while reading the response")
        if req_id == _SENTINEL:
            return "".join(chunks).strip()
        chunks.append(body)


async def rcon_command(
    host: str, port: int, password: str, command: str, timeout: float = 8.0, *, multi: bool = False
) -> str:
    """Run one RCON command and return the response.

    `multi=False` (default): reads EXACTLY ONE response packet (no extra packets sent). This is
    the path Project Zomboid uses.
    `multi=True`: Source RCON multi-packet read for responses >4096 B (e.g. Minecraft); see
    `_read_command_response`. Only used when a driver asks for it explicitly.
    """
    try:
        reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout)
    except (OSError, asyncio.TimeoutError) as e:
        raise RconError(f"RCON unreachable: {e}") from e
    try:
        writer.write(_encode(1, _AUTH, password))
        await writer.drain()
        # Source RCON may send an empty RESPONSE_VALUE before the AUTH_RESPONSE.
        req_id, ptype, _ = await asyncio.wait_for(_read_packet(reader), timeout)
        if ptype != _AUTH_RESPONSE:
            req_id, ptype, _ = await asyncio.wait_for(_read_packet(reader), timeout)
        if req_id == -1:
            raise RconError("RCON authentication failed")

        writer.write(_encode(2, _EXECCOMMAND, command))
        if multi:
            # No empty RESPONSE_VALUE terminator is sent here: Paper closes the connection as
            # soon as it sees one (see `_read_command_response`). The end of the response is
            # detected by idleness, not by an echo.
            await writer.drain()
            return await _read_command_response(reader, timeout)
        await writer.drain()
        _, _, body = await asyncio.wait_for(_read_packet(reader), timeout)
        return body.strip()
    except RconError:
        raise  # pass specific RCON errors (auth failure, multi-read timeout) through unchanged
    except Exception as e:
        # Every read/socket error AFTER the connect (asyncio.TimeoutError, IncompleteReadError,
        # ConnectionReset, …) becomes an RconError. Otherwise a bare asyncio.TimeoutError (server
        # accepts TCP but no longer answers = "hanging") would escape the `except RconError` of
        # the watchdog/stats poller → fail_count never rises → no auto-restart, sessions stay
        # open. Mirrors bercon.py (Reforger). CancelledError derives from BaseException and is
        # deliberately NOT caught (task cancellation must propagate).
        raise RconError(f"RCON error while sending/reading the response: {e}") from e
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass

