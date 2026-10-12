"""What the forward path touched, the rollback takes back — including what did NOT exist before.

**One pattern:** the rollback only knows what was already there on the forward path. Anything
newly created stays behind, and the rollback still reports success.

* `mods/apply`: `_stash_config` only saves what exists (`os.path.isfile`), `write_spawnregions`
  writes unconditionally. If there was no `spawnregions.lua` before — the normal case for a
  server that never had map mods — `_restore_config` can neither write it back nor remove it and
  returns `ok=True`. The Lua file created by the failed draft stays in the world, and a complete
  rollback is reported.
* `version_manager`: `_persist_version(save_previous=True)` moves the running values to
  `previous_*`. `_restore_previous` brings them back — but leaves `previous_*` in place. Then
  `image_tag == previous_image_tag` holds, and the rollback button answers 400 "no version
  change": after a rolled-back change the emergency path is dead.

**The pair table below IS the class-closing part.** A guard is not an option here — the symmetry
of two functions cannot be expressed on the syntax tree. Every new forward/rollback pair is
entered here.
"""
from __future__ import annotations

import json
import os

import pytest
from sqlmodel import Session, select

from app.models.server import GameServer, GameType, ManagementMode
from app.services import ini_editor as ini
from app.services import version_manager as vm
from app.services.mods import apply as mod_apply

# The pairs that exist in the world-writing core. Whoever adds one enters it here — and writes
# the round-trip test for it.
PAIRS = [
    ("mods/apply", "_stash_config", "_restore_config"),
    ("mods/apply", "_stash_world", "_restore_world"),
    ("mods/apply", "_stash_downloads", "_restore_downloads"),
    ("version_manager", "_persist_version", "_restore_version_fields"),
]


def _srv(root) -> GameServer:
    s = GameServer(
        key="pz-test", game_type=GameType.pz, management_mode=ManagementMode.dashboard,
        container_name="pz-test-container", data_dir=str(root), enabled=True,
        extra=json.dumps({"servername": "servertest"}),
    )
    s.id = 1
    return s


def _write(path, text="x"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


# ------------------------------------------------------- Pair 1: .ini and spawnregions.lua

def test_a_newly_created_spawnregions_is_removed_again(tmp_path):
    """The round trip: absent -> stash -> written -> restore -> absent again."""
    srv = _srv(tmp_path)
    _write(ini.ini_path(srv), "Mods=\nMap=Muldraugh, KY\n")
    lua = ini.spawnregions_path(srv)
    assert not os.path.exists(lua), "setup: the file must not exist beforehand"

    qpath = str(tmp_path / "q")
    os.makedirs(os.path.join(qpath, "config"), exist_ok=True)
    saved = mod_apply._stash_config(srv, qpath)

    mod_apply.write_spawnregions(srv, ["Muldraugh, KY"])
    assert os.path.exists(lua), "setup: the forward path must really create the file"

    ok = mod_apply._restore_config(srv, saved)

    assert not os.path.exists(lua), (
        "The spawnregions.lua created by the failed draft is still in the world — "
        "the rollback was incomplete."
    )
    assert ok is True, "a complete rollback also reports True"


def test_an_existing_spawnregions_is_written_back(tmp_path):
    """Cross-check: if it was there, the OLD content comes back — not a removal."""
    srv = _srv(tmp_path)
    _write(ini.ini_path(srv), "Mods=\nMap=Muldraugh, KY\n")
    lua = ini.spawnregions_path(srv)
    _write(lua, "-- old revision\n")

    qpath = str(tmp_path / "q")
    os.makedirs(os.path.join(qpath, "config"), exist_ok=True)
    saved = mod_apply._stash_config(srv, qpath)
    mod_apply.write_spawnregions(srv, ["Newtown"])

    assert mod_apply._restore_config(srv, saved) is True
    with open(lua, encoding="utf-8") as f:
        assert f.read() == "-- old revision\n"


# ------------------------------------------------------- Pair 2: the six version fields

@pytest.fixture()
def registry(mem_engine, monkeypatch):
    monkeypatch.setattr(vm, "engine", mem_engine)
    with Session(mem_engine) as s:
        row = GameServer(
            key="pz-test", game_type=GameType.pz, management_mode=ManagementMode.dashboard,
            container_name="pz-test", enabled=True,
            image_tag="B", game_version="B", server_type="release",
            previous_image_tag="A", previous_game_version="A", previous_type="release",
        )
        s.add(row)
        s.commit()
    return mem_engine


def _fields(engine) -> dict:
    with Session(engine) as s:
        row = s.exec(select(GameServer).where(GameServer.key == "pz-test")).first()
        return {
            "image_tag": row.image_tag, "game_version": row.game_version,
            "server_type": row.server_type, "previous_image_tag": row.previous_image_tag,
            "previous_game_version": row.previous_game_version, "previous_type": row.previous_type,
        }


def test_a_rolled_back_change_keeps_the_rollback_button_working(registry):
    """B -> C fails: afterwards the state must be exactly as before, previous_* included."""
    before = _fields(registry)
    assert before["image_tag"] == "B" and before["previous_image_tag"] == "A"

    snapshot = vm._persist_version(
        "pz-test", image_tag="C", game_version=None, server_type=None, save_previous=True,
    )
    assert _fields(registry)["image_tag"] == "C"

    vm._restore_version_fields("pz-test", snapshot)

    assert _fields(registry) == before, (
        "After the rollback ALL six fields must be back to their previous state. "
        "If `previous_image_tag` stays on B, image_tag == previous_image_tag holds and the "
        "rollback button answers 400 'no version change'."
    )


def test_the_rollback_stays_usable(registry):
    """The actual damage in one sentence: after the rollback previous_* does not point at itself."""
    snapshot = vm._persist_version(
        "pz-test", image_tag="C", game_version=None, server_type=None, save_previous=True,
    )
    vm._restore_version_fields("pz-test", snapshot)
    f = _fields(registry)
    assert f["image_tag"] != f["previous_image_tag"], (
        "image_tag == previous_image_tag — the rollback button has no target any more."
    )


def test_the_pair_table_is_complete():
    """Class-closing instead of a guard: every pair really exists and is listed here."""
    missing = []
    for module, forward, rollback in PAIRS:
        target = mod_apply if module == "mods/apply" else vm
        for name in (forward, rollback):
            if not hasattr(target, name):
                missing.append(f"{module}.{name}")
    assert not missing, (
        "The table names functions that do not exist (any more) — then it is not a balance "
        "sheet but decoration:\n  " + "\n  ".join(missing)
    )
