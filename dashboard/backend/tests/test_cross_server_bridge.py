"""Chat adapters (parse_chat), announce commands, bridge ingest/fanout.

Core risks: (a) only genuine player chat is ingested (no system/join lines),
(b) the fanout NEVER sends back to the origin and marks the injection in the loop_guard,
(c) the watermark prevents a backlog flood on the first poll.
"""
import asyncio
import datetime as dt

import pytest
from sqlmodel import Session, select

from app.models.cross_server import ChatBridgeMessage, CrossServerSettings
from app.models.server import GameServer, GameType
from app.services.cross_server import bridge, bus, loop_guard
from app.services.drivers.minecraft import MinecraftDriver
from app.services.drivers.pz import PZDriver
from app.services.drivers.vintagestory import VintageStoryDriver


@pytest.fixture(autouse=True)
def _reset():
    loop_guard.reset()
    bridge.reset_state()
    yield
    loop_guard.reset()
    bridge.reset_state()


# ----------------------------------------------------------------- parse_chat

def test_mc_parse_chat_extracts_only_player_chat():
    drv = MinecraftDriver()
    log = "\n".join([
        "2026-07-07T10:00:01.100000000Z [10:00:01] [Server thread/INFO]: <Bob> hello world",
        "2026-07-07T10:00:02.100000000Z [10:00:02] [Server thread/INFO]: Alice joined the game",
        "2026-07-07T10:00:03.100000000Z [10:00:03] [Server thread/INFO]: [Server] an announcement",
        "2026-07-07T10:00:04.100000000Z [10:00:04] [Server thread/INFO]: Bob was slain by a zombie",
        "2026-07-07T10:00:05.100000000Z [10:00:05] [Server thread/INFO]: <Alice> hi Bob",
    ])
    chats = drv.parse_chat(GameServer(key="mc", game_type=GameType.minecraft), log)
    assert [(p, m) for p, m, _ in chats] == [("Bob", "hello world"), ("Alice", "hi Bob")]
    assert all(isinstance(at, dt.datetime) for _, _, at in chats)


def test_pz_parse_chat_default_empty():
    # A bare log string yields [] for PZ → no crash, no chat.
    drv = PZDriver()
    assert drv.parse_chat(GameServer(key="pz", game_type=GameType.pz), "some log") == []


def test_pz_parse_chat_from_debuglog_format():
    """PZ (B42): chat from the DebugLog file — format Got message:ChatMessage{chat,author,text}.
    Public channels only; the 'sent to chat' line (duplicate) is NOT matched."""
    import datetime as dt

    drv = PZDriver()
    log = "\n".join([
        "[17-06-26 20:30:30.793][info] Got message:ChatMessage{chat=Local, author='Carol42', text='over here!'}.",
        "[17-06-26 20:30:30.793][info] Message ChatMessage{chat=Local, author='Carol42', text='over here!'} sent to chat (id = 2) members.",
        "[17-06-26 20:31:00.000][info] Got message:ChatMessage{chat=Admin, author='mod', text='secret'}.",
        "[17-06-26 20:31:05.000][info] Got message:ChatMessage{chat=General, author='Bob', text='hello world'}.",
    ])
    out = drv.parse_chat(GameServer(key="pz", game_type=GameType.pz), log)
    assert [(p, m) for p, m, _ in out] == [("Carol42", "over here!"), ("Bob", "hello world")]
    assert out[0][2] == dt.datetime(2026, 6, 17, 20, 30, 30, 793000)  # PZ timestamp parsed


def test_pz_chat_log_path_picks_newest(tmp_path):
    import os

    drv = PZDriver()
    logs = tmp_path / "data" / "Logs"
    logs.mkdir(parents=True)
    old = logs / "2026-07-07_10-00_DebugLog-server.txt"
    old.write_text("x", encoding="utf-8")
    new = logs / "2026-07-08_10-00_DebugLog-server.txt"
    new.write_text("y", encoding="utf-8")
    os.utime(str(old), (1000, 1000))
    os.utime(str(new), (2000, 2000))
    srv = GameServer(key="pz", game_type=GameType.pz, data_dir=str(tmp_path))
    assert os.path.basename(drv.chat_log_path(srv)) == "2026-07-08_10-00_DebugLog-server.txt"


def test_pz_chat_log_path_none_when_missing(tmp_path):
    drv = PZDriver()
    srv = GameServer(key="pz", game_type=GameType.pz, data_dir=str(tmp_path / "nope"))
    assert drv.chat_log_path(srv) is None


def test_vs_parse_chat_from_chatlog_format():
    """VS: chat from server-chat.log — `[Chat] <group> | <players>: <text>`. Group 0 (public) only."""
    import datetime as dt

    drv = VintageStoryDriver()
    log = "\n".join([
        "24.1.2026 16:30:32 [Chat] 0 | Dave1: hi all",
        "24.1.2026 16:35:40 [Chat] 0 | Carol42: hello there",
        "24.1.2026 16:40:00 [Chat] 5 | Mallory: private group chat",  # other group → do not bridge
        "8.7.2026 16:15:59 [Server Notification] Dedicated Server now running on Port 42420",  # no chat
    ])
    out = drv.parse_chat(GameServer(key="vs", game_type=GameType.vintagestory), log)
    assert [(p, m) for p, m, _ in out] == [("Dave1", "hi all"), ("Carol42", "hello there")]
    assert out[0][2] == dt.datetime(2026, 1, 24, 16, 30, 32)


def test_ingest_reads_from_file_when_chat_log_path_set(monkeypatch, mem_engine, tmp_path):
    """PZ path: the ingest reads the DebugLog FILE (not docker.logs), the watermark applies."""
    from app.services import servers as servers_registry

    f = tmp_path / "cur_DebugLog-server.txt"
    line1 = "[08-07-26 10:00:30.000][info] Got message:ChatMessage{chat=General, author='Bob', text='one'}.\n"
    f.write_text(line1, encoding="utf-8")

    class _FileDrv:
        def chat_log_path(self, server):
            return str(f)

        def parse_chat(self, server, raw):
            return PZDriver().parse_chat(server, raw)

    a = _srv("pz", 1, bridge_in=True)
    monkeypatch.setattr(bridge, "engine", mem_engine)
    monkeypatch.setattr(bridge, "docker", _LogDocker())
    monkeypatch.setattr(servers_registry, "list_enabled_with_driver", lambda: [(a, _FileDrv())])

    with Session(mem_engine) as s:
        _enable_bridge(s)
    asyncio.run(bridge.ingest_once())  # baseline → no ingest
    with Session(mem_engine) as s:
        assert s.exec(select(ChatBridgeMessage)).all() == []

    f.write_text(
        line1 + "[08-07-26 10:01:00.000][info] Got message:ChatMessage{chat=General, author='Bob', text='two'}.\n",
        encoding="utf-8",
    )
    asyncio.run(bridge.ingest_once())
    with Session(mem_engine) as s:
        rows = s.exec(select(ChatBridgeMessage)).all()
    assert [r.message for r in rows] == ["two"]  # only the fresh file line


def test_mc_parse_chat_ignores_injected_say_echo():
    """Our own `say` injection with `<…>` in the body must NOT be re-parsed as player chat
    (anchored _MC_CHAT_RE)."""
    drv = MinecraftDriver()
    log = "\n".join([
        # this is how our bridge injection (say) appears in the log — `<Bob>` sits IN THE MIDDLE of the text:
        "2026-07-07T10:00:01.100000000Z [10:00:01] [Server thread/INFO]: [Server] [A] Alice: hi <Bob> x",
        # and an RCON say variant:
        "2026-07-07T10:00:02.100000000Z [10:00:02] [Server thread/INFO]: [Rcon] <Eve> spoof",
    ])
    assert drv.parse_chat(GameServer(key="mc", game_type=GameType.minecraft), log) == []


# ----------------------------------------------------------------- announce (sink command)

def _capture_announce(drv):
    sent = []

    async def rec(server, command, *, timeout=8.0):
        sent.append(command)
        return ""

    drv.console = rec  # instance override of the console method
    return sent


def test_announce_commands_per_game():
    pz, mc, vs = PZDriver(), MinecraftDriver(), VintageStoryDriver()
    sp, sm, sv = _capture_announce(pz), _capture_announce(mc), _capture_announce(vs)
    srv = GameServer(key="x", game_type=GameType.pz)
    asyncio.run(pz.announce(srv, "hello"))
    asyncio.run(mc.announce(srv, "hello"))
    asyncio.run(vs.announce(srv, "hello"))
    assert sp == ['servermsg "hello"']
    assert sm == ["say hello"]
    assert sv == ["announce hello"]  # VS: no leading slash (the RCON mod injects like the console)


def test_announce_sanitizes_rcon_injection():
    mc = MinecraftDriver()
    sent = _capture_announce(mc)
    asyncio.run(mc.announce(GameServer(key="x", game_type=GameType.minecraft), 'hi";stop\n rest'))
    assert sent == ["say histop rest"]  # quote/semicolon/control characters removed


def test_announce_strips_angle_brackets():
    """`<`/`>` must go, so a bridged message never looks like `<Name> chat`."""
    mc = MinecraftDriver()
    sent = _capture_announce(mc)
    asyncio.run(mc.announce(GameServer(key="x", game_type=GameType.minecraft), "[A] Alice: hi <Bob> x"))
    assert sent == ["say [A] Alice: hi Bob x"]


# ----------------------------------------------------------------- Bridge Fanout

class _SinkDriver:
    def __init__(self):
        self.announced = []

    def capabilities(self):
        return frozenset({"announce", "console", "players"})  # announce-capable (the bridge gate lets it through)

    async def announce(self, server, text):
        self.announced.append(text)


class _Docker:
    async def state(self, name):
        return {"running": True}


def _enable_bridge(session):
    session.add(CrossServerSettings(chat_bridge_enabled=True))
    session.commit()


def _srv(key, sid, **kw):
    s = GameServer(key=key, game_type=GameType.minecraft, container_name=key, display_name=key.upper(), **kw)
    s.id = sid
    return s


def test_fanout_excludes_origin_and_marks_injected(monkeypatch, mem_engine):
    from app.services import servers as servers_registry

    a, b = _srv("a", 1, bridge_out=True), _srv("b", 2, bridge_out=True)
    drv_a, drv_b = _SinkDriver(), _SinkDriver()
    monkeypatch.setattr(bridge, "engine", mem_engine)
    monkeypatch.setattr(bridge, "docker", _Docker())
    monkeypatch.setattr(
        servers_registry, "list_enabled_with_driver", lambda: [(a, drv_a), (b, drv_b)]
    )

    with Session(mem_engine) as s:
        _enable_bridge(s)

    # 1st run: baseline (bus empty) → cursor=0, no fanout.
    asyncio.run(bridge.fanout_once())
    assert drv_a.announced == [] and drv_b.announced == []

    # Put chat from A (server_id=1) on the bus.
    with Session(mem_engine) as s:
        s.add(ChatBridgeMessage(server_id=1, server_key="a", kind="chat", player="Bob", message="hi"))
        s.commit()

    # 2nd run: fan out to B (not to A — source exclusion).
    asyncio.run(bridge.fanout_once())
    assert drv_b.announced == ["[A] Bob: hi"]      # default chat_format with the origin display name
    assert drv_a.announced == []                    # the origin gets NOTHING back
    assert loop_guard.is_injected("[A] Bob: hi") is True   # injection marked (echo protection)


def test_fanout_source_exclusion_by_key_when_id_none(monkeypatch, mem_engine):
    """server_id can be NULL → the exclusion MUST work via server_key, otherwise the origin
    would get its own message back."""
    from app.services import servers as servers_registry

    a = GameServer(key="a", game_type=GameType.minecraft, container_name="a", display_name="A", bridge_out=True)
    a.id = None  # unpersisted fallback
    b = _srv("b", 2, bridge_out=True)
    drv_a, drv_b = _SinkDriver(), _SinkDriver()
    monkeypatch.setattr(bridge, "engine", mem_engine)
    monkeypatch.setattr(bridge, "docker", _Docker())
    monkeypatch.setattr(servers_registry, "list_enabled_with_driver", lambda: [(a, drv_a), (b, drv_b)])

    with Session(mem_engine) as s:
        _enable_bridge(s)
    asyncio.run(bridge.fanout_once())  # Baseline
    with Session(mem_engine) as s:
        s.add(ChatBridgeMessage(server_id=None, server_key="a", kind="chat", player="Bob", message="hi"))
        s.commit()
    asyncio.run(bridge.fanout_once())
    assert drv_a.announced == []                 # origin a (id None) excluded via server_key
    assert drv_b.announced == ["[A] Bob: hi"]


def test_fanout_does_not_mark_bare_body(monkeypatch, mem_engine):
    """Only the formatted line is marked, NOT the bare body — otherwise a genuine identical chat
    ("gg") from another server would be wrongly suppressed."""
    from app.services import servers as servers_registry

    a, b = _srv("a", 1, bridge_out=True), _srv("b", 2, bridge_out=True)
    monkeypatch.setattr(bridge, "engine", mem_engine)
    monkeypatch.setattr(bridge, "docker", _Docker())
    monkeypatch.setattr(servers_registry, "list_enabled_with_driver", lambda: [(a, _SinkDriver()), (b, _SinkDriver())])

    with Session(mem_engine) as s:
        _enable_bridge(s)
    asyncio.run(bridge.fanout_once())  # Baseline
    with Session(mem_engine) as s:
        s.add(ChatBridgeMessage(server_id=1, server_key="a", kind="chat", player="Bob", message="gg"))
        s.commit()
    asyncio.run(bridge.fanout_once())
    assert loop_guard.is_injected("[A] Bob: gg") is True    # formatted line marked
    assert loop_guard.is_injected("gg") is False             # bare body NOT marked
    # → a genuine "gg" from server B is still ingested (not dropped as an echo)
    assert loop_guard.should_ingest("b", "Alice", "gg") is True


def test_fanout_noop_when_bridge_disabled(monkeypatch, mem_engine):
    from app.services import servers as servers_registry

    a, b = _srv("a", 1, bridge_out=True), _srv("b", 2, bridge_out=True)
    drv_b = _SinkDriver()
    monkeypatch.setattr(bridge, "engine", mem_engine)
    monkeypatch.setattr(bridge, "docker", _Docker())
    monkeypatch.setattr(servers_registry, "list_enabled_with_driver", lambda: [(a, _SinkDriver()), (b, drv_b)])

    with Session(mem_engine) as s:  # settings row default: chat_bridge_enabled=False
        bus.get_settings(s)
        s.add(ChatBridgeMessage(server_id=1, server_key="a", kind="chat", player="Bob", message="hi"))
        s.commit()

    asyncio.run(bridge.fanout_once())
    assert drv_b.announced == []  # bridge OFF → nothing flows


# ----------------------------------------------------------------- Bridge ingest (watermark)

class _ChatDriver:
    """parse_chat returns a fixed list; announce unused."""

    def __init__(self, lines):
        self._lines = lines

    def chat_log_path(self, server):
        return None  # Docker log source (no file adapter)

    def parse_chat(self, server, raw):
        return list(self._lines)


class _LogDocker:
    async def state(self, name):
        return {"running": True}

    async def logs(self, name, tail=400, timestamps=True):
        return "logdata"


def test_ingest_baseline_then_only_fresh(monkeypatch, mem_engine):
    from app.services import servers as servers_registry

    a = _srv("a", 1, bridge_in=True)
    t0 = dt.datetime(2026, 7, 7, 10, 0, 0)
    t1 = dt.datetime(2026, 7, 7, 10, 0, 30)
    drv = _ChatDriver([("Bob", "one", t0)])
    monkeypatch.setattr(bridge, "engine", mem_engine)
    monkeypatch.setattr(bridge, "docker", _LogDocker())
    monkeypatch.setattr(servers_registry, "list_enabled_with_driver", lambda: [(a, drv)])

    with Session(mem_engine) as s:
        _enable_bridge(s)

    # 1st run: baseline (watermark at t0) → NO ingest of the backlog.
    asyncio.run(bridge.ingest_once())
    with Session(mem_engine) as s:
        assert s.exec(select(ChatBridgeMessage)).all() == []

    # New line at t1 (> watermark) appears in the log tail.
    drv._lines = [("Bob", "one", t0), ("Bob", "two", t1)]
    asyncio.run(bridge.ingest_once())
    with Session(mem_engine) as s:
        rows = s.exec(select(ChatBridgeMessage)).all()
    assert [(r.player, r.message) for r in rows] == [("Bob", "two")]  # only the fresh line


def test_the_chat_prune_deletes_old_rows_and_keeps_fresh_ones(monkeypatch, mem_engine):
    """`chat_bridge_messages` needs a retention rule like metrics, audit, quarantine and backups:
    it grows with every chat line of every server, plus three indexes.
    """
    import datetime as dt

    from app.models.cross_server import ChatBridgeMessage
    from app.services.cross_server import bus

    monkeypatch.setattr(bus, "engine", mem_engine)
    monkeypatch.setattr(bus.settings, "chat_retention_days", 14)

    from app.models.player import utcnow_naive
    now = utcnow_naive()
    with Session(mem_engine) as s:
        s.add(ChatBridgeMessage(server_key="mc", message="old", at=now - dt.timedelta(days=30)))
        s.add(ChatBridgeMessage(server_key="mc", message="borderline", at=now - dt.timedelta(days=13)))
        s.add(ChatBridgeMessage(server_key="mc", message="fresh", at=now))
        s.commit()

    bus.prune_chat()

    with Session(mem_engine) as s:
        remaining = sorted(m.message for m in s.exec(select(ChatBridgeMessage)).all())
    assert remaining == ["borderline", "fresh"], (
        f"the prune hit the wrong rows: {remaining}"
    )
