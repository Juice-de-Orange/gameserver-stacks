"""One RCON player list per server for everything that only SHOWS or samples it.

Every RCON command is a TCP connection of its own (`services/rcon.py`), and Minecraft/Paper logs
each one ("Thread RCON Client /… started" and "… shutting down"). The fleet page polls every 6 s,
the status strip every 12 s in every open tab, the join page on each visit, and the metrics
sampler and the session tracker once a minute — with a few tabs open the game log got two lines
about once a second, and the game server a connection for each.

So these paths share one answer per server: asked at most once per `MAX_AGE_S`, and concurrent
asks wait for the one in flight instead of opening their own. A player count up to 15 s old is
fine for a display and for session tracking (which works to the minute).

**Not for decisions.** The watchdog's liveness check, the idle auto-stop, the auto-update's
"is it empty?" and the boot proof after a version change call the driver directly: a cached
answer there would prove nothing or act on a stale "nobody online". Errors are never kept — a
failed ask must not stand in for the next one.
"""
from __future__ import annotations

import asyncio
import time
import weakref

MAX_AGE_S = 15.0

# server key -> (taken at, answer, latency in ms of the ask that produced it)
_answers: dict[str, tuple[float, dict, float]] = {}
# One lock per server key and event loop (a lock belongs to the loop it was first used in).
_locks: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, dict[str, asyncio.Lock]] = (
    weakref.WeakKeyDictionary()
)


def _now() -> float:
    return time.monotonic()


def _fresh(key: str) -> tuple[dict, float] | None:
    hit = _answers.get(key)
    if hit is not None and _now() - hit[0] < MAX_AGE_S:
        return hit[1], hit[2]
    return None


async def players(server, driver) -> tuple[dict, float]:
    """`driver.players(server)` and the latency in ms of the ask that produced the answer."""
    if (hit := _fresh(server.key)) is not None:
        return hit
    locks = _locks.setdefault(asyncio.get_running_loop(), {})
    async with locks.setdefault(server.key, asyncio.Lock()):
        if (hit := _fresh(server.key)) is not None:
            return hit
        started = time.perf_counter()
        answer = await driver.players(server)
        latency = round((time.perf_counter() - started) * 1000, 1)
        _answers[server.key] = (_now(), answer, latency)
        return answer, latency


def forget(key: str | None = None) -> None:
    """Drop the kept answer of one server, or of all."""
    if key is None:
        _answers.clear()
    else:
        _answers.pop(key, None)
