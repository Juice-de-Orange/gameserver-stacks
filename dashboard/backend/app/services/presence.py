"""Who was online last? Remembered in memory, written out in batches.

**Why not simply write inside the request.** Setting `user.last_seen` and committing in
`get_current_user` has three defects with one root cause, a mere DISPLAY value pushing itself
into the path of every request:

1. **The separate session is only half real.** `user` belongs to the request's session; setting
   an attribute dirties the instance, and SQLAlchemy autoflushes it into the endpoint's
   transaction on the next query **anyway**. If that fails, it takes down exactly the request
   the `try/except` was meant to protect.
2. **On the WebSocket path it blocks the event loop.** `user_from_token` is called synchronously
   from `async` handlers (`/ws/cross-chat` every three seconds). A connection setup plus
   `UPDATE` would run on the ONE uvicorn worker there.
3. **Every request past the throttle pulls a second connection from the pool** while the first
   is still held.

So the request path does **nothing but a dict assignment** here. The values are written out in
a batch, once a minute, from a scheduler job.

**Process-local state is sound because exactly ONE uvicorn worker runs**, the same reasoning as
for `auth/ratelimit.py` and the permission cache (a hard constraint of this application).

**What a restart costs:** up to one minute of "last seen". For a display that counts in minutes
that is no loss, and `lifespan` flushes once more on shutdown, so an orderly restart loses
nothing at all.
"""
from __future__ import annotations

import datetime as dt
import logging

from sqlalchemy import update
from sqlmodel import Session

from ..models.player import utcnow_naive
from ..models.user import User

log = logging.getLogger("uvicorn.error")

# Upper bound as in `auth/ratelimit.py`: unbounded input from the network does not belong in
# process memory. These are verified user IDs rather than foreign input, but the bound costs
# nothing and makes the claim "this store does not grow" checkable.
MAX_ENTRIES = 4096

_seen: dict[int, dt.datetime] = {}


def naive_utc(value: dt.datetime | None) -> dt.datetime | None:
    """Aware or not, the result is always naive UTC (or None).

    **Why this is not cosmetic:** `users.last_login` and `created_at` are written timezone-AWARE,
    `last_seen` naive. A `max()` or a subtraction across both forms raises `TypeError`, and that
    comparison sits in `UserOut.from_user`, i.e. in the user list itself.
    """
    if value is None:
        return None
    return value if value.tzinfo is None else value.astimezone(dt.timezone.utc).replace(tzinfo=None)


def remember(user_id: int | None) -> None:
    """Note presence. Costs one dict entry, no database access.

    Called at EVERY auth choke point: `get_current_user` (HTTP) and `user_from_token`
    (WebSockets). Two choke points, not one: someone sitting in the live console for an hour
    produces authenticated traffic throughout, and without the second one exactly that user
    would show a stale value again.
    """
    if user_id is None:
        return
    if len(_seen) >= MAX_ENTRIES and user_id not in _seen:
        # `next(iter(...))` WITHOUT a default can raise `StopIteration` in the middle of the
        # request path if the job thread emptied the dict between `len()` and `iter()`, which
        # is exactly what this module must rule out. The default turns it into a no-op.
        _seen.pop(next(iter(_seen), None), None)
    _seen[user_id] = utcnow_naive()


def open_() -> int:
    """Number of entries waiting to be written out (for tests and diagnostics)."""
    return len(_seen)


def forget_all() -> None:
    """Tests only: process memory otherwise outlives the test boundary."""
    _seen.clear()


def flush(engine) -> int | None:
    """Write the noted timestamps. Returns the count, or **None** if writing failed.

    `0` means "nothing noted", `None` means "could not write": two different answers.
    Reporting both as `0` would let an unknown pass as a result; `services/a2s.py` spells out
    the rule: *an unclear answer is an exception, never a zero.*

    **Drain first, then write.** The entries are taken out of the dict BEFORE the database is
    touched: a request arriving in the meantime lands in the next round instead of being lost.
    If the write fails the timestamps are gone, on purpose. Putting them back would mean
    pushing an old value over a possibly newer one, the wrong price for a display value.

    A failure is logged **with its cause** (`exc_info=True`) and swallowed: a scheduler job that
    raises leaves only a single line without a traceback.
    """
    global _seen
    if not _seen:
        return 0
    # **One swap, not two steps.** `list(...)` followed by `.clear()` is not atomic: a
    # `remember()` between the two statements would end up neither in the batch nor in the next
    # round. With the rebind, an entry that still falls into the OLD dict stays part of the
    # batch.
    batch, _seen = list(_seen.items()), {}
    try:
        with Session(engine) as s:
            for user_id, when in batch:
                s.execute(update(User).where(User.id == user_id).values(last_seen=when))
            s.commit()
    except Exception:  # noqa: BLE001 - a display value is no reason to lose a job
        log.warning("presence could not be written (%d entries dropped)", len(batch),
                    exc_info=True)
        return None
    return len(batch)


def latest_seen(user_id: int | None) -> dt.datetime | None:
    """The noted timestamp if there is one, otherwise None.

    Keeps the user list from lagging by up to a minute: `UserOut.from_user` takes the later of
    the two values (database or noted entry).
    """
    return _seen.get(user_id) if user_id is not None else None
