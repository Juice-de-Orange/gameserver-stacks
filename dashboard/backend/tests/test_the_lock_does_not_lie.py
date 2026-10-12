"""Whoever asks the world lock gets the truth — and whoever wants it gets it or a 409.

Four points on one theme:

* **`held_by` must not report "free" while an operation is already under way.**
  `asyncio.Lock.release()` sets `_locked = False` and only wakes the waiter via a callback — in
  between, `locked()` says "free" although a world-writing operation is waiting. A second
  operation would then be **accepted instead of rejected**, wait for minutes in `hold()` and
  afterwards work with a state it measured BEFORE the first operation: mandatory backup and
  rollback against a world that is already half moved.
* **The weekly run must ask the lock before packing** — otherwise the archive is created in the
  middle of extracting a world and then sits at the top of the list as the newest successful
  backup.
* **"Running when in doubt" must not turn into "start when in doubt" for the scheduled restart.**
  The error direction of `is_running` is cautious for stopping; for the question "may I restart?"
  it is fail-open, and `restart_server` has `docker start` in its `finally`.
* **An aborted operation must not leave the watchdog silenced.** The silencing is set before it
  is certain the operation takes place at all — with an unconfirmed stop it would stay for 20
  minutes, precisely in the situation in which the watchdog is needed.
"""
import asyncio

import pytest
from fastapi import HTTPException

from app.models.server import GameServer, GameType
from app.services import world_lock


def _srv(key="pz-1", sid=1, container=None) -> GameServer:
    s = GameServer(key=key, game_type=GameType.pz, container_name=container or key, enabled=True)
    s.id = sid
    return s


@pytest.fixture(autouse=True)
def fresh_locks(monkeypatch):
    """`asyncio.Lock` binds to the loop of its first wait — one per test."""
    monkeypatch.setattr(world_lock, "_locks", {})
    monkeypatch.setattr(world_lock, "_holders", {})


# --------------------------------------------------------------------- The lock itself

def test_a_second_world_operation_is_rejected_instead_of_queued():
    """Waiting here means working on later with stale measurements. That is not worth waiting for."""
    async def second_attempt():
        async with world_lock.hold("pz-1", "backup.restore"):
            pass                              # must never be reached

    async def scenario():
        async with world_lock.hold("pz-1", "mods.apply"):
            assert world_lock.held_by("pz-1") == "mods.apply"
            # `wait_for`, so that the OLD behaviour (waiting) ends as a failure instead of a hang:
            # a test that blocks the run gets switched off instead of read.
            with pytest.raises(HTTPException) as ei:
                await asyncio.wait_for(second_attempt(), timeout=2)
            assert ei.value.status_code == 409
            assert "a mod change" in str(ei.value.detail)

    asyncio.run(scenario())


def test_after_the_end_the_lock_is_free_again_immediately():
    """Cross-check: the rejection must not outlive the end of the operation."""
    async def scenario():
        async with world_lock.hold("pz-1", "mods.apply"):
            pass
        assert world_lock.held_by("pz-1") is None
        async with world_lock.hold("pz-1", "backup.restore"):
            assert world_lock.held_by("pz-1") == "backup.restore"

    asyncio.run(scenario())


def test_another_server_is_not_affected():
    async def scenario():
        async with world_lock.hold("pz-1", "mods.apply"):
            async with world_lock.hold("pz-2", "backup.restore"):
                assert world_lock.held_by("pz-2") == "backup.restore"

    asyncio.run(scenario())


# --------------------------------------------------------------------- The weekly run

def test_the_weekly_run_does_not_pack_during_a_running_world_operation(monkeypatch, mem_engine):
    """An archive of a half-moved world is worse than a skipped archive."""
    from app.services import backup as backup_svc
    from app.services import servers as servers_registry

    srv = _srv("pz-1", 1)

    class _Drv:
        def backup_spec(self, server):
            from app.services.drivers.base import BackupSpec
            return BackupSpec(data_dir="/irrelevant", world_subdirs=("Saves",))

    monkeypatch.setattr(servers_registry, "list_enabled_with_driver", lambda: [(srv, _Drv())])
    monkeypatch.setattr(backup_svc.servers_registry, "list_enabled_with_driver",
                        lambda: [(srv, _Drv())])
    packed: list[str] = []
    monkeypatch.setattr(backup_svc, "create_backup",
                        lambda label, **kw: packed.append(label) or {"name": "x", "size": 1})
    incidents: list[tuple[str, str]] = []
    monkeypatch.setattr(backup_svc, "_incident",
                        lambda server, kind, detail: incidents.append((kind, detail)))

    async def scenario():
        async with world_lock.hold("pz-1", "mods.apply"):
            return await backup_svc.scheduled_backup()

    result = asyncio.run(scenario())

    assert packed == [], "packed in the middle of a running world operation"
    assert incidents and incidents[0][0] == "backup_skipped"
    assert result and result[0].get("skipped")


# --------------------------------------------------------------------- The scheduled restart

def test_an_unanswerable_running_state_starts_no_server(monkeypatch, mem_engine,
                                                                fake_driver_cls, wire_lifecycle):
    """"Running when in doubt" is right for STOPPING — for restarting it is fail-open."""
    from sqlmodel import Session

    from app.models import RestartSchedule
    from app.services import restarts
    from app.services import servers as servers_registry

    srv = _srv("pz-1", 7, "cont-pz")

    class _DockerWithoutAnswer:
        def __init__(self):
            self.actions: list[tuple[str, str]] = []

        async def state(self, name):
            raise RuntimeError("dockerproxy unreachable")

        async def action(self, name, action, **kw):
            self.actions.append((name, action))

    docker = _DockerWithoutAnswer()
    monkeypatch.setattr(restarts, "engine", mem_engine)
    wire_lifecycle(docker)
    monkeypatch.setattr(restarts, "get_driver", lambda s: fake_driver_cls())
    monkeypatch.setattr(restarts, "watchdog_suppress", lambda key, mins: None)
    monkeypatch.setattr(servers_registry, "resolve_server", lambda sid: srv)
    monkeypatch.setattr(restarts.mod_state, "guard_boot", lambda s: [])

    with Session(mem_engine) as s:
        sch = RestartSchedule(time_hhmm="05:00", server_id=7, backup_first=False, enabled=True)
        s.add(sch)
        s.commit()
        sid = sch.id

    asyncio.run(restarts._do_restart(sid))

    assert docker.actions == [], "a server was started although nobody knew whether it was running"


# --------------------------------------------------------------------- The watchdog

def test_an_aborted_operation_does_not_leave_the_watchdog_silenced(monkeypatch, fake_driver_cls):
    """The stop could not be confirmed — then the watchdog is what can still help."""
    from app.services import lifecycle, watchdog

    srv = _srv("pz-1", 1, "cont-pz")

    class _DockerKeepsRunning:
        async def state(self, name):
            return {"exists": True, "running": True, "restarts": 0}

        async def action(self, name, act, **kw):
            pass

    monkeypatch.setattr(lifecycle, "docker", _DockerKeepsRunning())
    monkeypatch.setattr(lifecycle, "_STOP_MARK_DELAY", 0)
    monkeypatch.setattr(lifecycle, "_driver", lambda s: fake_driver_cls())
    monkeypatch.setattr(lifecycle, "stop_grace", lambda s: 1)
    monkeypatch.setattr(lifecycle, "_locks", {})
    watchdog._states.pop("pz-1", None)

    before = watchdog._state("pz-1").suppress_until

    with pytest.raises(HTTPException) as ei:
        asyncio.run(lifecycle.stop_and_snapshot(srv, fake_driver_cls(), "pre-test"))
    assert ei.value.status_code == 409

    assert watchdog._state("pz-1").suppress_until == before, \
        "the silencing stayed in place although nothing happened at all"
