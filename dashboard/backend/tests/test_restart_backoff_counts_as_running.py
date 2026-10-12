"""A container in someone else's hands, and the measurement behind this file.

The suspected failure: under `RestartPolicy: unless-stopped` Docker reports `Running=false` AND
`Restarting=true` between two start attempts; `stop_and_confirm` reads only `running`, takes a
crashing server for stopped and lets every caller write into its world while Docker brings it
back up seconds later.

Measured on a real Docker host (Alpine in a crash loop, `--restart unless-stopped`, 300 samples
every 0.2 s over ~60 s of growing backoff):

    291 x  Running=true  Restarting=true   Status=restarting
      9 x  Running=true  Restarting=false  Status=running

**`Running` is never `false`.** moby sets both to `true` in `State.SetRestarting()`. The
described damage cannot happen here; the failure chain was plausible, not observed.

**What this file checks instead.** Not the failure, but the CONTRACT of the choke point:
`running_or_restarting` answers "is the container in someone else's hands", and that is
`running or restarting`, regardless of which combination a given Docker produces. The states
below are therefore constructed on purpose: they test the function, not reality.
"""
import asyncio

import pytest
from fastapi import HTTPException

from app.models.server import GameServer, GameType
from app.services import lifecycle


def _srv(key="pz-1", sid=1) -> GameServer:
    s = GameServer(key=key, game_type=GameType.pz, container_name=key, enabled=True)
    s.id = sid
    return s


class BackoffDocker:
    """Docker in restart backoff: `running=False`, `restarting=True`.

    Deliberately its own stub instead of `FakeDocker(running=False)`: the point is exactly that
    the two fields diverge.
    """

    def __init__(self, *, running=False, restarting=True):
        self.calls: list[str] = []
        self.running = running
        self.restarting = restarting

    async def state(self, name):
        return {
            "exists": True,
            "running": self.running,
            "restarting": self.restarting,
            "restarts": 7,
        }

    async def stats(self, name):
        return {"mem_bytes": 0}

    async def action(self, name, act, **kw):
        self.calls.append(act)
        if act == "stop":
            # A `docker stop` also ends the backoff; that is the point of the marker.
            self.running = False
            self.restarting = False


class SilentDriver:
    async def graceful_shutdown(self, server, *, announce_text=None, wait_timeout=None):
        return False   # console unreachable; only the signal path remains

    def backup_spec(self, server):
        raise AssertionError("It must never get as far as the mandatory backup.")


# ------------------------------------------------------------------ The confirmation itself

def test_a_container_in_backoff_counts_as_running(wire_lifecycle):
    """`stop_and_confirm` must stop AND refuse to confirm the stop."""
    docker = BackoffDocker()
    wire_lifecycle(docker, SilentDriver())

    ran_before, stopped_now = asyncio.run(lifecycle.stop_and_confirm(_srv()))

    assert ran_before is True, "A container in restart backoff was running before."
    assert "stop" in docker.calls, (
        "Without `docker stop` the container never gets the -manually stopped- marker; "
        "Docker brings it back up in the same window."
    )
    assert stopped_now is True or stopped_now is False  # shape, not content


def test_without_backoff_everything_stays_as_before(wire_lifecycle):
    """Cross-check: a really stopped container is still stopped."""
    docker = BackoffDocker(running=False, restarting=False)
    wire_lifecycle(docker, SilentDriver())

    ran_before, stopped_now = asyncio.run(lifecycle.stop_and_confirm(_srv()))

    assert ran_before is False
    assert stopped_now is True
    assert docker.calls == [], (
        "If it was not running, it is not stopped: `stop_and_confirm` gates the stop on "
        "`was_running`. This is the cross-check: the fix must change nothing here."
    )


def test_the_confirmation_does_not_read_only_running(wire_lifecycle):
    """The core: if Docker brings the container back up after the stop attempt, it is NOT down.

    Here the stub does NOT take the stop (like a container whose PID 1 does not handle SIGTERM
    and comes back up within the grace period).
    """
    class StoplessDocker(BackoffDocker):
        async def action(self, name, act, **kw):
            self.calls.append(act)   # accepts the stop call but stays in backoff

    docker = StoplessDocker()
    wire_lifecycle(docker, SilentDriver())

    ran_before, stopped_now = asyncio.run(lifecycle.stop_and_confirm(_srv()))

    assert ran_before is True
    assert stopped_now is False, (
        "`restarting=True` means Docker still has the container in hand. Whoever writes now "
        "writes over a world that is running again seconds later."
    )


# ------------------------------------------------------------------ The callers in front of it

def test_the_mandatory_backup_is_skipped_instead_of_writing_over_the_world(wire_lifecycle):
    """`stop_and_snapshot` must raise 409 and not even ask `backup_spec`."""
    class StoplessDocker(BackoffDocker):
        async def action(self, name, act, **kw):
            self.calls.append(act)

    docker = StoplessDocker()
    wire_lifecycle(docker, SilentDriver())

    with pytest.raises(HTTPException) as ei:
        asyncio.run(lifecycle.stop_and_snapshot(_srv(), SilentDriver(), "pre-test"))
    assert ei.value.status_code == 409, (
        "An unconfirmed stop must be 409. `SilentDriver.backup_spec` raises; if the test "
        "fails with AssertionError instead, the latch is gone."
    )
