"""The operating commands for a host reboot (`app.cli`).

They are the only place where a server is brought back up without a human — so the record list
must be exact. A missing entry leaves a game server silently down after the reboot; an extra one
starts a deliberately stopped server on its own.
"""
import json
from pathlib import Path

from app import cli
from app.models.server import GameServer, GameType


def _srv(key, container=None, game=GameType.pz):
    s = GameServer(key=key, game_type=game, container_name=container or key)
    s.id = abs(hash(key)) % 1000
    return s


class _Docker:
    def __init__(self, running: set[str]):
        self.running = set(running)
        self.actions: list[tuple[str, str]] = []

    async def state(self, name):
        return {"running": name in self.running, "exists": True}

    async def action(self, name, action, **kw):
        self.actions.append((name, action))


def _wire(monkeypatch, servers, docker, stopped):
    from app.services import servers as registry

    monkeypatch.setattr(registry, "list_enabled", lambda: list(servers))
    monkeypatch.setattr(registry, "get_by_key", lambda k: next((s for s in servers if s.key == k), None))
    monkeypatch.setattr(cli, "RECORD_PATH", "unused")

    async def fake_stop(server, *, announce_text=None, wait_timeout=None):
        stopped.append(server.key)
        docker.running.discard(server.container_name)
        return True

    from app.services import lifecycle

    monkeypatch.setattr(lifecycle, "stop_server", fake_stop)
    monkeypatch.setattr("app.services.docker_client.docker", docker)


def test_stop_all_records_only_the_running_ones_and_stops_them(monkeypatch, tmp_path):
    servers = [_srv("pz-1"), _srv("mc", game=GameType.minecraft), _srv("vs", game=GameType.vintagestory)]
    docker = _Docker(running={"pz-1", "vs"})
    stopped: list[str] = []
    _wire(monkeypatch, servers, docker, stopped)
    record = str(tmp_path / "record.json")

    assert cli.main(["stop-all", record]) == 0

    assert stopped == ["pz-1", "vs"]           # the stopped MC is not touched
    assert json.loads(Path(record).read_text(encoding="utf-8"))["running"] == ["pz-1", "vs"]


def test_the_record_list_is_written_BEFORE_stopping(monkeypatch, tmp_path):
    """If the run breaks off midway (systemd kills it, power loss), the list must still be
    complete — otherwise only part of the servers comes back after the reboot."""
    servers = [_srv("a"), _srv("b")]
    docker = _Docker(running={"a", "b"})
    record = str(tmp_path / "record.json")
    seen: list[list[str]] = []

    from app.services import lifecycle

    async def stop_and_look(server, *, announce_text=None, wait_timeout=None):
        seen.append(json.loads(Path(record).read_text(encoding="utf-8"))["running"])
        raise RuntimeError("it breaks off here")

    _wire(monkeypatch, servers, docker, [])
    monkeypatch.setattr(lifecycle, "stop_server", stop_and_look)

    assert cli.main(["stop-all", record]) == 0     # one failure must not abort the shutdown
    assert seen[0] == ["a", "b"]                # both were listed before the first one stopped


def test_start_recorded_brings_them_back_and_cleans_up(monkeypatch, tmp_path):
    servers = [_srv("pz-1"), _srv("vs", game=GameType.vintagestory)]
    docker = _Docker(running=set())
    _wire(monkeypatch, servers, docker, [])
    record = str(tmp_path / "record.json")
    Path(record).write_text(json.dumps({"running": ["pz-1", "vs"]}), encoding="utf-8")

    assert cli.main(["start-recorded", record]) == 0

    assert docker.actions == [("pz-1", "start"), ("vs", "start")]
    assert json.loads(Path(record).read_text(encoding="utf-8"))["running"] == []


def test_without_a_record_list_nothing_happens(monkeypatch, tmp_path):
    docker = _Docker(running=set())
    _wire(monkeypatch, [], docker, [])
    assert cli.main(["start-recorded", str(tmp_path / "missing.json")]) == 0
    assert docker.actions == []


def test_a_vanished_server_is_skipped(monkeypatch, tmp_path):
    """A server may have been removed from the registry between shutdown and boot."""
    docker = _Docker(running=set())
    _wire(monkeypatch, [_srv("stays")], docker, [])
    record = str(tmp_path / "record.json")
    Path(record).write_text(json.dumps({"running": ["gone", "stays"]}), encoding="utf-8")

    assert cli.main(["start-recorded", record]) == 0
    assert docker.actions == [("stays", "start")]


def test_an_unknown_command_reports_itself(capsys):
    assert cli.main(["no-such-command"]) == 2
    assert "stop-all" in capsys.readouterr().out


def test_fix_links_derives_from_the_events_and_does_not_guess(monkeypatch, mem_engine, capsys):
    """The dry run must show what WOULD happen — and touch nothing that is unclear.

    Old `player_links` sit on `game="pz"` because the UI once sent no game and the model defaults
    to `"pz"`. MC/VS/Reforger players then never see their statistics: `_game_scope` filters on
    the servers of THIS game.

    The game is derived from real events. A name that appears in several games or in none is NOT
    touched — a wrong mapping credits someone else's playtime to the account, which is worse than
    an empty page.
    """
    from sqlmodel import Session

    from app import cli
    from app.models.player import PlayerEvent, PlayerLink
    from app.models.server import GameServer, GameType

    monkeypatch.setattr(cli, "engine", mem_engine, raising=False)
    import app.db
    monkeypatch.setattr(app.db, "engine", mem_engine)

    with Session(mem_engine) as s:
        pz = GameServer(key="pz-1", game_type=GameType.pz, container_name="pz-1")
        mc = GameServer(key="mc", game_type=GameType.minecraft, container_name="minecraft")
        s.add(pz)
        s.add(mc)
        s.commit()
        s.refresh(pz)
        s.refresh(mc)
        # clearly Minecraft
        s.add(PlayerLink(user_id=1, ingame_username="Steve", game="pz"))
        s.add(PlayerEvent(server_id=mc.id, username="Steve", kind="join"))
        # appears on both -> unclear, left alone
        s.add(PlayerLink(user_id=2, ingame_username="Twofold", game="pz"))
        s.add(PlayerEvent(server_id=mc.id, username="Twofold", kind="join"))
        s.add(PlayerEvent(server_id=pz.id, username="Twofold", kind="join"))
        # no events -> unclear
        s.add(PlayerLink(user_id=3, ingame_username="Unseen", game="pz"))
        s.commit()

    assert cli.main(["fix-links"]) == 0
    out = capsys.readouterr().out
    assert "'pz' → 'minecraft'" in out and "Steve" in out
    assert "Twofold" in out and "several games" in out
    assert "Unseen" in out and "no events" in out
    assert "Dry run" in out

    with Session(mem_engine) as s:
        assert [li.game for li in s.exec(__import__("sqlmodel").select(PlayerLink)).all()] == ["pz"] * 3, \
            "the dry run wrote"

    assert cli.main(["fix-links", "--write"]) == 0
    with Session(mem_engine) as s:
        games = {li.ingame_username: li.game for li in s.exec(__import__("sqlmodel").select(PlayerLink)).all()}
    assert games == {"Steve": "minecraft", "Twofold": "pz", "Unseen": "pz"}
