"""The world lock — one per server across ALL operations that touch world files.

A lock per operation kind (separate `_locks` dicts, routers checking
`operations.running(server, <kind>)` for their own kind only) is not enough: two browser tabs would
suffice to run a mod apply and a version change over the same world at the same time.
"""
from __future__ import annotations

import asyncio

import pytest

from fastapi import HTTPException

from app.services import lifecycle, world_lock


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    # `asyncio.Lock` binds to the event loop on the first wait, and every test has its own via
    # `asyncio.run` — without a reset the second waiting test fails.
    monkeypatch.setattr(world_lock, "_locks", {})
    monkeypatch.setattr(world_lock, "_holders", {})


def test_two_different_operation_kinds_exclude_each_other():
    """The core: not "two mod applies" but mod apply AGAINST version change.

    **They exclude each other by REJECTION, not by waiting.** A queued second operation would
    still start after the first — with a plan (preflight, file list, running state, mandatory
    backup) from BEFORE the first operation, while the world looks different afterwards. A
    rejected operation can be triggered again; one that ran through with a wrong plan cannot.
    """
    trace: list[str] = []

    async def scenario():
        async def mods():
            async with world_lock.hold("pz-1", "mods.apply"):
                trace.append("mods:start")
                await asyncio.sleep(0.05)
                trace.append("mods:end")

        async def version():
            await asyncio.sleep(0.01)   # starts halfway through
            try:
                async with world_lock.hold("pz-1", "version.change"):
                    trace.append("version:start")
                    trace.append("version:end")
            except HTTPException as e:
                trace.append(f"version:rejected:{e.status_code}")

        await asyncio.gather(mods(), version())

    asyncio.run(scenario())
    assert trace == ["version:rejected:409", "mods:start", "mods:end"] or            trace == ["mods:start", "version:rejected:409", "mods:end"], (
        f"The second operation was queued instead of rejected: {trace}"
    )


def test_different_servers_do_not_block_each_other():
    """The lock is per server — otherwise a long mod operation on pz-1 would hold up the
    Minecraft backup."""
    trace: list[str] = []

    async def scenario():
        async def long():
            async with world_lock.hold("pz-1", "mods.apply"):
                await asyncio.sleep(0.05)
                trace.append("pz:end")

        async def short():
            await asyncio.sleep(0.01)
            async with world_lock.hold("mc", "backup.restore"):
                trace.append("mc:end")

        await asyncio.gather(long(), short())

    asyncio.run(scenario())
    assert trace == ["mc:end", "pz:end"], f"servers block each other: {trace}"


def test_held_by_names_the_blocking_operation():
    """So that the router answers 409 with the RIGHT name.

    "A mod change is already running for this server" is a lie when a version change is actually
    running — and the operator then looks in the wrong place.
    """
    seen: list[str | None] = []

    async def scenario():
        async def holder():
            async with world_lock.hold("pz-1", "version.change"):
                await asyncio.sleep(0.03)

        async def asker():
            await asyncio.sleep(0.01)
            seen.append(world_lock.held_by("pz-1"))
            seen.append(world_lock.held_by("mc"))

        await asyncio.gather(holder(), asker())

    asyncio.run(scenario())
    assert seen == ["version.change", None]
    assert world_lock.held_by("pz-1") is None, "the lock was not released"


def test_the_world_lock_does_not_deadlock_with_the_lifecycle_lock(monkeypatch, fake_docker_cls,
                                                                   wire_lifecycle, fake_driver_cls):
    """The binding order: world_lock OUTSIDE, lifecycle INSIDE.

    `asyncio.Lock` is not reentrant, and every world-writing operation calls
    `stop_and_confirm` -> `lifecycle._lock` in its middle. Reversing the order builds a deadlock
    that only shows in production — as a server that can no longer be stopped.
    """
    from app.models.server import GameServer, GameType

    srv = GameServer(key="pz-1", game_type=GameType.pz, container_name="pz-1")
    srv.id = 1
    docker = fake_docker_cls()
    wire_lifecycle(docker, fake_driver_cls())

    async def scenario():
        async with world_lock.hold(srv.key, "mods.apply"):
            # Inside the world lock a stop (and with it the lifecycle lock) must work.
            return await lifecycle.stop_and_confirm(srv)

    was_running, stopped = asyncio.run(asyncio.wait_for(scenario(), timeout=5))
    assert stopped, "the stop inside the world lock did not get through"
