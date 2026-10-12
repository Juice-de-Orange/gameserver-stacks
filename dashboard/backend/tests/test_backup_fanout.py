"""The weekly run backs up EVERY enabled server, not just pz-1.

Registering `scheduled_backup` without `data_dir` makes the job fall back to ONE fixed PZ data
folder, and every other server never gets an automatic backup — the one damage that cannot be
made good afterwards. The tests here pin down the property that prevents it.
"""
import asyncio
import json
import os

import pytest

from app.models import GameServer, GameType
from app.services import backup as bk
from app.services import servers as servers_registry
from app.services.drivers.base import BackupSpec


def _srv(key: str, data_dir: str, game: GameType = GameType.pz) -> GameServer:
    return GameServer(
        id=abs(hash(key)) % 10_000,
        key=key,
        game_type=game,
        display_name=key,
        container_name=key,
        data_dir=data_dir,
        game_ports=json.dumps({"game": "16261/udp"}),
    )


class _Drv:
    """Driver stub whose BackupSpec differs per server (as in reality)."""

    def __init__(self, subdirs=("Saves", "Server"), suffix="/data", hooks=False, fail=False):
        self.subdirs = subdirs
        self.suffix = suffix
        self.hooks = hooks
        self.fail = fail
        self.hook_calls: list[str] = []

    def backup_spec(self, server):
        if self.fail:
            raise RuntimeError(f"broken driver for {server.key}")
        return BackupSpec(
            data_dir=f"{server.data_dir}{self.suffix}",
            world_subdirs=self.subdirs,
            pre_hook=(lambda: self.hook_calls.append("pre")) if self.hooks else None,
            post_hook=(lambda: self.hook_calls.append("post")) if self.hooks else None,
        )


@pytest.fixture()
def collector(monkeypatch):
    """Replace `create_backup`/`prune_backups` with collectors — no real tar.gz needed."""
    calls: list[dict] = []
    pruned: list[tuple[int, str]] = []

    def _create(label, *, data_dir, world_subdirs, config_subdirs=(), pre_hook=None,
                post_hook=None, server=None):
        if pre_hook:
            pre_hook()
        try:
            calls.append(
                {"label": label, "data_dir": data_dir, "world_subdirs": world_subdirs}
            )
            return {"name": f"20260802-040000-{label}.tar.gz", "size": 42}
        finally:
            if post_hook:
                post_hook()

    monkeypatch.setattr(bk, "create_backup", _create)
    monkeypatch.setattr(bk, "prune_backups", lambda days, *, data_dir, **kw: pruned.append((days, data_dir)) or 0)
    return calls, pruned


def _wire(monkeypatch, pairs):
    monkeypatch.setattr(servers_registry, "list_enabled_with_driver", lambda: pairs)


def test_every_registry_server_is_backed_up(monkeypatch, collector):
    calls, pruned = collector
    pairs = [
        (_srv("pz-1", "/srv/gameservers/pz-1"), _Drv()),
        (_srv("mc", "/srv/gameservers/minecraft", GameType.minecraft), _Drv(subdirs=("world",))),
        (_srv("vs", "/srv/gameservers/vintagestory", GameType.vintagestory), _Drv(subdirs=("Saves",), suffix="/gamedata")),
        (_srv("rf-1", "/srv/gameservers/reforger", GameType.arma_reforger), _Drv(subdirs=("profile",), suffix="")),
        (_srv("pz-test", "/srv/gameservers/pz-test"), _Drv()),
    ]
    _wire(monkeypatch, pairs)

    result = asyncio.run(bk.scheduled_backup())

    # The core: as many runs as registry rows — not one.
    assert len(calls) == len(pairs)
    assert [c["data_dir"] for c in calls] == [
        "/srv/gameservers/pz-1/data",
        "/srv/gameservers/minecraft/data",
        "/srv/gameservers/vintagestory/gamedata",
        "/srv/gameservers/reforger",
        "/srv/gameservers/pz-test/data",
    ]
    assert [e["server"] for e in result] == ["pz-1", "mc", "vs", "rf-1", "pz-test"]
    assert all("error" not in e for e in result)


def test_world_folders_come_from_the_backup_spec(monkeypatch, collector):
    """Without this, `("Saves","Server")` would be packed for Minecraft — that is, nothing."""
    calls, _ = collector
    _wire(monkeypatch, [(_srv("mc", "/srv/gameservers/minecraft", GameType.minecraft), _Drv(subdirs=("world", "world_nether")))])

    asyncio.run(bk.scheduled_backup())

    assert calls[0]["world_subdirs"] == ("world", "world_nether")
    assert calls[0]["label"] == "weekly"


def test_quiesce_hooks_are_passed_through(monkeypatch, collector):
    """MC/VS pause writing before packing. Without the hook you back up a torso."""
    _, _ = collector
    drv = _Drv(hooks=True)
    _wire(monkeypatch, [(_srv("mc", "/srv/gameservers/minecraft", GameType.minecraft), drv)])

    asyncio.run(bk.scheduled_backup())

    assert drv.hook_calls == ["pre", "post"]


def test_a_failed_quiesce_creates_no_archive_at_all(tmp_path):
    """The test above only checks THAT the hook is called — not that it has an effect.

    Here the real `create_backup` runs against a real directory. `pre_hook` sits outside the
    `try` on purpose: if it raises, no file may remain. Otherwise a snapshot of a 4 GB world
    being written would sit at the top of the list as "newest backup".
    """
    (tmp_path / "world").mkdir()
    (tmp_path / "world" / "level.dat").write_text("x", encoding="utf-8")

    def _quiesce_fails() -> None:
        raise RuntimeError("Minecraft quiesce (save-off) failed: RCON dead")

    with pytest.raises(RuntimeError, match="save-off"):
        bk.create_backup("weekly", data_dir=str(tmp_path), world_subdirs=("world",),
                         pre_hook=_quiesce_fails)

    assert os.listdir(bk.backup_dir(str(tmp_path))) == [], "a torso archive was left behind"


def test_a_failed_follow_up_keeps_the_archive_but_reports_it(tmp_path):
    """`save-on` fails AFTER the minutes-long tar run.

    The archive is fine and is kept — the SERVER is not: its autosave stays off until the next
    restart. This state must not vanish without a trace.
    """
    (tmp_path / "world").mkdir()
    (tmp_path / "world" / "level.dat").write_text("x", encoding="utf-8")

    def _follow_up_fails() -> None:
        raise RuntimeError("Minecraft quiesce (save-on) failed: RCON dead")

    result = bk.create_backup("weekly", data_dir=str(tmp_path), world_subdirs=("world",),
                                post_hook=_follow_up_fails)

    assert result["size"] > 0
    assert "save-on" in result["quiesce_warning"]


def test_a_broken_server_does_not_take_the_others_down(monkeypatch, collector):
    calls, _ = collector
    pairs = [
        (_srv("pz-1", "/srv/gameservers/pz-1"), _Drv()),
        (_srv("mc", "/srv/gameservers/minecraft", GameType.minecraft), _Drv(fail=True)),
        (_srv("vs", "/srv/gameservers/vintagestory", GameType.vintagestory), _Drv(subdirs=("Saves",), suffix="/gamedata")),
    ]
    _wire(monkeypatch, pairs)

    result = asyncio.run(bk.scheduled_backup())

    assert [c["data_dir"] for c in calls] == ["/srv/gameservers/pz-1/data", "/srv/gameservers/vintagestory/gamedata"]
    assert result[1]["server"] == "mc" and "error" in result[1]
    assert "error" not in result[2]


def test_retention_runs_per_server_directory(monkeypatch, collector):
    """There is no central backup directory — 90 days must apply per data_dir."""
    _, pruned = collector
    pairs = [
        (_srv("pz-1", "/srv/gameservers/pz-1"), _Drv()),
        (_srv("vs", "/srv/gameservers/vintagestory", GameType.vintagestory), _Drv(subdirs=("Saves",), suffix="/gamedata")),
    ]
    _wire(monkeypatch, pairs)

    asyncio.run(bk.scheduled_backup())

    assert pruned == [(90, "/srv/gameservers/pz-1/data"), (90, "/srv/gameservers/vintagestory/gamedata")]


def test_the_job_is_registered_as_a_single_instance():
    """A fan-out over five tar.gz runs must not overlap with the next round.

    In addition, `routers/server.py::_next_backup()` depends on the job ID — whoever renames it
    silently drops the overview to "no date".
    """
    import inspect

    from app import main

    source = inspect.getsource(main.lifespan)
    block = source.split('id="weekly_backup"')[0].rsplit("scheduler.add_job(", 1)[1]
    assert "scheduled_backup" in block
    assert "max_instances=1" in source.split('id="weekly_backup"')[1].split(")")[0]


# ------------------------------------------------------------------ Retention

def _archive(tmp_path, name, *, age_days=0, size=1000):
    import os
    import time

    bdir = tmp_path / "backups"
    bdir.mkdir(exist_ok=True)
    p = bdir / name
    p.write_bytes(b"x" * size)
    if age_days:
        t = time.time() - age_days * 86400
        os.utime(p, (t, t))
    return p


def test_retention_deletes_by_age(tmp_path):
    old = _archive(tmp_path, "20250101-000000-weekly.tar.gz", age_days=200)
    middle = _archive(tmp_path, "20260101-000000-weekly.tar.gz", age_days=100)
    new1 = _archive(tmp_path, "20260701-000000-weekly.tar.gz", age_days=5)
    new2 = _archive(tmp_path, "20260801-000000-weekly.tar.gz", age_days=1)

    removed = bk.prune_backups(90, data_dir=str(tmp_path))

    assert removed == 2
    assert not old.exists() and not middle.exists()
    assert new1.exists() and new2.exists()


def test_retention_keeps_the_size_limit(tmp_path):
    """The reason the limit exists: a Minecraft world can produce 4 GB per archive.

    Without it, 90 days would be around 52 GB for ONE server — easily more than a disk has free.
    """
    files = [
        _archive(tmp_path, f"2026080{i}-000000-weekly.tar.gz", age_days=10 - i, size=1000)
        for i in range(1, 6)
    ]

    removed = bk.prune_backups(90, data_dir=str(tmp_path), max_bytes=2500, keep_min=2)

    assert removed == 3
    assert [p.exists() for p in files] == [False, False, False, True, True]


def test_the_limit_never_deletes_the_last_backup(tmp_path):
    """A rule that may delete the last save is worse than none at all."""
    files = [
        _archive(tmp_path, f"2026080{i}-000000-weekly.tar.gz", age_days=400, size=10_000)
        for i in range(1, 4)
    ]

    bk.prune_backups(90, data_dir=str(tmp_path), max_bytes=1, keep_min=2)

    remaining = [p for p in files if p.exists()]
    assert len(remaining) == 2, "minimum stock undercut"
    assert remaining == files[1:], "the NEWEST should have stayed"


def test_without_a_limit_the_old_behaviour_remains(tmp_path):
    """`max_bytes=None` means no size limit — callers outside the job (restore, mod apply) must
    not suddenly behave differently."""
    files = [_archive(tmp_path, f"2026080{i}-000000-weekly.tar.gz", size=10_000) for i in range(1, 6)]

    assert bk.prune_backups(90, data_dir=str(tmp_path)) == 0
    assert all(p.exists() for p in files)


def test_the_job_passes_the_limit_from_the_settings(monkeypatch):
    """Otherwise the limit would exist but never apply."""
    seen: list[dict] = []
    monkeypatch.setattr(
        bk, "create_backup",
        lambda label, **kw: {"name": "20260802-040000-weekly.tar.gz", "size": 1},
    )
    monkeypatch.setattr(bk, "prune_backups", lambda days, **kw: seen.append({"days": days, **kw}) or 0)
    monkeypatch.setattr(bk.settings, "backup_retention_days", 90)
    monkeypatch.setattr(bk.settings, "backup_max_gb_per_server", 12.0)
    monkeypatch.setattr(bk.settings, "backup_keep_min", 2)
    _wire(monkeypatch, [(_srv("mc", "/srv/gameservers/minecraft", GameType.minecraft), _Drv())])

    asyncio.run(bk.scheduled_backup())

    assert seen == [{"days": 90, "data_dir": "/srv/gameservers/minecraft/data",
                        "max_bytes": 12_000_000_000, "keep_min": 2}]
