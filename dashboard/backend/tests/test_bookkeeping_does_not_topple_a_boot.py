"""A bookkeeping error must not roll back a proven boot.

The transaction boundary belongs behind the boot proof, not behind the bookkeeping.

* `mods/apply._apply_locked`: if `state.snapshot_applied`, `_finish(change_id, "ok")` and
  `state.invalidate` sat IN the `try` whose `except` triggers the full rollback, a `console-db`
  gone at exactly that moment would make a pure DB error stop the server proven via RCON, write
  the old `.ini` back and pull world cells out of quarantine.
* `version_manager._change_version_locked`: the same, but more destructive. There the `except`
  runs `_rollback_swap` — `docker.remove(name, force=True)` on the just-verified container plus
  `extract_backup` of the pre-version archive over the data folder.

**The rule:** from the proven boot on, the operation has succeeded. What goes wrong afterwards
is a note in the result — not a reason to touch a running world. Model in the codebase:
`_report_rollback` and `operations.run`, which already separate work and bookkeeping.
"""
import asyncio
import json
import os

import pytest
from fastapi import HTTPException

from app.models.server import GameServer, GameType, ManagementMode
from app.services.mods import apply as mod_apply
from app.services.mods import state as mod_state
from app.services.mods.preflight import BASE_MAP, ModPlan


def _write(path, text="x"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def _item(root, wid, *, mod_ids=()):
    base = os.path.join(root, "workshop-mods", "content", "108600", str(wid), "42")
    _write(os.path.join(base, "mod.info"), "\n".join(f"id={m}" for m in mod_ids) + "\n")


async def _boot_ok(server, driver, timeout=900.0):
    return True


class FakeDriver:
    def __init__(self, root):
        self.root = root

    async def announce(self, server, text):
        pass

    async def save(self, server):
        pass

    def backup_spec(self, server):
        from app.services.drivers.base import BackupSpec
        return BackupSpec(data_dir=os.path.join(self.root, "data"),
                          world_subdirs=("Saves", "Server"))


@pytest.fixture()
def env(tmp_path, monkeypatch, mem_engine, wire_lifecycle, fake_docker_cls):
    root = str(tmp_path)
    srv = GameServer(
        key="pz-test", game_type=GameType.pz, management_mode=ManagementMode.dashboard,
        container_name="pz-test-container", data_dir=root, enabled=True,
        extra=json.dumps({"servername": "servertest"}),
    )
    srv.id = 1
    _write(os.path.join(root, "data", "Server", "servertest.ini"),
           f"Mods=\nWorkshopItems=\nMap={BASE_MAP}\nPVP=true\n")

    monkeypatch.setattr(mod_state, "engine", mem_engine)
    monkeypatch.setattr(mod_apply, "engine", mem_engine)
    monkeypatch.setattr("app.services.servers.get_by_key", lambda k: srv)

    docker = fake_docker_cls()
    monkeypatch.setattr(mod_apply, "docker", docker)
    wire_lifecycle(docker, FakeDriver(root))
    monkeypatch.setattr(mod_apply, "get_driver", lambda s: FakeDriver(root))
    monkeypatch.setattr(mod_apply.watchdog, "suppress", lambda k, m: None)
    monkeypatch.setattr(mod_apply, "create_backup",
                        lambda label, **kw: {"name": f"{label}.tar.gz", "size": 1})
    monkeypatch.setattr(mod_apply, "verify_boot", _boot_ok)
    mod_state.invalidate(srv.key)
    return {"root": root, "server": srv, "docker": docker, "monkeypatch": monkeypatch}


def test_a_db_error_after_the_boot_proof_rolls_nothing_back(env):
    """`snapshot_applied` raises — the server stays up, the new .ini stays in place."""
    root, srv = env["root"], env["server"]
    _item(root, "111", mod_ids=["TownMod"])
    mod_state.set_draft(srv, ModPlan(("111",), ("TownMod",), (BASE_MAP,)))

    def _db_gone(*a, **k):
        raise RuntimeError("console-db does not answer")
    env["monkeypatch"].setattr(mod_state, "snapshot_applied", _db_gone)

    result = asyncio.run(mod_apply.apply_draft("pz-test"))

    assert result["status"] == "ok", (
        "The boot was proven via RCON. A bookkeeping error afterwards does not turn it into a "
        "failed operation."
    )
    assert env["docker"].calls == ["stop", "start"], (
        "There was a second stop — the rollback ran although the server provably came up: "
        f"{env['docker'].calls}"
    )
    assert mod_state.live_plan(srv).mods == ("TownMod",), ".ini was written back"
    assert result.get("set_id") is None
    assert "note" in result and "history" in result["note"]["message"], (
        "The bookkeeping failure must be in the result — otherwise it is invisible."
    )
    assert result["note"]["code"] == "mods.history_not_written"
    assert result["note"]["params"] == {"error": "console-db does not answer"}


def test_a_db_error_while_finishing_rolls_nothing_back_either(env):
    """Same seam, different line: `_finish` writes the history."""
    root, srv = env["root"], env["server"]
    _item(root, "111", mod_ids=["TownMod"])
    mod_state.set_draft(srv, ModPlan(("111",), ("TownMod",), (BASE_MAP,)))

    real_finish = mod_apply._finish

    def _only_on_ok(change_id, status, *a, **k):
        if status == "ok":
            raise RuntimeError("console-db does not answer")
        return real_finish(change_id, status, *a, **k)
    env["monkeypatch"].setattr(mod_apply, "_finish", _only_on_ok)

    result = asyncio.run(mod_apply.apply_draft("pz-test"))

    assert result["status"] == "ok"
    assert env["docker"].calls == ["stop", "start"]
    assert mod_state.live_plan(srv).mods == ("TownMod",)


def test_a_real_boot_failure_still_rolls_back(env):
    """Cross-check — the transaction boundary may only exclude the bookkeeping."""
    root, srv = env["root"], env["server"]
    _item(root, "111", mod_ids=["TownMod"])
    mod_state.set_draft(srv, ModPlan(("111",), ("TownMod",), (BASE_MAP,)))

    async def _boot_fail(server, driver, timeout=900.0):
        return False
    env["monkeypatch"].setattr(mod_apply, "verify_boot", _boot_fail)

    with pytest.raises(HTTPException) as ei:
        asyncio.run(mod_apply.apply_draft("pz-test"))

    assert ei.value.status_code == 502
    assert mod_state.live_plan(srv).mods == (), "the .ini should have been written back"
