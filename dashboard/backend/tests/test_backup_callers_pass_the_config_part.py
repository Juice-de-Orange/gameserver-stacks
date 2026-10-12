"""Whoever triggers a backup also says which part of it is only configuration.

**Why this is a guard and not a comment.** `backup._pack` checks whether any WORLD CONTENT landed
in the archive — not merely whether anything landed. It can only make that distinction if it
knows `config_subdirs`. A caller that omits it gets no misbehaviour and no warning: its archive is
created normally, and the guard inside is blunt. A rule that is written down but whose callers
nobody checks.

There are several backup paths (`services/backup.scheduled_backup`, `services/lifecycle`,
`services/restarts`, `services/mods/apply`, `routers/server_ops`, `routers/config`), and the
next one will come.
"""
from __future__ import annotations

import ast
import pathlib

APP_DIR = pathlib.Path(__file__).resolve().parent.parent / "app"


def _calls_with_spec() -> list[tuple[str, int, set[str]]]:
    """Every `create_backup(...)` call that feeds `world_subdirs=` from a spec."""
    hits: list[tuple[str, int, set[str]]] = []
    for path in sorted(APP_DIR.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            # `create_backup(...)`, `bk.create_backup(...)` and also the form
            # `asyncio.to_thread(create_backup, ...)`.
            names = {
                n.id for n in ast.walk(node.func) if isinstance(n, ast.Name)
            } | {
                a.attr for a in ast.walk(node.func) if isinstance(a, ast.Attribute)
            } | {
                n.id for arg in node.args for n in ast.walk(arg) if isinstance(n, ast.Name)
            } | {
                a.attr for arg in node.args for a in ast.walk(arg) if isinstance(a, ast.Attribute)
            }
            if "create_backup" not in names:
                continue
            keywords = {kw.arg for kw in node.keywords if kw.arg}
            if "world_subdirs" in keywords:
                hits.append((str(path.relative_to(APP_DIR)), node.lineno, keywords))
    return hits


def test_there_are_callers_at_all() -> None:
    """Otherwise the test below would be green because it finds nothing — the classic dud."""
    assert len(_calls_with_spec()) >= 6


def test_every_caller_passes_config_subdirs() -> None:
    missing = [
        f"{file}:{line}"
        for file, line, keywords in _calls_with_spec()
        if "config_subdirs" not in keywords
    ]
    assert not missing, (
        "These backup calls do not say which part is only configuration — the guard in "
        "`_pack` (\"world content present?\") is blunt there:\n  " + "\n  ".join(missing)
    )
