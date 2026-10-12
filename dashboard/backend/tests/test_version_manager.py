"""Version change: heuristics and guards (the full swap is not covered here)."""
import asyncio

import pytest
from fastapi import HTTPException

from app.models.server import GameServer, GameType, ManagementMode
from app.services import version_manager as vm


def _srv(**over):
    base = dict(
        key="s", game_type=GameType.pz, management_mode=ManagementMode.dashboard,
        image_tag="42.19.0-unstable", enabled=True,
    )
    base.update(over)
    s = GameServer(**base)
    s.id = 1
    return s


class _Driver:
    def backup_spec(self, server):
        raise AssertionError("the guard must trigger BEFORE the backup")


def test_parse_and_direction():
    assert vm.parse_ver("42.19.0-unstable") == (42, 19, 0)
    assert vm._direction("42.19.0", "42.20.0") == "up"
    assert vm._direction("42.19.0", "42.18.0") == "down"
    assert vm._direction("42.19.0", "42.19.0") == "same"
    assert vm._direction("stable", "latest") == "unknown"   # unparsable → not treated as a downgrade


def test_compose_guard(monkeypatch):
    monkeypatch.setattr(vm, "_load", lambda k: _srv(management_mode=ManagementMode.compose))
    with pytest.raises(HTTPException) as ei:
        asyncio.run(vm.change_version("s", image_tag="42.20.0-unstable"))
    assert ei.value.status_code == 409


def test_downgrade_requires_confirm(monkeypatch):
    monkeypatch.setattr(vm, "_load", lambda k: _srv(image_tag="42.19.0-unstable"))
    monkeypatch.setattr(vm, "get_driver", lambda s: _Driver())
    with pytest.raises(HTTPException) as ei:
        asyncio.run(vm.change_version("s", image_tag="42.18.0-unstable"))  # downgrade without confirm
    assert ei.value.status_code == 400
    assert "Downgrade" in str(ei.value.detail)


def test_no_change_rejected(monkeypatch):
    monkeypatch.setattr(vm, "_load", lambda k: _srv(image_tag="42.19.0-unstable"))
    monkeypatch.setattr(vm, "get_driver", lambda s: _Driver())
    with pytest.raises(HTTPException) as ei:
        asyncio.run(vm.change_version("s", image_tag="42.19.0-unstable"))  # identical
    assert ei.value.status_code == 400


class _ReachedBackup(Exception):
    """Sentinel: the flow got as far as the mandatory backup (= the image was present)."""


class _BackupDriver:
    """Sentinel: `backup_spec` marks "ran through to the mandatory backup".

    The mandatory backup sits AFTER the confirmed stop (before, the snapshot was a hot copy of
    the writing world). The sentinel therefore fires later — tests using it must wire up the
    stop path as well.
    """

    def backup_spec(self, server):
        raise _ReachedBackup

    async def graceful_shutdown(self, server):
        return True

    async def announce(self, server, text):
        return None


def test_failed_pull_aborts_before_anything_is_touched(monkeypatch):
    """For PZ the image tag IS the version. If the target image cannot be obtained, the change
    must abort BEFORE backup/container are touched — otherwise it would stop in the middle of the swap."""
    class _Dk:
        async def image_exists(self, image):
            return False

        async def pull(self, image, on_progress=None):
            raise RuntimeError("no network")

    monkeypatch.setattr(vm, "docker", _Dk())
    monkeypatch.setattr(vm, "_load", lambda k: _srv(image_repo="repo"))
    monkeypatch.setattr(vm, "get_driver", lambda s: _Driver())  # backup_spec would assert
    with pytest.raises(HTTPException) as ei:
        asyncio.run(vm.change_version("s", image_tag="42.20.0-release"))
    assert ei.value.status_code == 502
    assert "IMAGES" in str(ei.value.detail)   # note about the missing proxy permission


def test_present_image_is_not_pulled_again(monkeypatch):
    pulled: list = []

    class _Dk:
        def __init__(self):
            self.running = True

        async def image_exists(self, image):
            return True

        async def pull(self, image, on_progress=None):
            pulled.append(image)

        async def state(self, name):
            # A `{name}-prev` exists only if an earlier change left one behind.
            # A stub that ALWAYS reports it invents a state that does not exist here.
            return {"exists": not name.endswith("-prev"), "running": self.running}

        async def action(self, name, act, **kw):
            self.running = act != "stop"

        async def logs(self, name, **kw):
            return ""

    dk = _Dk()
    monkeypatch.setattr(vm, "docker", dk)
    monkeypatch.setattr(vm, "_load", lambda k: _srv(image_repo="repo"))
    monkeypatch.setattr(vm, "get_driver", lambda s: _BackupDriver())
    # The stop sits BEFORE the mandatory backup, so it must be able to run through here.
    monkeypatch.setattr(vm.lifecycle, "docker", dk)
    monkeypatch.setattr(vm.lifecycle, "_STOP_MARK_DELAY", 0)
    monkeypatch.setattr(vm.lifecycle, "_suppress", lambda *a, **k: None)
    monkeypatch.setattr(vm.lifecycle, "_driver", lambda s: _BackupDriver())
    monkeypatch.setattr(vm.lifecycle, "stop_grace", lambda s: 120)
    monkeypatch.setattr(vm.lifecycle, "_locks", {})
    with pytest.raises(_ReachedBackup):       # ran through to the mandatory backup
        asyncio.run(vm.change_version("s", image_tag="42.20.0-release"))
    assert pulled == []


def test_pull_progress_aggregates_layers():
    state = {"status": "", "current": 0, "total": 0, "percent": None}
    sink = vm._progress_sink(state)
    sink({"id": "a", "status": "Downloading", "progressDetail": {"current": 50, "total": 100}})
    sink({"id": "b", "status": "Downloading", "progressDetail": {"current": 25, "total": 100}})
    sink({"id": "a", "status": "Downloading", "progressDetail": {"current": 100, "total": 100}})
    assert (state["current"], state["total"]) == (125, 200)   # per layer the LAST state
    assert state["percent"] == 62.5



def _wire_lifecycle_for_rollback(monkeypatch, dk):
    """`_rollback_swap` stops via `lifecycle.stop_and_confirm`, so the stop runs through
    `lifecycle`, not `vm.docker` — tests must wire it there, otherwise they reach the real
    Docker client."""
    monkeypatch.setattr(vm.lifecycle, "docker", dk)
    monkeypatch.setattr(vm.lifecycle, "_STOP_MARK_DELAY", 0)
    monkeypatch.setattr(vm.lifecycle, "_suppress", lambda *a, **k: None)
    monkeypatch.setattr(vm.lifecycle, "_driver", lambda s: None)
    monkeypatch.setattr(vm.lifecycle, "stop_grace", lambda s: 120)
    monkeypatch.setattr(vm.lifecycle, "_locks", {})


def test_rollback_force_removes_and_restores(monkeypatch):
    """Force-remove the failed (possibly running) new container, bring back the old one, restore
    the world only through a stopped container, return True."""
    from types import SimpleNamespace

    calls: list = []

    class _Dk:
        async def remove(self, name, force=False):
            calls.append(("remove", name, force))

        async def rename(self, a, b):
            calls.append(("rename", a, b))

        async def action(self, name, act, **kw):
            calls.append(("action", name, act))

        async def state(self, name):
            return {"running": False, "exists": True}  # after stop → stopped

        async def create(self, name, payload):
            calls.append(("create", name))

    extracts: list = []
    dk = _Dk()
    monkeypatch.setattr(vm, "docker", dk)
    _wire_lifecycle_for_rollback(monkeypatch, dk)
    monkeypatch.setattr(vm, "_restore_version_fields", lambda k, snap: None)
    monkeypatch.setattr(vm, "_load", lambda k: _srv())
    monkeypatch.setattr(vm, "build_create_payload", lambda s, sec: {})
    monkeypatch.setattr(vm.servers_registry, "resolve_secret", lambda s: "")
    monkeypatch.setattr(vm, "extract_backup", lambda name, data_dir=None: extracts.append(name))
    # The archive is a name here, not a file — the authenticity check (`check_restorable`,
    # in front of BOTH restore paths) would otherwise fail on the missing file before the
    # actual case is reached.
    monkeypatch.setattr(vm, "check_restorable", lambda *a, **k: None)
    monkeypatch.setattr(vm.watchdog, "suppress", lambda k, m: None)

    spec = SimpleNamespace(data_dir="/x/data", world_subdirs=("Saves", "Server"))
    container_restored, world_restored = asyncio.run(
        vm._rollback_swap("s", "c", "c-prev", spec, "bk.tar.gz", True, None, old_existed=True, renamed=True)
    )
    assert (container_restored, world_restored) == (True, True)
    assert ("remove", "c", True) in calls        # new container FORCE-removed (force, possibly running)
    assert ("rename", "c-prev", "c") in calls     # old container brought back
    assert extracts == ["bk.tar.gz"]              # world restored (container confirmed stopped)
    assert ("action", "c", "start") in calls      # was_running → started again


def _rollback_setup(monkeypatch, *, still_running=False, extract_raises=False):
    """Shared setup for the partial-rollback cases."""
    from types import SimpleNamespace

    class _Dk:
        async def remove(self, name, force=False):
            pass

        async def rename(self, a, b):
            pass

        async def action(self, name, act, **kw):
            pass

        async def state(self, name):
            return {"running": still_running, "exists": True}

        async def create(self, name, payload):
            pass

    def _extract(name, data_dir=None):
        if extract_raises:
            raise OSError("archive corrupt")

    monkeypatch.setattr(vm, "docker", _Dk())
    monkeypatch.setattr(vm, "_restore_version_fields", lambda k, snap: None)
    monkeypatch.setattr(vm, "_load", lambda k: _srv())
    monkeypatch.setattr(vm, "build_create_payload", lambda s, sec: {})
    monkeypatch.setattr(vm.servers_registry, "resolve_secret", lambda s: "")
    monkeypatch.setattr(vm, "extract_backup", _extract)
    # The archive is a name here, not a file — the authenticity check (`check_restorable`,
    # in front of BOTH restore paths) would otherwise fail on the missing file before the
    # actual case is reached.
    monkeypatch.setattr(vm, "check_restorable", lambda *a, **k: None)
    monkeypatch.setattr(vm.watchdog, "suppress", lambda k, m: None)
    return SimpleNamespace(data_dir="/x/data", world_subdirs=("Saves", "Server"))


def test_rollback_reports_no_success_when_the_world_did_not_come_back(monkeypatch):
    """The container is back, the WORLD is not — that is NOT "rolled back".

    An operator who reads "rolled back" stops looking while the world is still in the state the
    new version migrated it to. That is exactly what the mandatory backup is for.
    """
    spec = _rollback_setup(monkeypatch, extract_raises=True)
    assert asyncio.run(
        vm._rollback_swap("s", "c", "c-prev", spec, "bk.tar.gz", True, None, old_existed=True, renamed=True)
    ) == (True, False)


def test_rollback_reports_no_success_when_the_container_is_still_running(monkeypatch):
    """Second path to the same state: the restore is deliberately skipped (no writing over a
    running world) — that must not count as "rolled back" either."""
    spec = _rollback_setup(monkeypatch, still_running=True)
    assert asyncio.run(
        vm._rollback_swap("s", "c", "c-prev", spec, "bk.tar.gz", True, None, old_existed=True, renamed=True)
    ) == (True, False)


# --- Old images and disk space --------------------------------------------------------------

def test_cleanup_spares_the_rollback_image(monkeypatch):
    """The most important part of the cleanup is what it does NOT delete.

    `rollback()` switches to `previous_image_tag`. Without that image the only rollback would be
    another 10 GB pull — exactly when something is broken.
    """
    removed: list[str] = []

    class _Dk:
        async def image_sizes(self, repo):
            return {"42.20.0-release": 10_400_000_000,
                    "42.19.0-unstable": 10_200_000_000,
                    "42.18.0-unstable": 9_900_000_000}

        async def remove_image(self, image, force=False):
            removed.append(image)
            return True

    monkeypatch.setattr(vm, "docker", _Dk())
    srv = _srv()
    srv.image_repo = "danixu86/project-zomboid-dedicated-server"
    srv.image_tag = "42.20.0-release"
    srv.previous_image_tag = "42.19.0-unstable"

    out = asyncio.run(vm._cleanup_old_images(srv, {srv.image_tag, srv.previous_image_tag}))

    assert out == ["danixu86/project-zomboid-dedicated-server:42.18.0-unstable"]
    assert removed == out
    assert not any("42.20.0" in r or "42.19.0" in r for r in removed)


def test_a_still_referenced_image_is_not_counted_as_removed(monkeypatch):
    """Docker answers 409 when a container still uses the image. That is not an error (forcing
    would be exactly wrong), but it is NOT a removal either — logging it as removed lies in the
    success message."""
    class _Dk:
        async def image_sizes(self, repo):
            return {"old": 1}

        async def remove_image(self, image, force=False):
            return False          # 409: still referenced

    monkeypatch.setattr(vm, "docker", _Dk())
    srv = _srv()
    srv.image_repo = "repo"
    assert asyncio.run(vm._cleanup_old_images(srv, {"new"})) == []


def test_cleanup_never_fails_a_successful_change(monkeypatch):
    """An error during cleanup has no consequence — the server already runs the new version."""
    class _Dk:
        async def image_sizes(self, repo):
            return {"old": 1}

        async def remove_image(self, image, force=False):
            raise RuntimeError("image still referenced")

    monkeypatch.setattr(vm, "docker", _Dk())
    srv = _srv()
    srv.image_repo = "repo"
    assert asyncio.run(vm._cleanup_old_images(srv, {"new"})) == []


def test_the_pull_aborts_when_space_is_insufficient(monkeypatch):
    """Two PZ images are about 10 GB each. Without this check a change fails at a full disk in
    the middle of the pull — AFTER the mandatory backup."""
    class _Dk:
        async def image_sizes(self, repo):
            return {"42.20.0-release": 10_400_000_000}

    monkeypatch.setattr(vm, "docker", _Dk())
    monkeypatch.setattr(vm, "_free_space_bytes", lambda: 3_000_000_000)

    with pytest.raises(HTTPException) as ei:
        asyncio.run(vm._check_space("repo", "42.21.0-release"))
    assert ei.value.status_code == 507
    assert ei.value.detail["code"] == "version.disk_space_low"
    assert "3.0 GB" in ei.value.detail["message"] and "12" in ei.value.detail["message"]


def test_an_unclear_measurement_blocks_no_change(monkeypatch):
    """An unanswered question must not prevent a legitimate version change."""
    class _Dk:
        async def image_sizes(self, repo):
            return {}

    monkeypatch.setattr(vm, "docker", _Dk())
    monkeypatch.setattr(vm, "_free_space_bytes", lambda: None)
    asyncio.run(vm._check_space("repo", "x"))          # space not measurable → let through

    monkeypatch.setattr(vm, "_free_space_bytes", lambda: 1)
    asyncio.run(vm._check_space("repo", "x"))          # no size known → do not guess


def _wire_swap(monkeypatch) -> list[str]:
    """Fakes for a full, successful swap (`change_version`); returns the trace of Docker calls."""
    trace: list[str] = []

    class _Dk:
        async def image_exists(self, image):
            return True

        async def pull(self, image, on_progress=None):
            return None

        async def state(self, name):
            return {"exists": not name.endswith("-prev"), "running": "stop" not in trace}

        async def action(self, name, act, **kw):
            trace.append(act)

        async def remove(self, name, force=False):
            trace.append("remove")

        async def rename(self, a, b):
            trace.append("rename")

        async def create(self, name, payload):
            trace.append("create")

        async def logs(self, name, **kw):
            return ""

    class _Drv:
        def backup_spec(self, server):
            from app.services.drivers.base import BackupSpec
            return BackupSpec(data_dir="/tmp", world_subdirs=("Saves",))

        async def graceful_shutdown(self, server):
            return True

        async def announce(self, server, text):
            return None

    dk = _Dk()
    monkeypatch.setattr(vm, "docker", dk)
    monkeypatch.setattr(vm, "_load", lambda k: _srv(image_repo="repo"))
    monkeypatch.setattr(vm, "get_driver", lambda s: _Drv())
    monkeypatch.setattr(vm, "_record_pending", lambda *a, **kw: 1)
    monkeypatch.setattr(vm, "_finish", lambda *a, **kw: None)
    monkeypatch.setattr(vm, "_persist_version", lambda *a, **kw: None)
    monkeypatch.setattr(vm, "build_create_payload", lambda *a, **kw: {})
    monkeypatch.setattr(vm.servers_registry, "resolve_secret", lambda s: "x")
    monkeypatch.setattr(vm.watchdog, "suppress", lambda *a: None)
    monkeypatch.setattr(vm.lifecycle, "docker", dk)
    monkeypatch.setattr(vm.lifecycle, "_STOP_MARK_DELAY", 0)
    monkeypatch.setattr(vm.lifecycle, "_suppress", lambda *a, **k: None)
    monkeypatch.setattr(vm.lifecycle, "_driver", lambda s: _Drv())
    monkeypatch.setattr(vm.lifecycle, "stop_grace", lambda s: 120)
    monkeypatch.setattr(vm.lifecycle, "_locks", {})
    # `lifecycle.create_backup`: version_manager backs up through the helper and holds no
    # reference of its own.
    monkeypatch.setattr(vm.lifecycle, "create_backup",
                        lambda label, **kw: trace.append("backup") or {"name": f"{label}.tar.gz"})

    async def _boot(*a, **kw):
        return True

    monkeypatch.setattr(vm, "verify_boot", _boot)
    return trace


def test_stop_happens_before_the_mandatory_backup_and_is_confirmed(monkeypatch):
    """Three traps, all on the only rollback path of the swap.

    1. The mandatory backup must be taken AFTER the stop. PZ has no quiesce hooks, so an archive
       taken before is a hot copy of the writing world. If the swap fails, `_rollback_swap`
       restores exactly this archive and reports `world_restored=True` — "rolled back" to a torn
       snapshot.
    2. `try: was_running = ... except: was_running = False` — "when in doubt it is not running".
       With the proxy gone the server would stay down after a rollback.
    3. `lifecycle.stop_server(...)` in `try/except: pass` right before `docker.rename` and
       `docker.create` with THE SAME binds. If the `except` swallows an error, two game
       processes run on the same world directory — the data-loss path that `stop_and_confirm`
       exists to prevent.
    """
    trace = _wire_swap(monkeypatch)

    asyncio.run(vm.change_version("s", image_tag="42.20.0-release"))

    assert "stop" in trace and "backup" in trace, trace
    assert trace.index("stop") < trace.index("backup"), (
        f"The mandatory backup was taken BEFORE the stop — a hot copy of the writing world, and it "
        f"is the only rollback of the swap. Order: {trace}"
    )
    assert trace.index("backup") < trace.index("rename"), (
        f"Nothing may sit between backup and rename. Order: {trace}"
    )


def test_the_downgrade_gate_reaches_the_browser_synchronously(monkeypatch):
    """The gate must be checked before the background operation starts, not inside it.

    Otherwise the endpoint answers 200 and the UI never sees the 400 it needs to ask for
    `confirm_downgrade` (`Version.tsx` sends `change(true)` only from the `r.status === 400`
    branch) — a deliberate downgrade would be possible only via curl. The same applies to "no
    version change given", "compose-managed" and "game type not supported".
    """
    from app.models.server import ManagementMode

    srv = _srv(image_repo="repo")
    srv.image_tag = "42.20.0-release"
    srv.game_version = "42.20.0-release"
    monkeypatch.setattr(vm, "_load", lambda k: srv)

    # Downgrade without confirmation -> 400 WITH the detail object the confirmation prompt needs.
    with pytest.raises(HTTPException) as ei:
        vm.check_change_preconditions("s", image_tag="42.19.0-release")
    assert ei.value.status_code == 400
    assert isinstance(ei.value.detail, dict), "the UI needs old/new for the confirmation prompt"
    assert "Downgrade" in ei.value.detail["error"]
    assert ei.value.detail["old"] == "42.20.0-release"
    assert ei.value.detail["new"] == "42.19.0-release"

    # WITH confirmation it passes.
    vm.check_change_preconditions("s", image_tag="42.19.0-release", confirm_downgrade=True)

    # An upgrade needs no confirmation.
    vm.check_change_preconditions("s", image_tag="42.21.0-release")

    # The same version -> 400, not "operation started".
    with pytest.raises(HTTPException) as ei:
        vm.check_change_preconditions("s", image_tag="42.20.0-release")
    assert ei.value.status_code == 400 and ei.value.detail["code"] == "version.no_change"

    # compose-managed -> 409.
    srv.management_mode = ManagementMode.compose
    with pytest.raises(HTTPException) as ei:
        vm.check_change_preconditions("s", image_tag="42.21.0-release")
    assert ei.value.status_code == 409


def test_the_endpoint_checks_before_the_operation_starts():
    """Structural: the check must come BEFORE `operations.start`, not after it."""
    import inspect

    from app.routers import versions

    q = inspect.getsource(versions.change_version)
    assert "check_change_preconditions" in q, "the endpoint does not check the preconditions at all"
    assert q.index("check_change_preconditions") < q.index("operations.start"), (
        "The check comes AFTER the operation start — by then the endpoint has already answered 200."
    )


def test_a_recreate_reports_a_configuration_change_not_a_version_change(monkeypatch):
    """The edit page recreates the container through the version path; its final line must fit."""
    from app.services import operations

    _wire_swap(monkeypatch)
    recreated = asyncio.run(vm.change_version("s", rebuild=True))
    assert recreated["direction"] == "recreate"
    assert operations._final_text(recreated, "version.change") == (
        "The container was recreated with the saved settings."
    )
    changed = asyncio.run(vm.change_version("s", image_tag="42.20.0-release"))
    assert "summary" not in changed
    assert operations._final_text(changed, "version.change") == "The version change is complete."


def test_a_failed_completion_is_a_coded_note_and_no_failure(monkeypatch):
    """The new version runs; that the history could not be written is a note with the reason."""
    _wire_swap(monkeypatch)

    def _db_gone(*a, **kw):
        raise RuntimeError("db gone")

    monkeypatch.setattr(vm, "_finish", _db_gone)
    result = asyncio.run(vm.change_version("s", image_tag="42.20.0-release"))
    assert result["status"] == "ok"
    assert result["note"]["code"] == "version.completion_failed"
    assert result["note"]["params"] == {"error": "db gone"}
