"""Apply as a transaction: either all of it — or the server runs as before afterwards.

Everything uses fakes for Docker/backup/boot proof; no container and no network is touched.
"""
import asyncio
import json
import os
from pathlib import Path

import pytest
from fastapi import HTTPException

from app.models.server import GameServer, GameType, ManagementMode
from app.services import world_lock
from app.services.mods import apply as mod_apply
from app.services.mods import state as mod_state
from app.services.mods.preflight import BASE_MAP, ModPlan


# --------------------------------------------------------------------- Setup

def _server(root) -> GameServer:
    srv = GameServer(
        key="pz-test", game_type=GameType.pz, management_mode=ManagementMode.dashboard,
        container_name="pz-test-container", data_dir=str(root), enabled=True,
        extra=json.dumps({"servername": "servertest"}),
    )
    srv.id = 1
    return srv


def _write(path, text="x"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def _ini(root, *, mods="", items="", maps=BASE_MAP):
    _write(os.path.join(root, "data", "Server", "servertest.ini"),
           f"Mods={mods}\nWorkshopItems={items}\nMap={maps}\nPVP=true\n")


def _item(root, wid, *, mod_ids=(), maps=None):
    base = os.path.join(root, "workshop-mods", "content", "108600", str(wid), "42")
    _write(os.path.join(base, "mod.info"), "\n".join(f"id={m}" for m in mod_ids) + "\n")
    for name, cell_list in (maps or {}).items():
        for x, y in cell_list:
            _write(os.path.join(base, "media", "maps", name, f"{x}_{y}.lotheader"))


class FakeDriver:
    def __init__(self, root):
        self.root = root

    async def announce(self, server, text):
        pass

    async def save(self, server):
        pass

    def backup_spec(self, server):
        from app.services.drivers.base import BackupSpec
        return BackupSpec(data_dir=os.path.join(self.root, "data"), world_subdirs=("Saves", "Server"))


@pytest.fixture()
def env(tmp_path, monkeypatch, mem_engine, wire_lifecycle, fake_docker_cls):
    """Server + DB + fakes, shared by all tests here.

    `fake_docker_cls` comes from `conftest.py` because the shared stub can **fail** on request
    (`fail_state`) — that is what lets the `except` branches around `docker.state()` be tested.
    """
    root = str(tmp_path)
    srv = _server(root)
    _ini(root)

    monkeypatch.setattr(mod_state, "engine", mem_engine)
    monkeypatch.setattr(mod_apply, "engine", mem_engine)
    monkeypatch.setattr("app.services.servers.get_by_key", lambda k: srv)

    docker = fake_docker_cls()
    monkeypatch.setattr(mod_apply, "docker", docker)
    wire_lifecycle(docker, FakeDriver(root))  # the stop goes through `lifecycle`
    monkeypatch.setattr(mod_apply, "get_driver", lambda s: FakeDriver(root))
    monkeypatch.setattr(mod_apply.watchdog, "suppress", lambda k, m: None)

    backups: list[str] = []
    monkeypatch.setattr(mod_apply, "create_backup", lambda label, **kw: (
        backups.append(label) or {"name": f"{label}.tar.gz", "size": 1}
    ))
    monkeypatch.setattr(mod_apply, "verify_boot", _boot_ok)
    mod_state.invalidate(srv.key)
    return {"root": root, "server": srv, "docker": docker, "backups": backups,
            "monkeypatch": monkeypatch}


async def _boot_ok(server, driver, timeout=900.0):
    return True


async def _boot_fail(server, driver, timeout=900.0):
    return False


# --------------------------------------------------------------------- Tests

def test_blockers_stop_everything_before_the_server_is_touched(env):
    """A blocker in the draft must trigger neither backup nor stop — NOTHING happens."""
    mod_state.set_draft(env["server"], ModPlan((), (), ("GhostMap", BASE_MAP)))

    with pytest.raises(HTTPException) as ei:
        asyncio.run(mod_apply.apply_draft("pz-test"))

    assert ei.value.status_code == 409
    codes = {b["code"] for b in ei.value.detail["blockers"]}
    assert "MAP_WITHOUT_PROVIDER" in codes
    assert env["docker"].calls == []          # never stopped
    assert env["backups"] == []               # no backup was needed


def test_nothing_to_do_is_rejected(env):
    with pytest.raises(HTTPException) as ei:
        asyncio.run(mod_apply.apply_draft("pz-test"))
    assert ei.value.status_code == 400


def test_successful_apply_writes_config_and_records_history(env):
    root, srv = env["root"], env["server"]
    _item(root, "111", mod_ids=["CityMod"], maps={"Newtown": [(90, 90)]})
    mod_state.set_draft(srv, ModPlan(("111",), ("CityMod",), ("Newtown", BASE_MAP)))

    result = asyncio.run(mod_apply.apply_draft("pz-test"))

    assert result["status"] == "ok"
    assert env["backups"] == ["pre-mod-apply"]          # mandatory backup ran
    assert env["docker"].calls == ["stop", "start"]
    # the .ini now carries the draft …
    live = mod_state.live_plan(srv)
    assert live.mods == ("CityMod",) and live.maps == ("Newtown", BASE_MAP)
    # … and the spawn regions were regenerated.
    from app.services import ini_editor as ini
    lua = Path(ini.spawnregions_path(srv)).read_text(encoding="utf-8")
    assert "Muldraugh, KY" in lua and lua.startswith("function SpawnRegions()")
    # history says ok.
    hist = mod_apply.history(srv)
    assert hist and hist[0]["status"] == "ok"


def test_backup_failure_aborts_and_restarts_the_server(env):
    root, srv = env["root"], env["server"]
    _item(root, "111", mod_ids=["CityMod"])
    mod_state.set_draft(srv, ModPlan(("111",), ("CityMod",), (BASE_MAP,)))

    def _boom(label, **kw):
        raise OSError("disk full")

    env["monkeypatch"].setattr(mod_apply, "create_backup", _boom)

    with pytest.raises(HTTPException) as ei:
        asyncio.run(mod_apply.apply_draft("pz-test"))

    assert ei.value.status_code == 502
    assert ei.value.detail["code"] == "mods.mandatory_backup_failed"
    assert env["docker"].calls == ["stop", "start"]     # started again
    assert mod_state.live_plan(srv).mods == ()          # .ini unchanged


def test_failed_boot_rolls_back_the_configuration(env):
    root, srv = env["root"], env["server"]
    _item(root, "111", mod_ids=["CityMod"])
    mod_state.set_draft(srv, ModPlan(("111",), ("CityMod",), (BASE_MAP,)))
    env["monkeypatch"].setattr(mod_apply, "verify_boot", _boot_fail)

    with pytest.raises(HTTPException) as ei:
        asyncio.run(mod_apply.apply_draft("pz-test"))

    assert ei.value.status_code == 502
    # The .ini is back on the old revision — that is the actual safety promise.
    assert mod_state.live_plan(srv).mods == ()
    assert mod_apply.history(srv)[0]["status"] in {"rolled_back", "failed"}


def test_world_files_are_moved_to_quarantine_not_deleted(env):
    """A map whose cells the world already generated is dropped: the files move into
    quarantine — reversible instead of deleted."""
    root, srv = env["root"], env["server"]
    _item(root, "111", mod_ids=["CityMod"], maps={"Newtown": [(3, 4)]})
    _ini(root, mods="CityMod", items="111", maps=f"Newtown;{BASE_MAP}")
    chunk = os.path.join(root, "data", "Saves", "Multiplayer", "servertest", "map", "96", "128.bin")
    _write(chunk)                       # cell (3,4) → chunk (96,128)
    _write(os.path.join(root, "data", "Saves", "Multiplayer", "servertest", "players.db"))

    mod_state.set_draft(srv, ModPlan((), (), (BASE_MAP,)))   # everything out

    # Without confirmation the world reference blocks …
    with pytest.raises(HTTPException) as ei:
        asyncio.run(mod_apply.apply_draft("pz-test"))
    assert "WORLD_MAP_REMOVED" in {b["code"] for b in ei.value.detail["blockers"]}

    # … with confirmation it is reset surgically.
    result = asyncio.run(mod_apply.apply_draft("pz-test", confirm_world_reset=True))
    assert result["world_files_moved"] >= 1
    assert not os.path.exists(chunk)                                   # out of the world
    assert os.path.exists(os.path.join(
        root, "data", "Saves", "Multiplayer", "servertest", "players.db"))  # rest untouched

    quarantined = os.path.join(mod_apply.quarantine_root(srv), result["quarantine"],
                               "world", "map", "96", "128.bin")
    assert os.path.isfile(quarantined)                                 # moved, not deleted

    # The download has to go too: otherwise the server keeps loading downloaded maps —
    # "taking it off the lists" alone NEVER removes a map mod.
    assert result["downloads_removed"] == ["111"]
    assert not os.path.isdir(os.path.join(root, "workshop-mods", "content", "108600", "111"))
    assert os.path.isdir(os.path.join(mod_apply.quarantine_root(srv), result["quarantine"],
                                      "downloads", "111"))
    manifest = json.loads(Path(os.path.join(
        mod_apply.quarantine_root(srv), result["quarantine"], "manifest.json")).read_text(encoding="utf-8"))
    assert manifest["world"]


# --- When the socket proxy drops out ----------------------------------------------------------
#
# These three cases enter the `except` branches around `docker.state()` — exactly where it is
# decided whether to write over a possibly running world. `fail_state` (conftest) makes the
# outage reproducible; a stub whose `state` always returns a dict never reaches these branches.

def test_an_unconfirmable_stop_does_not_clear_out_under_the_running_game(env):
    """If the run state cannot be queried, the server counts as running — NOTHING is moved.

    The trap: if a raising `docker.state` left `was_running` at `False`, the stop would be skipped
    and `_stash_world` would pull the world cells out from under the running game. `verify_boot`
    would even report success afterwards — RCON answers, the server was never down.
    """
    root, srv = env["root"], env["server"]
    _item(root, "111", mod_ids=["CityMod"], maps={"Newtown": [(3, 4)]})
    _ini(root, mods="CityMod", items="111", maps=f"Newtown;{BASE_MAP}")
    chunk = os.path.join(root, "data", "Saves", "Multiplayer", "servertest", "map", "96", "128.bin")
    _write(chunk)
    mod_state.set_draft(srv, ModPlan((), (), (BASE_MAP,)))

    env["docker"].fail_state = True          # socket proxy gone

    with pytest.raises(HTTPException) as ei:
        asyncio.run(mod_apply.apply_draft("pz-test", confirm_world_reset=True))

    assert ei.value.status_code == 409
    assert os.path.exists(chunk), "world cell was moved under the possibly running game"
    assert env["backups"] == [], "work was done although the stop was not confirmed"
    assert mod_state.live_plan(srv).mods == ("CityMod",)   # .ini untouched


def test_the_rollback_does_not_write_into_a_world_that_could_still_be_running(env):
    """The most dangerous of the three: the rollback runs BECAUSE something is broken right now.

    The boot proof and the run state go through the same socket proxy. If it is gone, both fail —
    and the rollback must not push the quarantined world cells back into a game it does not know
    to be stopped.
    """
    root, srv = env["root"], env["server"]
    _item(root, "111", mod_ids=["CityMod"], maps={"Newtown": [(3, 4)]})
    _ini(root, mods="CityMod", items="111", maps=f"Newtown;{BASE_MAP}")
    chunk = os.path.join(root, "data", "Saves", "Multiplayer", "servertest", "map", "96", "128.bin")
    _write(chunk)
    mod_state.set_draft(srv, ModPlan((), (), (BASE_MAP,)))

    async def _boot_fails_because_the_proxy_is_gone(server, driver, timeout=900.0):
        env["docker"].fail_state = True
        return False

    env["monkeypatch"].setattr(mod_apply, "verify_boot", _boot_fails_because_the_proxy_is_gone)

    with pytest.raises(HTTPException) as ei:
        asyncio.run(mod_apply.apply_draft("pz-test", confirm_world_reset=True))
    assert ei.value.status_code == 502

    # The world cell stays in quarantine — safe and reversible there. Pushing it back under a
    # possibly running server would be the damage, not the rescue.
    assert not os.path.exists(chunk)
    quarantines = mod_apply.list_quarantines(srv)
    assert quarantines and quarantines[0]["world_files"] >= 1
    # And the operation honestly says it did NOT roll back.
    assert mod_apply.history(srv)[0]["status"] == "failed"


def test_a_dot_as_quarantine_name_does_not_delete_all_quarantines(env):
    """`DELETE …/mods/quarantine/%2E` must not run `shutil.rmtree` on the root directory.

    The trap is a containment check that lets the root itself through (`path != root and …` —
    for `"."`, `path == root` and the `and` short-circuits). Quarantines are the rollback material
    of EVERY past mod change; this would be the delete button for all of it.
    """
    srv = env["server"]
    root = mod_apply.quarantine_root(srv)
    existing = os.path.join(root, "20260101-000000-mod-apply")
    os.makedirs(os.path.join(existing, "world"), exist_ok=True)

    for bad in (".", "./", "", " ", "..", "../..", "\x00"):
        with pytest.raises(HTTPException) as ei:
            asyncio.run(mod_apply.delete_quarantine(srv, bad))
        assert ei.value.status_code in (400, 404), f"{bad!r} was not rejected"

    assert os.path.isdir(existing), "the existing quarantines were deleted too"


def test_a_second_apply_while_one_runs_is_rejected(env):
    async def run():
        lock = world_lock._lock("pz-test")
        await lock.acquire()
        try:
            with pytest.raises(HTTPException) as ei:
                await mod_apply.apply_draft("pz-test")
            assert ei.value.status_code == 409
        finally:
            lock.release()
            world_lock._locks.clear()

    asyncio.run(run())


def test_spawnregion_names_with_control_characters_cannot_break_the_lua(env):
    """A newline in a folder name must not produce syntactically broken Lua → boot crash."""
    srv = env["server"]
    mod_apply.write_spawnregions(srv, ["Good", 'Év"il\nInjection', "Also good"])
    from app.services import ini_editor as ini
    lua = Path(ini.spawnregions_path(srv)).read_text(encoding="utf-8")

    assert lua.count("{ name =") == 3          # all three entries, none swallowed
    assert lua.count('"') == 12                # 4 quotes per entry (name + file) — no stray ones
    assert '"il' not in lua                    # the injected quote is gone
    assert "ÉvilInjection" in lua              # quote and newline removed, name stays readable
    assert lua.rstrip().endswith("end")


# --- Managing quarantine ----------------------------------------------------------------------

def test_quarantine_can_be_listed_restored_and_deleted(env):
    """Data set aside must be visible AND recoverable — otherwise "moved instead of deleted" is
    just a promise nobody can redeem."""
    root, srv = env["root"], env["server"]
    _item(root, "111", mod_ids=["CityMod"], maps={"Newtown": [(3, 4)]})
    _ini(root, mods="CityMod", items="111", maps=f"Newtown;{BASE_MAP}")
    chunk = os.path.join(root, "data", "Saves", "Multiplayer", "servertest", "map", "96", "128.bin")
    _write(chunk)
    mod_state.set_draft(srv, ModPlan((), (), (BASE_MAP,)))
    result = asyncio.run(mod_apply.apply_draft("pz-test", confirm_world_reset=True))

    listed = mod_apply.list_quarantines(srv)
    assert listed and listed[0]["name"] == result["quarantine"]
    assert listed[0]["world_files"] >= 1
    assert listed[0]["downloads"] == ["111"]
    assert listed[0]["size"] > 0

    # Restoring only works on a stopped server.
    env["docker"].running = True
    with pytest.raises(HTTPException) as ei:
        asyncio.run(mod_apply.restore_quarantine(srv, result["quarantine"]))
    assert ei.value.status_code == 409

    env["docker"].running = False
    back = asyncio.run(mod_apply.restore_quarantine(srv, result["quarantine"]))
    assert back["restored_world_files"] >= 1
    assert os.path.exists(chunk)                     # world file back
    assert os.path.isdir(os.path.join(root, "workshop-mods", "content", "108600", "111"))

    asyncio.run(mod_apply.delete_quarantine(srv, result["quarantine"]))
    assert mod_apply.list_quarantines(srv) == []


def test_quarantine_name_cannot_escape_its_directory(env):
    with pytest.raises(HTTPException) as ei:
        asyncio.run(mod_apply.delete_quarantine(env["server"], "../../../etc"))
    assert ei.value.status_code in (400, 404)


def test_old_quarantines_are_pruned(env):
    """One operation sets several GB aside — without retention the disk fills up."""
    import time

    srv = env["server"]
    old = os.path.join(mod_apply.quarantine_root(srv), "20200101-000000-mod-apply")
    os.makedirs(os.path.join(old, "world"), exist_ok=True)
    os.utime(old, (time.time() - 90 * 86400, time.time() - 90 * 86400))
    fresh = os.path.join(mod_apply.quarantine_root(srv), "29991231-000000-mod-apply")
    os.makedirs(fresh, exist_ok=True)

    assert mod_apply.prune_quarantines(srv, keep_days=30) == 1
    assert not os.path.isdir(old)
    assert os.path.isdir(fresh)      # the young one stays


# --- A return value that must be read ---------------------------------------------------------
#
# `_stash_world` and `_restore_world` swallow every OSError per file (log.warning) and return only
# what was REALLY moved. That result must be compared with the expected list: otherwise
# `_apply_locked` reports "ok" with `world_files_moved: len(moved)`, and `_rollback` reports
# "running again" without checking whether the world came back at all. Both are the same gap
# from two sides — hence one test block.

def test_a_partially_failed_stash_does_not_report_ok(env, monkeypatch):
    """If even one world file of the dropped map stays behind, that is exactly the state the whole
    feature is built against: a world cell without a map provider — join crash without warning.
    The manifest does not know the file then, so `restore_quarantine` cannot place it either."""
    root, srv = env["root"], env["server"]
    _item(root, "111", mod_ids=["CityMod"], maps={"Newtown": [(3, 4), (3, 5)]})
    _ini(root, mods="CityMod", items="111", maps=f"Newtown;{BASE_MAP}")
    for chunk in ("128.bin", "160.bin"):
        _write(os.path.join(root, "data", "Saves", "Multiplayer", "servertest", "map", "96", chunk))
    mod_state.set_draft(srv, ModPlan((), (), (BASE_MAP,)))

    real_move = mod_apply.shutil.move
    counter = {"n": 0}

    def _second_file_jams(src, dst, *a, **kw):
        if src.endswith(".bin"):
            counter["n"] += 1
            if counter["n"] == 2:
                raise OSError("Device or resource busy")
        return real_move(src, dst, *a, **kw)

    monkeypatch.setattr(mod_apply.shutil, "move", _second_file_jams)

    with pytest.raises(HTTPException) as ei:
        asyncio.run(mod_apply.apply_draft("pz-test", confirm_world_reset=True))
    assert "of 2 world files" in str(ei.value.detail), (
        f"A world file stayed behind, reported was: {ei.value.detail!r} — exactly the "
        "orphaned-cell state the feature is built against."
    )
    assert mod_apply.history(srv)[0]["status"] in {"rolled_back", "failed"}
    # And the rollback took hold: the file that had made it into quarantine is back in the
    # world.
    assert os.path.isfile(
        os.path.join(root, "data", "Saves", "Multiplayer", "servertest", "map", "96", "128.bin")
    ), "the rollback did not bring back the already moved world file"


def test_an_incomplete_rollback_does_not_report_running_again(env, monkeypatch):
    """`verify_boot` alone is only RCON proof that SOME server came up. `_rollback` must also
    read the return values of `_restore_world`/`_restore_config`: otherwise the operator reads
    "the previous configuration is running again" while world cells still sit in quarantine."""
    root, srv = env["root"], env["server"]
    _item(root, "111", mod_ids=["CityMod"], maps={"Newtown": [(3, 4)]})
    _ini(root, mods="CityMod", items="111", maps=f"Newtown;{BASE_MAP}")
    _write(os.path.join(root, "data", "Saves", "Multiplayer", "servertest", "map", "96", "128.bin"))
    mod_state.set_draft(srv, ModPlan((), (), (BASE_MAP,)))

    # The FIRST boot proof (after applying) fails -> rollback. The SECOND (after the rollback)
    # succeeds. Exactly this constellation is the trap: the server runs again, but the world
    # files are still in quarantine.
    boot = iter([False, True])

    async def _boot(*a, **kw):
        return next(boot, True)

    monkeypatch.setattr(mod_apply, "verify_boot", _boot)

    real_move = mod_apply.shutil.move
    in_rollback = {"yes": False}

    def _rollback_jams(src, dst, *a, **kw):
        # On the rollback `src` points into quarantine, on the forward path `dst`.
        if in_rollback["yes"] and "quarantine" in src:
            raise OSError("Device or resource busy")
        return real_move(src, dst, *a, **kw)

    monkeypatch.setattr(mod_apply.shutil, "move", _rollback_jams)

    real_rollback = mod_apply._rollback

    async def _marked(*a, **kw):
        in_rollback["yes"] = True
        return await real_rollback(*a, **kw)

    monkeypatch.setattr(mod_apply, "_rollback", _marked)

    with pytest.raises(HTTPException) as ei:
        asyncio.run(mod_apply.apply_draft("pz-test", confirm_world_reset=True))

    assert ei.value.detail["code"] == "mods.change_failed_rollback_incomplete", (
        f"The rollback was incomplete, reported was: {ei.value.detail!r}. "
        "'The previous configuration is running again' tells the operator: you can keep "
        "playing."
    )
    assert mod_apply.history(srv)[0]["status"] == "failed", (
        "History says 'rolled_back' although the world did not come back."
    )


def test_the_quarantine_list_is_built_after_the_stop(env, monkeypatch):
    """The final save still writes chunks during the stop — they have to go too.

    `plan_world_reset` is a filesystem snapshot: `cells.generated_cells` lists `map/<x>/`,
    `world_files_for_cells` only takes what exists. A list built before the stop misses the cells
    that PZ's `save`+`quit` only persists then — they stay behind in the world. An orphaned cell
    without a map provider: the same chain the whole feature is built against, one step subtler.
    """
    root, srv = env["root"], env["server"]
    _item(root, "111", mod_ids=["CityMod"], maps={"Newtown": [(3, 4)]})
    _ini(root, mods="CityMod", items="111", maps=f"Newtown;{BASE_MAP}")
    save = os.path.join(root, "data", "Saves", "Multiplayer", "servertest")
    _write(os.path.join(save, "map", "96", "128.bin"))
    mod_state.set_draft(srv, ModPlan((), (), (BASE_MAP,)))

    # The driver writes ANOTHER cell of the same map while shutting down — exactly what a real
    # `save`+`quit` does.
    real_stop = mod_apply.lifecycle.stop_and_confirm

    async def _stop_with_final_save(server, **kw):
        result = await real_stop(server, **kw)
        _write(os.path.join(save, "map", "96", "129.bin"))
        return result

    monkeypatch.setattr(mod_apply.lifecycle, "stop_and_confirm", _stop_with_final_save)

    result = asyncio.run(mod_apply.apply_draft("pz-test", confirm_world_reset=True))

    assert result["status"] == "ok", result
    straggler = os.path.join(save, "map", "96", "129.bin")
    assert not os.path.exists(straggler), (
        "The cell written by the final save stayed behind in the world — orphaned cell "
        "without a map provider."
    )
    assert os.path.isfile(os.path.join(
        mod_apply.quarantine_root(srv), result["quarantine"], "world", "map", "96", "129.bin"
    )), "the straggler did not end up in quarantine"
