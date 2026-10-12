"""Docker log demuxing.

Docker multiplexes container logs for non-TTY containers: one 8-byte header per frame (stream id +
size as big-endian). All our containers run without `Tty` — `container_spec` does not set it, the
Docker default is `false` — so the stream really is framed.

`_demux` is stateless and assumes the buffer starts at a frame boundary. That holds for a complete
response body; the live tail (`/ws/logs`) receives `aiter_bytes()` chunks, and TCP chunks do not
fall on frame boundaries.
"""
import pytest

from app.services.docker_client import LogDemuxer, _demux


def _frame(text: str, stream: int = 1) -> bytes:
    """Build a Docker log frame: 1 byte stream, 3 bytes padding, 4 bytes size (BE)."""
    payload = text.encode("utf-8")
    return bytes([stream, 0, 0, 0]) + len(payload).to_bytes(4, "big") + payload


def test_a_complete_body_is_demuxed():
    data = _frame("first line\n") + _frame("second line\n", stream=2)
    assert _demux(data) == "first line\nsecond line\n"


@pytest.mark.parametrize("cut", range(1, 40))
def test_any_chunk_boundary_yields_the_same_text(cut):
    """The result must not depend on WHERE the network splits.

    A cut in the middle of the payload made a stateless call read 8 payload bytes as a header:
    they vanished, and the phantom size read from them shifted the rest.
    """
    expected = "Steam init done\nLoading world\nReady.\n"
    data = _frame("Steam init done\n") + _frame("Loading world\n") + _frame("Ready.\n")

    d = LogDemuxer()
    text = d.feed(data[:cut]) + d.feed(data[cut:])
    assert text == expected


def test_a_cut_inside_the_header_loses_nothing():
    """The second variant: the boundary falls into the 8 header bytes themselves. A stateless
    call emitted 1–7 raw header bytes as text — visible as replacement characters in the browser."""
    data = _frame("A" * 20) + _frame("B" * 20)
    header_middle = 8 + 20 + 3          # in the middle of the second frame's header

    d = LogDemuxer()
    text = d.feed(data[:header_middle]) + d.feed(data[header_middle:])
    assert text == "A" * 20 + "B" * 20
    assert "�" not in text


def test_fed_byte_by_byte_yields_the_same():
    data = _frame("line one\n") + _frame("line two\n")
    d = LogDemuxer()
    assert "".join(d.feed(data[i : i + 1]) for i in range(len(data))) == "line one\nline two\n"


def test_multibyte_characters_survive_the_boundary():
    """UTF-8 at the frame boundary: `errors="replace"` must have nothing to do here."""
    data = _frame("Café déjà vu — running ✓\n")
    d = LogDemuxer()
    text = d.feed(data[:12]) + d.feed(data[12:])
    assert text == "Café déjà vu — running ✓\n"


def test_the_rest_buffer_does_not_grow_without_bound():
    """A phantom size from a broken stream must not fill memory."""
    d = LogDemuxer()
    # The header promises 2 GB, but never enough arrives.
    broken = bytes([1, 0, 0, 0]) + (2 << 30).to_bytes(4, "big")
    d.feed(broken)
    for _ in range(3):
        d.feed(b"x" * (LogDemuxer.MAX_REST // 2))
    assert len(d._rest) <= LogDemuxer.MAX_REST
