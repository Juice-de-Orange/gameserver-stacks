"""Restoring a backup must not overwrite world data irrecoverably with one click.

It needs a mandatory backup before, a CONFIRMED stop (not just `docker stop` without checking)
and a boot verification afterwards.
"""
import asyncio
import os
from pathlib import Path
import tarfile

import pytest
from fastapi import HTTPException

from app.services import backup as bk
from app.services import lifecycle
from app.services import restore as restore_svc
from app.models.server import GameServer, GameType
from app.services.drivers.base import BackupSpec


def _write(path, text="x"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


class _Server:
    def __init__(self, root):
        self.key = "pz-1"
        self.id = 1
        self.container_name = "pz-1"
        self.data_dir = str(root)


class _Driver:
    def __init__(self, data_dir):
        self.data_dir = data_dir

    def backup_spec(self, server):
        return BackupSpec(data_dir=self.data_dir, world_subdirs=("Saves", "Server"))

    async def graceful_shutdown(self, server):
        return False  # like the base default: no in-game shutdown in the stub

    async def announce(self, server, text):
        return None


class _Docker:
    def __init__(self, *, running=True, stops=True):
        self.running = running
        self.stops = stops
        self.calls: list[str] = []

    async def state(self, name):
        return {"exists": True, "running": self.running, "restarts": 0}

    async def action(self, name, act, **kw):
        self.calls.append(act)
        if act == "stop" and self.stops:
            self.running = False
        if act == "start":
            self.running = True


@pytest.fixture()
def env(tmp_path, monkeypatch):
    data = os.path.join(str(tmp_path), "data")
    _write(os.path.join(data, "Saves", "Multiplayer", "world", "chunk.bin"), "NEW")
    _write(os.path.join(data, "Server", "servertest.ini"), "Mods=new\n")

    # An archive with OLD content that is to be restored.
    archive = bk.create_backup("manual", data_dir=data, world_subdirs=("Saves", "Server"))
    _write(os.path.join(data, "Server", "servertest.ini"), "Mods=changed\n")

    srv, drv = _Server(tmp_path), _Driver(data)
    docker = _Docker()
    monkeypatch.setattr(restore_svc, "docker", docker)
    # The stop runs through `lifecycle` (in-game shutdown, see there).
    monkeypatch.setattr(lifecycle, "docker", docker)
    monkeypatch.setattr(lifecycle, "_STOP_MARK_DELAY", 0)
    monkeypatch.setattr(lifecycle, "_locks", {})  # one event loop per test
    monkeypatch.setattr(lifecycle, "_suppress", lambda *a, **k: None)
    monkeypatch.setattr(lifecycle, "_driver", lambda s: drv)
    monkeypatch.setattr(lifecycle, "stop_grace", lambda s: 120)

    async def boot_ok(server, driver, timeout=900.0):
        return True

    monkeypatch.setattr(restore_svc, "verify_boot", boot_ok)
    return {"srv": srv, "drv": drv, "docker": docker, "data": data,
            "archive": archive["name"], "monkeypatch": monkeypatch}


def test_a_safety_backup_is_taken_before_overwriting(env):
    """Without this safety net the current state would be gone for good after a mistake."""
    result = asyncio.run(restore_svc.restore(env["srv"], env["drv"], env["archive"]))

    assert result["ok"] and result["safety_backup"].endswith(".tar.gz")
    assert "pre-restore" in result["safety_backup"]
    assert result["summary"]["code"] == "restore.done"
    assert result["summary"]["params"] == {"name": env["archive"]}
    names = [b["name"] for b in bk.list_backups(data_dir=env["data"])]
    assert any("pre-restore" in n for n in names)


def test_nothing_is_written_while_the_container_still_runs(env):
    """Like `version_manager`, the restore checks the stop. Extracting over a running world
    damages it."""
    env["docker"].stops = False           # stop has no effect
    before = Path(os.path.join(env["data"], "Server", "servertest.ini")).read_text(encoding="utf-8")

    with pytest.raises(HTTPException) as ei:
        asyncio.run(restore_svc.restore(env["srv"], env["drv"], env["archive"]))

    assert ei.value.status_code == 409
    assert ei.value.detail["code"] == "lifecycle.stop_not_confirmed"
    after = Path(os.path.join(env["data"], "Server", "servertest.ini")).read_text(encoding="utf-8")
    assert after == before                # untouched


def test_a_failed_boot_is_reported_with_the_way_back(env):
    async def boot_fail(server, driver, timeout=900.0):
        return False

    env["monkeypatch"].setattr(restore_svc, "verify_boot", boot_fail)
    with pytest.raises(HTTPException) as ei:
        asyncio.run(restore_svc.restore(env["srv"], env["drv"], env["archive"]))

    assert ei.value.status_code == 502
    assert "pre-restore" in str(ei.value.detail)   # names the earlier backup


def test_a_stopped_server_stays_stopped(env):
    env["docker"].running = False
    result = asyncio.run(restore_svc.restore(env["srv"], env["drv"], env["archive"]))
    assert result["started"] is False
    assert "start" not in env["docker"].calls


def test_missing_backup_is_rejected_before_anything_happens(env):
    with pytest.raises(HTTPException) as ei:
        asyncio.run(restore_svc.restore(env["srv"], env["drv"], "20200101-000000-doesnotexist.tar.gz"))
    assert ei.value.status_code == 404
    assert env["docker"].calls == []


def test_invalid_backup_name_is_rejected(env):
    with pytest.raises(HTTPException) as ei:
        asyncio.run(restore_svc.restore(env["srv"], env["drv"], "../../etc/passwd"))
    assert ei.value.status_code == 400


# --- Selective restore ---------------------------------------------------------------------------
def test_only_the_configuration_can_be_restored(env):
    """A broken .ini can be restored without overwriting the WHOLE world — days of play time
    for one line."""
    world = os.path.join(env["data"], "Saves", "Multiplayer", "world", "chunk.bin")
    _write(world, "NEWER-PROGRESS")

    asyncio.run(restore_svc.restore(env["srv"], env["drv"], env["archive"], subdirs=("Server",)))

    assert Path(world).read_text(encoding="utf-8") == "NEWER-PROGRESS"   # world untouched
    ini = Path(os.path.join(env["data"], "Server", "servertest.ini")).read_text(encoding="utf-8")
    assert ini == "Mods=new\n"                                          # configuration restored


def test_backup_contents_lists_the_restorable_parts(env):
    assert sorted(bk.backup_contents(env["archive"], data_dir=env["data"])) == ["Saves", "Server"]


def test_restoring_an_absent_part_fails_loudly(env):
    with pytest.raises(FileNotFoundError):
        bk.extract_backup(env["archive"], data_dir=env["data"], subdirs=("DoesNotExist",))


def test_a_part_not_in_the_archive_is_rejected_before_the_server_stops(env):
    """`extract_backup` only notices once the server is already stopped — that is too late.

    A part name such as `Server` exists only in Project Zomboid archives; for Minecraft, Vintage
    Story and Reforger the archive holds only world folders. Checked late, the flow was: stop,
    confirm the stop, take the safety archive — and THEN the restore fails on an empty match set,
    the `except` branch raises 500 ("the server stays stopped") and the restart below is never
    reached. A button presented as harmless left the server offline.

    The check is cheap compared with what it prevents: it reads the archive's name list a second
    time (about twenty seconds of gzip for a 4 GB Minecraft archive), but on rejection it saves
    the stop and a full safety archive. A full restore without `subdirs` does not pay for it.
    """
    with pytest.raises(HTTPException) as ei:
        asyncio.run(restore_svc.restore(env["srv"], env["drv"], env["archive"], subdirs=("profile",)))

    assert ei.value.status_code == 400
    assert "profile" in str(ei.value.detail)
    assert "Saves" in str(ei.value.detail), "the message must say what IS in the archive"
    assert env["docker"].calls == [], "the server was stopped for a restore that could not succeed"
    assert not any("pre-restore" in b["name"] for b in bk.list_backups(data_dir=env["data"])), \
        "a safety archive was created for a restore that could never run"


def test_a_part_in_the_archive_still_goes_through(env):
    """Cross-check: the check must not block the valid case as well."""
    asyncio.run(restore_svc.restore(env["srv"], env["drv"], env["archive"], subdirs=("Server",)))
    assert "stop" in env["docker"].calls


def test_two_manual_backups_do_not_overlap(monkeypatch):
    """Two concurrent runs tear each other's archive apart.

    A long backup request can be cut by the proxy (a multi-GB Minecraft world takes minutes); the
    page then shows an error although the archive is still being written, and the obvious second
    click starts a second run over the same world.

    The shared, non-counting state is the game server's save switch: MC's `pre_hook` sets
    `save-off` + `save-all flush`, the `post_hook` `save-on`. When run 1 finishes its archive,
    its `post_hook` turns autosave back on — in the middle of run 2's
    `tarfile.open(..., "w:gz")`. From then on run 2 packs a world that is being written and
    stores the result as a regular, successful backup. Keeping `pre_hook` outside the `try`
    cannot prevent that across two overlapping runs.

    A `threading.Lock`, not an `asyncio.Lock`: `bk.create_backup` runs in the thread pool.
    """
    import threading

    from app.services import backup as bk

    packing = threading.Event()   # run 1 has reached the packing step
    release = threading.Event()   # …and may finish now
    concurrent: list[int] = []
    counter = {"n": 0}
    guard = threading.Lock()

    def _slow(*a, **kw):
        with guard:
            counter["n"] += 1
            concurrent.append(counter["n"])
        packing.set()
        release.wait(3.0)
        with guard:
            counter["n"] -= 1
        return {"name": "x.tar.gz", "size": 1}

    results: list[object] = []

    def run():
        try:
            results.append(bk.create_backup_exclusive("pz-1", _slow))
        except Exception as e:  # noqa: BLE001 — the rejection IS the expected result
            results.append(e)

    t1 = threading.Thread(target=run, daemon=True)
    t2 = threading.Thread(target=run, daemon=True)
    t1.start()
    assert packing.wait(3.0), "run 1 never reached the packing step"
    t2.start()
    t2.join(3.0)
    release.set()
    t1.join(3.0)

    assert max(concurrent) == 1, f"two runs in the archive at the same time: {concurrent}"
    assert any(isinstance(e, RuntimeError) for e in results), \
        "the second run was not rejected"


def test_the_driver_knows_which_part_is_the_configuration():
    """`Server` is a Project Zomboid folder name, not a general one.

    Which folders "configuration only" means is therefore stated in the `BackupSpec` — and for
    three of the four games the answer is empty.
    """
    from app.routers.server_ops import _resolve_parts
    from app.services.drivers.base import BackupSpec

    pz = BackupSpec(data_dir="/x", world_subdirs=("Saves", "Server"), config_subdirs=("Server",))
    mc = BackupSpec(data_dir="/x", world_subdirs=("world", "world_nether"))

    assert _resolve_parts("config", pz) == ("Server",)
    assert _resolve_parts(None, mc) is None            # full restore, unchanged
    assert _resolve_parts("Saves,Server", pz) == ("Saves", "Server")   # explicit, unchanged

    with pytest.raises(HTTPException) as ei:
        _resolve_parts("config", mc)
    assert ei.value.status_code == 400
    assert ei.value.detail["code"] == "backup.no_config_part"


def test_the_four_drivers_state_what_their_archive_contains():
    """On the real driver, not a dummy — otherwise the test checks its own assumption."""
    from app.services.drivers.minecraft import MinecraftDriver
    from app.services.drivers.pz import PZDriver
    from app.services.drivers.reforger import ReforgerDriver

    srv = GameServer(key="pz-1", game_type=GameType.pz, container_name="c", data_dir="/x")
    assert PZDriver().backup_spec(srv).config_subdirs == ("Server",)
    assert "backup-config-only" in PZDriver().capabilities()

    rf = GameServer(key="rf-1", game_type=GameType.arma_reforger, container_name="c", data_dir="/x")
    assert ReforgerDriver().backup_spec(rf).config_subdirs == ()
    assert "backup-config-only" not in ReforgerDriver().capabilities()
    assert "backup-config-only" not in MinecraftDriver().capabilities()


def test_a_manual_backup_during_a_restore_is_rejected(monkeypatch):
    """Otherwise the archive is created IN THE MIDDLE of unpacking the world.

    `create_backup` silently skips missing directories — the torso would land at the top of the
    list as the "newest backup" and so as an apparent rollback. The "Backup now" button stays
    clickable during a running restore; `busy` only covers the request, not the background
    operation.
    """
    from app.routers import server_ops
    from app.services import world_lock

    srv = GameServer(key="pz-1", game_type=GameType.pz, container_name="c", data_dir="/x")
    srv.id = 1
    packed: list[str] = []
    # A complete dummy, so that a slip-through fails at the ASSERTION and not on a follow-up
    # error — otherwise the red reads like a broken test instead of the real finding.
    monkeypatch.setattr(server_ops.bk, "create_backup",
                        lambda *a, **kw: packed.append("tar") or {"name": "x.tar.gz", "size": 1})
    monkeypatch.setattr(server_ops, "record", lambda *a, **k: None)

    async def _with_lock_held():
        async with world_lock.hold("pz-1", "backup.restore"):
            with pytest.raises(HTTPException) as ei:
                await server_ops.create_backup(
                    server=srv, driver=_Driver("/x"), session=None, user=None,
                )
            return ei.value

    monkeypatch.setattr(world_lock, "_locks", {})
    error = asyncio.run(_with_lock_held())
    assert error.status_code == 409
    assert "a restore" in str(error.detail)
    assert packed == [], "an archive was packed despite the 409"


def test_full_restore_still_covers_everything(env):
    world = os.path.join(env["data"], "Saves", "Multiplayer", "world", "chunk.bin")
    _write(world, "NEWER-PROGRESS")
    asyncio.run(restore_svc.restore(env["srv"], env["drv"], env["archive"]))
    assert Path(world).read_text(encoding="utf-8") == "NEW"   # without subdirs: everything restored


def test_archive_really_contains_both_trees(env):
    path = bk.backup_path(env["archive"], data_dir=env["data"])
    with tarfile.open(path, "r:gz") as tar:
        tops = {m.name.split("/", 1)[0] for m in tar.getmembers()}
    assert {"Saves", "Server"} <= tops



def test_the_safety_backup_is_taken_only_after_the_confirmed_stop(
    monkeypatch, tmp_path, fake_docker_cls, wire_lifecycle,
):
    """"Back up, then stop" is the wrong order for PZ — there is no quiesce hook.

    `pz.backup_spec` returns `pre_hook`/`post_hook` as None (the base defaults). The `tarfile`
    run would be a hot copy of a RUNNING world that is being written: half-written `map/*.bin`,
    `players.db` mid-write. And exactly this archive is named as the rollback after
    `extract_backup` has overwritten the current state.

    The same wrong order has a second cost: if the stop fails (409), an archive already exists —
    one nobody asked for and that retention counts.
    """
    from app.services import restore as restore_mod

    trace: list[str] = []

    class _WithTrace(fake_docker_cls):
        async def action(self, name, act, **kw):
            trace.append(f"docker:{act}")
            return await super().action(name, act, **kw)

    class _Drv:
        def backup_spec(self, server):
            return BackupSpec(data_dir=str(tmp_path), world_subdirs=("Saves",))

        async def graceful_shutdown(self, server):
            return True

        async def announce(self, server, text):
            return None

    srv = GameServer(key="pz-1", game_type=GameType.pz, container_name="pz-1",
                     data_dir=str(tmp_path))
    srv.id = 1
    drv = _Drv()
    docker = _WithTrace()
    wire_lifecycle(docker, drv)
    monkeypatch.setattr(restore_mod, "docker", docker)
    monkeypatch.setattr(restore_mod.bk, "valid_backup_name", lambda n: True)
    monkeypatch.setattr(restore_mod.os.path, "exists", lambda p: True)
    # Patch `lifecycle.create_backup`, not `bk.create_backup`: the helper holds its own
    # reference from the module import — a patch on the source module does not reach it.
    monkeypatch.setattr(lifecycle, "create_backup",
                        lambda label, **kw: trace.append(f"backup:{label}") or {"name": f"{label}.tar.gz"})
    monkeypatch.setattr(restore_mod.bk, "extract_backup", lambda *a, **kw: trace.append("extract"))
    # The archive is only a name here, not a file — otherwise the authenticity check
    # (`check_restorable`, in front of BOTH restore paths) trips over the missing file
    # before the case under test is reached.
    monkeypatch.setattr(restore_mod.bk, "check_restorable", lambda *a, **k: None)

    async def _boot(*a, **kw):
        return True

    monkeypatch.setattr(restore_mod, "verify_boot", _boot)

    asyncio.run(restore_mod.restore(srv, drv, "world.tar.gz"))

    assert "backup:pre-restore" in trace and "docker:stop" in trace, trace
    assert trace.index("docker:stop") < trace.index("backup:pre-restore"), (
        f"The safety backup was taken BEFORE the stop — a hot copy of a world being written. "
        f"Order: {trace}"
    )
    assert trace.index("backup:pre-restore") < trace.index("extract"), (
        "The rollback must exist before the overwrite."
    )


def _old_copies_cannot_be_deleted(env):
    """Root-owned files in the replaced version: deleting the set-aside copy fails."""
    real_remove = bk._remove

    def _remove(path):
        if bk._ASIDE_SUFFIX in path:
            raise PermissionError(13, "Permission denied", path)
        real_remove(path)

    env["monkeypatch"].setattr(bk, "_remove", _remove)


def test_an_old_copy_left_behind_is_named_in_the_result(env):
    """The restore worked, but the replaced version still takes the space — the operator must
    see where, not only the log."""
    _old_copies_cannot_be_deleted(env)
    result = asyncio.run(restore_svc.restore(env["srv"], env["drv"], env["archive"]))

    assert result["ok"] and result["started"] is True
    note = result["note"]
    assert note["code"] == "restore.old_copy_left"
    paths = note["params"]["paths"].split(", ")
    assert len(paths) == 2 and all(bk._ASIDE_SUFFIX in p for p in paths)
    assert all(os.path.isdir(p) for p in paths), "the copies are really still there"
    # The restored world is in place all the same.
    ini = Path(os.path.join(env["data"], "Server", "servertest.ini")).read_text(encoding="utf-8")
    assert ini == "Mods=new\n"


def test_a_stopped_server_gets_both_notes(env):
    env["docker"].running = False
    _old_copies_cannot_be_deleted(env)
    result = asyncio.run(restore_svc.restore(env["srv"], env["drv"], env["archive"]))

    assert result["started"] is False
    first, second = result["note"]
    assert first == "The server was stopped before and stays stopped."
    assert second["code"] == "restore.old_copy_left"


def test_without_leftovers_there_is_no_warning(env):
    result = asyncio.run(restore_svc.restore(env["srv"], env["drv"], env["archive"]))
    assert "note" not in result
    leftovers = [n for n in os.listdir(env["data"]) if bk._ASIDE_SUFFIX in n]
    assert leftovers == []


def test_old_copies_root_wrote_are_handed_to_the_cleanup_helper(env):
    """What the API cannot delete goes to `provisioning.remove_leftovers_as_root`; once that
    has deleted it, there is nothing left to warn about."""
    import shutil

    from app.services import provisioning

    _old_copies_cannot_be_deleted(env)
    handed: list[str] = []

    async def as_root(server, paths):
        handed.extend(paths)
        for p in paths:
            shutil.rmtree(p)
        return []

    env["monkeypatch"].setattr(provisioning, "remove_leftovers_as_root", as_root)
    result = asyncio.run(restore_svc.restore(env["srv"], env["drv"], env["archive"]))

    assert result["ok"] and "note" not in result
    assert len(handed) == 2 and all(bk._ASIDE_SUFFIX in p for p in handed)
    assert [n for n in os.listdir(env["data"]) if bk._ASIDE_SUFFIX in n] == []
