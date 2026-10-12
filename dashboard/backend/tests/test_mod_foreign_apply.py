"""File-based mod apply (Minecraft / Vintage Story) — the counterpart of `test_mod_apply.py`.

Pins down that the summary counts the files actually moved, not the REQUESTED selection, and that
the branch without boot proof says so.
"""
import asyncio
import os
import zipfile

import pytest
from fastapi import HTTPException

from app.models.server import GameServer, GameType, ManagementMode
from app.services import lifecycle
from app.services import world_lock
from app.services.mods import foreign
from app.services.mods import foreign_apply as fa


def _srv(root) -> GameServer:
    srv = GameServer(
        key="mc", game_type=GameType.minecraft, management_mode=ManagementMode.dashboard,
        container_name="minecraft", data_dir=str(root), enabled=True, server_type="PAPER",
    )
    srv.id = 3
    return srv


def _plugin(path: str, name: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("plugin.yml", f"name: {name}\nversion: 1.0\nmain: x.Y\n")


@pytest.fixture()
def env(tmp_path, monkeypatch, wire_lifecycle, fake_docker_cls):
    srv = _srv(tmp_path)
    _plugin(os.path.join(foreign.mods_dir(srv), "on.jar"), "On")
    _plugin(os.path.join(foreign.disabled_dir(srv), "off.jar"), "Off")

    docker = fake_docker_cls()
    monkeypatch.setattr(fa, "docker", docker)
    monkeypatch.setattr(fa, "get_driver", lambda s: _FakeDriver(tmp_path))
    wire_lifecycle(docker, _FakeDriver(tmp_path))
    monkeypatch.setattr(fa.watchdog, "suppress", lambda k, m: None)

    backups: list[str] = []
    monkeypatch.setattr(lifecycle, "create_backup", lambda label, **kw: (
        backups.append(label) or {"name": f"{label}.tar.gz", "size": 1}
    ))

    async def _boot_ok(server, driver, timeout=900.0):
        return True

    monkeypatch.setattr(fa, "verify_boot", _boot_ok)
    world_lock._locks.clear()
    world_lock._holders.clear()
    return {"server": srv, "docker": docker, "backups": backups, "monkeypatch": monkeypatch}


class _FakeDriver:
    def __init__(self, root):
        self.root = root

    async def announce(self, server, text):
        pass

    async def save(self, server):
        pass

    def backup_spec(self, server):
        from app.services.drivers.base import BackupSpec

        return BackupSpec(data_dir=str(self.root), world_subdirs=("world",))


def test_the_good_case_moves_and_counts_what_actually_moved(env):
    srv = env["server"]

    result = asyncio.run(fa.apply(srv, {"off.jar"}))

    assert result["status"] == "ok"
    assert env["backups"] == ["pre-modfiles"]
    assert result["enabled"] == ["off.jar"] and result["disabled"] == ["on.jar"]
    assert result["summary"] == "1 enabled, 1 disabled"
    assert os.path.isfile(os.path.join(foreign.mods_dir(srv), "off.jar"))


def test_a_failed_move_does_not_end_green(env, monkeypatch):
    """`summary` must not mirror `len(enabled)` — the selection from the request.

    Otherwise a failed `_move` becomes a missing list entry and nothing else: the operation ends
    green while the crashing plugin stays in `mods/`. The user sees only `summary` in
    `Operation.tsx` — the lists next to it are not shown.
    """
    def _broken(src, dst):
        raise OSError("Read-only file system")

    monkeypatch.setattr(foreign.shutil, "move", _broken)

    with pytest.raises(HTTPException) as ei:
        asyncio.run(fa.apply(env["server"], {"off.jar"}))

    assert ei.value.status_code == 502
    assert ei.value.detail["code"] == "mods.files_not_moved"
    assert "off.jar" in ei.value.detail["params"]["files"] and "on.jar" in ei.value.detail["params"]["files"]


def test_a_stopped_server_gets_no_invented_boot_proof(env):
    """If the server was not running before, the operation returns early — without `verify_boot`.

    That is correct (it is not started just for this), but the message must say so instead of
    looking like any other success message.
    """
    env["docker"].running = False

    result = asyncio.run(fa.apply(env["server"], {"off.jar"}))

    assert result["started"] is False
    assert "no boot proof" in result["summary"]


def test_an_unchanged_selection_is_rejected(env):
    with pytest.raises(HTTPException) as ei:
        asyncio.run(fa.apply(env["server"], {"on.jar"}))
    assert ei.value.status_code == 400


def test_a_second_run_while_one_is_running_is_rejected(env):
    async def run():
        lock = world_lock._lock("mc")
        await lock.acquire()
        try:
            with pytest.raises(HTTPException) as ei:
                await fa.apply(env["server"], {"off.jar"})
            assert ei.value.status_code == 409
        finally:
            lock.release()

    asyncio.run(run())


# --- The rollback hangs on the right question -------------------------------------------------
#
# Three things `_rollback` must get right at once:
#
# 1. Its result must be used — the 502 message names the server state, not only the files that
#    could not be moved.
# 2. The rollback depends on whether files were moved, not on `was_running`. On a STOPPED server
#    (the normal case when swapping mods) a half-moved mix would otherwise be read by
#    `foreign.scan` as the "current selection".
# 3. Files that do not move back must not skip the start: the container would stay down with a
#    silent watchdog. The PZ twin `apply._rollback` also starts on `complete=False`.
#
# Hence: `_rollback` ALWAYS moves files back and starts only if the server was running before.

def _make_move_fail(monkeypatch, *, only_on_rollback=False):
    """`foreign.set_enabled` reports a file that could not be moved."""
    real = foreign.set_enabled
    state = {"n": 0}

    def _fake(server, enabled):
        state["n"] += 1
        result = real(server, enabled)
        if only_on_rollback and state["n"] == 1:
            return result
        result["failed"] = ["stuck.jar"]
        return result

    monkeypatch.setattr(foreign, "set_enabled", _fake)


def test_a_stopped_server_gets_its_rollback(env):
    """The core: the rollback depends on moved files, not on the wish to start."""
    srv, docker = env["server"], env["docker"]
    docker.running = False          # the normal case when swapping mods
    _make_move_fail(env["monkeypatch"])

    with pytest.raises(HTTPException) as ei:
        asyncio.run(fa.apply(srv, {"on.jar", "off.jar"}))

    assert ei.value.status_code == 502
    # The old selection is back — not the mix.
    current = {m.filename for m in foreign.scan(srv) if m.enabled}
    assert current == {"on.jar"}, (
        f"The half-moved state stayed and now counts as the selection: {current}"
    )
    assert "start" not in docker.calls, "a server that was stopped before was started"


def test_the_message_names_the_server_state(env):
    """The user must know whether the server is running again."""
    srv = env["server"]
    _make_move_fail(env["monkeypatch"])

    with pytest.raises(HTTPException) as ei:
        asyncio.run(fa.apply(srv, {"on.jar", "off.jar"}))

    text = str(ei.value.detail)
    assert "stuck.jar" in text
    assert ("is running" in text or "WARNING" in text), (
        "The 502 names only the files, not the server state:\n  " + text
    )


def test_the_rollback_starts_even_if_a_file_stays_behind(env):
    """A file that stays behind must not skip the start — the container would stay down, the watchdog silent."""
    srv, docker = env["server"], env["docker"]
    docker.running = True

    async def _boot_fail(server, driver, timeout=900.0):
        return False
    env["monkeypatch"].setattr(fa, "verify_boot", _boot_fail)
    _make_move_fail(env["monkeypatch"], only_on_rollback=True)

    with pytest.raises(HTTPException) as ei:
        asyncio.run(fa.apply(srv, {"on.jar", "off.jar"}))

    assert ei.value.status_code == 502
    assert docker.calls[-1] == "start", (
        "After the rollback the server must come up again. The last call was a stop — "
        "the container stayed down, with a silent watchdog. "
        f"Calls: {docker.calls}"
    )
    assert "WARNING" in str(ei.value.detail)


def test_if_every_move_fails_the_server_still_comes_up(env):
    """The rollback condition is "were files moved" — the edge case is NOTHING moved at all.

    If every attempt fails, `enabled` and `disabled` are empty and `failed` is full. That case
    must still end in `docker start`; otherwise the server stays down after the confirmed stop,
    with a silent watchdog.
    """
    srv, docker = env["server"], env["docker"]
    docker.running = True

    def _nothing_works(server, enabled):
        return {"enabled": [], "disabled": [], "failed": ["stuck.jar"]}
    env["monkeypatch"].setattr(foreign, "set_enabled", _nothing_works)

    with pytest.raises(HTTPException) as ei:
        asyncio.run(fa.apply(srv, {"on.jar", "off.jar"}))

    assert ei.value.status_code == 502
    assert docker.calls[-1] == "start", (
        "No move succeeded, so nothing was moved — the server must still come up again. "
        f"Calls: {docker.calls}"
    )


def test_an_unreadable_mod_folder_in_the_rollback_does_not_swallow_the_message(env):
    """`set_enabled` calls `scan` internally, which raises on an unreadable folder — inside the
    rollback that must not replace the message."""
    srv, docker = env["server"], env["docker"]
    docker.running = True
    state = {"n": 0}

    def _ok_first_then_blind(server, enabled):
        state["n"] += 1
        if state["n"] == 1:
            return {"enabled": ["off.jar"], "disabled": ["on.jar"], "failed": []}
        raise OSError("The mod folder exists but cannot be listed")
    env["monkeypatch"].setattr(foreign, "set_enabled", _ok_first_then_blind)

    async def _boot_fail(server, driver, timeout=900.0):
        return False
    env["monkeypatch"].setattr(fa, "verify_boot", _boot_fail)

    with pytest.raises(HTTPException) as ei:
        asyncio.run(fa.apply(srv, {"on.jar", "off.jar"}))

    assert ei.value.status_code == 502, "a raw OSError replaced the 502"
    assert "WARNING" in str(ei.value.detail)
    assert docker.calls[-1] == "start"
