"""A function that is supposed to write must not return silently.

`routers/server_ops.put_config` decides solely on whether `driver.set_config` **returns**: if it
gets through, the endpoint answers 200 and writes a `config.update` audit entry. An
`except OSError: return` in the driver turns that into a success message about a change that never
happened — for MC and VS additionally with `restart_required: true`, so the operator restarts the
server and loses play time for nothing.

Aggravating: `config_fields` returns an **empty** list on the same `OSError`, which also drops the
block list of env-managed keys. Exactly the constellation in which nobody notices anything any more.

The dividing line is READING versus WRITING: a display may come up empty (`config_fields`,
`list_mods`), a write may not. That is why this guard applies only to functions whose name
announces writing.

Limit, stated honestly: only the single-statement form (`pass` / `return` / `return None`) and only
the naming convention are detected. An `except OSError: log.debug(...); return` would be green and
just as silent. This is a tripwire against accidents, not a lock against intent.
"""
from __future__ import annotations

import ast
from pathlib import Path

SERVICES = Path(__file__).parent.parent / "app" / "services"

# Names that announce writing. Everything else may come up empty.
WRITE_PREFIXES = ("set_", "write_", "_stash_", "_restore_", "save_")

# Places where a silent exit is deliberately right. The key is `file:function` — not the file
# alone, otherwise one exception would exempt the whole driver from the check.
# Every entry needs a reason: an exception list nobody reads any more is worse than no rule.
ALLOWED: dict[str, str] = {
    "app/services/drivers/reforger.py:save_slots":
        "False alarm of the naming rule: `save_` is a noun here (the save slots), not a command. "
        "The function READS the save slots and their metadata; a missing directory must yield an "
        "empty list there, not an error.",
}


def _silent_exits(tree: ast.AST) -> list[tuple[int, str]]:
    """(line, function name) for every `except …:` branch in a write function whose body
    consists **only** of `pass`, `return` or `return None`."""
    hits: list[tuple[int, str]] = []
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if not fn.name.startswith(WRITE_PREFIXES):
            continue
        for node in ast.walk(fn):
            if not isinstance(node, ast.ExceptHandler) or len(node.body) != 1:
                continue
            stmt = node.body[0]
            silent = isinstance(stmt, ast.Pass) or (
                isinstance(stmt, ast.Return)
                and (stmt.value is None
                     or (isinstance(stmt.value, ast.Constant) and stmt.value.value is None))
            )
            if silent:
                hits.append((stmt.lineno, fn.name))
    return hits


def test_no_write_function_returns_silently():
    violations: list[str] = []
    used_exceptions: set[str] = set()
    for path in sorted(SERVICES.rglob("*.py")):
        rel = path.relative_to(SERVICES.parent.parent).as_posix()
        for lineno, fname in _silent_exits(ast.parse(path.read_text(encoding="utf-8"))):
            if f"{rel}:{fname}" in ALLOWED:
                used_exceptions.add(f"{rel}:{fname}")
                continue
            violations.append(f"{rel}:{lineno} → {fname}()")
    assert not violations, (
        "These write functions return silently on error. The caller cannot tell a failure\n"
        "from a success and reports 200 + audit entry:\n  "
        + "\n  ".join(violations)
    )
    # Against rot of the exception list: an entry that no longer exists is a permission nobody
    # checks any more.
    dead = set(ALLOWED) - used_exceptions
    assert not dead, f"These exceptions no longer match anything and should be deleted: {dead}"


def test_the_guard_would_find_the_old_bug(tmp_path):
    """Cross-check — without it there would be no proof that the guard checks anything at all."""
    p = tmp_path / "old.py"
    p.write_text(
        "def set_config(self, s, u):\n"
        "    try:\n"
        "        lines = read()\n"
        "    except OSError:\n"
        "        return\n"
        "\n"
        "def set_other(self, s):\n"
        "    try:\n"
        "        write()\n"
        "    except OSError:\n"
        "        pass\n"
        "\n"
        "def config_fields(self, s):\n"      # READING may come up empty
        "    try:\n"
        "        return read()\n"
        "    except OSError:\n"
        "        return []\n"
        "\n"
        "def set_loud(self, s):\n"           # right: translates instead of swallowing
        "    try:\n"
        "        write()\n"
        "    except OSError as e:\n"
        "        raise ValueError(str(e)) from e\n",
        encoding="utf-8",
    )
    found = _silent_exits(ast.parse(p.read_text(encoding="utf-8")))
    assert found == [(5, "set_config"), (11, "set_other")], (
        "The guard must find the two silent writers — and report neither the reader "
        "(config_fields) nor the loud writer."
    )
