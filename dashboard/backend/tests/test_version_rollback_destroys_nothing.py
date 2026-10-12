"""The rollback of a version change must never remove the OLD, good container.

`_rollback_swap` starts with `docker.remove(name, force=True)` to remove the failed NEW
container. That assumption holds only if `docker.rename(name, prev)` ran before. If exactly that
`rename` fails, `name` still points at the old, known-good container — and the rollback
force-deletes it, then promotes the existing `{name}-prev` to production and reports
`container_restored=True`, i.e. "rolled back".

The path there is built into the code: a leftover `{name}-prev` from an earlier change is an
expected state (the success path only logs when it cannot remove the old container), and it is
exactly the state in which `rename` fails with 409 "name already in use". The chain in one
sentence: change 1 leaves `pz-1-prev` behind · change 2 fails at `rename` · the rollback deletes
`pz-1` (the running production save) and promotes a leftover two generations old in its place.

Two guards, two tests: the rollback touches `name` only if the rename **really** ran — and a
leftover that cannot be removed aborts the change **before** anything is touched. The second
follows the rule `restarts.py` already knows: an operation must not arm a state whose rollback is
demonstrably missing.
"""
from __future__ import annotations

import asyncio

import pytest

from app.services import version_manager as vm


class _Docker:
    """Docker stub that keeps a record — and fails on request."""

    def __init__(self, *, remove_fails_for: set[str] | None = None, exists: set[str] | None = None):
        self.removed: list[str] = []
        self.renamed: list[tuple[str, str]] = []
        self.created: list[str] = []
        self._remove_fails_for = remove_fails_for or set()
        self._exists = exists if exists is not None else set()

    async def remove(self, name, force=False):
        if name in self._remove_fails_for:
            raise RuntimeError(f"500 from the proxy while removing {name}")
        self.removed.append(name)
        self._exists.discard(name)

    async def rename(self, old, new):
        self.renamed.append((old, new))

    async def create(self, name, payload):
        self.created.append(name)

    async def state(self, name):
        return {"exists": name in self._exists, "running": False}

    async def action(self, name, act, **kw):
        pass


def test_without_a_successful_rename_name_is_not_removed(monkeypatch):
    """`name` is then still the OLD container — removing it is the data loss itself."""
    dk = _Docker(exists={"pz-1", "pz-1-prev"})
    monkeypatch.setattr(vm, "docker", dk)
    monkeypatch.setattr(vm, "_restore_version_fields", lambda k, snap: None)
    monkeypatch.setattr(vm, "_load", lambda k: object())

    asyncio.run(vm._rollback_swap(
        "pz-1", "pz-1", "pz-1-prev", spec=None, backup_name="",
        was_running=False, driver=None, old_existed=True, renamed=False,
    ))

    assert "pz-1" not in dk.removed, (
        "the rollback deleted the old, good production container"
    )
    assert ("pz-1-prev", "pz-1") not in dk.renamed, (
        "a leftover two generations old was promoted to production"
    )


def test_after_a_successful_rename_the_new_container_is_removed(monkeypatch):
    """The cross-check: in the normal case the rollback MUST clean up, otherwise the test checks nothing."""
    dk = _Docker(exists={"pz-1", "pz-1-prev"})
    monkeypatch.setattr(vm, "docker", dk)
    monkeypatch.setattr(vm, "_restore_version_fields", lambda k, snap: None)
    monkeypatch.setattr(vm, "_load", lambda k: object())

    asyncio.run(vm._rollback_swap(
        "pz-1", "pz-1", "pz-1-prev", spec=None, backup_name="",
        was_running=False, driver=None, old_existed=True, renamed=True,
    ))

    assert "pz-1" in dk.removed, "the failed new container was left behind"
    assert ("pz-1-prev", "pz-1") in dk.renamed, "the old container did not come back"


def test_a_leftover_that_cannot_be_removed_aborts_the_change(monkeypatch):
    """Without a free `{name}-prev` there is no rollback — so no change is started at all.

    Swallowing the removal error (`try/except: pass`) lets the change run on and fail only at
    `rename` — halfway through, with the server stopped and a rollback pointing at the wrong
    container.
    """
    dk = _Docker(remove_fails_for={"pz-1-prev"}, exists={"pz-1", "pz-1-prev"})
    monkeypatch.setattr(vm, "docker", dk)

    with pytest.raises(Exception) as e:
        asyncio.run(vm._remove_old_leftover("pz-1-prev"))
    assert "pz-1-prev" in str(e.value)
    assert dk.renamed == [] and dk.created == [], "something was touched anyway"


def test_without_a_leftover_the_removal_passes_silently(monkeypatch):
    """Cross-check: the normal case must not fail, otherwise no change is possible at all."""
    dk = _Docker(exists=set())
    monkeypatch.setattr(vm, "docker", dk)
    asyncio.run(vm._remove_old_leftover("pz-1-prev"))
