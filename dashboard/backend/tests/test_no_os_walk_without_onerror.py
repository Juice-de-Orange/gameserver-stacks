"""`os.walk` without `onerror` drops read errors silently — in the mod package that is an answer.

**What this guard stands against.** `cells.cells_of_map_dir` and `cells.find_map_dirs` once had
an `except OSError` around their loop and returned an empty set on error. Both were ineffective
or wrong:

* The `except` was **dead code**. `os.walk` does not raise — it reports every error to the
  `onerror` callback and discards it by default (`onerror=None`).
* The empty set is not information here but an **answer**: "this map yields no cells" is exactly
  the condition under which the preflight reports `MAP_WITHOUT_PROVIDER` — the blocker against
  the join crash. Unreadable looked like a spawn overlay. Fail-open.

The same form sat in `inventory._builds_of` and `inventory._scan_item`: an overlooked `mod.info`
there means "this build does not exist" or "this mod ID does not exist" — both inputs for
preflight blockers. All four places pass `onerror=cells.report_read_error`.

**The exceptions are measured, not estimated.** The package has six `os.walk` calls. Four answer
a decision question (above). The other two sum bytes for a display or a space estimate — they
already swallow per file on purpose (`except OSError: pass`), and a number a few bytes too small
is no wrong yes/no there. They are named below. If this list grows, that is the sign that the
rule no longer fits — not the permission to bypass it.

**Outside `services/mods/` this guard checks nothing.** The only other call in the backend
(`drivers/reforger.py`, size of a data directory) is the same harmless kind.
"""
from __future__ import annotations

import ast
import pathlib

PACKAGE = pathlib.Path(__file__).parent.parent / "app" / "services" / "mods"

# `file::function` -> reason. Both only sum bytes.
ALLOWED: dict[str, str] = {
    "apply.py::_dir_size": (
        "Sums the size of a quarantine folder for display. Already swallows per file "
        "(`except OSError: pass`); a number too small is no wrong yes/no here."
    ),
    "foreign.py::_size_of": (
        "The same for a foreign mod folder — display and space estimate, no decision about "
        "blockers or world files."
    ),
}


def _walk_calls(tree: ast.AST) -> list[tuple[int, bool, str]]:
    """(line, has_onerror, enclosing_function) per `os.walk(...)` call."""
    parents: dict[ast.AST, ast.AST] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node

    def function_of(node: ast.AST) -> str:
        cur = parents.get(node)
        while cur is not None:
            if isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef)):
                return cur.name
            cur = parents.get(cur)
        return "<module level>"

    hits = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        if isinstance(f, ast.Attribute) and f.attr == "walk" and isinstance(f.value, ast.Name) and f.value.id == "os":
            has = any(kw.arg == "onerror" for kw in node.keywords)
            hits.append((node.lineno, has, function_of(node)))
    return hits


def _violations(source: str, file: str) -> list[str]:
    out = []
    for line, has_onerror, fn in _walk_calls(ast.parse(source)):
        if has_onerror or f"{file}::{fn}" in ALLOWED:
            continue
        out.append(f"{file}:{line} ({fn})")
    return out


def test_every_os_walk_in_the_mod_package_reports_its_read_errors():
    violations: list[str] = []
    for p in sorted(PACKAGE.glob("*.py")):
        violations += _violations(p.read_text(encoding="utf-8"), p.name)

    assert not violations, (
        "These `os.walk` calls discard read errors silently. In the mod package an incomplete\n"
        "tree is not a gap but an ANSWER — and it decides about blockers and about world\n"
        "files. Pass `onerror=cells.report_read_error`, or add to ALLOWED with a reason:\n  "
        + "\n  ".join(violations)
    )


def test_the_guard_would_have_found_the_old_code():
    """Cross-check against the real code before the fix — otherwise it is not shown that it bites."""
    before = (
        "import os\n"
        "def cells_of_map_dir(map_dir):\n"
        "    out = set()\n"
        "    try:\n"
        "        for _root, _dirs, files in os.walk(map_dir):\n"
        "            pass\n"
        "    except OSError:\n"
        "        return set()\n"
        "    return out\n"
    )
    assert _violations(before, "cells.py") == ["cells.py:5 (cells_of_map_dir)"]

    after = before.replace("os.walk(map_dir)", "os.walk(map_dir, onerror=report_read_error)")
    assert _violations(after, "cells.py") == []


def test_the_exceptions_still_apply():
    """An exception that no longer matches anything is a permission nobody checks any more."""
    present = set()
    for p in sorted(PACKAGE.glob("*.py")):
        for _line, _has, fn in _walk_calls(ast.parse(p.read_text(encoding="utf-8"))):
            present.add(f"{p.name}::{fn}")

    stale = [k for k in ALLOWED if k not in present]
    assert not stale, "These exceptions should be deleted:\n  " + "\n  ".join(stale)


def test_the_exceptions_stay_shorter_than_the_rule():
    """Project rule: a guard whose exception list would be longer than its rule is not built —
    then the places are measured and the measurement written down. Here: four secured calls
    against two named exceptions. If that tips over, the guard itself is the finding."""
    secured = 0
    for p in sorted(PACKAGE.glob("*.py")):
        for _line, has, _fn in _walk_calls(ast.parse(p.read_text(encoding="utf-8"))):
            secured += 1 if has else 0
    assert secured > len(ALLOWED), (
        f"{secured} secured calls against {len(ALLOWED)} exceptions — the rule no longer holds."
    )
