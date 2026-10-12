"""Two `tarfile` runs over the same world exclude each other — on EVERY path there.

Minecraft's `pre_hook` sets `save-off`, the `post_hook` `save-on`; when run 1 finishes its
archive, its `post_hook` switches autosave back on **in the middle of run 2's ongoing
`tarfile.open`**. From then on run 2 packs a world being written and files the result as a
regular, successful backup.

There are several backup paths: the "Backup now" button, the weekly run over all servers, the
mandatory backup before a mod apply, the one before a scheduled restart, the config backup and
`lifecycle.stop_and_snapshot` (and with it restore, version change and the MC/VS mod change).
A helper with the right rule that callers bypass is the same failure class as with
`stop_and_confirm`, so **the rule moves to the choke point** instead of reminding every caller:
`create_backup` itself takes the lock. Whoever packs locks; there is no way around it that does
not bypass `create_backup`.

The lock is per **data folder**, not per server key: the directory is the thing that gets torn.
"""
from __future__ import annotations

import threading

import pytest

from app.services import backup as bk


def _world(tmp_path, name: str):
    """A data folder with a world in it — enough for `tarfile` to have something to do."""
    base = tmp_path / name
    (base / "Saves").mkdir(parents=True)
    (base / "Saves" / "world.bin").write_bytes(b"x" * 4096)
    return str(base)


def test_two_runs_over_the_same_world_run_one_after_the_other(tmp_path):
    """The core: while run 1 packs, run 2 does not even reach its `pre_hook`."""
    base = _world(tmp_path, "world")
    sequence: list[str] = []
    a_holds = threading.Event()
    b_has_knocked = threading.Event()
    a_may_finish = threading.Event()

    def hook_a():
        sequence.append("a-start")
        a_holds.set()
        # A holds the lock until the test releases it — B must wait during that time.
        assert a_may_finish.wait(timeout=10), "test setup hangs"

    def hook_b():
        sequence.append("b-start")

    def run_a():
        bk.create_backup("a", data_dir=base, world_subdirs=("Saves",), pre_hook=hook_a)
        sequence.append("a-end")

    def run_b():
        b_has_knocked.set()
        bk.create_backup("b", data_dir=base, world_subdirs=("Saves",), pre_hook=hook_b)
        sequence.append("b-end")

    ta = threading.Thread(target=run_a)
    ta.start()
    assert a_holds.wait(timeout=10), "run A never reached its pre_hook"

    tb = threading.Thread(target=run_b)
    tb.start()
    assert b_has_knocked.wait(timeout=10), "run B never started"

    # B has verifiably started. Without the lock, "b-start" would now be in the sequence right
    # away; the wait here therefore measures NOT the thread start time, only the lock.
    tb.join(timeout=0.5)
    assert "b-start" not in sequence, (
        f"run B packed while run A holds the world — sequence: {sequence}"
    )

    a_may_finish.set()
    ta.join(timeout=10)
    tb.join(timeout=10)
    assert sequence == ["a-start", "a-end", "b-start", "b-end"], sequence


def test_different_worlds_do_not_block_each_other(tmp_path):
    """The cross-check for the lock: it must not lock more than necessary.

    Without this case, "one global lock over all servers" would be green too — and the weekly run
    over five servers would then run strictly one after the other although it packs five
    different worlds.
    """
    a = _world(tmp_path, "world-a")
    b = _world(tmp_path, "world-b")
    both_inside = threading.Barrier(2, timeout=10)

    def hook():
        # If both arrive here, they really run at the same time. If they locked each other, the
        # barrier would run into its timeout and the test would turn red.
        both_inside.wait()

    error: list[BaseException] = []

    def run(base):
        try:
            bk.create_backup("x", data_dir=base, world_subdirs=("Saves",), pre_hook=hook)
        except BaseException as e:  # noqa: BLE001 — the test wants to see the reason
            error.append(e)

    ta = threading.Thread(target=run, args=(a,))
    tb = threading.Thread(target=run, args=(b,))
    ta.start()
    tb.start()
    ta.join(timeout=15)
    tb.join(timeout=15)
    assert not error, f"two different worlds blocked each other: {error}"


def test_the_manual_path_does_not_deadlock_itself(tmp_path):
    """`create_backup_exclusive` wraps `create_backup` — the same lock, the same thread.

    Were it not reentrant, the "Backup now" button would lock itself out: taken outside,
    requested again inside, and the operation hangs forever.
    """
    base = _world(tmp_path, "world")

    def _pack():
        return bk.create_backup("manual", data_dir=base, world_subdirs=("Saves",))

    result = bk.create_backup_exclusive(base, _pack)
    assert result["name"].endswith("-manual.tar.gz")


def test_a_second_manual_run_is_rejected_immediately(tmp_path):
    """The UI path stays fail-fast: the human should get an answer, not a progress bar."""
    base = _world(tmp_path, "world")
    inside = threading.Event()
    next_run = threading.Event()

    def _slow():
        inside.set()
        assert next_run.wait(timeout=10)
        return {"name": "whatever", "size": 0}

    t = threading.Thread(target=lambda: bk.create_backup_exclusive(base, _slow))
    t.start()
    assert inside.wait(timeout=10)
    with pytest.raises(RuntimeError, match="already running"):
        bk.create_backup_exclusive(base, lambda: {"name": "n", "size": 0})
    next_run.set()
    t.join(timeout=10)
