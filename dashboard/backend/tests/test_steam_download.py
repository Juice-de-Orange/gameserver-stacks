"""The mirror step of `steam_download` — and why it needs a lock.

The block runs in `asyncio.to_thread` because `shutil.copytree` would otherwise stall the single
uvicorn worker for the length of the copy (hundreds of MB to GB).

**Transactionality does not come from the block.** On the event loop, a block without a single
`await` was atomic against every other coroutine; in the worker thread it no longer is. `staging`
and `retired` are derived from `wid` and therefore identical for two concurrent calls. Call B
deletes, in its cleanup loop, the staging directory A is copying into — if that lands between
A's `copytree` and A's `os.replace(staging, dst)`, A moves a half-deleted directory into place and
reports `ok=True`.

That is exactly the state the side-folder choreography is meant to prevent: half a mod cannot be
told apart from a broken one by the preflight. And unlike `…/mods/apply`, `POST …/mods/items` has
NO in-flight lock — a double click is enough.
"""
from __future__ import annotations

import asyncio
import os

from app.services import steam_download as sd


def _source(tmp_path, files: int = 40):
    src = tmp_path / "source"
    src.mkdir()
    for i in range(files):
        (src / f"file_{i:03}.txt").write_text("x" * 256, encoding="utf-8")
    return str(src), files


def test_two_concurrent_mirrors_do_not_install_half_a_mod(tmp_path, monkeypatch):
    """Two overlapping calls for THE SAME workshop ID must not tear each other apart.

    Everyday triggers: a double click on "Install", a second browser tab, or a retry because
    steamcmd takes up to 300 s. Copying a PZ workshop map takes seconds to minutes — the window
    is correspondingly wide.
    """
    src, count = _source(tmp_path)
    target_base = tmp_path / "workshop"
    dst = str(target_base / "123")

    # Initial install, so that `retired` and the cleanup loop really run.
    os.makedirs(dst, exist_ok=True)
    (tmp_path / "workshop" / "123" / "old.txt").write_text("old", encoding="utf-8")

    # Deterministic rather than timing-dependent. A sleep-based version easily stays green even
    # WITHOUT the lock (pause before the copy, or an offset too small to hit the window). A test
    # that depends on a `sleep` is a test that silently turns green some day — hence two events:
    #
    #   A copies half  ->  a_half  ->  B clears A's staging  ->  b_cleared  ->
    #   A copies the rest and moves the INCOMPLETE directory into place.
    import threading

    a_half = threading.Event()
    b_cleared = threading.Event()
    first_started = threading.Event()

    def _slow_copytree(source, target, *args, **kw):
        import shutil as _sh

        first = not first_started.is_set()
        first_started.set()
        os.makedirs(target, exist_ok=True)
        names = sorted(os.listdir(source))
        for i, name in enumerate(names):
            _sh.copy2(os.path.join(source, name), os.path.join(target, name))
            if first and i == len(names) // 2:
                a_half.set()
                b_cleared.wait(10)   # wait for B's cleanup loop
        return target

    real_rmtree = sd.shutil.rmtree

    def _rmtree(path, **kw):
        result = real_rmtree(path, **kw)
        if a_half.is_set():           # this is B's cleanup loop
            b_cleared.set()
        return result

    monkeypatch.setattr(sd.shutil, "copytree", _slow_copytree)
    monkeypatch.setattr(sd.shutil, "rmtree", _rmtree)

    async def scenario():
        a = asyncio.create_task(sd.mirror_into_workshop("123", src, dst))
        while not a_half.is_set():    # B starts only once A is in the middle of copying
            await asyncio.sleep(0.005)
        b = asyncio.create_task(sd.mirror_into_workshop("123", src, dst))
        return await asyncio.gather(a, b)

    error_a, error_b = asyncio.run(scenario())

    present = sorted(p for p in os.listdir(dst)) if os.path.isdir(dst) else []
    succeeded = [f for f in (error_a, error_b) if f is None]
    assert succeeded, f"both mirrors failed: {error_a!r} / {error_b!r}"
    assert len(present) == count, (
        f"the target folder has {len(present)} instead of {count} files — half a mod. "
        f"Returns: {error_a!r} / {error_b!r}"
    )


def test_a_failed_mirror_leaves_the_old_state_in_place(tmp_path, monkeypatch):
    """The rollback path of the choreography."""
    src, _ = _source(tmp_path, files=3)
    dst = str(tmp_path / "workshop" / "123")
    os.makedirs(dst)
    (tmp_path / "workshop" / "123" / "old.txt").write_text("old state", encoding="utf-8")

    def _broken(a, b, *args, **kw):
        raise OSError("no space left on device")

    monkeypatch.setattr(sd.shutil, "copytree", _broken)

    error = asyncio.run(sd.mirror_into_workshop("123", src, dst))
    assert error and "no space left" in error
    assert (tmp_path / "workshop" / "123" / "old.txt").read_text(encoding="utf-8") == "old state"


# --------------------------------------------------------------------- symlinks in the cache
#
# The steamcmd cache is writable by the helper container, which runs downloaded content. A
# workshop item holding a symlink must not make the API copy a file from OUTSIDE the cache into a
# server's workshop folder (`copytree` follows symlinks by default).

def _item_with_links(tmp_path):
    secret = tmp_path / "outside" / "secret.txt"
    secret.parent.mkdir()
    secret.write_text("api-only", encoding="utf-8")
    src = tmp_path / "cache" / "123"
    src.mkdir(parents=True)
    (src / "mod.info").write_text("id=Demo\n", encoding="utf-8")
    os.symlink(secret, src / "harmless.txt")
    os.symlink(secret.parent, src / "media")
    return str(src)


def test_symlinks_in_the_item_are_not_followed_out_of_the_cache(tmp_path, caplog):
    src = _item_with_links(tmp_path)
    dst = str(tmp_path / "workshop" / "123")

    with caplog.at_level("WARNING", logger="uvicorn.error"):
        error = asyncio.run(sd.mirror_into_workshop("123", src, dst))

    assert error is None
    assert os.path.isfile(os.path.join(dst, "mod.info"))
    assert not os.path.lexists(os.path.join(dst, "harmless.txt")), \
        "a file from outside the cache was copied into the workshop folder"
    assert not os.path.lexists(os.path.join(dst, "media")), "a folder outside the cache was copied"
    assert any("symlink" in r.getMessage() for r in caplog.records), "skipping must be visible"


def test_an_item_folder_that_is_itself_a_symlink_is_refused(tmp_path):
    outside = tmp_path / "other-server"
    outside.mkdir()
    (outside / "world.bin").write_text("someone else's", encoding="utf-8")
    src = tmp_path / "cache" / "123"
    src.parent.mkdir()
    os.symlink(outside, src)
    dst = str(tmp_path / "workshop" / "123")

    error = asyncio.run(sd.mirror_into_workshop("123", str(src), dst))

    assert error and "symlink" in error
    assert not os.path.exists(dst)


def test_special_files_are_skipped_instead_of_blocking_the_copy(tmp_path):
    """A FIFO would make `copy2` wait for a writer forever, holding the mirror thread."""
    src = tmp_path / "cache" / "123"
    src.mkdir(parents=True)
    (src / "mod.info").write_text("id=Demo\n", encoding="utf-8")
    os.mkfifo(src / "pipe")
    dst = str(tmp_path / "workshop" / "123")

    assert asyncio.run(sd.mirror_into_workshop("123", str(src), dst)) is None
    assert os.path.isfile(os.path.join(dst, "mod.info"))
    assert not os.path.lexists(os.path.join(dst, "pipe"))
