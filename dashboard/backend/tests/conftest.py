"""Shared fixtures.

Most tests build `GameServer(...)` in memory and need no database. Where real persistence
isolation matters (scoping by server_id), `mem_engine` provides an in-memory SQLite engine with
all tables. `FakeDriver` stubs the GameDriver interface (no real RCON/Docker) and is shared
through the `fake_driver_cls` fixture.

* `fake_docker_cls` — a Docker stub that can also **fail**. A stub that always returns a dict
  never enters a single `except` branch around `docker.state()`, and that is exactly where the
  fail-open spots sit.
* `api_client` — behaviour-based access to the real app: WHO answers a route, and with WHICH
  status.
"""
import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel, create_engine

from app.services.drivers.base import BackupSpec
from app.services.rcon import RconError


@pytest.fixture(autouse=True)
def _presence_empty():
    """`services/presence._seen` is PROCESS-GLOBAL and would otherwise outlive every test.

    Every test that authenticates leaves an entry there under its user id. Each `mem_engine`
    starts at id=1 again, so a later test would get the "last online" of a completely different
    test for its own user 1 — `UserOut.from_user` takes the later of the two values. That is
    order-dependent and invisible, hence here and not in a single test file.
    """
    from app.services import presence
    presence.forget_all()
    yield
    presence.forget_all()


@pytest.fixture(autouse=True)
def _player_counts_fresh(monkeypatch):
    """`services/player_count` keeps a process-global answer per server key. Tests reuse keys
    and change what their fake driver answers between two calls, so the sharing window is 0
    here; `tests/test_player_count_is_shared.py` opens it where it tests it."""
    from app.services import player_count

    monkeypatch.setattr(player_count, "MAX_AGE_S", 0.0)
    player_count.forget()
    yield
    player_count.forget()


@pytest.fixture(autouse=True)
def _no_running_containers(monkeypatch):
    """The port check (`services/ports.py`) asks Docker which host ports running containers hold.

    No test has a Docker daemon behind the socket proxy's address: without this stub every create
    and save of a row would wait for a name lookup that cannot succeed. A test that wants
    containers patches `DockerClient.running_containers` itself.
    """
    from app.services.docker_client import DockerClient

    async def _none(self):
        return []

    monkeypatch.setattr(DockerClient, "running_containers", _none)


@pytest.fixture()
def mem_engine():
    import app.models  # noqa: F401 — registers all tables on SQLModel.metadata

    eng = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,  # one shared in-memory DB across all connections
    )
    SQLModel.metadata.create_all(eng)
    yield eng
    eng.dispose()


class FakeDriver:
    """Minimal GameDriver stub for the fan-out tests (no RCON/Docker)."""

    # The watchdog asks this seam too: `drv.players()` is its only liveness probe, and for a
    # server that structurally cannot answer it (Valheim with `SERVER_PUBLIC=false`) an
    # impossible proof is no proof. For this stub the probe is usable.
    def players_usable_as_boot_proof(self, server):
        return True

    # The watchdog asks a DIFFERENT question: can `players()` answer at all?
    def players_is_queryable(self, server):
        return True

    def __init__(self, names=None, fail_rcon=False):
        self.names = list(names or [])
        self.fail_rcon = fail_rcon
        self.sent: list[tuple[str, str]] = []  # (server_key, command) — for assertions

    async def console(self, server, command, *, timeout=8.0):
        if self.fail_rcon:
            raise RconError("down")
        self.sent.append((server.key, command))
        return ""

    async def announce(self, server, text, *, timeout=8.0):
        # mirrors GameDriver.announce (base): PZ `servermsg "…"` via console → calls land in .sent
        from app.services.drivers.base import sanitize_message

        await self.console(server, f'servermsg "{sanitize_message(text)}"')

    async def save(self, server):
        # mirrors GameDriver.save (base): PZ `save` via console → calls land in .sent
        await self.console(server, "save")

    async def graceful_shutdown(self, server):
        # mirrors GameDriver.graceful_shutdown (base): the default cannot shut itself down.
        # Individual tests override this to check the game-specific path.
        return False

    async def players(self, server):
        if self.fail_rcon:
            raise RconError("down")
        return {"count": len(self.names), "names": list(self.names)}

    def parse_events(self, server, log_text):
        return []

    def backup_spec(self, server):
        return BackupSpec(data_dir=f"{server.data_dir}/data", world_subdirs=("Saves", "Server"))


@pytest.fixture()
def fake_driver_cls():
    return FakeDriver


class FakeDocker:
    """Docker stub that **fails** on request — the case that exists in reality.

    `docker.state()` talks to the socket proxy over HTTP (`services/docker_client.py`:
    `httpx.get` + `raise_for_status`). If the proxy is gone, a timeout or a 5xx comes back, i.e.
    an exception. At that moment the surrounding code decides whether it writes over a world
    that may be RUNNING — the choice made in the `except` is a data-loss path, not an edge case.

    Without `fail_state` it behaves like a plain stub that always answers.
    """

    def __init__(self, running=True, *, restarting=False, fail_state=False, fail_action=False,
                 mem_bytes=0):
        self.calls: list[str] = []
        self.running = running
        # A container under `unless-stopped` reports `running=false` AND `restarting=true`
        # between two start attempts. Without this field `.get("restarting")` yields `None`, and
        # the case — a boot crash loop during which someone starts a world operation — cannot be
        # reproduced at all.
        self.restarting = restarting
        self.fail_state = fail_state
        self.fail_action = fail_action
        self.mem_bytes = mem_bytes

    async def state(self, name):
        if self.fail_state:
            raise RuntimeError("dockerproxy unreachable")
        return {
            "exists": True, "running": self.running, "restarting": self.restarting, "restarts": 0,
        }

    async def stats(self, name):
        if self.fail_state:
            raise RuntimeError("dockerproxy unreachable")
        return {"mem_bytes": self.mem_bytes}

    async def action(self, name, act, **kw):
        if self.fail_action:
            raise RuntimeError("dockerproxy unreachable")
        self.calls.append(act)
        self.running = act != "stop"


@pytest.fixture()
def fake_docker_cls():
    return FakeDocker


@pytest.fixture()
def api_client():
    """TestClient on the real app — no network, no Postgres, no lifespan.

    Deliberately **not** `with TestClient(app)`: the context manager starts the lifespan and with
    it the database and the scheduler. Starlette answers requests without it, and that is all
    that is needed here.

    That no Postgres is needed follows from the order in `auth/deps.get_current_user`:
    `oauth2_scheme` comes BEFORE `get_session`, so a request without a token ends with 401 before
    a session is ever opened. A case that needs a DB overrides
    `app.dependency_overrides[get_session]`.
    """
    from fastapi.testclient import TestClient

    from app.main import app

    client = TestClient(app, raise_server_exceptions=False)
    yield client
    app.dependency_overrides.clear()


@pytest.fixture()
def wire_lifecycle(monkeypatch):
    """Wire the fake Docker/driver into `services.lifecycle` as well.

    ALL stop/restart paths run through `services/lifecycle.py`. A test that only replaces
    `restarts.docker` (or similar) would otherwise reach past it to the real Docker client. The
    stop-mark delay also drops to 0 so that the tests do not sleep two seconds per stop.
    """
    from app.services import lifecycle

    def _wire(docker, driver=None):
        monkeypatch.setattr(lifecycle, "docker", docker)
        monkeypatch.setattr(lifecycle, "_STOP_MARK_DELAY", 0)
        monkeypatch.setattr(lifecycle, "_suppress", lambda *a, **k: None)
        if driver is not None:
            monkeypatch.setattr(lifecycle, "_driver", lambda s: driver)
        monkeypatch.setattr(lifecycle, "stop_grace", lambda s: 120)
        # Fresh locks per test: an `asyncio.Lock` binds to the event loop on its first wait, and
        # every test has its own through `asyncio.run`. Without the reset the second test that
        # WAITS on the same key fails with "bound to a different event loop" — depending on
        # test order.
        monkeypatch.setattr(lifecycle, "_locks", {})

    return _wire
