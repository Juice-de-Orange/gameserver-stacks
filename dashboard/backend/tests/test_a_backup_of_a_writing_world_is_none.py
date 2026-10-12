"""A game that cannot save is stopped, and a stopped server needs no quiesce.

Valheim has no external save command (no console, no stdin, `save()` is a no-op), so a hot
backup copies the world WHILE it is being written. Three things belong together here:

* **A structural note is not an incident.** `create_backup` deliberately records NO incident
  for `QuiesceWarning(structural=True)`; `scheduled_backup` must see that distinction in the
  result, or it records one anyway for every `quiesce_warning` (noise for something that cannot
  fail).
* **No hook against a STOPPED container.** `stop_and_snapshot` stops with confirmation; running
  the quiesce hook afterwards can only fail and reports on a container that no longer exists.
* **Where no save command exists, stopping is the only way to a consistent archive.**
"""
from __future__ import annotations

import asyncio

import pytest

from app.services import backup as backup_svc
from app.services.drivers.base import BackupSpec, QuiesceWarning


def test_valheim_is_stopped_for_the_backup():
    """The game without a save command is exactly what this flag is meant for."""
    from app.services.drivers.valheim import ValheimDriver
    from app.models.server import GameServer

    srv = GameServer(key="vh", game_type="valheim", display_name="Valheim",
                     container_name="valheim", data_dir="/srv/gameservers/valheim", enabled=True)
    assert ValheimDriver().backup_spec(srv).stop_for_consistency is True


@pytest.mark.parametrize("module,cls", [
    ("app.services.drivers.pz", "PZDriver"),
    ("app.services.drivers.minecraft", "MinecraftDriver"),
])
def test_a_game_that_can_save_is_not_stopped(module, cls):
    """A stop would be needless downtime where a `save` is enough; PZ and MC can do that."""
    import importlib

    from app.models.server import GameServer

    drv = getattr(importlib.import_module(module), cls)()
    srv = GameServer(key="x", game_type="pz", display_name="X", container_name="x",
                     data_dir="/tmp/x", enabled=True)
    assert drv.backup_spec(srv).stop_for_consistency is False


def test_the_default_is_not_stopping():
    """New drivers inherit the cheaper path; stopping is the exception, not the rule."""
    assert BackupSpec(data_dir="/x", world_subdirs=()).stop_for_consistency is False


def test_a_structural_note_records_no_incident(tmp_path, monkeypatch):
    """**The core.** No incident for something that CANNOT fail.

    The result dict is checked, because that is exactly what `scheduled_backup` decides on.
    """
    world = tmp_path / "worlds_local"
    world.mkdir()
    (world / "world.db").write_text("x", encoding="utf-8")

    def hook():
        raise QuiesceWarning("no external save command", structural=True)

    result = backup_svc.create_backup(
        "test", data_dir=str(tmp_path), world_subdirs=("worlds_local",),
        pre_hook=hook, post_hook=None, server=None,
    )
    assert result.get("quiesce_warning"), "the note stays: nobody may assume consistency"
    assert result.get("quiesce_structural") is True, (
        "Without this flag `scheduled_backup` records an incident after all, for every "
        "Valheim backup."
    )


def test_a_real_quiesce_error_stays_an_incident(tmp_path):
    """The other direction: the fix must not sweep away the ONE real error as well."""
    world = tmp_path / "worlds_local"
    world.mkdir()
    (world / "world.db").write_text("x", encoding="utf-8")

    def hook():
        raise QuiesceWarning("save point only half written")   # structural=False

    result = backup_svc.create_backup(
        "test", data_dir=str(tmp_path), world_subdirs=("worlds_local",),
        pre_hook=hook, post_hook=None, server=None,
    )
    assert result.get("quiesce_warning")
    assert not result.get("quiesce_structural"), (
        "A half-written save point IS a failure and belongs in plain sight."
    )


def test_a_confirmed_stop_runs_no_more_quiesce_hooks(monkeypatch):
    """A hook on a stopped container can only fail.

    It would record incidents carrying the error of a container that no longer exists. Checked:
    `stop_and_snapshot` does not even pass the hooks on.
    """
    from app.services import lifecycle

    called: list[str] = []

    def pre():                                     # pragma: no cover — must not run
        called.append("pre")

    def post():                                    # pragma: no cover — must not run
        called.append("post")

    class _Drv:
        def backup_spec(self, server):
            return BackupSpec(data_dir="/x", world_subdirs=("w",), pre_hook=pre, post_hook=post)

    seen: dict = {}

    def _fake_create_backup(label, **kw):
        seen.update(kw)
        return {"name": "x.tar.gz", "size": 1}

    async def _fake_stop_and_confirm(server, **kw):
        return True, True

    monkeypatch.setattr(lifecycle, "create_backup", _fake_create_backup)
    monkeypatch.setattr(lifecycle, "stop_and_confirm", _fake_stop_and_confirm)

    class _Srv:
        key = "vs"
        id = 4
        container_name = "vintagestory"

    was_running, _ = asyncio.run(lifecycle.stop_and_snapshot(_Srv(), _Drv(), "mandatory"))
    assert was_running is True
    assert called == [], "no quiesce hook runs on a server whose stop is confirmed"
    assert seen["pre_hook"] is None and seen["post_hook"] is None, (
        "The hooks must not even be passed on."
    )
