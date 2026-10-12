"""The fleet view does not turn "don't know" into "nobody there".

This is the dominant failure shape of this project, always the same pattern: a query fails, and
the result looks like an answer. `auto_update.is_empty` read an empty name list as "nobody
online" and swapped the image under two players; `parse_avorion_players` turned "No players
online." into a player of that name.

The fleet view is especially prone to it, because it queries every server at once and has an
obvious fallback at hand: the last metric sample. That is exactly what does not work — on an
RCON error it deliberately writes `players=0` so the history graph gets no gap
(`services/metrics.py`). For a graph that is acceptable; as information it would be the laundry
that turns an unknown into a zero.

**Cross-check:** an `except: players = 0` (instead of `None`) in `fleet._one_server`, or a
fallback to `MetricSample`, turns `test_rcon_dead_*` red.
"""
import asyncio
import datetime as dt

import pytest
from sqlmodel import Session

from app.models import MetricSample
from app.models.player import utcnow_naive
from app.models.server import GameServer, GameType
from app.services import fleet
from app.services import resource_budget as budget_svc
from app.services import restarts
from app.services import servers as servers_registry


def _srv(key="fac", sid=1, game=GameType.factorio):
    s = GameServer(
        key=key, game_type=game, container_name=key, data_dir=f"/srv/gameservers/{key}",
        display_name=key, idle_autostop_enabled=True, idle_timeout_minutes=120,
        last_active_at=utcnow_naive() - dt.timedelta(minutes=10),
    )
    s.id = sid
    return s


class _Drv:
    """`error=True` = RCON does not answer. `hangs=True` = it never answers."""

    def __init__(self, count=3, error=False, hangs=False):
        self.count, self.error, self.hangs = count, error, hangs

    async def players(self, server):
        if self.hangs:
            await asyncio.sleep(30)
        if self.error:
            raise RuntimeError("rcon down")
        return {"count": self.count, "names": ["a", "b", "c"][: self.count]}

    def overview_stats(self, server):
        return {"max_players": 12}

    def backup_spec(self, server):
        raise NotImplementedError  # irrelevant here -> last_backup_at stays None


class _Docker:
    def __init__(self, running=True):
        self.running = running

    async def state(self, name):
        return {
            "exists": True, "running": self.running, "restarting": False, "restarts": 0,
            "status": "running" if self.running else "exited", "health": None,
            "started_at": "2026-08-01T10:00:00.000000000Z", "max_heap_bytes": None,
        }

    async def stats(self, name):
        return {"mem_bytes": 1024, "mem_limit": 4096, "cpu_percent": 5.0, "net_rx": 1, "net_tx": 2}

    async def host_info(self):
        return {"cpu_count": 8, "mem_total_bytes": 32 * 1024**3}


@pytest.fixture(autouse=True)
def _environment(monkeypatch, mem_engine):
    fleet.clear_cache()
    monkeypatch.setattr(fleet, "engine", mem_engine)
    monkeypatch.setattr(restarts, "next_restart", lambda **kw: None)
    monkeypatch.setattr(budget_svc, "get_budget", lambda: type("B", (), {"default_idle_timeout_minutes": 28800})())

    async def _usage(*a, **kw):
        return {"total_ram_mb": 0, "used_ram_mb": 0, "total_cpu_cores": 0.0, "used_cpu_cores": 0.0, "free_ram_mb": None, "running": []}

    monkeypatch.setattr(budget_svc, "usage", _usage)
    yield
    fleet.clear_cache()


def _run(monkeypatch, pairs, docker=None):
    monkeypatch.setattr(fleet, "docker", docker or _Docker())
    monkeypatch.setattr(servers_registry, "list_enabled_with_driver", lambda: pairs)
    return asyncio.run(fleet.fleet_status(force=True))


def test_rcon_answers_delivers_live(monkeypatch):
    d = _run(monkeypatch, [(_srv(), _Drv(count=3))])["servers"][0]
    assert d["players"] == {"count": 3, "names": ["a", "b", "c"]}
    assert d["players_source"] == "live"
    assert d["rcon"]["reachable"] is True


def test_rcon_dead_reports_unknown_not_zero(monkeypatch):
    d = _run(monkeypatch, [(_srv(), _Drv(error=True))])["servers"][0]
    assert d["players"] is None, "\"don't know\" was turned into a number"
    assert d["players_source"] == "unknown"
    assert d["rcon"]["reachable"] is False


def test_rcon_dead_does_not_fall_back_to_the_metric_either(monkeypatch, mem_engine):
    """The metric sample is there as a temptation — it must not be used.

    `metrics._sample_server` writes `players=0` on RCON errors. Taking it as a fallback here
    reports "nobody online" for a server we know nothing about.
    """
    with Session(mem_engine) as s:
        s.add(MetricSample(running=True, players=0, cpu_percent=1.0, mem_bytes=1, server_id=1))
        s.commit()
    d = _run(monkeypatch, [(_srv(), _Drv(error=True))])["servers"][0]
    assert d["players"] is None
    assert d["players_source"] == "unknown"


def test_a_hanging_server_does_not_hold_up_the_fleet(monkeypatch):
    monkeypatch.setattr(fleet, "RCON_BUDGET_S", 0.05)
    pairs = [(_srv("fac", 1), _Drv(hangs=True)), (_srv("mc", 2, GameType.minecraft), _Drv(count=1))]
    d = _run(monkeypatch, pairs)
    by_key = {r["key"]: r for r in d["servers"]}
    assert by_key["fac"]["players"] is None and by_key["fac"]["players_source"] == "unknown"
    # The healthy server next to it keeps delivering — the round fans out in parallel.
    assert by_key["mc"]["players"]["count"] == 1


def test_a_stopped_server_says_off_instead_of_unknown(monkeypatch):
    """A stopped server is not a guess: it was not asked in the first place."""
    d = _run(monkeypatch, [(_srv(), _Drv())], docker=_Docker(running=False))["servers"][0]
    assert d["players"] is None
    assert d["players_source"] == "off"
    assert d["idle"]["reason"] == "not_running"


def test_a_broken_server_does_not_drop_out_of_the_list(monkeypatch):
    """Otherwise "cannot be queried" would read as "does not exist" in the overview."""
    class _Broken(_Docker):
        async def state(self, name):
            raise RuntimeError("proxy gone")

    pairs = [(_srv("fac", 1), _Drv())]
    d = _run(monkeypatch, pairs, docker=_Broken())["servers"][0]
    assert d["key"] == "fac"
    assert d["container"]["running"] is None
    assert d["players_source"] == "unknown"
    assert "proxy gone" in d["error"]


def test_host_says_where_its_numbers_come_from(monkeypatch):
    d = _run(monkeypatch, [(_srv(), _Drv())])["host"]
    assert d["cpu_count"] == 8 and d["source"] == "docker_info"


def test_without_docker_info_the_budget_is_not_passed_off_as_the_host(monkeypatch):
    class _WithoutInfo(_Docker):
        async def host_info(self):
            return {}

    d = _run(monkeypatch, [(_srv(), _Drv())], docker=_WithoutInfo())["host"]
    assert d["cpu_count"] is None
    assert d["source"] == "budget", "labelling a budget as host load would be a lie"


def test_a_fresh_install_without_servers_still_measures_its_disk(monkeypatch, tmp_path):
    """Seen in a real browser on a fresh install: "DISK — not readable". The disk was measured
    on the first server's data directory only, and there was none. DATA_ROOT is where every
    server will live and is mounted into the API at the same path — measure that."""
    from app.config import settings

    monkeypatch.setattr(settings, "data_root", str(tmp_path))
    disk = _run(monkeypatch, [])["host"]["disk"]
    assert disk is not None and disk["total"] > 0


def test_an_unmounted_data_root_stays_not_readable(monkeypatch, tmp_path):
    """The other side: nothing measurable is reported as unknown, not as an empty disk."""
    from app.config import settings

    monkeypatch.setattr(settings, "data_root", str(tmp_path / "not-mounted"))
    assert _run(monkeypatch, [])["host"]["disk"] is None


class _LimitedDocker(_Docker):
    """A JVM container: -Xmx 1 GiB, container limit 2 GiB (or none)."""

    def __init__(self, limit):
        super().__init__()
        self.limit = limit

    async def state(self, name):
        return {**await super().state(name), "max_heap_bytes": 1024**3, "memory": self.limit}


def test_the_memory_figure_comes_with_the_container_limit(monkeypatch):
    """`mem_bytes` is the container's memory (heap + JVM + native); measured against -Xmx it
    read 145 % permanently. The fleet sends the container's own limit to compare against."""
    d = _run(monkeypatch, [(_srv(), _Drv())], docker=_LimitedDocker(2 * 1024**3))["servers"][0]
    assert d["stats"]["mem_container_limit"] == 2 * 1024**3
    fleet.clear_cache()
    d = _run(monkeypatch, [(_srv(), _Drv())], docker=_LimitedDocker(0))["servers"][0]
    assert d["stats"]["mem_container_limit"] is None, "no limit set: nothing to compare against"
