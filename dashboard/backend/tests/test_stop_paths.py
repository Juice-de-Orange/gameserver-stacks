"""Guard: there is exactly ONE stop/restart path.

The original bug was not exotic but a call site that forgot a parameter: the most-used button
called `docker.action(name, "stop")` without `stop_timeout` and so got 10 instead of 120 seconds of
grace period. Ten other places did it right, which is why nobody noticed.

This test prevents the regression structurally: `docker.action(..., "stop"|"restart")` may only
appear in `services/lifecycle.py`. Everything else goes through `lifecycle.stop_server` /
`lifecycle.restart_server` and so gets the broadcast, world save, game-correct shutdown and grace
period automatically.
"""
import ast
import pathlib

APP = pathlib.Path(__file__).resolve().parent.parent / "app"

# Exactly ONE allowed place. The list is as short as it should be.
ALLOWED = {"services/lifecycle.py"}


def _stop_or_restart_calls(path: pathlib.Path) -> list[tuple[int, str]]:
    """(line, action) for every suspicious `<anything>.action(…)` in the file.

    Suspicious is not only the literal `"stop"`/`"restart"` but every form in which the action
    cannot be read off — `action(name, act)`, `action(name, action="stop")`, `action(*args)`.
    Exactly those forms would otherwise slip past the guard; they are reported as `"unclear"` so
    they have to be resolved deliberately instead of being overlooked.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Attribute) and func.attr == "action"):
            continue
        kwargs = {kw.arg: kw.value for kw in node.keywords if kw.arg}
        if len(node.args) >= 2:
            second: ast.expr | None = node.args[1]
        elif "action" in kwargs:
            second = kwargs["action"]
        elif any(isinstance(a, ast.Starred) for a in node.args):
            found.append((node.lineno, "unclear"))
            continue
        else:
            continue  # e.g. `action(name)` — does not exist, but then it is not a stop
        if isinstance(second, ast.Constant):
            if second.value in {"stop", "restart"}:
                found.append((node.lineno, second.value))
        else:
            found.append((node.lineno, "unclear"))
    return found


def test_stop_and_restart_only_via_lifecycle():
    offenders: list[str] = []
    for path in APP.rglob("*.py"):
        rel = path.relative_to(APP).as_posix()
        if rel in ALLOWED:
            continue
        for lineno, action in _stop_or_restart_calls(path):
            offenders.append(f"{rel}:{lineno} → docker.action(..., {action!r})")
    assert not offenders, (
        "These places bypass `services/lifecycle.py` and so lose the game-correct "
        "shutdown:\n  " + "\n  ".join(offenders)
    )


def test_the_guard_would_find_the_old_bug(tmp_path):
    """Cross-check — without it there is no proof the test checks anything at all."""
    bad = tmp_path / "bad.py"
    bad.write_text('async def f():\n    await docker.action(name, "stop")\n', encoding="utf-8")
    assert _stop_or_restart_calls(bad) == [(2, "stop")]


def test_the_guard_is_not_fooled_by_workarounds(tmp_path):
    """The obvious escape routes: alias, variable, keyword, unpacked arguments."""
    bad = tmp_path / "tricky.py"
    bad.write_text(
        "async def f():\n"
        '    d = docker; await d.action(name, "restart")\n'   # alias → still caught
        "    await docker.action(name, act)\n"                # variable → unclear
        '    await docker.action(name, action="stop")\n'      # keyword
        "    await docker.action(*args)\n",                   # unpacked
        encoding="utf-8",
    )
    assert _stop_or_restart_calls(bad) == [(2, "restart"), (3, "unclear"), (4, "stop"), (5, "unclear")]


def test_start_stays_allowed(tmp_path):
    """Only stop/restart are dangerous — a `start` may appear anywhere."""
    ok = tmp_path / "good.py"
    ok.write_text('async def f():\n    await docker.action(name, "start")\n', encoding="utf-8")
    assert _stop_or_restart_calls(ok) == []


# --- `stop_server` only where nothing is written afterwards -------------------------------------
#
# Rule: before any write to world files use `lifecycle.stop_and_confirm`, not `stop_server` — the
# latter also returns when Docker never accepted the stop. The guard above only collects raw
# `docker.action(..., "stop")` and does not know the difference between the two helpers, so this
# rule needs its own guard.

STOP_SERVER_ALLOWED = {
    # Paths whose PURPOSE is the stop and that touch no file afterwards.
    "app/cli.py": "stop-all on host shutdown — the host powers down afterwards.",
    "app/services/idle_autostop.py": "idle stop: stopping is the goal, not an intermediate step.",
    "app/routers/server.py": "the stop button.",
    "app/routers/servers.py": "switching a server off: stopping is the goal, nothing is written afterwards.",
}


def _stop_server_calls(tree: ast.AST) -> list[int]:
    """Lines with `<x>.stop_server(...)` — by attribute name, so alias-proof."""
    return sorted(
        node.lineno for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "stop_server"
    )


def test_stop_server_only_where_nothing_is_written_afterwards():
    violations: list[str] = []
    used: set[str] = set()
    for path in sorted(APP.rglob("*.py")):
        rel = path.relative_to(APP.parent).as_posix()
        if rel == "app/services/lifecycle.py":
            continue  # the home of the function
        lines = _stop_server_calls(ast.parse(path.read_text(encoding="utf-8")))
        if not lines:
            continue
        if rel in STOP_SERVER_ALLOWED:
            used.add(rel)
            continue
        violations.extend(f"{rel}:{line}" for line in lines)
    assert not violations, (
        "`stop_server` also returns when Docker did not accept the stop.\n"
        "Code that touches world files afterwards needs `stop_and_confirm` (or `stop_and_snapshot`\n"
        "when it also backs up):\n  " + "\n  ".join(violations)
    )
    stale = set(STOP_SERVER_ALLOWED) - used
    assert not stale, f"these exceptions no longer match anything and should be removed: {stale}"


def test_the_stop_server_guard_is_not_fooled_by_workarounds(tmp_path):
    """Cross-check — otherwise there is no proof it finds anything at all."""
    p = tmp_path / "old.py"
    p.write_text(
        "await lifecycle.stop_server(server)\n"
        "l = lifecycle\n"
        "await l.stop_server(server, announce_text='x')\n"
        "await lifecycle.stop_and_confirm(server)\n",   # correct → no hit
        encoding="utf-8",
    )
    assert _stop_server_calls(ast.parse(p.read_text(encoding="utf-8"))) == [1, 3]
