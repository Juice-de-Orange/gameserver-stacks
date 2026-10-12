"""Guard: every `create_backup` call passes its server.

**The failure it guards against.** For Minecraft a failed quiesce hook means the autosave of the
RUNNING server stays off until someone restarts it — silent data loss that has nothing to do with
the backup itself.

That was only visible if the caller read `quiesce_warning` from the result, and only one of six
callers did (`scheduled_backup`). In the worst case — the lead-up raises between `save-off` and
`save-all flush` — there is no result to read at all, because the exception leaves the function.

The answer is the choke point: `create_backup` records the incident itself as soon as it knows
which server it packs for. For that every caller must pass `server=`; this guard pins it.

**Exception list: empty** — no caller is exempt. Only the `create_backup` call lines INSIDE the
definition of `create_backup` itself are skipped (`_BACKUP_INTERNAL`, see below): those are
recursive or helper calls of the choke point, not callers.
"""
import ast
import pathlib

APP = pathlib.Path(__file__).resolve().parent.parent / "app"


# **The hand-over forms used in the codebase — and the obvious ones that could appear.**
# `run_in_executor`, `partial` and `anyio.to_thread.run_sync` would otherwise slip through
# silently; the list is the promise, not a docstring.
_HANDOVER_FORMS = {"to_thread", "run_sync", "run_in_executor", "partial"}


def _call_name(k: ast.Call) -> str:
    f = k.func
    if isinstance(f, ast.Attribute):
        return f.attr
    if isinstance(f, ast.Name):
        return f.id
    return ""


def _calls_without_server(path: pathlib.Path) -> list[tuple[int, str]]:
    """(line, form) for every `create_backup` call without `server=`.

    Covers the direct call `create_backup(...)` and the hand-over to one of `_HANDOVER_FORMS`,
    e.g. `asyncio.to_thread(create_backup, ...)` — there the arguments sit on the `to_thread`.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    hits: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = _call_name(node)
        if name == "create_backup":
            if not any(kw.arg == "server" for kw in node.keywords):
                hits.append((node.lineno, "direct"))
        elif name in _HANDOVER_FORMS and node.args:
            first = node.args[0]
            # `run_in_executor(None, create_backup, ...)` has the callable at position 1
            candidates = [a for a in node.args[:2]]
            targets_backup = any(
                (isinstance(a, ast.Name) and a.id == "create_backup")
                or (isinstance(a, ast.Attribute) and a.attr == "create_backup")
                for a in candidates
            )
            if targets_backup and not any(kw.arg == "server" for kw in node.keywords):
                hits.append((node.lineno, name))
            del first
    return hits


# The one named exception in `services/backup.py`: the internal hand-off from `create_backup` to
# `_create_backup_locked` lives there and PASSES `server` through — it is the definition, not a
# caller. By line number instead of the whole file, so `scheduled_backup` stays under the rule.
def _backup_internal_lines() -> set[int]:
    import ast as _ast
    source = (APP / "services" / "backup.py").read_text(encoding="utf-8")
    tree = _ast.parse(source)
    lines: set[int] = set()
    for n in _ast.walk(tree):
        if isinstance(n, _ast.FunctionDef | _ast.AsyncFunctionDef) and n.name == "create_backup":
            for k in _ast.walk(n):
                if isinstance(k, _ast.Call):
                    lines.add(k.lineno)
    return lines


_BACKUP_INTERNAL = _backup_internal_lines()


def test_every_mandatory_backup_call_knows_its_server():
    violations: list[str] = []
    for path in APP.rglob("*.py"):
        rel = path.relative_to(APP).as_posix()
        if rel == "services/backup.py":
            # **Only the definition and the internal hand-off are exempt, not the file.**
            # `scheduled_backup` is a full caller; skipping the whole file would let a removed
            # `server=` there go unnoticed.
            for line, form in _calls_without_server(path):
                if line in _BACKUP_INTERNAL:
                    continue
                violations.append(f"{rel}:{line} ({form})")
            continue
        for line, form in _calls_without_server(path):
            violations.append(f"{rel}:{line} ({form})")
    assert not violations, (
        "These callers pack an archive without saying for which server. A failed quiesce hook "
        "stays invisible — for Minecraft that means autosave off until someone restarts:\n  "
        + "\n  ".join(violations)
    )


def test_the_guard_would_find_the_old_error(tmp_path):
    """Cross-check — both call forms as they looked before the rule."""
    bad = tmp_path / "bad.py"
    bad.write_text(
        "async def f():\n"
        '    bk.create_backup("manual", data_dir=d, pre_hook=p, post_hook=q)\n'
        "    await asyncio.to_thread(create_backup, 'weekly', data_dir=d, pre_hook=p)\n",
        encoding="utf-8",
    )
    assert _calls_without_server(bad) == [(2, "direct"), (3, "to_thread")]


def test_with_server_the_call_is_clean(tmp_path):
    """The target state."""
    ok = tmp_path / "good.py"
    ok.write_text(
        "async def f():\n"
        '    bk.create_backup("manual", data_dir=d, server=server)\n'
        "    await asyncio.to_thread(create_backup, 'weekly', data_dir=d, server=server)\n",
        encoding="utf-8",
    )
    assert _calls_without_server(ok) == []


def test_other_to_thread_calls_stay_untouched(tmp_path):
    """The guard must hit only this one function."""
    ok = tmp_path / "neutral.py"
    ok.write_text(
        "async def f():\n"
        "    await asyncio.to_thread(prune_backups, 90, data_dir=d)\n"
        "    await asyncio.to_thread(extract_backup, name, data_dir=d)\n",
        encoding="utf-8",
    )
    assert _calls_without_server(ok) == []
