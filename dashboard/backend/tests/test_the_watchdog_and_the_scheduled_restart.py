"""Boot window, silence and refusal — the handover between the watchdog and the scheduled restart.

**All of these hang on one seam:** the process-local state through which the stopping jobs tell
the watchdog "this is mine, stay out". It leaked in both directions.

*The watchdog stepped in too early:*
* A boot the dashboard did not drive (Docker after `unless-stopped`, a freshly started backend)
  had **no** window at all — after three minutes of dead RCON the watchdog restarted a server
  that was still loading its world. After a host reboot that hits every server at once.
* The failure counter survived the silencing. The scheduled restart inevitably collects failures
  before it (`announce`, `save`, mandatory backup run deliberately without silence) — a single
  dead answer after the silence was then enough for an autoheal.
* The mandatory backup ran with an **armed** watchdog: for PZ the `save` before it blocks the
  game thread, RCON goes quiet, and three rounds later the watchdog restarts the container in
  the middle of the `tarfile` run.
* The grace period after its own autoheal was ASSIGNED raw (5 min) instead of set via
  `suppress()` — throwing away the 10 minutes `lifecycle` had set right before.

*And it stepped in where it must not:*
* A restart correctly refused by the world lock (409) was booked as "restart_failed", started
  the 20-minute cooldown and cleared the counter — for an intervention that never happened. A
  server that really hung stayed hung twenty minutes longer.
* After a `create-then-swap` the remembered restart counter belonged to the DELETED container.
  `restarts > last_restart_count` never became true again — boot-loop detection was dead after
  every version change.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import time
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.config import settings
from app.services import restarts, watchdog
from app.services.rcon import RconError


def _srv(key="pz-1"):
    return SimpleNamespace(id=1, key=key, container_name=key, game_type=None, enabled=True)


def _docker_state(*, running=True, started_min_ago=120.0, restarts_=0, cid="c1"):
    ts = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None) - dt.timedelta(minutes=started_min_ago)
    return {"exists": True, "running": running, "restarting": False, "restarts": restarts_,
            "id": cid, "started_at": ts.isoformat() + "Z"}


class _DeadRcon:
    def players_is_queryable(self, server):
        return True          # the probe IS queryable — it just does not answer

    async def players(self, server):
        raise RconError("dead")


@pytest.fixture(autouse=True)
def _clean():
    watchdog._states.clear()
    yield
    watchdog._states.clear()


def _run(monkeypatch, server, drv, state):
    class _Dk:
        async def state(self, name):
            return state
    monkeypatch.setattr(watchdog, "docker", _Dk())

    async def _no_blockers(_s):
        return []
    monkeypatch.setattr(watchdog.mod_state, "guard_boot", _no_blockers)
    monkeypatch.setattr(watchdog, "_record", lambda *a, **k: incidents.append(a))
    asyncio.run(watchdog._check_server(server, drv))


incidents: list = []


def test_a_booting_container_is_not_a_hang(monkeypatch):
    """The container has only been running for two minutes — RCON may still be silent."""
    incidents.clear()
    st = watchdog._state("pz-1")
    _run(monkeypatch, _srv(), _DeadRcon(), _docker_state(started_min_ago=2.0))
    assert st.fail_count == 0, "a round inside the boot window must not count as a failure"

    # Outside the window it does count — otherwise the watchdog would be switched off.
    _run(monkeypatch, _srv(), _DeadRcon(), _docker_state(started_min_ago=60.0))
    assert st.fail_count == 1


def test_an_unreadable_start_time_grants_the_window_instead_of_taking_it(monkeypatch):
    """`None` means don't-know — and the safe direction here is: do not touch."""
    incidents.clear()
    st = watchdog._state("pz-1")
    z = _docker_state()
    z["started_at"] = None
    _run(monkeypatch, _srv(), _DeadRcon(), z)
    assert st.fail_count == 0


def test_silencing_clears_the_failure_counter(monkeypatch):
    """Otherwise ONE dead answer after the silence is enough for an autoheal."""
    incidents.clear()
    st = watchdog._state("pz-1")
    st.fail_count = 2                      # collected before the silence (announce/save/backup)
    watchdog.suppress("pz-1", 10)
    _run(monkeypatch, _srv(), _DeadRcon(), _docker_state())
    assert st.fail_count == 0


def test_a_new_container_resets_the_boot_loop_detection(monkeypatch):
    """`create-then-swap` creates a new container; its counter starts at 0."""
    incidents.clear()
    st = watchdog._state("pz-1")
    _run(monkeypatch, _srv(), _DeadRcon(), _docker_state(running=False, restarts_=7, cid="old"))
    assert st.last_restart_count == 7
    _run(monkeypatch, _srv(), _DeadRcon(), _docker_state(running=False, restarts_=0, cid="new"))
    assert st.last_restart_count == 0, "the counter belonged to the deleted container"
    # and from now on growth triggers again
    _run(monkeypatch, _srv(), _DeadRcon(), _docker_state(running=False, restarts_=1, cid="new"))
    assert any(a[0] == "boot_crash_loop" for a in incidents)


def test_a_refused_restart_is_not_a_failure(monkeypatch):
    """409 from the world lock: no "failed" incident, no cooldown, the counter stays."""
    incidents.clear()
    st = watchdog._state("pz-1")
    st.fail_count = 2

    class _Lc:
        async def restart_server(self, server):
            raise HTTPException(409, detail="Restart refused: world operation running")
    import app.services.lifecycle as real
    monkeypatch.setattr(real, "restart_server", _Lc().restart_server)
    _run(monkeypatch, _srv(), _DeadRcon(), _docker_state())

    kinds = [a[0] for a in incidents]
    assert "restart_failed" not in kinds, "a refusal is not a failure"
    assert "restart_blocked" in kinds
    assert st.last_restart is None, "no cooldown for an intervention that never happened"
    assert st.fail_count == 3, "the counter must not be cleared — the server is still hanging"


def test_the_silence_after_the_autoheal_does_not_shorten_another_one(monkeypatch):
    """The autoheal must extend the silence via `suppress()`, never assign `suppress_until` raw."""
    incidents.clear()
    st = watchdog._state("pz-1")
    st.fail_count = 2

    async def _restart(server):
        watchdog.suppress(server.key, 30)   # this is what `lifecycle.restart_server` does in `finally`
    import app.services.lifecycle as real
    monkeypatch.setattr(real, "restart_server", _restart)
    _run(monkeypatch, _srv(), _DeadRcon(), _docker_state())

    remaining = (st.suppress_until - time.monotonic()) / 60.0
    assert remaining > 20, f"the longer silence was thrown away ({remaining:.1f} min)"


def test_a_fresh_host_is_not_in_a_cooldown(monkeypatch):
    """`time.monotonic()` counts from the host's boot. With "never restarted" stored as `0.0`,
    the first twenty minutes after a host boot looked like the cooldown after an autoheal that
    never happened, and a hanging server stayed hung (a CI runner six minutes after its boot
    showed it: no restart, no incident)."""
    incidents.clear()
    monkeypatch.setattr(settings, "watchdog_cooldown_min", 20)
    monkeypatch.setattr(watchdog, "time", SimpleNamespace(monotonic=lambda: 360.0, time=time.time))
    st = watchdog._state("pz-1")
    st.fail_count = 2
    restarted = []

    async def _restart(server):
        restarted.append(server.key)
    import app.services.lifecycle as real
    monkeypatch.setattr(real, "restart_server", _restart)
    _run(monkeypatch, _srv(), _DeadRcon(), _docker_state())

    assert restarted == ["pz-1"], "the third dead answer restarts, whatever the host's uptime"
    assert "auto_restart" in [a[0] for a in incidents]
    assert st.last_restart == 360.0


def test_the_silence_as_a_block_withdraws_itself():
    """`silence()` is for sections with an end — the mandatory backup is exactly such a section."""
    st = watchdog._state("pz-1")
    assert st.suppress_until == 0.0
    with watchdog.silence("pz-1", 15):
        assert st.suppress_until > time.monotonic()
    assert st.suppress_until == 0.0, "after the block the watchdog must be armed again"

    # Another, longer silence set inside the block belongs to someone else and stays.
    with watchdog.silence("pz-1", 5):
        watchdog.suppress("pz-1", 60)
    assert (st.suppress_until - time.monotonic()) / 60.0 > 50


def test_the_mandatory_backup_runs_under_the_world_lock_and_a_silent_guard(monkeypatch):
    """Both latches were missing: the run was invisible to the world lock and the watchdog armed."""
    observed: dict = {}

    class _Drv:
        def backup_spec(self, server):
            return SimpleNamespace(data_dir="/d", world_subdirs=("w",), config_subdirs=(),
                                   pre_hook=None, post_hook=None, stop_for_consistency=False)
        async def announce(self, server, text): pass
        async def save(self, server): pass

    def _pack(*a, **k):
        st = watchdog._state("pz-1")
        observed["holds_world_lock"] = restarts.world_lock.held_by("pz-1")
        observed["watchdog_silent"] = st.suppress_until > time.monotonic()
        return {"name": "x.tar.gz"}

    monkeypatch.setattr(restarts, "create_backup", _pack)
    monkeypatch.setattr(restarts, "get_driver", lambda s: _Drv())
    _set_up_restart(monkeypatch, running=True)

    asyncio.run(restarts._do_restart(sid=1))
    assert observed["holds_world_lock"] == "restart.backup"
    assert observed["watchdog_silent"] is True
    # and afterwards the guard is armed again
    assert watchdog._state("pz-1").suppress_until <= time.monotonic() + 16 * 60


def test_a_running_world_operation_is_not_a_failed_restart(monkeypatch):
    """The 04:45 auto-update and the 05:00 restart inevitably overlap."""
    local_incidents: list = []
    monkeypatch.setattr(restarts, "_incident", lambda s, k, d: local_incidents.append(k))

    class _Drv:
        def backup_spec(self, server):
            return SimpleNamespace(data_dir="/d", world_subdirs=("w",), config_subdirs=(),
                                   pre_hook=None, post_hook=None, stop_for_consistency=False)
        async def announce(self, server, text): pass
        async def save(self, server): pass

    monkeypatch.setattr(restarts, "get_driver", lambda s: _Drv())
    monkeypatch.setattr(restarts, "create_backup", lambda *a, **k: {"name": "x"})
    _set_up_restart(monkeypatch, running=True)

    async def _run():
        async with restarts.world_lock.hold("pz-1", "version.change"):
            await restarts._do_restart(sid=1)
    asyncio.run(_run())
    assert local_incidents == [], f"refusal booked as an incident: {local_incidents}"


def _set_up_restart(monkeypatch, *, running: bool):
    """The environment of `_do_restart`, without a database and without Docker."""
    plan = SimpleNamespace(id=1, server_id=1, backup_first=True, enabled=True)

    class _Sess:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def get(self, model, sid): return plan
    monkeypatch.setattr(restarts, "Session", lambda engine: _Sess())
    monkeypatch.setattr(restarts.servers_registry, "resolve_server", lambda sid: _srv())

    async def _run_state(server, **kw):
        return running
    monkeypatch.setattr(restarts.lifecycle, "run_state", _run_state)

    async def _restart(server, *, precondition=None, **kw):
        # **The first line of the real `restart_server`.** A dummy that does not reproduce the
        # latch only tests the dummy.
        restarts.world_lock.refuse_during_world_operation(server.key, "Restart")
    monkeypatch.setattr(restarts.lifecycle, "restart_server", _restart)

    async def _no_blockers(_s):
        return []
    monkeypatch.setattr(restarts.mod_state, "guard_boot", _no_blockers)


# ---------------------------------------------------------------------------------------------
# What happens between two jobs when one of them waits.
#
# **The common core:** a decision taken BEFORE waiting on `lifecycle._lock` stayed valid
# unchanged afterwards. The lock serialises, it does not re-validate — and the wait here is
# minutes, not milliseconds: `stop_grace` is 120 s for Project Zomboid, and `restart_server`
# holds the same lock across stop AND start.


def test_the_idle_stop_repeats_its_decision_inside_the_lock(monkeypatch):
    """Otherwise it stops the server the 05:00 restart has just brought up."""
    from app.services import idle_autostop as ia

    stopped: list[str] = []

    async def _stop(server, *, precondition=None, **kw):
        # This is how `lifecycle.stop_server` behaves: the precondition is asked INSIDE the
        # lock, i.e. after the wait.
        if precondition is not None and not await precondition():
            return False
        stopped.append(server.key)
        return True

    monkeypatch.setattr(ia.lifecycle, "stop_server", _stop)
    monkeypatch.setattr(ia.backup, "is_packing", lambda d: False)
    monkeypatch.setattr(ia, "_touch", lambda sid: None)
    monkeypatch.setattr(ia, "_incident", lambda sid, text: None)

    now = dt.datetime(2026, 8, 25, 12, 0)
    srv = SimpleNamespace(id=1, key="pz-1", container_name="pz-1", enabled=True,
                          idle_autostop_enabled=True, idle_timeout_minutes=60, last_active_at=now - dt.timedelta(days=30))

    # During the wait another job starts the server: the SECOND `docker.state` reports a fresh
    # start, the grace period applies.
    states = iter([
        {"running": True, "started_at": (now - dt.timedelta(hours=9)).isoformat() + "Z"},
        {"running": True, "started_at": (now - dt.timedelta(seconds=20)).isoformat() + "Z"},
    ])

    class _Dk:
        async def state(self, name):
            return next(states)
    monkeypatch.setattr(ia, "docker", _Dk())
    monkeypatch.setattr(ia, "utcnow_naive", lambda: now)

    class _Drv:
        async def players(self, server):
            return {"count": 0, "names": []}
        def backup_spec(self, server):
            return SimpleNamespace(data_dir="/d")

    asyncio.run(ia._check_server(srv, _Drv(), default_timeout=60))
    assert stopped == [], "the server that had just been started was stopped seconds later"


def test_the_idle_job_does_not_stop_while_packing(monkeypatch):
    """The guard against this existed in only ONE direction."""
    from app.services import idle_autostop as ia

    stopped: list[str] = []
    monkeypatch.setattr(ia.lifecycle, "stop_server",
                        lambda server, **kw: stopped.append(server.key))
    monkeypatch.setattr(ia.backup, "is_packing", lambda d: True)     # tarfile is running
    monkeypatch.setattr(ia, "_touch", lambda sid: None)

    now = dt.datetime(2026, 8, 25, 12, 0)
    srv = SimpleNamespace(id=1, key="pz-1", container_name="pz-1", enabled=True,
                          idle_autostop_enabled=True, idle_timeout_minutes=60, last_active_at=now - dt.timedelta(days=30))

    class _Dk:
        async def state(self, name):
            return {"running": True, "started_at": (now - dt.timedelta(hours=9)).isoformat() + "Z"}
    monkeypatch.setattr(ia, "docker", _Dk())
    monkeypatch.setattr(ia, "utcnow_naive", lambda: now)

    class _Drv:
        async def players(self, server):
            return {"count": 0, "names": []}
        def backup_spec(self, server):
            return SimpleNamespace(data_dir="/d")

    asyncio.run(ia._check_server(srv, _Drv(), default_timeout=60))
    assert stopped == [], "a stop in the middle of the tar run yields exactly the torso the " \
                           "weekly run prevents in the other direction"


def test_the_pack_lock_is_queryable_from_outside(tmp_path):
    from app.services import backup

    folder = str(tmp_path)
    assert backup.is_packing(folder) is False

    seen = {}

    def _work():
        seen["meanwhile"] = backup.is_packing(folder)
        return {"name": "x"}

    backup.create_backup_exclusive(folder, _work)
    assert seen["meanwhile"] is True
    assert backup.is_packing(folder) is False


def test_an_unqueryable_state_is_not_skipped_silently(monkeypatch):
    """All three other abort paths write an incident — this one must too."""
    local_incidents: list[str] = []
    monkeypatch.setattr(restarts, "_incident", lambda s, k, d: local_incidents.append(k))
    _set_up_restart(monkeypatch, running=True)

    async def _unknown(server, **kw):
        return None
    monkeypatch.setattr(restarts.lifecycle, "run_state", _unknown)

    asyncio.run(restarts._do_restart(sid=1))
    assert local_incidents == ["restart_skipped"], \
        "the maintenance restart is skipped and nobody learns about it"


def test_a_server_without_a_possible_liveness_probe_is_not_healed(monkeypatch):
    """Valheim with the Steam query switched off: `players()` structurally cannot tell anything."""
    incidents.clear()
    st = watchdog._state("vh")

    class _NotQueryable(_DeadRcon):
        def players_is_queryable(self, server):
            return False

    for _ in range(5):
        _run(monkeypatch, _srv("vh"), _NotQueryable(), _docker_state())
    assert st.fail_count == 0
    assert [a[0] for a in incidents] == [], \
        "a server that CANNOT answer is not sick — it is not queryable"


def test_the_liveness_probe_is_a_different_question_from_the_boot_proof():
    """Asking `players_usable_as_boot_proof` here would switch the watchdog off for Project Zomboid.

    PZ returns `False` there, not because the player query does not work but because
    `verify_boot` uses the stronger RCON handshake. The two questions coincide ONLY for Valheim;
    everywhere else `players()` is queryable, even where it is not a boot proof.
    """
    from app.services.drivers import get_driver
    from app.models.server import GameType

    class _S:
        def __init__(self, gt):
            self.game_type, self.key, self.container_name = gt, "x", "x"
            self.data_dir, self.extra, self.image_tag = "/d", "{}", "t"
            self.rcon_host, self.rcon_port, self.game_version = "x", 27015, ""
            self.id, self.display_name = 1, "x"

    for gt in (GameType.pz, GameType.minecraft, GameType.vintagestory):
        srv = _S(gt)
        drv = get_driver(srv)
        assert drv.players_is_queryable(srv) is True, (
            f"{gt}: the player query is queryable — the watchdog needs it as a liveness probe, "
            f"even where it is not a boot proof"
        )
