"""Whoever silences the watchdog before the stop is certain must also take the silence back.

Silencing the watchdog BEFORE it is certain that the operation happens at all is a trap: if the
stop cannot be confirmed, the request ends with 409 "nothing was changed" — and the silence would
stay for 15-20 minutes. `watchdog.suppress` extends via `max()` and never shortens, so there is no
inverse operation. In that window `_check_server` returns at once: no autoheal, no `rcon_hang`
report, no `boot_crash_loop` detection — exactly when the stop demonstrably did not work and the
server may be hanging.

**Why this is a file of its own and not one case in `test_the_lock_does_not_lie.py`:** the rule
existed in `lifecycle.py` while a hand-made copy in `mods/apply.py` went around it — the dominant
bug class of this project. It is closed structurally here instead of place by place: silencing
belongs to the choke point `lifecycle.stop_and_confirm`, which every confirmed stop goes through
anyway.

The guard's exemption list is exactly one file long (`services/lifecycle.py` — the choke point
itself).
"""
import ast
import asyncio
import pathlib

import pytest
from fastapi import HTTPException

from app.models.server import GameServer, GameType

APP = pathlib.Path(__file__).resolve().parent.parent / "app"

# Only the choke point itself may silence before the stop is confirmed — it takes it back.
ALLOWED = {"services/lifecycle.py"}

_SILENCE = {"suppress", "watchdog_suppress", "_suppress"}
_STOP = {"stop_and_confirm", "stop_and_snapshot"}


def _names(tree: ast.AST, wanted: set[str], module_suffix: str) -> set[str]:
    """Also count names imported under an alias — `from ..watchdog import suppress as quiet`.

    Without this the guard could be bypassed with one line, and a guard that can be bypassed is none.
    """
    names = set(wanted)
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and (node.module or "").endswith(module_suffix):
            for alias in node.names:
                if alias.name in wanted:
                    names.add(alias.asname or alias.name)
    return names


def _call_name(node: ast.Call) -> str:
    func = node.func
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return ""


def silence_before_stop(source: str, filename: str = "<test>") -> list[tuple[str, int]]:
    """(function name, line) for every silencing BEFORE a stop in the same function."""
    tree = ast.parse(source, filename=filename)
    silence = _names(tree, _SILENCE, "watchdog")
    stop = _names(tree, _STOP, "lifecycle")
    hits: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        silence_lines, stop_lines = [], []
        for n in ast.walk(node):
            if isinstance(n, ast.Call):
                name = _call_name(n)
                if name in silence:
                    silence_lines.append(n.lineno)
                elif name in stop:
                    stop_lines.append(n.lineno)
        for line in silence_lines:
            if any(line < s for s in stop_lines):
                hits.append((node.name, line))
    return hits


def test_nobody_but_the_choke_point_silences_before_the_stop():
    offenders: list[str] = []
    for path in APP.rglob("*.py"):
        # `.as_posix()`: on Windows the paths would otherwise be compared with backslashes and
        # the exemption list would have no effect.
        rel = path.relative_to(APP).as_posix()
        if rel in ALLOWED:
            continue
        for function, line in silence_before_stop(path.read_text(encoding="utf-8"), str(path)):
            offenders.append(f"{rel}:{line} in {function}()")
    assert not offenders, (
        "These places silence the watchdog before the stop is confirmed, without taking the "
        "silence back on the abort path. Instead of exempting them here, use "
        "`lifecycle.stop_and_confirm(..., suppress_minutes=…)`.\n  "
        + "\n  ".join(offenders)
    )


def test_the_guard_would_find_the_old_hand_made_copy():
    """Cross-check — without it there is no evidence that the test checks anything at all.

    The form the hand-made copy in `mods/apply.py` used to have.
    """
    old = (
        "async def _apply_locked(server_key):\n"
        "    watchdog.suppress(server_key, SUPPRESS_MINUTES)\n"
        "    was_running, stopped = await lifecycle.stop_and_confirm(server)\n"
        "    if not stopped:\n"
        "        raise HTTPException(409, detail='nothing was changed')\n"
    )
    assert silence_before_stop(old) == [("_apply_locked", 2)]


def test_the_guard_is_not_fooled_by_an_alias():
    old = (
        "from ..services.watchdog import suppress as quiet\n"
        "from ..services.lifecycle import stop_and_confirm as halt\n"
        "async def f(server):\n"
        "    quiet(server.key, 20)\n"
        "    return await halt(server)\n"
    )
    assert silence_before_stop(old) == [("f", 4)]


def test_the_guard_does_not_report_the_right_order():
    """Silencing after the stop is the normal case (boot window) — not a violation."""
    good = (
        "async def _rollback(server):\n"
        "    _, stopped = await lifecycle.stop_and_confirm(server)\n"
        "    await docker.action(server.container_name, 'start')\n"
        "    watchdog.suppress(server.key, 20)\n"
    )
    assert silence_before_stop(good) == []


# --------------------------------------------------------------------- the effect itself

def _srv(key="pz-1", sid=1, container="cont-pz") -> GameServer:
    s = GameServer(key=key, game_type=GameType.pz, container_name=container, enabled=True)
    s.id = sid
    return s


class _DockerKeepsRunning:
    """The stop is sent, but the container is still up afterwards."""

    async def state(self, name):
        return {"exists": True, "running": True, "restarts": 0}

    async def action(self, name, act, **kw):
        pass


def _real_silence(monkeypatch, fake_driver_cls, docker=None):
    """`wire_lifecycle` stubs out `_suppress` — this is exactly what is measured here."""
    from app.services import lifecycle, watchdog

    monkeypatch.setattr(lifecycle, "docker", docker or _DockerKeepsRunning())
    monkeypatch.setattr(lifecycle, "_STOP_MARK_DELAY", 0)
    monkeypatch.setattr(lifecycle, "_driver", lambda s: fake_driver_cls())
    monkeypatch.setattr(lifecycle, "stop_grace", lambda s: 1)
    monkeypatch.setattr(lifecycle, "_locks", {})
    watchdog._states.pop("pz-1", None)
    return lifecycle, watchdog


def test_an_unconfirmed_stop_leaves_no_silence(monkeypatch, fake_driver_cls):
    lifecycle, watchdog = _real_silence(monkeypatch, fake_driver_cls)
    before = watchdog._state("pz-1").suppress_until

    was_running, stopped = asyncio.run(
        lifecycle.stop_and_confirm(_srv(), suppress_minutes=20)
    )

    assert not stopped
    assert watchdog._state("pz-1").suppress_until == before, \
        "the silence stayed although the stop could not be confirmed"


def test_a_confirmed_stop_stays_silent(monkeypatch, fake_driver_cls, fake_docker_cls):
    """Cross-check: the revert must not hit the normal case — otherwise the boot looks like a hang."""
    lifecycle, watchdog = _real_silence(monkeypatch, fake_driver_cls, fake_docker_cls())

    asyncio.run(lifecycle.stop_and_confirm(_srv(), suppress_minutes=20))

    assert watchdog._state("pz-1").suppress_until > 0, \
        "the watchdog ran into the in-game shutdown"


def test_someone_elses_silence_is_not_reverted_too(monkeypatch, fake_driver_cls):
    """If someone else silenced for longer, the silence is theirs — not reset to zero."""
    lifecycle, watchdog = _real_silence(monkeypatch, fake_driver_cls)
    watchdog.suppress("pz-1", 30)          # a scheduled restart, a second operation …
    other = watchdog._state("pz-1").suppress_until

    asyncio.run(lifecycle.stop_and_confirm(_srv(), suppress_minutes=5))

    assert watchdog._state("pz-1").suppress_until == other


def test_mod_apply_does_not_leave_the_watchdog_silent(monkeypatch, mem_engine,
                                                       fake_driver_cls, tmp_path):
    """The same situation through `mods.apply`.

    Deliberately lean setup — it only goes as far as the unconfirmed stop, then it ends.
    """
    import json
    import os

    from app.models.server import ManagementMode
    from app.services import lifecycle, watchdog
    from app.services.mods import apply as mod_apply
    from app.services.mods import state as mod_state
    from app.services.mods.preflight import BASE_MAP, ModPlan

    root = str(tmp_path)
    srv = GameServer(key="pz-1", game_type=GameType.pz, management_mode=ManagementMode.dashboard,
                     container_name="cont-pz", data_dir=root, enabled=True,
                     extra=json.dumps({"servername": "servertest"}))
    srv.id = 1
    ini = os.path.join(root, "data", "Server", "servertest.ini")
    os.makedirs(os.path.dirname(ini), exist_ok=True)
    with open(ini, "w", encoding="utf-8") as f:
        f.write(f"Mods=CityMod\nWorkshopItems=111\nMap={BASE_MAP}\nPVP=true\n")

    class _Drv:
        async def announce(self, server, text):
            pass

        async def save(self, server):
            pass

        def backup_spec(self, server):
            from app.services.drivers.base import BackupSpec
            return BackupSpec(data_dir=os.path.join(root, "data"), world_subdirs=("Saves",))

    monkeypatch.setattr(mod_state, "engine", mem_engine)
    monkeypatch.setattr(mod_apply, "engine", mem_engine)
    monkeypatch.setattr("app.services.servers.get_by_key", lambda k: srv)
    monkeypatch.setattr(mod_apply, "get_driver", lambda s: _Drv())
    docker = _DockerKeepsRunning()
    monkeypatch.setattr(mod_apply, "docker", docker)
    monkeypatch.setattr(lifecycle, "docker", docker)
    monkeypatch.setattr(lifecycle, "_STOP_MARK_DELAY", 0)
    monkeypatch.setattr(lifecycle, "_driver", lambda s: _Drv())
    monkeypatch.setattr(lifecycle, "stop_grace", lambda s: 1)
    monkeypatch.setattr(lifecycle, "_locks", {})
    mod_state.invalidate(srv.key)
    mod_state.set_draft(srv, ModPlan((), (), (BASE_MAP,)))
    watchdog._states.pop("pz-1", None)
    before = watchdog._state("pz-1").suppress_until

    with pytest.raises(HTTPException) as ei:
        asyncio.run(mod_apply.apply_draft("pz-1", confirm_world_reset=True))

    assert ei.value.status_code == 409
    assert watchdog._state("pz-1").suppress_until == before, \
        '20 minutes without watchdog although the response said "nothing was changed"'


def test_a_longer_silence_set_during_the_stop_survives_the_revert(monkeypatch, fake_driver_cls):
    """Someone else silences for longer WHILE our stop runs — the revert must leave that alone.

    Our silence (20 min) is set first, a scheduled restart extends it to 30 min in between, the
    stop is not confirmed. Reverting with `min()` would reset the value to our "before" (0.0) and
    take the other caller's silence with it.
    """
    class _DockerOtherSilencesMeanwhile(_DockerKeepsRunning):
        async def state(self, name):
            from app.services import watchdog

            watchdog.suppress("pz-1", 30)
            return await super().state(name)

    lifecycle, watchdog = _real_silence(monkeypatch, fake_driver_cls,
                                        _DockerOtherSilencesMeanwhile())

    asyncio.run(lifecycle.stop_and_confirm(_srv(), suppress_minutes=20))

    assert watchdog._state("pz-1").suppress_until > 0, \
        "the other caller's silence was reverted together with ours"
