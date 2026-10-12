"""A game that cannot be told to save (Valheim) is never archived while it runs.

After A12 the weekly and the manual backup stopped Valheim first, but the mandatory backup of a
scheduled restart still copied the running server: Valheim only saves on stop and every 20
minutes, so the archive held the last autosave. The restart stops the server anyway; the archive
is now taken in that gap. A configuration write takes no archive at all: Valheim's settings only
reach the container through a recreate, which backs up after its stop.
"""
import asyncio
import json
from types import SimpleNamespace

import pytest
from sqlmodel import Session, select

from app.models import RestartSchedule, ServerIncident
from app.models.server import ConsoleKind, GameServer, GameType
from app.services.drivers.base import BackupSpec


def _srv(tmp_path):
    s = GameServer(key="vh-1", game_type=GameType.valheim, container_name="gs-vh-1",
                   data_dir=str(tmp_path), console_kind=ConsoleKind.none, enabled=True)
    s.id = 2
    return s


class _Docker:
    """Run state that follows stop/start, and every step in one timeline."""

    def __init__(self, timeline, *, refuse_stop=False):
        self.running = True
        self.timeline = timeline
        self.refuse_stop = refuse_stop

    async def state(self, name):
        return {"exists": True, "running": self.running, "restarting": False, "restarts": 0}

    async def action(self, name, act, **kw):
        if act == "stop" and self.refuse_stop:
            raise RuntimeError("dockerproxy: 403")
        self.timeline.append(act)
        self.running = act != "stop"


class _Valheim:
    """The parts of `ValheimDriver` the restart uses: no save command, so a stop is needed."""

    async def announce(self, server, text):
        return None

    async def save(self, server):
        return None

    async def graceful_shutdown(self, server):
        return False

    def backup_spec(self, server):
        def _cannot_save():
            raise AssertionError("the quiesce hook ran: the archive was taken from the running server")

        return BackupSpec(data_dir=f"{server.data_dir}/config", world_subdirs=("worlds_local",),
                          stop_for_consistency=True, pre_hook=_cannot_save)


@pytest.fixture()
def restart(monkeypatch, mem_engine, wire_lifecycle, tmp_path):
    """Run one scheduled restart with "back up first" for a Valheim server; return what happened."""
    from app.services import backup, restarts, world_lock
    from app.services import servers as servers_registry

    def _run(*, pack_fails=False, stopped_meanwhile=False, refuse_stop=False):
        timeline: list[str] = []
        docker = _Docker(timeline, refuse_stop=refuse_stop)
        server = _srv(tmp_path)
        wire_lifecycle(docker)
        monkeypatch.setattr(world_lock, "_locks", {})
        monkeypatch.setattr(world_lock, "_holders", {})
        monkeypatch.setattr(restarts, "engine", mem_engine)
        monkeypatch.setattr("app.db.engine", mem_engine)
        monkeypatch.setattr(restarts, "get_driver", lambda s: _Valheim())
        monkeypatch.setattr(restarts, "watchdog_suppress", lambda *a: None)
        monkeypatch.setattr(servers_registry, "resolve_server", lambda sid: server)

        async def no_blockers(_s):
            return []

        monkeypatch.setattr(restarts.mod_state, "guard_boot", no_blockers)

        def pack(label, **kw):
            timeline.append("pack while " + ("RUNNING" if docker.running else "stopped"))
            assert kw["pre_hook"] is None and kw["post_hook"] is None
            if pack_fails:
                raise OSError("disk full")
            return {"name": f"20261012-050000-{label}.tar.gz"}

        # Both places an archive could come from: the hot path in `restarts` and `backup`.
        monkeypatch.setattr(restarts, "create_backup", pack)
        monkeypatch.setattr(backup, "create_backup", pack)

        if stopped_meanwhile:
            async def gone(_server, _sid):
                return False

            monkeypatch.setattr(restarts, "_still_running_and_wanted", gone)

        with Session(mem_engine) as s:
            sch = RestartSchedule(server_id=2, backup_first=True, enabled=True)
            s.add(sch)
            s.commit()
            sid = sch.id
        asyncio.run(restarts._do_restart(sid))
        with Session(mem_engine) as s:
            incidents = [(i.kind, i.detail) for i in s.exec(select(ServerIncident)).all()]
        return SimpleNamespace(timeline=timeline, running=docker.running, incidents=incidents)

    return _run


def test_the_scheduled_restart_archives_valheim_while_it_is_stopped(restart):
    r = restart()
    assert r.timeline == ["stop", "pack while stopped", "start"], r.timeline
    assert r.running
    assert r.incidents == []


def test_a_failed_archive_does_not_leave_the_server_down(restart):
    r = restart(pack_fails=True)
    assert r.timeline == ["stop", "pack while stopped", "start"], r.timeline
    assert r.running
    [(kind, detail)] = r.incidents
    assert kind == "backup_failed"
    assert "disk full" in detail and "started again" in detail


def test_a_server_stopped_meanwhile_is_neither_archived_nor_started(restart):
    """The idle auto-stop may have stopped it while this restart waited for the lock."""
    r = restart(stopped_meanwhile=True)
    assert r.timeline == []


def test_an_unconfirmed_stop_archives_nothing_and_says_so(restart):
    """Docker did not take the stop: the server still runs, so no hot copy and no start."""
    r = restart(refuse_stop=True)
    assert r.timeline == []
    assert r.running
    [(kind, detail)] = r.incidents
    assert kind == "restart_failed" and "could not be confirmed" in detail


def test_a_valheim_config_write_takes_no_archive_and_names_the_recreate(monkeypatch, mem_engine):
    """The write only reaches the container through a recreate, which archives after its stop."""
    from app.models.user import User
    from app.routers import server_ops
    from app.services import backup
    from app.services.drivers.valheim import ValheimDriver

    monkeypatch.setattr("app.db.engine", mem_engine)
    server = GameServer(
        key="vh-1", game_type=GameType.valheim, container_name="gs-vh-1",
        data_dir="/data/servers/vh-1", console_kind=ConsoleKind.none,
        game_ports='{"game": "2456/udp"}', extra=json.dumps({"env": {"SERVER_NAME": "Old"}}),
    )
    user = User(username="admin", display_name="Admin", pw_hash="x")
    with Session(mem_engine) as s:
        s.add(server)
        s.add(user)
        s.commit()
        s.refresh(server)
        s.refresh(user)

    packed: list[str] = []
    for name in ("create_backup", "create_backup_exclusive", "backup_with_stop"):
        monkeypatch.setattr(backup, name, lambda *a, _name=name, **k: packed.append(_name))

    with Session(mem_engine) as s:
        out = server_ops.put_config(
            server_ops.ConfigUpdate(values={"SERVER_NAME": "New"}), server, ValheimDriver(), s, user,
        )
    assert packed == []
    assert out["ok"] is True
    assert "recreate" in json.dumps(out)
