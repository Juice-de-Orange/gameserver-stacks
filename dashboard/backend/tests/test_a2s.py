"""A2S client against a real UDP server in the test, not against a dummy.

Why real UDP: the client is the dashboard's only eye on a Valheim server. A test that replaces
`_query` does not check the bytes that really go over the wire, and that is exactly where the
bugs of this kind of protocol live (challenge, ordering, lengths).
"""
import asyncio
import struct

import pytest

from app.services import a2s

HEADER = b"\xff\xff\xff\xff"


def _info_packet(name="My Server", map_name="", game="Valheim", version="0.220.5",
                 players=2, max_players=10, password=1) -> bytes:
    return (
        HEADER + b"I" + bytes([17])
        + name.encode() + b"\x00" + map_name.encode() + b"\x00" + b"valheim\x00"
        + game.encode() + b"\x00"
        # The protocol carries the app ID as a TRUNCATED short; 892970 does not fit, which is
        # exactly why the client does not read it.
        + struct.pack("<H", 892970 & 0xFFFF)
        + bytes([players, max_players, 0, ord("d"), ord("l"), password, 0])
        + version.encode() + b"\x00"
    )


def _player_packet(names) -> bytes:
    out = HEADER + b"D" + bytes([len(names)])
    for i, n in enumerate(names):
        out += bytes([i]) + n.encode() + b"\x00" + struct.pack("<i", 0) + struct.pack("<f", 12.5)
    return out


class _Server(asyncio.DatagramProtocol):
    """A real UDP server that answers from a script."""

    def __init__(self, script):
        self.script = list(script)
        self.received: list[bytes] = []
        self.transport = None

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data, addr):
        self.received.append(data)
        if self.script:
            response = self.script.pop(0)
            if response is not None:
                self.transport.sendto(response, addr)


async def _with_server(script, work):
    loop = asyncio.get_running_loop()
    transport, protocol = await loop.create_datagram_endpoint(
        lambda: _Server(script), local_addr=("127.0.0.1", 0)
    )
    port = transport.get_extra_info("sockname")[1]
    try:
        return await work(port), protocol
    finally:
        transport.close()


def _run(script, work):
    return asyncio.run(_with_server(script, work))


# ------------------------------------------------------------------ A2S_INFO

def test_info_is_read():
    result, _ = _run([_info_packet()], lambda p: a2s.info("127.0.0.1", p, timeout=2.0))
    assert result["players"] == 2 and result["max_players"] == 10
    assert result["version"] == "0.220.5" and result["name"] == "My Server"
    assert result["password"] is True


def test_the_challenge_is_answered():
    """Since 2020 Steam servers answer the first request with `A` + a number."""
    challenge = HEADER + b"A" + struct.pack("<i", 0x1234)
    result, srv = _run([challenge, _info_packet()], lambda p: a2s.info("127.0.0.1", p, timeout=2.0))
    assert result["players"] == 2
    assert len(srv.received) == 2
    # The second request carries the challenge unchanged at its end.
    assert srv.received[1].endswith(struct.pack("<i", 0x1234))
    assert srv.received[1].startswith(HEADER + b"TSource Engine Query\x00")


def test_a_second_challenge_does_not_become_an_endless_loop():
    """Otherwise the dashboard would flood a foreign host with packets."""
    challenge = HEADER + b"A" + struct.pack("<i", 1)
    with pytest.raises(a2s.A2sError, match="challenge"):
        _run([challenge, challenge], lambda p: a2s.info("127.0.0.1", p, timeout=2.0))


def test_no_response_is_an_error_and_not_a_zero():
    """The rule of this module: unclear means unknown.

    A silently reported 0 would mean "nobody there" to `idle_autostop`, and then a server gets
    stopped while someone is playing on it.
    """
    with pytest.raises(a2s.A2sError, match="no response"):
        _run([None], lambda p: a2s.info("127.0.0.1", p, timeout=0.3))


def test_a_truncated_response_is_noticed():
    short = HEADER + b"I" + bytes([17]) + b"Just a name\x00"
    with pytest.raises(a2s.A2sError):
        _run([short], lambda p: a2s.info("127.0.0.1", p, timeout=2.0))


def test_a_split_response_counts_as_unknown():
    """0xFFFFFFFE is the header of a multi-packet response. It is deliberately not reassembled:
    a half-understood reassembly would be exactly the guess this module is meant to avoid."""
    split = b"\xff\xff\xff\xfe" + b"whatever"
    with pytest.raises(a2s.A2sError, match="packet header"):
        _run([split], lambda p: a2s.info("127.0.0.1", p, timeout=2.0))


def test_a_foreign_response_type_is_not_interpreted():
    with pytest.raises(a2s.A2sError, match="unexpected response type"):
        _run([HEADER + b"Z" + b"x" * 20], lambda p: a2s.info("127.0.0.1", p, timeout=2.0))


# ------------------------------------------------------------------ A2S_PLAYER

def test_player_list_is_read():
    listing, srv = _run([_player_packet(["Alice", "Bob"])],
                        lambda p: a2s.players("127.0.0.1", p, timeout=2.0))
    assert [e["name"] for e in listing] == ["Alice", "Bob"]
    assert listing[0]["duration"] == 12.5
    # A2S_PLAYER carries a challenge field from the start; -1 means "have none yet".
    assert srv.received[0] == HEADER + b"U" + b"\xff\xff\xff\xff"


def test_empty_names_are_not_thrown_away():
    """Not every game fills the name field. The COUNT is still a statement."""
    listing, _ = _run([_player_packet(["", ""])], lambda p: a2s.players("127.0.0.1", p, timeout=2.0))
    assert len(listing) == 2


def test_an_empty_server_is_not_an_error():
    listing, _ = _run([_player_packet([])], lambda p: a2s.players("127.0.0.1", p, timeout=2.0))
    assert listing == []
