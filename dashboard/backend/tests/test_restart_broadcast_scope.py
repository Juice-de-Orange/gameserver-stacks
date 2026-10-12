"""Scheduled restarts and broadcasts honour server_id (and target_all)."""
import asyncio
import datetime as dt
from types import SimpleNamespace

from sqlmodel import Session

from app.models import BroadcastSchedule, RestartSchedule
from app.models.server import GameServer, GameType


def _srv(key, sid, container=None, enabled=True):
    s = GameServer(key=key, game_type=GameType.pz, container_name=container or key, enabled=enabled)
    s.id = sid
    return s


class _Docker:
    def __init__(self, running_names=None):
        self.actions: list[tuple[str, str]] = []
        self._running_names = running_names  # None → all running

    async def state(self, name):
        running = True if self._running_names is None else (name in self._running_names)
        return {"running": running}

    async def action(self, name, action, **kw):
        self.actions.append((name, action))


# --------------------------------------------------------------- _do_restart

def test_do_restart_targets_scheduled_server(monkeypatch, mem_engine, fake_driver_cls, wire_lifecycle):
    from app.services import restarts, watchdog
    from app.services import servers as servers_registry

    b = _srv("b", 2, "cont-b")
    fake = fake_driver_cls()
    docker = _Docker()
    suppressed: list[tuple[str, float]] = []
    monkeypatch.setattr(restarts, "engine", mem_engine)
    wire_lifecycle(docker)
    monkeypatch.setattr(restarts, "get_driver", lambda s: fake)
    monkeypatch.setattr(restarts, "watchdog_suppress", lambda key, mins: suppressed.append((key, mins)))
    monkeypatch.setattr(servers_registry, "resolve_server", lambda sid: b if sid == 2 else None)

    with Session(mem_engine) as s:
        sch = RestartSchedule(time_hhmm="05:00", server_id=2, backup_first=False, enabled=True)
        s.add(sch)
        s.commit()
        sid = sch.id

    asyncio.run(restarts._do_restart(sid))
    assert docker.actions == [("cont-b", "stop"), ("cont-b", "start")]
    # The window comes from `watchdog.BOOT_WINDOW_MIN`, the one number this project uses for a
    # boot (`verify_boot`, 900 s). A shorter fixed value is too short by construction — the
    # watchdog acts 3 minutes after the silence ends.
    assert suppressed == [("b", watchdog.BOOT_WINDOW_MIN)]
    assert ("b", 'servermsg "Server is restarting now…"') in fake.sent
    assert ("b", "save") in fake.sent


def test_do_restart_without_server_is_noop(monkeypatch, mem_engine, fake_driver_cls, wire_lifecycle):
    """A schedule without `server_id` belongs to no server — there is no default to fall back to."""
    from app.services import restarts

    docker = _Docker()
    monkeypatch.setattr(restarts, "engine", mem_engine)
    wire_lifecycle(docker)
    monkeypatch.setattr(restarts, "get_driver", lambda s: fake_driver_cls())
    monkeypatch.setattr(restarts, "watchdog_suppress", lambda *a: None)

    with Session(mem_engine) as s:
        sch = RestartSchedule(time_hhmm="05:00", server_id=None, enabled=True)
        s.add(sch)
        s.commit()
        sid = sch.id

    asyncio.run(restarts._do_restart(sid))
    assert docker.actions == []


def test_do_restart_missing_or_disabled_is_noop(monkeypatch, mem_engine, fake_driver_cls, wire_lifecycle):
    from app.services import restarts
    from app.services import servers as servers_registry

    docker = _Docker()
    monkeypatch.setattr(restarts, "engine", mem_engine)
    wire_lifecycle(docker)
    monkeypatch.setattr(restarts, "get_driver", lambda s: fake_driver_cls())
    monkeypatch.setattr(restarts, "watchdog_suppress", lambda *a: None)

    with Session(mem_engine) as s:
        sch = RestartSchedule(server_id=99, enabled=True)
        s.add(sch)
        s.commit()
        sid = sch.id

    # deleted row → resolve returns None
    monkeypatch.setattr(servers_registry, "resolve_server", lambda sid_: None)
    asyncio.run(restarts._do_restart(sid))
    # disabled row → the guard applies
    monkeypatch.setattr(servers_registry, "resolve_server", lambda sid_: _srv("d", 3, enabled=False))
    asyncio.run(restarts._do_restart(sid))
    assert docker.actions == []


def test_do_restart_backup_uses_spec(monkeypatch, mem_engine, fake_driver_cls, wire_lifecycle):
    from app.services import restarts
    from app.services import servers as servers_registry

    b = _srv("b", 2, "cont-b")
    calls: list[tuple[str, dict]] = []
    monkeypatch.setattr(restarts, "engine", mem_engine)
    wire_lifecycle(_Docker())
    monkeypatch.setattr(restarts, "get_driver", lambda s: fake_driver_cls())
    monkeypatch.setattr(restarts, "watchdog_suppress", lambda *a: None)
    monkeypatch.setattr(restarts, "create_backup", lambda label, **kw: calls.append((label, kw)) or {"name": "x"})
    monkeypatch.setattr(servers_registry, "resolve_server", lambda sid: b)

    with Session(mem_engine) as s:
        sch = RestartSchedule(server_id=2, backup_first=True, enabled=True)
        s.add(sch)
        s.commit()
        sid = sch.id

    asyncio.run(restarts._do_restart(sid))
    assert len(calls) == 1
    label, kw = calls[0]
    assert label == "pre-restart"
    assert kw["data_dir"] == f"{b.data_dir}/data"  # from the driver's BackupSpec


# ----------------------------------------------------------------- broadcasts

def test_broadcast_single_target_and_clean(monkeypatch, mem_engine, fake_driver_cls):
    from app.services import broadcasts
    from app.services import servers as servers_registry

    b = _srv("b", 2, "cont-b")
    fake = fake_driver_cls()
    monkeypatch.setattr(broadcasts, "engine", mem_engine)
    monkeypatch.setattr(broadcasts, "get_driver", lambda s: fake)
    monkeypatch.setattr(servers_registry, "resolve_server", lambda sid: b)

    with Session(mem_engine) as s:
        bc = BroadcastSchedule(message='Hello; "café"', server_id=2, target_all=False, enabled=True)
        s.add(bc)
        s.commit()
        bid = bc.id

    asyncio.run(broadcasts._send(bid))
    # _clean removes ; " and control characters → only to server B
    assert fake.sent == [("b", 'servermsg "Hello café"')]


def test_broadcast_target_all_skips_stopped(monkeypatch, mem_engine, fake_driver_cls):
    from app.services import broadcasts
    from app.services import servers as servers_registry

    a, b = _srv("a", 1, "cont-a"), _srv("b", 2, "cont-b")
    fa, fb = fake_driver_cls(), fake_driver_cls()
    monkeypatch.setattr(broadcasts, "engine", mem_engine)
    monkeypatch.setattr(broadcasts, "docker", _Docker(running_names={"cont-a"}))  # only A is running
    monkeypatch.setattr(servers_registry, "list_enabled_with_driver", lambda: [(a, fa), (b, fb)])

    with Session(mem_engine) as s:
        bc = BroadcastSchedule(message="Everyone", target_all=True, enabled=True)
        s.add(bc)
        s.commit()
        bid = bc.id

    asyncio.run(broadcasts._send(bid))
    assert fa.sent == [("a", 'servermsg "Everyone"')]
    assert fb.sent == []  # B stopped → skipped


# ----------------------------------------------------------------- next_restart

def test_next_restart_filters_by_server(monkeypatch, mem_engine):
    from app.services import restarts

    monkeypatch.setattr(restarts, "engine", mem_engine)
    with Session(mem_engine) as s:
        s1 = RestartSchedule(server_id=1, enabled=True)
        s2 = RestartSchedule(server_id=2, enabled=True)
        s.add(s1)
        s.add(s2)
        s.commit()
        id1, id2 = s1.id, s2.id

    t1, t2 = dt.datetime(2026, 1, 1, 5, 0), dt.datetime(2026, 1, 1, 6, 0)
    jobs = [
        SimpleNamespace(id=f"restart_{id1}", next_run_time=t1),
        SimpleNamespace(id=f"restart_{id2}", next_run_time=t2),
        SimpleNamespace(id=f"restart_{id1}_warn_5", next_run_time=dt.datetime(2026, 1, 1, 4, 55)),
    ]
    monkeypatch.setattr(restarts.scheduler, "get_jobs", lambda: jobs)

    assert restarts.next_restart() == t1.isoformat()               # global: earliest
    assert restarts.next_restart(server_id=2) == t2.isoformat()    # server 2 only
    assert restarts.next_restart(server_id=1) == t1.isoformat()


def test_a_failed_mandatory_backup_stops_the_restart(
    monkeypatch, mem_engine, fake_driver_cls, wire_lifecycle,
):
    """A failed mandatory backup records an incident with its cause and cancels the restart.

    The operator ticked "create a backup before the restart". Any error in `create_backup`
    (full disk, a throwing quiesce hook, a tarfile error) must not let the restart go ahead:
    if the server then fails to come up, there would be no backup and no trace of why. Like the
    mod gate, a scheduled restart must not activate a state whose rollback is known to be missing.
    """
    from app.services import restarts
    from app.services import servers as servers_registry

    b = _srv("b", 2, "cont-b")
    docker = _Docker()
    incidents: list[tuple[str, str]] = []
    monkeypatch.setattr(restarts, "engine", mem_engine)
    wire_lifecycle(docker)
    monkeypatch.setattr(restarts, "get_driver", lambda s: fake_driver_cls())
    monkeypatch.setattr(restarts, "watchdog_suppress", lambda *a: None)
    monkeypatch.setattr(restarts, "_incident", lambda s, k, d: incidents.append((k, d)))
    monkeypatch.setattr(servers_registry, "resolve_server", lambda sid_: b)

    def _no_space(label, **kw):
        raise OSError("no space left on device")

    monkeypatch.setattr(restarts, "create_backup", _no_space)

    with Session(mem_engine) as s:
        sch = RestartSchedule(server_id=2, backup_first=True, enabled=True)
        s.add(sch)
        s.commit()
        sid = sch.id

    asyncio.run(restarts._do_restart(sid))

    assert docker.actions == [], (
        "Restarted without the ticked mandatory backup — the rollback is missing exactly "
        "when a restart needs it."
    )
    assert incidents and incidents[0][0] == "backup_failed"
    assert "no space" in incidents[0][1], "the incident does not state the cause"


def test_a_scheduled_restart_leaves_a_stopped_server_alone(
    monkeypatch, mem_engine, fake_driver_cls, wire_lifecycle,
):
    """A "restart" of a stopped server is not a restart but a start nobody asked for.

    `lifecycle.restart_server` has `docker start` in its `finally` — right for a restart, wrong
    for a server someone (or the idle auto-stop) left down on purpose. Without the check the two
    work against each other: the schedule starts an idle server every night (loading, writing and
    discarding the world, with an incident each cycle) and the idle auto-stop stops it again,
    because `idle_autostop` deliberately does not reset its activity clock for a scheduled restart.
    """
    from app.services import restarts
    from app.services import servers as servers_registry

    b = _srv("b", 2, "cont-b")
    docker = _Docker(running_names=[])   # the server is OFF
    monkeypatch.setattr(restarts, "engine", mem_engine)
    wire_lifecycle(docker)
    monkeypatch.setattr(restarts, "get_driver", lambda s: fake_driver_cls())
    monkeypatch.setattr(restarts, "watchdog_suppress", lambda *a: None)
    monkeypatch.setattr(servers_registry, "resolve_server", lambda sid_: b)

    with Session(mem_engine) as s:
        sch = RestartSchedule(server_id=2, backup_first=False, enabled=True)
        s.add(sch)
        s.commit()
        sid = sch.id

    asyncio.run(restarts._do_restart(sid))

    assert docker.actions == [], (
        f"The scheduled restart touched a stopped server ({docker.actions}): "
        "up every night, down again every morning."
    )


def test_a_scheduled_restart_still_restarts_a_running_server(
    monkeypatch, mem_engine, fake_driver_cls, wire_lifecycle,
):
    """Cross-check for the test above — the normal case must not be affected."""
    from app.services import restarts
    from app.services import servers as servers_registry

    b = _srv("b", 2, "cont-b")
    docker = _Docker()                   # running
    monkeypatch.setattr(restarts, "engine", mem_engine)
    wire_lifecycle(docker)
    monkeypatch.setattr(restarts, "get_driver", lambda s: fake_driver_cls())
    monkeypatch.setattr(restarts, "watchdog_suppress", lambda *a: None)
    monkeypatch.setattr(servers_registry, "resolve_server", lambda sid_: b)

    with Session(mem_engine) as s:
        sch = RestartSchedule(server_id=2, backup_first=False, enabled=True)
        s.add(sch)
        s.commit()
        sid = sch.id

    asyncio.run(restarts._do_restart(sid))
    assert [a for _, a in docker.actions] == ["stop", "start"]


def test_a_failed_mandatory_backup_does_not_silence_the_watchdog(
    monkeypatch, mem_engine, fake_driver_cls, wire_lifecycle,
):
    """An aborted restart must not have called `watchdog_suppress` already.

    Otherwise the watchdog is muted although no restart happens — in exactly the situation that
    needs it: the mandatory backup typically fails because the MC quiesce hook throws, and it
    throws because the game hangs. The watchdog would heal that hang after `fail_threshold` rounds.
    """
    from app.services import restarts
    from app.services import servers as servers_registry

    b = _srv("b", 2, "cont-b")
    suppressed: list[tuple] = []
    monkeypatch.setattr(restarts, "engine", mem_engine)
    wire_lifecycle(_Docker())
    monkeypatch.setattr(restarts, "get_driver", lambda s: fake_driver_cls())
    monkeypatch.setattr(restarts, "watchdog_suppress", lambda *a: suppressed.append(a))
    monkeypatch.setattr(restarts, "_incident", lambda *a: None)
    monkeypatch.setattr(servers_registry, "resolve_server", lambda sid_: b)
    monkeypatch.setattr(restarts, "create_backup", lambda label, **kw: (_ for _ in ()).throw(
        OSError("no space left on device")))

    with Session(mem_engine) as s:
        sch = RestartSchedule(server_id=2, backup_first=True, enabled=True)
        s.add(sch)
        s.commit()
        sid = sch.id

    asyncio.run(restarts._do_restart(sid))
    assert suppressed == [], (
        "The watchdog was silenced although no restart happened."
    )


def test_a_stop_in_between_prevents_the_restart(
    monkeypatch, mem_engine, fake_driver_cls, wire_lifecycle,
):
    """Broadcast, save and backup lie between the first state check and the restart.

    With a multi-GB Minecraft world that is minutes, and the hourly idle stop can fall into this
    window. `restart_server` has `docker start` in its `finally` and would bring the server that
    was just stopped on purpose straight back up — the same up/down loop, one level deeper.
    """
    from app.services import restarts
    from app.services import servers as servers_registry

    b = _srv("b", 2, "cont-b")
    docker = _Docker()
    monkeypatch.setattr(restarts, "engine", mem_engine)
    wire_lifecycle(docker)
    monkeypatch.setattr(restarts, "get_driver", lambda s: fake_driver_cls())
    monkeypatch.setattr(restarts, "watchdog_suppress", lambda *a: None)
    monkeypatch.setattr(servers_registry, "resolve_server", lambda sid_: b)

    # Running on the FIRST call, no longer on the second (right before the restart).
    #
    # The seam is `run_state`, which returns `True | False | None`: the scheduled restart must
    # tell "not running" from "cannot be queried", because in the second case it records an
    # incident instead of skipping silently.
    responses = iter([True, False])
    monkeypatch.setattr(restarts.lifecycle, "run_state",
                        lambda s, **kw: _coro(next(responses, False)))

    with Session(mem_engine) as s:
        sch = RestartSchedule(server_id=2, backup_first=False, enabled=True)
        s.add(sch)
        s.commit()
        sid = sch.id

    asyncio.run(restarts._do_restart(sid))
    assert docker.actions == [], (
        f"The restart undid the idle stop in the backup window: {docker.actions}"
    )


async def _coro(value):
    return value
