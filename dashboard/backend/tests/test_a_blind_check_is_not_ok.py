"""What cannot be read is not "fine".

**Three places, one failure direction:** a read error turns into a green result.

* `foreign.scan` swallowed read errors per folder (`except OSError: continue`) and returned a
  SHORTER list without a mark. `check()` only iterates over it, finds zero blockers on an empty
  list, and the apply runs stop, mandatory backup and restart, moves nothing and reports success.
  The PZ counterpart `mods/inventory.py` turns this fail-closed into a
  `WORKSHOP_DIR_UNREADABLE` blocker.
* `state.live_plan` caught `OSError` and returned an empty `ModPlan()`. `preflight.check` finds
  nothing in it, `status()` reports a green traffic light, and `guard_boot` returns an empty
  blocker list: the guard in front of scheduled restart, autoheal and `perform_action` is silently
  OFF. On top of that `reset_draft` writes the empty plan to the database.
* `foreign._folder_metadata` did `except (OSError, ValueError): pass` and fell back to the folder
  name without a `problem`, against the rule `read_metadata` sets out explicitly. Result: no
  `MODFILE_UNREADABLE`, and from then on the `mod_id` is the folder name, so `MOD_DUPLICATE` no
  longer fires either.

**Two different means, on purpose.** `foreign.scan` RAISES: whoever raises cannot be overlooked.
`state.live_plan` gets a field instead, because `guard_boot` has a blanket
`except Exception: return []`: an exception there would be fail-open again, of all things.

**And one special rule without which it would get expensive:** PZ creates the `.ini` only on the
first boot. A file that does not exist yet must therefore not be a blocker, or a freshly cloned
`pz-test` can no longer start.

No guard for this: the rule would be "`except OSError` around `os.listdir` in `services/mods/`
must not carry on silently", and the legitimate exceptions would be longer than the rule.
"""
import asyncio
import json
import os

import pytest

from app.models.server import GameServer, GameType, ManagementMode
from app.services import ini_editor as ini
from app.services.mods import foreign
from app.services.mods import inventory
from app.services.mods import preflight
from app.services.mods import state as mod_state


def _mc(root) -> GameServer:
    s = GameServer(key="mc", game_type=GameType.minecraft, management_mode=ManagementMode.dashboard,
                   container_name="minecraft", data_dir=str(root), enabled=True,
                   server_type="PAPER")
    s.id = 3
    return s


def _pz(root) -> GameServer:
    s = GameServer(key="pz-test", game_type=GameType.pz,
                   management_mode=ManagementMode.dashboard, container_name="servertest",
                   data_dir=str(root), enabled=True,
                   extra=json.dumps({"servername": "servertest"}))
    s.id = 1
    return s


def _vs(root) -> GameServer:
    s = GameServer(key="vs", game_type=GameType.vintagestory,
                   management_mode=ManagementMode.dashboard, container_name="vintagestory",
                   data_dir=str(root), enabled=True)
    s.id = 4
    return s


# ------------------------------------------------------- 1. The unreadable mod folder

def test_an_unreadable_mod_folder_is_loud(tmp_path, monkeypatch):
    """The folder EXISTS and cannot be listed; that is not an empty folder."""
    srv = _mc(tmp_path)
    os.makedirs(foreign.mods_dir(srv), exist_ok=True)

    real = os.listdir

    def _no_access(p):
        if os.path.abspath(p) == os.path.abspath(foreign.mods_dir(srv)):
            raise PermissionError("Permission denied")
        return real(p)
    monkeypatch.setattr(foreign.os, "listdir", _no_access)

    with pytest.raises(OSError):
        foreign.scan(srv)


def test_a_missing_mod_folder_stays_silent(tmp_path):
    """Cross-check: absent is something other than unreadable."""
    srv = _mc(tmp_path)
    assert foreign.scan(srv) == []


def test_an_active_file_the_scan_does_not_know_blocks(tmp_path):
    """Fail-closed at the check choke point: otherwise the apply runs stop+backup+restart for nothing.

    Trigger: after a switch from PAPER to FABRIC `mods_dir` points to another path; the stored
    selection names files the scan does not see there.
    """
    srv = _mc(tmp_path)
    os.makedirs(foreign.mods_dir(srv), exist_ok=True)

    result = asyncio.run(foreign.check(srv, {"vanished.jar"}))

    codes = {f.code for f in result.blockers}
    assert "MODFILE_MISSING" in codes, (
        f"Zero blockers for a file that does not exist; the apply reports success: {codes}"
    )


# ------------------------------------------------------- 2. The unreadable .ini

def test_an_unreadable_ini_does_not_turn_the_traffic_light_green(tmp_path, monkeypatch):
    srv = _pz(tmp_path)
    path = ini.ini_path(srv)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write("Mods=\nMap=Muldraugh, KY\n")

    def _no_access(p):
        raise PermissionError("Permission denied")
    monkeypatch.setattr(mod_state.ini, "get_ini_at", _no_access)

    plan = mod_state.live_plan(srv)
    assert plan.readable is False, "the empty plan looks like a clean server"
    assert plan.detail

    result = preflight.check(plan, inventory.scan(srv))
    assert any(f.code == "INI_UNREADABLE" for f in result.blockers), (
        f"no blocker: {[f.code for f in result.findings]}"
    )


def test_an_ini_not_created_yet_is_not_a_blocker(tmp_path):
    """PZ writes the .ini only on the first boot; otherwise a fresh pz-test would be dead."""
    srv = _pz(tmp_path)
    plan = mod_state.live_plan(srv)
    assert plan.readable is False
    result = preflight.check(plan, inventory.scan(srv))
    assert not [f for f in result.blockers if f.code == "INI_UNREADABLE"], (
        "a missing .ini must not block the start"
    )


def test_a_readable_ini_stays_unchanged(tmp_path):
    """Cross-check: the normal case."""
    srv = _pz(tmp_path)
    path = ini.ini_path(srv)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write("Mods=TownMod\nWorkshopItems=111\nMap=Muldraugh, KY\n")

    plan = mod_state.live_plan(srv)
    assert plan.readable is True and plan.mods == ("TownMod",)


def test_the_boot_gate_sees_the_unreadable_ini(tmp_path, monkeypatch):
    """`guard_boot` stands in front of scheduled restart, autoheal and the start button."""
    srv = _pz(tmp_path)
    path = ini.ini_path(srv)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write("Mods=\nMap=Muldraugh, KY\n")

    def _no_access(p):
        raise PermissionError("Permission denied")
    monkeypatch.setattr(mod_state.ini, "get_ini_at", _no_access)
    mod_state.invalidate(srv.key)

    assert asyncio.run(mod_state.guard_boot(srv)), "the boot gate is silently OFF on an unreadable .ini"


# ------------------------------------------------------- 3. The broken modinfo.json

def test_a_broken_modinfo_reports_a_problem(tmp_path):
    srv = _vs(tmp_path)
    folder = os.path.join(foreign.mods_dir(srv), "BrokenMod")
    os.makedirs(folder, exist_ok=True)
    with open(os.path.join(folder, "modinfo.json"), "w", encoding="utf-8") as f:
        f.write("{ this is not JSON")

    meta = foreign._folder_metadata(folder)
    assert meta.get("problem"), (
        "The broken modinfo.json silently falls back to the folder name: no "
        "MODFILE_UNREADABLE, and from then on the mod_id is the folder name, so MOD_DUPLICATE "
        "no longer fires either."
    )


def test_a_folder_without_any_modinfo_stays_fine(tmp_path):
    """Cross-check: the fallback to the folder name is right for this case and stays."""
    srv = _vs(tmp_path)
    folder = os.path.join(foreign.mods_dir(srv), "PlainMod")
    os.makedirs(folder, exist_ok=True)

    meta = foreign._folder_metadata(folder)
    assert not meta.get("problem")
    assert meta["mod_id"] == "PlainMod"
