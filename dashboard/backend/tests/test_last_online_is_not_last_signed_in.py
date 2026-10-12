""""Last online" must not show when a password was last typed.

The user list showed "Last online: 5 d ago" next to the operator's own, signed-in account. Two
independent causes:

1. **The display hung on `last_login`.** That value is written only in `routers/auth.login`;
   `/api/auth/refresh` never touches it. With `JWT_REFRESH_TTL_DAYS=14` one can work for two
   weeks without it moving. The field answers "last SIGNED IN", not "last online".
2. **Timestamps did not carry their zone.** A `TIMESTAMP` without zone comes out of the DB naive
   and out of Pydantic without suffix; `new Date("…T16:00:00")` reads local time in the browser
   and is off by the UTC offset. Fixed for `UserOut` (`_timestamp_as_utc`).

The first fix wrote `last_seen` directly in `get_current_user`: the request's ORM instance became
dirty and was autoflushed into its transaction, the WebSocket path blocked the event loop, and
every request drew a second pool connection. The tests for it were hollow (written against the
real engine, the connection error swallowed, only an in-memory attribute checked). These tests
therefore run the write **against a real database** and read the result back from it.
"""
import datetime as dt
import inspect
import pathlib

from sqlmodel import Session, select

from app.auth import deps
from app.models import Role, User, UserStatus
from app.models.player import utcnow_naive
from app.schemas import UserOut
from app.services import presence

FRONTEND = pathlib.Path(__file__).resolve().parents[2] / "frontend" / "src"


def _user(session, name="admin", **fields) -> User:
    u = User(display_name=name, username=name, pw_hash="x", role=Role.admin,
             status=UserStatus.active, **fields)
    session.add(u)
    session.commit()
    session.refresh(u)
    return u


def _from_db(engine, uid):
    with Session(engine) as s:
        return s.exec(select(User).where(User.id == uid)).first().last_seen


# ---- Remembering costs no database ----

def test_remembering_does_not_touch_the_database():
    """The whole point: the request path only adds a dict entry.

    No engine is involved — if this were a write, the test would need a connection.
    """
    presence.remember(7)
    assert presence.open_() == 1
    assert presence.latest_seen(7) is not None


def test_the_store_does_not_grow_without_bound():
    """Capped like the login throttle — process memory without a cap is a trap."""
    for i in range(presence.MAX_ENTRIES + 50):
        presence.remember(i)
    assert presence.open_() <= presence.MAX_ENTRIES


# ---- Flushing, against a REAL database ----

def test_the_remembered_time_lands_in_the_right_row(mem_engine):
    """Reads back from the DB instead of checking an in-memory dummy.

    Fails if the statement sets the wrong field, loses the `WHERE` or never runs.
    """
    with Session(mem_engine) as s:
        a = _user(s, "admin")
        b = _user(s, "other")
        a_id, b_id = a.id, b.id

    presence.remember(a_id)
    assert presence.flush(mem_engine) == 1

    assert _from_db(mem_engine, a_id) is not None, "the timestamp did not arrive"
    assert _from_db(mem_engine, b_id) is None, (
        "an unrelated user was written too — the `WHERE` is missing"
    )


def test_only_last_seen_is_touched(mem_engine):
    """`last_login` answers a DIFFERENT question and must not move along."""
    signed_in = utcnow_naive() - dt.timedelta(days=5)
    with Session(mem_engine) as s:
        uid = _user(s, last_login=signed_in).id

    presence.remember(uid)
    presence.flush(mem_engine)

    with Session(mem_engine) as s:
        fresh = s.exec(select(User).where(User.id == uid)).first()
    assert fresh.last_seen is not None
    assert fresh.last_login == signed_in, "`last_login` was written too"


def test_flushing_clears_the_pending_entries(mem_engine):
    """Flushing twice must not do the work twice."""
    with Session(mem_engine) as s:
        uid = _user(s).id
    presence.remember(uid)
    assert presence.flush(mem_engine) == 1
    assert presence.flush(mem_engine) == 0
    assert presence.open_() == 0


def test_a_broken_database_does_not_kill_the_job():
    """A DISPLAY must not cost a scheduler job — nor a request."""
    presence.remember(1)
    assert presence.flush(engine=None) is None  # `None` is not an engine
    assert presence.open_() == 0, "the pending entries are stuck after a failure"


def test_a_failure_is_not_nothing_to_do():
    """`0` means "nothing pending", `None` means "could not write".

    Reporting both as `0` would let the caller — the lifespan, which promises "an orderly deploy
    loses nothing" — not tell them apart.
    """
    assert presence.flush(engine=None) == 0, "empty is not the same as broken"
    presence.remember(1)
    assert presence.flush(engine=None) is None


# ---- The display ----

def test_the_display_takes_the_fresher_value(mem_engine):
    """Up to a minute lies between two flushes — it must not be visible."""
    old = utcnow_naive() - dt.timedelta(hours=3)
    with Session(mem_engine) as s:
        uid = _user(s, last_seen=old).id
    presence.remember(uid)                      # just now, not yet written
    with Session(mem_engine) as s:
        fresh = s.exec(select(User).where(User.id == uid)).first()
        data = UserOut.from_user(fresh).model_dump(mode="json")
    assert data["last_seen"] > old.isoformat(), "the display lags behind the pending entry"


def test_a_zone_aware_value_does_not_break_the_display(mem_engine):
    """`last_login` is written zone-AWARE, `last_seen` naive.

    A `max()` over both forms raises `TypeError` — and that comparison sits in
    `UserOut.from_user`, i.e. in the user list itself. Aligning `last_seen` with the `last_login`
    pattern would take the page down.
    """
    with Session(mem_engine) as s:
        u = _user(s)
    u.last_seen = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=1)  # zone-aware
    presence.remember(u.id)
    data = UserOut.from_user(u).model_dump(mode="json")   # must NOT raise
    assert data["last_seen"] is not None


def test_timestamps_carry_their_zone(mem_engine):
    """Every timestamp in `UserOut` comes out with a zone suffix — even the naive one from the DB."""
    with Session(mem_engine) as s:
        u = _user(s, last_login=utcnow_naive(), last_seen=utcnow_naive())
        data = UserOut.from_user(u).model_dump(mode="json")

    checked = 0
    for field in ("created_at", "last_login", "last_seen"):
        value = data[field]
        assert value is not None, f"{field}: not delivered"
        assert value.endswith("Z") or value.endswith("+00:00"), (
            f"{field}={value!r}: no zone — the browser reads this as local time"
        )
        checked += 1
    assert checked == 3, "the field list has gone stale"


def test_an_empty_timestamp_stays_empty(mem_engine):
    """Cross-check: the serializer must not invent a time from "never"."""
    with Session(mem_engine) as s:
        u = _user(s)
        data = UserOut.from_user(u).model_dump(mode="json")
    assert data["last_seen"] is None and data["last_login"] is None


# ---- The choke points ----

def test_both_auth_choke_points_remember():
    """HTTP AND WebSocket.

    `get_current_user` serves HTTP, `_ws_user` the WebSockets (console, logs, cross-chat).
    Someone sitting in the live console for an hour produces authenticated traffic throughout —
    if the rule hangs only on the first, exactly that user shows a wrong value again.
    """
    checked = 0
    for name in ("get_current_user", "_ws_user"):
        source = inspect.getsource(getattr(deps, name))
        assert "presence.remember" in source, f"{name}: does not remember presence"
        checked += 1
    assert checked == 2, "the list of choke points has gone stale"


def test_the_request_path_does_not_write():
    """The core of the design, pinned as a rule.

    A write in `get_current_user` made the request's ORM instance dirty (autoflush wrote it into
    its transaction after all), blocked the event loop on the WebSocket path and drew a second
    pool connection per request.
    """
    for name in ("get_current_user", "_ws_user"):
        source = inspect.getsource(getattr(deps, name))
        for forbidden in ("commit(", "Session(engine)", "update("):
            assert forbidden not in source, (
                f"{name}: writes in the request path again ({forbidden}) — see "
                f"services/presence.py"
            )


def test_the_job_is_registered_and_runs_in_the_threadpool():
    """A guard nobody runs is none — and plain SQL does not belong in the loop."""
    from app import main
    source = inspect.getsource(main.lifespan)
    assert 'id="presence"' in source, "the job is not registered"
    assert "presence.flush" in source, (
        "shutdown does not flush — every deploy would cost up to a minute"
    )
    assert "to_thread" in inspect.getsource(main._presence_flush), (
        "the write runs in the loop instead of the threadpool"
    )


# ---- The contract with the UI ----

def test_the_ui_shows_last_online_from_last_seen():
    """`Users.tsx` must NOT hang "Last online" on `last_login` again.

    Contract test against the source, like the other frontend guards.

    It checks the **expression right after the label**, not the line: "Signed in:
    {u.last_login …}" stands next to it, and a line break from reformatting would have turned a
    line-based guard red without any real change.
    """
    source = (FRONTEND / "pages" / "Users.tsx").read_text(encoding="utf-8")
    position = source.find("seen.lastOnline")
    assert position >= 0, "the label \"Last online\" (`seen.lastOnline`) no longer exists"
    rest = source[position:]
    end = rest.find("seen.signedIn")       # up to the next label
    expression = rest[:end if end > 0 else 200]
    assert "last_seen" in expression, (
        f"\"Last online\" does not hang on last_seen: {expression.strip()[:120]}"
    )
    # Deliberately without word boundaries: `_` is a word character, a \b pattern would let
    # `last_login_at` or `u_last_login` through — exactly a renamed comeback.
    assert "last_login" not in expression, (
        f"\"Last online\" hangs on last_login again: {expression.strip()[:120]}"
    )
