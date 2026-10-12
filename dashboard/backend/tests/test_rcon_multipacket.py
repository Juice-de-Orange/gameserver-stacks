"""Multi-packet RCON read (sentinel terminator + timeout/EOF fallback)."""
import asyncio

from app.services import rcon


async def _run(*packets: bytes, eof: bool = False, timeout: float = 2.0) -> str:
    # Build the StreamReader inside the running loop (Py3.13: constructing it without a loop raises).
    r = asyncio.StreamReader()
    for p in packets:
        r.feed_data(p)
    if eof:
        r.feed_eof()
    return await rcon._read_command_response(r, timeout)


def test_accumulates_until_sentinel():
    out = asyncio.run(
        _run(
            rcon._encode(2, rcon._RESPONSE_VALUE, "Hello "),
            rcon._encode(2, rcon._RESPONSE_VALUE, "World"),
            rcon._encode(rcon._SENTINEL, rcon._RESPONSE_VALUE, ""),  # Terminator
            rcon._encode(2, rcon._RESPONSE_VALUE, "IGNORED-after-sentinel"),
        )
    )
    assert out == "Hello World"


def test_single_packet_then_sentinel():
    out = asyncio.run(
        _run(
            rcon._encode(2, rcon._RESPONSE_VALUE, "only"),
            rcon._encode(rcon._SENTINEL, rcon._RESPONSE_VALUE, ""),
        )
    )
    assert out == "only"


def test_eof_without_sentinel_returns_accumulated():
    # Server that does not echo the marker: EOF/timeout with ≥1 chunk → accumulated (legacy behaviour).
    out = asyncio.run(_run(rcon._encode(2, rcon._RESPONSE_VALUE, "Part"), eof=True))
    assert out == "Part"


def test_eof_with_no_chunk_raises():
    try:
        asyncio.run(_run(eof=True))
        raised = False
    except rcon.RconError:
        raised = True
    assert raised


class _FakeWriter:
    """Records what the client really puts on the wire."""

    def __init__(self) -> None:
        self.written: list[bytes] = []

    def write(self, data: bytes) -> None:
        self.written.append(data)

    async def drain(self) -> None:
        pass

    def close(self) -> None:
        pass

    async def wait_closed(self) -> None:
        pass

    def packets(self) -> list[tuple[int, int]]:
        """(req_id, ptype) per sent packet; strips the leading length (4 B)."""
        import struct
        out = []
        for raw in self.written:
            out.append(struct.unpack("<ii", raw[4:12]))
        return out


def test_multi_sends_no_response_value_packet():
    """Paper 26.1.2 drops the connection as soon as the client sends a RESPONSE_VALUE.

    Measured against a running server: with the terminator packet 5 of 24 calls succeeded,
    without it 24 of 24. So the client must not send it.
    """
    writer = _FakeWriter()
    reader = None

    async def _fake_open(host, port):
        return reader, writer

    async def _run():
        nonlocal reader
        reader = asyncio.StreamReader()
        reader.feed_data(rcon._encode(1, rcon._AUTH_RESPONSE, ""))   # auth ok
        reader.feed_data(rcon._encode(2, rcon._RESPONSE_VALUE, "pong"))
        reader.feed_eof()
        real = asyncio.open_connection
        asyncio.open_connection = _fake_open
        try:
            return await rcon.rcon_command("h", 1, "pw", "list", timeout=2.0, multi=True)
        finally:
            asyncio.open_connection = real

    assert asyncio.run(_run()) == "pong"
    ptypes = [ptype for _req, ptype in writer.packets()]
    assert rcon._AUTH in ptypes, "auth packet missing"
    assert rcon._EXECCOMMAND in ptypes, "command packet missing"
    assert rcon._RESPONSE_VALUE not in ptypes, (
        "The client sends a RESPONSE_VALUE terminator packet again; Paper then drops the "
        "connection (measured: 5/24 instead of 24/24)."
    )
