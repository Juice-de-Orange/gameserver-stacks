"""Regression tests for smaller fixes:
- days_valid: invalid days are detected (routers reject them instead of silently falling back to DAILY)
- version_manager: a second version change during a running swap → 409 instead of data loss
"""
import asyncio

import pytest
from fastapi import HTTPException

from app.services import restarts
from app.services import version_manager as vm


def test_days_valid_accepts_known_and_rejects_typos():
    assert restarts.days_valid("daily")
    assert restarts.days_valid("")
    assert restarts.days_valid("mon,wed,fri")
    assert restarts.days_valid("MON, Tue")   # case-insensitive + whitespace
    assert not restarts.days_valid("mo,mi,fr")  # German abbreviations (typo)
    assert not restarts.days_valid("funday")
    assert not restarts.days_valid("mon,xxx")   # one invalid token is enough


def test_version_change_rejects_concurrent_swap():
    """Through the world lock, not a separate `_swap_locks`."""
    from app.services import world_lock

    async def run():
        world_lock._locks.clear()
        world_lock._holders.clear()
        async with world_lock.hold("pz-1", "version.change"):
            with pytest.raises(HTTPException) as ei:
                await vm.change_version("pz-1", image_tag="whatever")
            assert ei.value.status_code == 409
        world_lock._locks.clear()

    asyncio.run(run())


def test_mod_apply_is_rejected_while_a_version_change_is_running():
    """World operations lock against each other, not only against their own kind.

    With per-kind locks two browser tabs are enough to run a mod apply and a version change on
    the same world at once: the second takes its mandatory backup over a half-moved world and
    removes the container whose boot the first is waiting for (up to 900 s).

    And the message must name the RIGHT operation — "a mod change is already running" would be a
    lie that sends the operator looking in the wrong place.
    """
    from app.services import world_lock
    from app.services.mods import apply as mod_apply

    async def run():
        world_lock._locks.clear()
        world_lock._holders.clear()
        async with world_lock.hold("pz-1", "version.change"):
            with pytest.raises(HTTPException) as ei:
                await mod_apply.apply_draft("pz-1")
            assert ei.value.status_code == 409
            assert "a version change" in str(ei.value.detail), ei.value.detail
        world_lock._locks.clear()

    asyncio.run(run())
