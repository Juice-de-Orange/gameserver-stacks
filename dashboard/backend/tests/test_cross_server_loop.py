"""Chat bridge loop protection and bus.

The loop guard is the structurally critical component (it prevents echo feedback loops).
The tests use deterministic `now` values instead of the wall clock (no flakiness, no sleep).
"""
import pytest
from sqlmodel import Session, select

from app.models.cross_server import ChatBridgeMessage
from app.models.server import GameServer, GameType
from app.services.cross_server import bus, loop_guard


@pytest.fixture(autouse=True)
def _reset_guard():
    loop_guard.reset()
    yield
    loop_guard.reset()


# ----------------------------------------------------------------- Loop guard

def test_new_chat_is_ingested():
    assert loop_guard.should_ingest("mc", "Bob", "hello world", now=1000.0) is True


def test_dedup_suppresses_immediate_repeat():
    assert loop_guard.should_ingest("mc", "Bob", "hello", now=1000.0) is True
    # same (server, player, message) inside the window → duplicate
    assert loop_guard.should_ingest("mc", "Bob", "hello", now=1005.0) is False
    # allowed again once the dedup window has passed
    assert loop_guard.should_ingest("mc", "Bob", "hello", now=1000.0 + 31) is True


def test_injected_line_is_not_reingested():
    """Core protection: our own injection echoing back as chat is NOT ingested again."""
    injected = "[servertest] Bob: hello world"
    loop_guard.mark_injected(injected, now=1000.0)
    # server B reads the injected line as supposed chat → must be dropped
    assert loop_guard.should_ingest("mc", "someone", injected, now=1001.0) is False


def test_injected_ttl_expires():
    loop_guard.mark_injected("[a] x: y", now=1000.0)
    assert loop_guard.is_injected("[a] x: y", now=1001.0) is True
    assert loop_guard.is_injected("[a] x: y", now=1000.0 + 91) is False


def test_injected_hash_is_whitespace_and_case_insensitive():
    loop_guard.mark_injected("[A]  Bob:   Hello", now=1000.0)
    assert loop_guard.is_injected("[a] bob: hello", now=1001.0) is True


def test_distinct_messages_all_pass():
    assert loop_guard.should_ingest("mc", "Bob", "one", now=1000.0) is True
    assert loop_guard.should_ingest("mc", "Bob", "two", now=1000.0) is True
    assert loop_guard.should_ingest("vs", "Bob", "one", now=1000.0) is True  # other server


def test_player_msg_composite_matches_injection():
    """The 'player: msg' composite is checked against the injected hashes too (defence in depth)."""
    loop_guard.mark_injected("Bob: hello", now=1000.0)
    assert loop_guard.should_ingest("mc", "Bob", "hello", now=1001.0) is False


# ----------------------------------------------------------------- Bus

def _seed_server(session: Session) -> GameServer:
    s = GameServer(key="mc", game_type=GameType.minecraft, display_name="MC", container_name="mc")
    session.add(s)
    session.commit()
    session.refresh(s)
    return s


def test_bus_records_real_chat(mem_engine):
    with Session(mem_engine) as session:
        srv = _seed_server(session)
        row = bus.record_inbound_chat(session, srv, "Bob", "hello")
        assert row is not None and row.kind == "chat" and row.server_key == "mc"
        assert row.dedup_key
        assert len(bus.recent_chat(session)) == 1


def test_bus_drops_injected_echo(mem_engine):
    with Session(mem_engine) as session:
        srv = _seed_server(session)
        loop_guard.mark_injected("[servertest] Bob: hi", now=None)  # mark the injection
        # echo of the same line as supposed chat → not persisted
        assert bus.record_inbound_chat(session, srv, "x", "[servertest] Bob: hi") is None
        assert session.exec(select(ChatBridgeMessage)).all() == []


def test_bus_ignores_empty_message(mem_engine):
    with Session(mem_engine) as session:
        srv = _seed_server(session)
        assert bus.record_inbound_chat(session, srv, "Bob", "   ") is None


def test_bus_settings_default_bridge_off(mem_engine):
    with Session(mem_engine) as session:
        st = bus.get_settings(session)
        assert st.chat_bridge_enabled is False  # off by default
        assert st.presence_enabled is True
        # idempotent: no second row
        bus.get_settings(session)
        assert len(session.exec(select(type(st))).all()) == 1
