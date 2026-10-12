"""`app.cli create-container` — the first step for a brand-new server.

The dashboard can recreate and start containers, but all those paths assume one already exists
(adopted games). For a truly new game `POST /{key}/start` answers 502 — there is nothing to start.
"""
import asyncio
from types import SimpleNamespace

import pytest
from sqlmodel import Session

from app import cli
from app.models.server import GameServer, GameType, ManagementMode


class _Docker:
    def __init__(self, exists=False, image_present=True):
        self._exists = exists
        self.image_present = image_present
        self.pulled: list[str] = []
        self.created: list[tuple[str, dict]] = []

    async def state(self, name):
        return {"exists": self._exists, "running": False, "status": "running" if self._exists else "missing"}

    async def image_exists(self, ref):
        return self.image_present

    async def running_containers(self):  # the port check (`services/ports.py`): nothing holds a port
        return []

    async def pull(self, ref, on_progress=None):
        self.pulled.append(ref)
        self.image_present = True

    async def remove(self, name, force=False):
        self.removed = [*getattr(self, "removed", []), name]

    async def create(self, name, payload):
        self.created.append((name, payload))


@pytest.fixture()
def registry(mem_engine, monkeypatch, tmp_path):
    from app.config import settings

    monkeypatch.setattr("app.db.engine", mem_engine)
    # The data directories are really created now (with the API's uid) — under a test root.
    monkeypatch.setattr(settings, "allowed_data_roots", [str(tmp_path)])
    srv = GameServer(
        key="fac", game_type=GameType.factorio, container_name="factorio",
        data_dir=f"{tmp_path}/factorio/fac", image_repo="factoriotools/factorio",
        image_tag="2.0.77", network="gameserver-net", game_ports='{"game": "34197/udp"}',
        management_mode=ManagementMode.dashboard, rcon_port=27015,
    )
    with Session(mem_engine) as s:
        s.add(srv)
        s.commit()
    return srv


def _wire(monkeypatch, dk, payload=None, seed=None):
    monkeypatch.setattr("app.services.docker_client.docker", dk)
    monkeypatch.setattr("app.services.container_spec.build_create_payload",
                        lambda server, secret: payload or {"Image": "x"})
    monkeypatch.setattr("app.services.servers.resolve_secret", lambda server: "secret")
    # `ensure_config` writes to `/srv/gameservers/...` on the real host — here a dummy that records
    # the call. The case "seed fails" has its own test below.
    calls: list[tuple] = []

    def _seed(self, server, secret=""):
        calls.append((server.key, secret))
        if seed is not None:
            seed()

    monkeypatch.setattr("app.services.drivers.factorio.FactorioDriver.ensure_config", _seed)
    return calls


def test_the_container_is_created(registry, monkeypatch, capsys):
    dk = _Docker()
    _wire(monkeypatch, dk)
    assert asyncio.run(cli._create_container("fac")) == 0
    assert [n for n, _ in dk.created] == ["factorio"]
    assert "created" in capsys.readouterr().out


def test_a_missing_image_is_pulled(registry, monkeypatch):
    dk = _Docker(image_present=False)
    _wire(monkeypatch, dk)
    assert asyncio.run(cli._create_container("fac")) == 0
    assert dk.pulled == ["factoriotools/factorio:2.0.77"]


def test_an_existing_container_is_not_overwritten(registry, monkeypatch, capsys):
    """An existing container is a running game or a stopped one with a world next to it.
    Replacing it is the job of the version change — that has a mandatory backup, boot proof
    and rollback."""
    dk = _Docker(exists=True)
    _wire(monkeypatch, dk)
    assert asyncio.run(cli._create_container("fac")) == 0
    assert dk.created == []
    assert "already exists" in capsys.readouterr().out


def test_an_unknown_server_fails_loudly(registry, monkeypatch, capsys):
    _wire(monkeypatch, _Docker())
    assert asyncio.run(cli._create_container("no-such-server")) == 2
    assert "not in the registry" in capsys.readouterr().out


def test_a_compose_server_is_rejected(mem_engine, monkeypatch, capsys):
    """For compose-managed servers Compose creates the container. A second creator next to it
    would mean two sources of truth for the same container."""
    monkeypatch.setattr("app.db.engine", mem_engine)
    with Session(mem_engine) as s:
        s.add(GameServer(key="old", game_type=GameType.pz, container_name="old",
                         management_mode=ManagementMode.compose))
        s.commit()
    _wire(monkeypatch, _Docker())
    assert asyncio.run(cli._create_container("old")) == 2
    assert "compose-managed" in capsys.readouterr().out


def test_the_command_is_wired_into_main(registry, monkeypatch):
    dk = _Docker()
    _wire(monkeypatch, dk)
    assert cli.main(["create-container", "fac"]) == 0
    assert [n for n, _ in dk.created] == ["factorio"]


def test_without_a_key_nothing_is_guessed(capsys):
    assert cli.main(["create-container"]) == 2
    assert "needs a server key" in capsys.readouterr().out


def test_the_payload_comes_from_container_spec(registry, monkeypatch):
    """A second payload builder would be a second truth about binds, ports and memory limits —
    exactly the kind of copy that drifts apart."""
    dk = _Docker()
    seen = {}

    def builder(server, secret):
        seen["key"] = server.key
        seen["secret"] = secret
        return {"Image": "factoriotools/factorio:2.0.77"}

    _wire(monkeypatch, dk)
    monkeypatch.setattr("app.services.container_spec.build_create_payload", builder)
    asyncio.run(cli._create_container("fac"))
    assert seen == {"key": "fac", "secret": "secret"}
    assert dk.created[0][1]["Image"] == "factoriotools/factorio:2.0.77"


def test_an_unpullable_image_reports_itself(registry, monkeypatch, capsys):
    class _Stubborn(_Docker):
        async def pull(self, ref, on_progress=None):
            self.pulled.append(ref)   # pretends to work — but the image stays missing

    dk = _Stubborn(image_present=False)
    _wire(monkeypatch, dk)
    assert asyncio.run(cli._create_container("fac")) == 1
    assert dk.created == []
    assert "could not be pulled" in capsys.readouterr().out


def test_the_stub_is_really_used(registry, monkeypatch):
    """Cross-check of the wiring: without it the cases above would run against a real Docker
    proxy and be green everywhere, because it only catches exceptions."""
    dk = _Docker()
    _wire(monkeypatch, dk)
    asyncio.run(cli._create_container("fac"))
    assert len(dk.created) == 1, "the Docker stub saw no create"


def test_nothing_is_started(registry, monkeypatch):
    """Creating is not starting. The start goes through `lifecycle`/the dashboard — that is where
    the boot gate, watchdog muting and the idle clock hang."""
    dk = _Docker()
    _wire(monkeypatch, dk)
    asyncio.run(cli._create_container("fac"))
    assert not hasattr(dk, "actions")
    assert SimpleNamespace(**dk.created[0][1]) is not None


def test_the_config_is_created_before_the_container(registry, monkeypatch):
    """`create-container` must write the file that makes the server remote-controllable at all.

    `resolve_secret` alone is not enough for Avorion and Factorio — their `container_env`
    deliberately does NOT pass the secret on (the images know no env variable for it), it reaches
    the server only through a file. Without `rconPassword` an Avorion server starts no RCON
    interface at all: no console, no player list, and the watchdog restarts it in a loop as "hung".
    """
    dk = _Docker()
    calls = _wire(monkeypatch, dk)
    assert asyncio.run(cli._create_container("fac")) == 0
    assert calls == [("fac", "secret")], "ensure_config was not called with the secret"


def test_a_failed_seed_prevents_the_container(registry, monkeypatch, capsys):
    """Fail-closed: a container without a control channel is worse than none.

    A missing one is simply created again; a broken one has to be repaired by hand on the host —
    `set_config` cannot fix it, because for Avorion it requires a stopped container.
    """
    def _broken():
        raise ValueError("galaxy folder cannot be created")

    dk = _Docker()
    _wire(monkeypatch, dk, seed=_broken)
    assert asyncio.run(cli._create_container("fac")) == 1
    assert dk.created == [], "no container may be created without a config"
    assert "No container created" in capsys.readouterr().out
