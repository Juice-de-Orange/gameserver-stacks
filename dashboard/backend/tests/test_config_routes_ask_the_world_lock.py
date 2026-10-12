"""Guard: no writing route without the world-operation latch.

**The error it stands against.** On rollback `mods/apply._restore_config` puts back the COMPLETE
`.ini` from before the operation; applying mods takes up to 20 minutes (boot proof). A save
through the dashboard in that window would be lost without a trace — with an audit entry about
something that no longer exists. The world lock prevents that, and a guard instead of per-route
fixes catches the next caller that walks past the rule.

**The rule.** Every `@router.put`/`post`/`delete`/`patch` function in `app/routers/` that writes a
config file (`ini.write_raw`, `ini.set_ini_keys_at`, `sb.write_values`, or a driver write path)
attaches the latch as a dependency — **in the signature OR on the decorator**
(`dependencies=[Depends(_no_world_operation)]`). Both forms count; see `_has_latch` and
`test_the_latch_on_the_decorator_counts_too`.

**Exception list: empty.**
"""
import ast
import pathlib

ROUTERS = pathlib.Path(__file__).resolve().parent.parent / "app" / "routers"

# Covers the CLASS, not just the forms in `routers/config.py`: `routers/server_ops.py` has write
# routes that write one level deeper (`driver.set_config` is the same `.ini` for PZ;
# `driver.set_mods`/`set_scenario`/`patch_config` hit Reforger's `config.json` during a mode
# change that holds the world lock for up to 300 s). `set_ini_keys` and `set_mods` are offered by
# `ini_editor` as well.
WRITES_CONFIG = {
    # direct write paths
    "write_raw", "set_ini_keys_at", "set_ini_keys", "write_values",
    # through the driver — one level deeper, but the same damage
    "set_config", "set_mods", "set_scenario", "patch_config",
}
LATCH = "_no_world_operation"
WRITING_METHODS = {"put", "post", "delete", "patch"}


def _call_name(call: ast.Call) -> str:
    f = call.func
    if isinstance(f, ast.Attribute):
        return f.attr
    if isinstance(f, ast.Name):
        return f.id
    return ""


def _is_writing_route(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    for deco in fn.decorator_list:
        if isinstance(deco, ast.Call) and isinstance(deco.func, ast.Attribute):
            if deco.func.attr in WRITING_METHODS:
                return True
    return False


def _has_latch(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """Is the latch attached to this route — in the signature OR on the decorator?

    Both forms count. `dependencies=[Depends(_no_world_operation)]` on `@router.put(...)` is the
    second common form; checking only the signature would report a correctly guarded route as a
    violation.
    """
    for default in list(fn.args.defaults) + list(fn.args.kw_defaults):
        if default is None:
            continue
        for k in ast.walk(default):
            if isinstance(k, ast.Name) and k.id == LATCH:
                return True
            if isinstance(k, ast.Attribute) and k.attr == LATCH:
                return True
    for deco in fn.decorator_list:
        for k in ast.walk(deco):
            if isinstance(k, ast.Name) and k.id == LATCH:
                return True
            if isinstance(k, ast.Attribute) and k.attr == LATCH:
                return True
    return False


def _unguarded_write_routes(path: pathlib.Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    open_: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        if not _is_writing_route(node):
            continue
        writes = any(
            isinstance(k, ast.Call) and _call_name(k) in WRITES_CONFIG for k in ast.walk(node)
        )
        if writes and not _has_latch(node):
            open_.append(f"{node.name} (line {node.lineno})")
    return open_


def test_every_writing_config_route_has_the_latch():
    violations: list[str] = []
    for path in ROUTERS.rglob("*.py"):
        for fn in _unguarded_write_routes(path):
            violations.append(f"{path.relative_to(ROUTERS).as_posix()}:{fn}")
    assert not violations, (
        "These routes write a config file without asking the world lock. A mod rollback "
        "overwrites it during its window of up to 20 minutes:\n  "
        + "\n  ".join(violations)
    )


def test_the_guard_would_find_the_old_error(tmp_path):
    """Cross-check — `put_raw` as it looked before the latch."""
    bad = tmp_path / "bad.py"
    bad.write_text(
        '@router.put("/{key}/config/raw")\n'
        "async def put_raw(body: RawUpdate, server: GameServer = Depends(_pz_only)):\n"
        "    ini.write_raw(ini.ini_path(server), content)\n",
        encoding="utf-8",
    )
    assert _unguarded_write_routes(bad) == ["put_raw (line 2)"]


def test_with_the_latch_the_route_is_clean(tmp_path):
    """The target state."""
    ok = tmp_path / "good.py"
    ok.write_text(
        '@router.put("/{key}/config/raw")\n'
        "async def put_raw(body: RawUpdate, server: GameServer = Depends(_no_world_operation)):\n"
        "    ini.write_raw(ini.ini_path(server), content)\n",
        encoding="utf-8",
    )
    assert _unguarded_write_routes(ok) == []


def test_reading_routes_stay_free(tmp_path):
    """GET does no harm — and the UI must be able to display during an operation."""
    ok = tmp_path / "read.py"
    ok.write_text(
        '@router.get("/{key}/config/raw")\n'
        "async def get_raw(server: GameServer = Depends(_pz_only)):\n"
        "    return ini.read_raw(ini.ini_path(server))\n",
        encoding="utf-8",
    )
    assert _unguarded_write_routes(ok) == []


def test_a_driver_write_path_is_caught_too(tmp_path):
    """Cross-check of the driver paths: the route writes one level deeper and must still be caught.

    `routers/server_ops.py:put_config` calls `driver.set_config`, and for PZ that lands in the
    same `.ini` that the mod rollback overwrites.
    """
    bad = tmp_path / "driver.py"
    bad.write_text(
        '@router.put("/{key}/config")\n'
        "def put_config(body: ConfigUpdate, server: GameServer = Depends(get_server)):\n"
        "    driver.set_config(server, body.values)\n",
        encoding="utf-8",
    )
    assert _unguarded_write_routes(bad) == ["put_config (line 2)"]


def test_the_latch_on_the_decorator_counts_too(tmp_path):
    """The second common form — otherwise the guard reports correct routes as violations."""
    ok = tmp_path / "decorator.py"
    ok.write_text(
        '@router.put("/{key}/mods", dependencies=[Depends(_no_world_operation)])\n'
        "def replace_mods(body: ModsReplace, server: GameServer = Depends(get_server)):\n"
        "    driver.set_mods(server, body.mods)\n",
        encoding="utf-8",
    )
    assert _unguarded_write_routes(ok) == []
