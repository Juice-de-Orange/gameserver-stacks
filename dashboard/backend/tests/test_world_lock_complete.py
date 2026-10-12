"""Every operation listed in `OPERATION_LABELS` must also TAKE the world lock.

**Why this rule and not a list of files.** `world_lock.OPERATION_LABELS` is the directory of the
world-writing operations — it exists so that the 409 message can name the blocking operation
("Already running for this server: a restore"). An entry there is therefore the promise that this
operation holds the lock. Adding the entry without calling `world_lock.hold(...)` builds exactly
the gap the lock exists against — and it cannot be seen from outside, because the *query*
(`held_by`) sits at the route and looks like complete protection.

Example: a restore route that checks `held_by()` and politely rejects with 409 while ANOTHER
operation runs, but never takes the lock itself. A mod apply or version change starting a second
later sees the lock free, passes its own `held_by` check and runs **over the same world at the
same time** — the very incident `world_lock.py` was built for.

The hit list of this test IS the work list, following the pattern of `test_stop_paths.py`.
"""
from __future__ import annotations

import ast
from pathlib import Path

from app.services.world_lock import OPERATION_LABELS

APP = Path(__file__).resolve().parents[1] / "app"


def _taken_operations() -> set[str]:
    """All names from `world_lock.hold(<key>, "<name>")` — on the syntax tree, not by regex.

    A regex would also find the example in the docstring of `hold()` itself; the syntax tree does
    not see strings as code.
    """
    taken: set[str] = set()
    for file in APP.rglob("*.py"):
        tree = ast.parse(file.read_text(encoding="utf-8"), filename=str(file))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            target = node.func
            if not (isinstance(target, ast.Attribute) and target.attr == "hold"):
                continue
            # `world_lock.hold(...)` — the receiver must be the module, not some other object
            # with a method of the same name.
            receiver = target.value
            if not (isinstance(receiver, ast.Name) and receiver.id == "world_lock"):
                continue
            if len(node.args) >= 2 and isinstance(node.args[1], ast.Constant):
                value = node.args[1].value
                if isinstance(value, str):
                    taken.add(value)
    return taken


def test_the_cross_check_finds_calls_at_all():
    """Without this line the guard below would be green even if the scan found nothing any more."""
    taken = _taken_operations()
    assert "mods.apply" in taken, "the AST scan no longer finds any hold() calls"
    assert len(taken) >= 3, f"suspiciously few hold() calls found: {sorted(taken)}"


def test_every_named_operation_also_takes_the_lock():
    taken = _taken_operations()
    missing = sorted(set(OPERATION_LABELS) - taken)
    assert not missing, (
        "These operations are listed in OPERATION_LABELS but never call "
        f"world_lock.hold(): {missing}. An entry there is the promise to hold the lock "
        "— without the call the operation checks whether another one runs, but then lets any "
        "second operation run over the same world at the same time."
    )


def test_no_operation_holds_the_lock_under_a_name_without_a_label():
    """The other direction: a name without a label entry reaches the operator as an internal
    identifier in the 409 message ("Already running for this server: version.change")."""
    without_label = sorted(_taken_operations() - set(OPERATION_LABELS))
    assert not without_label, (
        f"These operation names have no label in OPERATION_LABELS: {without_label}. "
        "The operator reads them raw in the 409 message."
    )
