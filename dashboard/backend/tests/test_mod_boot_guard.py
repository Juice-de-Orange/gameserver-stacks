"""The preflight cannot be bypassed — **for any game.**

It must not hang on "Apply" alone: a scheduled restart, the watchdog autoheal or a manual action
would otherwise boot the unchecked configuration anyway — the gate would be decoration.

`guard_boot` must cover every game that has a preflight (Avorion and all file-based games), not
just PZ — otherwise the mod traffic light shows the problem but the guard does not block. Factorio
is the most expensive case: an incomplete mod set is not a crash visible in the log but a door
that stays shut.
"""
import asyncio
import json
import os

from app.models.server import GameServer, GameType, ManagementMode
from app.services import restarts
from app.services.mods import state as mod_state
from app.services.mods.preflight import BASE_MAP


def _server(root, key="pz-1") -> GameServer:
    srv = GameServer(
        key=key, game_type=GameType.pz, management_mode=ManagementMode.dashboard,
        container_name="pz-1", data_dir=str(root), enabled=True,
        extra=json.dumps({"servername": "servertest"}),
    )
    srv.id = 1
    return srv


def _ini(root, maps):
    path = os.path.join(root, "data", "Server", "servertest.ini")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(f"Mods=\nWorkshopItems=\nMap={maps}\n")
    os.makedirs(os.path.join(root, "workshop-mods", "content", "108600"), exist_ok=True)


def test_guard_reports_blockers_of_the_running_configuration(tmp_path, monkeypatch, mem_engine):
    monkeypatch.setattr(mod_state, "engine", mem_engine)
    _ini(tmp_path, f"GhostMap;{BASE_MAP}")     # map without provider → join crash
    blockers = asyncio.run(mod_state.guard_boot(_server(tmp_path)))
    assert [b.code for b in blockers] == ["MAP_WITHOUT_PROVIDER"]
    assert "GhostMap" in mod_state.guard_message(blockers)


def test_guard_passes_a_healthy_configuration(tmp_path, monkeypatch, mem_engine):
    monkeypatch.setattr(mod_state, "engine", mem_engine)
    _ini(tmp_path, BASE_MAP)
    assert asyncio.run(mod_state.guard_boot(_server(tmp_path))) == []


def test_guard_also_applies_to_the_file_based_games(tmp_path, monkeypatch, mem_engine):
    """Minecraft, Vintage Story, Valheim and Factorio have a preflight and their mod traffic
    light evaluates it — so the guard before the start must ask for it too. A selected mod file
    that does not exist is a blocker for all four (`MODFILE_MISSING`).
    """
    monkeypatch.setattr(mod_state, "engine", mem_engine)
    srv = _server(tmp_path)
    srv.game_type = GameType.minecraft
    srv.server_type = "PAPER"

    from app.services.mods import foreign

    mods_dir = foreign.mods_dir(srv)
    os.makedirs(mods_dir, exist_ok=True)
    # A file that counts as active but does not exist cannot be seen by the scan — so it is
    # injected through the selection here, as it happens for real.
    monkeypatch.setattr(foreign, "scan", lambda s: [
        foreign.ForeignMod(filename="doesnotexist.jar", enabled=True, size=0, loader="paper",
                           mod_id="", version="", problems=("file missing",)),
    ])

    blockers = asyncio.run(mod_state.guard_boot(srv))
    assert blockers, "a broken mod state still gets through the gate on Minecraft"


def test_guard_stays_silent_for_a_game_without_preflight(tmp_path, monkeypatch, mem_engine):
    """Arma Reforger has no file-based mod folder — there is nothing to block there."""
    monkeypatch.setattr(mod_state, "engine", mem_engine)
    srv = _server(tmp_path)
    srv.game_type = GameType.arma_reforger
    assert asyncio.run(mod_state.guard_boot(srv)) == []


def test_guard_never_blocks_operations_through_its_own_failure(tmp_path, monkeypatch):
    """The guard must never become the outage itself — that would be worse than the problem."""
    def _boom(server):
        raise RuntimeError("broken")

    monkeypatch.setattr(mod_state, "live_plan", _boom)
    assert asyncio.run(mod_state.guard_boot(_server(tmp_path))) == []


def test_scheduled_restart_is_refused_while_the_config_is_broken(tmp_path, monkeypatch, mem_engine, wire_lifecycle):
    """A running server is better than one that dies when players join."""
    monkeypatch.setattr(mod_state, "engine", mem_engine)
    _ini(tmp_path, f"GhostMap;{BASE_MAP}")
    srv = _server(tmp_path)

    calls: list = []
    incidents: list = []

    class _Dk:
        async def action(self, name, act, **kw):
            calls.append(act)

        async def state(self, name):
            return {"running": True, "exists": True}

    monkeypatch.setattr(restarts, "engine", mem_engine)
    wire_lifecycle(_Dk())  # stop/restart goes through `lifecycle`
    monkeypatch.setattr(restarts.servers_registry, "resolve_server", lambda sid: srv)
    monkeypatch.setattr(restarts, "_incident", lambda s, kind, detail: incidents.append(kind))
    monkeypatch.setattr(restarts, "watchdog_suppress", lambda k, m: None)

    asyncio.run(restarts._do_restart(1))

    assert calls == []                        # no stop/restart
    assert incidents == ["restart_blocked"]   # but visibly recorded


def test_scheduled_restart_proceeds_with_a_healthy_config(tmp_path, monkeypatch, mem_engine, wire_lifecycle):
    monkeypatch.setattr(mod_state, "engine", mem_engine)
    _ini(tmp_path, BASE_MAP)
    srv = _server(tmp_path)
    calls: list = []

    class _Dk:
        async def action(self, name, act, **kw):
            calls.append(act)

        async def state(self, name):
            return {"running": True, "exists": True}

    monkeypatch.setattr(restarts, "engine", mem_engine)
    wire_lifecycle(_Dk())  # stop/restart goes through `lifecycle`
    monkeypatch.setattr(restarts.servers_registry, "resolve_server", lambda sid: srv)
    monkeypatch.setattr(restarts, "watchdog_suppress", lambda k, m: None)
    monkeypatch.setattr(restarts, "get_driver", lambda s: (_ for _ in ()).throw(NotImplementedError))

    asyncio.run(restarts._do_restart(1))
    assert calls == ["stop", "start"]  # restart = stop-then-start
