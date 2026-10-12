"""Guard: the result of a rollback is read, not thrown away.

**The bug it stands against.** `mods/foreign_apply` had `await _rollback(server, before, driver)`
as a bare statement, without assignment. `_rollback` can fail for several reasons (container not
stopped, file cannot be put back, start failed), and the 502 message below it only named the
files that could not be moved. The operator did not learn what state the server is in. Fifteen
lines further down the same module read the same return value correctly: the place was not
unknown, it was overlooked.

**The rule.** A call of a function whose name starts with `_rollback` never stands as a bare
statement. Whoever runs a rollback reads its result.

**Exception list: empty.** `_report_rollback` does not fall under the name pattern (it raises by
itself anyway and has no return value that says anything).
"""
import ast
import pathlib

APP = pathlib.Path(__file__).resolve().parent.parent / "app"


def _call_name(k: ast.Call) -> str:
    f = k.func
    if isinstance(f, ast.Attribute):
        return f.attr
    if isinstance(f, ast.Name):
        return f.id
    return ""


def _discarded_rollbacks(path: pathlib.Path) -> list[tuple[int, str]]:
    """(line, name) for every `_rollback*` call whose result nobody reads.

    Matched is `ast.Expr`, the node for "expression as statement". `await _rollback(...)` sits in
    it as `ast.Await`, so it is unwrapped.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    hits: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        value = None
        if isinstance(node, ast.Expr):
            value = node.value
        elif isinstance(node, ast.Assign) and _only_throwaway_targets(node.targets):
            # `_ = await _rollback(...)` is the same discarding with one line of disguise.
            value = node.value
        if value is None:
            continue
        if isinstance(value, ast.Await):
            value = value.value
        if isinstance(value, ast.Call) and _call_name(value).startswith("_rollback"):
            hits.append((node.lineno, _call_name(value)))
    return hits


def _only_throwaway_targets(targets) -> bool:
    """Are all assignment targets throwaway names (`_`, `__`, `___`)?"""
    names = []
    for t in targets:
        for k in ast.walk(t):
            if isinstance(k, ast.Name):
                names.append(k.id)
    return bool(names) and all(n.strip("_") == "" for n in names)


def test_no_rollback_without_a_reader():
    violations: list[str] = []
    for path in APP.rglob("*.py"):
        for line, name in _discarded_rollbacks(path):
            violations.append(f"{path.relative_to(APP).as_posix()}:{line} → {name}()")
    assert not violations, (
        "These rollbacks are run but their result is thrown away, so the operator does not "
        "learn what state the server is in:\n  " + "\n  ".join(violations)
    )


def test_the_guard_would_find_the_old_bug(tmp_path):
    """Cross-check: the code as it once stood in `foreign_apply`."""
    bad = tmp_path / "bad.py"
    bad.write_text(
        "async def f():\n"
        "    if was_running:\n"
        "        await _rollback(server, before, driver)\n",
        encoding="utf-8",
    )
    assert _discarded_rollbacks(bad) == [(3, "_rollback")]


def test_rollbacks_that_are_read_are_clean(tmp_path):
    """The target state, in both usual forms."""
    ok = tmp_path / "good.py"
    ok.write_text(
        "async def f():\n"
        "    files_restored, running = await _rollback(server, before, driver, start=True)\n"
        "    x, y = await _rollback_swap(a, b)\n"
        "    if (await _rollback(server)):\n"
        "        pass\n",
        encoding="utf-8",
    )
    assert _discarded_rollbacks(ok) == []


def test_other_calls_stay_untouched(tmp_path):
    """Only rollbacks: an `await docker.action(...)` without assignment is perfectly fine."""
    ok = tmp_path / "neutral.py"
    ok.write_text(
        "async def f():\n"
        '    await docker.action(name, "start")\n'
        "    await _report_rollback(server, error=e)\n",
        encoding="utf-8",
    )
    assert _discarded_rollbacks(ok) == []


def test_a_throwaway_name_counts_as_discarded(tmp_path):
    """`_ = await _rollback(...)` is the same bug with one line of disguise."""
    bad = tmp_path / "disguised.py"
    bad.write_text(
        "async def f():\n"
        "    _ = await _rollback(server, before, driver)\n"
        "    __, _ = await _rollback_swap(a, b)\n",
        encoding="utf-8",
    )
    assert _discarded_rollbacks(bad) == [(2, "_rollback"), (3, "_rollback_swap")]


def test_real_names_stay_allowed(tmp_path):
    """Cross-check: whoever names the result has read it; the test next to it checks that."""
    ok = tmp_path / "named.py"
    ok.write_text(
        "async def f():\n"
        "    files_restored, running = await _rollback(server, before, driver, start=True)\n",
        encoding="utf-8",
    )
    assert _discarded_rollbacks(ok) == []
