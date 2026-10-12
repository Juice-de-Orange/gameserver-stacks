"""Valheim as image 1.4.0 really runs it: worlds as directories, and a query that opens late.

* Every new world is a DIRECTORY (`worlds_local/Dedicated/_main.0.fwl2`, later `_main.1.db2`
  and chunk files). The driver only knew the `<Name>.db` + `<Name>.fwl` pair, so a new server's
  world was not listed, selectable or removable, and the configuration page described files that
  do not exist.
* The game logs "Game server connected" (the documented ready line) about a minute before it
  answers the Steam query ("Opened Steam server"). A check from inside in that window said only
  "no answer"; it has to say why.
"""
import asyncio
import json
import os

import pytest

from app.models.server import ConsoleKind, GameServer, GameType
from app.services.a2s import A2sError
from app.services.drivers.valheim import ValheimDriver


def _vh(tmp_path, world="Dedicated") -> GameServer:
    os.makedirs(tmp_path / "config" / "worlds_local", exist_ok=True)
    return GameServer(key="vh", game_type=GameType.valheim, container_name="valheim",
                      data_dir=str(tmp_path), console_kind=ConsoleKind.none,
                      game_ports='{"game": "2456/udp"}',
                      extra=json.dumps({"env": {"WORLD_NAME": world}}))


def _dir_world(tmp_path, name, files=("_main.0.fwl2", "_main.1.db2")):
    folder = tmp_path / "config" / "worlds_local" / name
    folder.mkdir(parents=True)
    for f in files:
        (folder / f).write_bytes(b"x" * 10)


def test_a_directory_world_is_listed_as_one_unit(tmp_path):
    server = _vh(tmp_path)
    _dir_world(tmp_path, "Dedicated")
    [entry] = ValheimDriver().world_saves(server)
    assert entry["name"] == "Dedicated" and entry["active"] is True
    assert entry["files"] == ("Dedicated",)
    assert entry["size"] == 20


def test_a_brand_new_world_with_only_its_metadata_is_the_running_world(tmp_path):
    """Before the first save the directory holds only `_main.0.fwl2` (measured on 1.4.0)."""
    server = _vh(tmp_path)
    _dir_world(tmp_path, "Dedicated", files=("_main.0.fwl2",))
    assert [e["name"] for e in ValheimDriver().world_saves(server)] == ["Dedicated"]


def test_a_directory_without_world_metadata_is_no_world(tmp_path):
    server = _vh(tmp_path)
    _dir_world(tmp_path, "Dedicated")
    _dir_world(tmp_path, "notes", files=("readme.txt",))
    _dir_world(tmp_path, "Dedicated_backup_auto-20261012120000")
    assert [e["name"] for e in ValheimDriver().world_saves(server)] == ["Dedicated"]


def test_both_formats_side_by_side_and_one_name_in_both_is_one_entry(tmp_path):
    server = _vh(tmp_path)
    folder = tmp_path / "config" / "worlds_local"
    _dir_world(tmp_path, "Dedicated")
    for f in ("Dedicated.db", "Dedicated.fwl", "Old.db", "Old.fwl"):
        (folder / f).write_bytes(b"x")
    saves = {e["name"]: e for e in ValheimDriver().world_saves(server)}
    assert set(saves) == {"Dedicated", "Old"}
    assert set(saves["Dedicated"]["files"]) == {"Dedicated", "Dedicated.db", "Dedicated.fwl"}


def test_a_directory_world_can_be_selected_and_removed(tmp_path):
    server = _vh(tmp_path)
    _dir_world(tmp_path, "Dedicated")
    _dir_world(tmp_path, "Second")

    class _Drv(ValheimDriver):
        written: dict = {}

        def write_registry_env(self, srv, env):  # noqa: D102
            self.written = env

    d = _Drv()
    d.select_world(server, "Second")
    assert d.written["WORLD_NAME"] == "Second"

    result = d.remove_world(server, "Second")
    assert result["files"] == ["Second"]
    assert not (tmp_path / "config" / "worlds_local" / "Second").exists()
    [moved] = os.listdir(tmp_path / "config" / "worlds_local.removed")
    assert moved.endswith("-Second")
    assert sorted(os.listdir(tmp_path / "config" / "worlds_local.removed" / moved)) == [
        "_main.0.fwl2", "_main.1.db2"]


def test_the_configuration_help_names_the_directory():
    [field] = [f for f in ValheimDriver()._FIELDS if f[0] == "WORLD_NAME"]
    assert "<name>/" in field[4] and "older world" in field[4]


# ------------------------------------------------------------------ the late Steam query

def _query(monkeypatch, *, log: str, running=True):
    async def no_answer(host, port, timeout=5.0):
        raise A2sError(f"A2S: no response from {host}:{port} within {timeout:g} s")

    async def state(name):
        return {"running": running, "started_at": "2026-10-12T12:03:41.749766336Z"}

    seen = {}

    async def logs(name, tail=200, timestamps=False, since=None):
        seen["since"] = since
        return log

    monkeypatch.setattr("app.services.a2s.info", no_answer)
    monkeypatch.setattr("app.services.docker_client.docker.state", state)
    monkeypatch.setattr("app.services.docker_client.docker.logs", logs)
    return seen


def test_silence_before_the_query_opens_says_why(tmp_path, monkeypatch):
    seen = _query(monkeypatch, log="12:05:57: Game server connected\n")
    with pytest.raises(A2sError) as e:
        asyncio.run(ValheimDriver().players(_vh(tmp_path)))
    assert "has not opened its Steam query yet" in str(e.value)
    assert "Opened Steam server" in str(e.value)
    assert seen["since"] == 1791806621, "only this start's log counts, not an earlier run's"


def test_silence_after_the_query_opened_stays_a_plain_failure(tmp_path, monkeypatch):
    _query(monkeypatch, log="12:05:57: Game server connected\n12:07:13: Opened Steam server\n")
    with pytest.raises(A2sError) as e:
        asyncio.run(ValheimDriver().players(_vh(tmp_path)))
    assert "no response" in str(e.value)


def test_a_stopped_container_gets_no_boot_explanation(tmp_path, monkeypatch):
    _query(monkeypatch, log="", running=False)
    with pytest.raises(A2sError) as e:
        asyncio.run(ValheimDriver().players(_vh(tmp_path)))
    assert "no response" in str(e.value)


def test_the_query_gives_up_before_the_reachability_check_does(tmp_path, monkeypatch):
    """The inside check waits 5 s; the explanation only arrives if this call is quicker."""
    from app.services.reachability import inside

    seen = {}

    async def info(host, port, timeout=5.0):
        seen["timeout"] = timeout
        return {"players": 0}

    async def players(host, port, timeout=5.0):
        return []

    monkeypatch.setattr("app.services.a2s.info", info)
    monkeypatch.setattr("app.services.a2s.players", players)
    asyncio.run(ValheimDriver().players(_vh(tmp_path)))
    assert seen["timeout"] < inside.TIMEOUT_S
