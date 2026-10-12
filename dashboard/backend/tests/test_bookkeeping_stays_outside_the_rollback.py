"""Guard: no database write inside a rollback `try`.

**The error it stands against.** The transaction boundary must sit behind the boot proof, not
behind the bookkeeping:

* `mods/apply._apply_locked` — with `state.snapshot_applied` and `_finish(change_id, "ok")` in the
  `try` whose `except` triggers the full rollback, a briefly absent `console-db` stops the server
  proven via RCON again, writes the old `.ini` back and pulls world cells out of quarantine —
  reported as "the previous configuration is running again".
* `version_manager._change_version_locked` — the same, but more destructive: there the `except`
  runs `_rollback_swap`, i.e. `docker.remove(name, force=True)` on the verified container plus
  `extract_backup` over the data folder. A bookkeeping error would throw away the new version
  TOGETHER WITH THE WORLD.

**The rule.** From the proven boot on, the operation has succeeded. What goes wrong afterwards is
a note in the result — not a reason to touch a running world. So: whatever writes to the database
does not sit in a `try` whose handler rolls back.

**Exception list: empty.** That is why this guard exists at all — were the list longer than the
rule, a measurement would stand here instead.
"""
import ast
import pathlib

APP = pathlib.Path(__file__).resolve().parent.parent / "app"

# Handlers that run a rollback. Name pattern, not enumeration: a future `_rollback_xyz` falls
# under it by itself.
def _is_rollback_call(name: str) -> bool:
    # Pattern instead of enumeration — with a list of single cases, a handler named `_go_back`
    # would have made the whole `try` invisible. Every name that NAMES a rollback matches.
    # `session.rollback()` is not a way back for the server, hence prefix/suffix instead of "in".
    return (name.startswith("_rollback")
            or name.endswith("_rollback")
            or "revert" in name.lower())


# Writing DB calls. `Session(` is included because every direct write path in the project goes
# through it.
# `record` is in the list WITHOUT an underscore: the real audit function is called `record`
# (`services/audit.py`), and `startswith("_record")` would never match it.
DB_WRITES = ("_finish", "record", "_record", "snapshot_applied", "_persist", "Session", "commit")


def _call_name(call: ast.Call) -> str:
    f = call.func
    if isinstance(f, ast.Attribute):
        return f.attr
    if isinstance(f, ast.Name):
        return f.id
    return ""


def _violations(path: pathlib.Path) -> list[tuple[int, str]]:
    """(line, call) for every DB write call in the `try` of a rolling-back handler."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    hits: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Try):
            continue
        rolls_back = any(
            isinstance(k, ast.Call) and _is_rollback_call(_call_name(k))
            for h in node.handlers for k in ast.walk(h)
        )
        if not rolls_back:
            continue
        for stmt in node.body:
            for k in ast.walk(stmt):
                if isinstance(k, ast.Call) and _call_name(k).startswith(DB_WRITES):
                    hits.append((k.lineno, _call_name(k)))
    return hits


def test_no_db_write_in_a_rollback_try():
    violations: list[str] = []
    for path in APP.rglob("*.py"):
        rel = path.relative_to(APP).as_posix()
        if not rel.startswith("services/"):
            continue
        for line, call in _violations(path):
            violations.append(f"{rel}:{line} → {call}()")
    assert not violations, (
        "This bookkeeping sits in the `try` of a rollback — a DB error would thereby roll back a "
        "proven boot. It belongs behind the `try/except`:\n  "
        + "\n  ".join(violations)
    )


def test_the_guard_would_find_the_old_error(tmp_path):
    """Cross-check — the code as it once stood in `_apply_locked`."""
    bad = tmp_path / "bad.py"
    bad.write_text(
        "async def f():\n"
        "    try:\n"
        "        await verify_boot(server, driver)\n"
        "        set_id = state.snapshot_applied(server, draft)\n"
        '        _finish(change_id, "ok")\n'
        "    except Exception as e:\n"
        "        await _report_rollback(server, error=e)\n",
        encoding="utf-8",
    )
    assert _violations(bad) == [(4, "snapshot_applied"), (5, "_finish")]


def test_a_try_without_rollback_stays_untouched(tmp_path):
    """Cross-check in the other direction: the rule applies ONLY to rolling-back handlers."""
    ok = tmp_path / "good.py"
    ok.write_text(
        "def f():\n"
        "    try:\n"
        '        _finish(vc_id, "ok")\n'
        "    except Exception:\n"
        "        log.warning('whatever')\n",
        encoding="utf-8",
    )
    assert _violations(ok) == []


def test_bookkeeping_behind_the_try_is_allowed(tmp_path):
    """The target state — exactly as it stands now in `apply.py` and `version_manager.py`."""
    ok = tmp_path / "right.py"
    ok.write_text(
        "async def f():\n"
        "    try:\n"
        "        await verify_boot(server, driver)\n"
        "    except Exception as e:\n"
        "        await _report_rollback(server, error=e)\n"
        "    try:\n"
        '        _finish(change_id, "ok")\n'
        "    except Exception:\n"
        "        note = 'history not written'\n",
        encoding="utf-8",
    )
    assert _violations(ok) == []
