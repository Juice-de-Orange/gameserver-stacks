"""The ingest watermark of the chat bridge.

- `_read_tail` must drop the partial first line after truncation, and `parse_chat` must skip
  lines without a parseable timestamp instead of falling back to now(); otherwise a fragment is
  ingested as "new" and old messages come back as ghost duplicates.
- `at > prev` alone loses messages in the same second as the watermark; the boundary second is
  tracked through a set of seen lines.
"""
import asyncio

from sqlmodel import Session, select

from app.models.cross_server import ChatBridgeMessage, CrossServerSettings
from app.models.server import GameServer, GameType
from app.services import servers as servers_registry
from app.services.cross_server import bridge, loop_guard
from app.services.drivers.pz import PZDriver


def _reset():
    loop_guard.reset()
    bridge.reset_state()


def test_read_tail_drops_partial_first_line(tmp_path):
    p = tmp_path / "log.txt"
    # Two complete lines; max_bytes so small that the first one is cut.
    p.write_text("AAAAAAAAAA\nBBBBBBBBBB\n", encoding="utf-8")
    out = bridge._read_tail(str(p), max_bytes=15)
    # The fragment "AAAA…" is gone; only the intact second line remains.
    assert "AAAAAAAAAA" not in out
    assert "BBBBBBBBBB" in out


def test_read_tail_keeps_all_when_not_truncated(tmp_path):
    p = tmp_path / "log.txt"
    p.write_text("one\ntwo\n", encoding="utf-8")
    out = bridge._read_tail(str(p), max_bytes=65536)
    assert "one" in out and "two" in out


def test_parse_chat_skips_lines_without_timestamp():
    drv = PZDriver()
    # Chat match, but NO leading [DD-MM-YY …] timestamp (e.g. a fragment) → skipped.
    log = "Got message:ChatMessage{chat=General, author='Bob', text='ghost'}."
    assert drv.parse_chat(GameServer(key="pz", game_type=GameType.pz), log) == []


class _LogDocker:
    async def state(self, name):
        return {"running": True}


def _enable(session):
    session.add(CrossServerSettings(chat_bridge_enabled=True))
    session.commit()


def _pz_line(ts, text):
    return f"[{ts}][info] Got message:ChatMessage{{chat=General, author='Bob', text='{text}'}}.\n"


def test_same_second_message_not_lost(monkeypatch, mem_engine, tmp_path):
    """Two messages in the same second, the second arrives only after the first poll:
    it must NOT be lost (watermark with a boundary-second set)."""
    _reset()
    f = tmp_path / "cur_DebugLog-server.txt"
    f.write_text(_pz_line("08-07-26 10:00:30.000", "one"), encoding="utf-8")

    class _FileDrv:
        def chat_log_path(self, server):
            return str(f)

        def parse_chat(self, server, raw):
            return PZDriver().parse_chat(server, raw)

    srv = GameServer(key="pz", game_type=GameType.pz, container_name="pz", bridge_in=True)
    srv.id = 1
    monkeypatch.setattr(bridge, "engine", mem_engine)
    monkeypatch.setattr(bridge, "docker", _LogDocker())
    monkeypatch.setattr(servers_registry, "list_enabled_with_driver", lambda: [(srv, _FileDrv())])

    with Session(mem_engine) as s:
        _enable(s)

    asyncio.run(bridge.ingest_once())  # baseline: sets the watermark to 10:00:30 (line 'one' seen)
    with Session(mem_engine) as s:
        assert s.exec(select(ChatBridgeMessage)).all() == []

    # Second message with an IDENTICAL timestamp, appended after the baseline read.
    f.write_text(
        _pz_line("08-07-26 10:00:30.000", "one") + _pz_line("08-07-26 10:00:30.000", "two"),
        encoding="utf-8",
    )
    asyncio.run(bridge.ingest_once())
    with Session(mem_engine) as s:
        rows = [r.message for r in s.exec(select(ChatBridgeMessage)).all()]
    assert rows == ["two"]  # 'two' got through, 'one' NOT duplicated
    _reset()
