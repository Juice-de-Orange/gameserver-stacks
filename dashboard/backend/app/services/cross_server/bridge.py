"""Chat bridge ingest and fanout.

Two scheduler jobs (ONE job each, `max_instances=1`). **Both are no-ops while
`chat_bridge_enabled=False`.** The model default is `False`, so the code can be wired into
`main.py` safely without any chat flowing. Whether the bridge is on is decided by the
settings row in the database, not by the default in the code.

Only games whose driver can announce can receive bridged chat (Reforger and Valheim cannot).
Avorion's chat log format has not been measured, so it is not ingested.

- `ingest_once` (~25 s): reads genuine player chat from `bridge_in` servers
  (`driver.parse_chat`) and feeds it, filtered by the guard, into the bus
  (`bus.record_inbound_chat`). A process-local watermark per server (last chat timestamp)
  keeps old log lines from being ingested again on every poll.
- `fanout_once` (~12 s): distributes new bus chat to all `bridge_out` servers EXCEPT the origin
  (source exclusion) via `driver.announce` (server message, not player chat) and marks the
  injection in `loop_guard` (echo protection). A process-local ID cursor prevents double fanout.

Single-worker invariant: watermarks and cursor are process-local.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging

from sqlmodel import Session, func, select

from ...db import engine
from ...models.cross_server import ChatBridgeMessage
from ...services import servers as servers_registry
from ...services.docker_client import docker
from . import bus, loop_guard

log = logging.getLogger("uvicorn.error")

# Process-local watermarks (single worker). Ingest: per server the last processed chat timestamp
# PLUS the (player, msg) lines already processed within that boundary second. Log timestamps
# only have one-second resolution; without the set, messages written in the same second after
# the read would be lost. Fanout: the last bus ID that was fanned out.
_last_at: dict[str, tuple[dt.datetime, set[tuple[str, str]]]] = {}
_fanout_cursor: dict[str, int] = {}


def _read_tail(path: str, max_bytes: int = 65536) -> str:
    """Read the last `max_bytes` of a (potentially large) log file, for the file-based chat
    ingest (PZ DebugLog). Blocking: call it via asyncio.to_thread in the ingest."""
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - max_bytes))
            data = f.read()
        # When truncated, the first line is a fragment (cut mid-line): drop it. A fragment
        # without a timestamp would otherwise be matched by the chat parser and slip past the
        # ingest watermark (ghost duplicates of old messages).
        if size > max_bytes:
            nl = data.find(b"\n")
            data = data[nl + 1:] if nl >= 0 else b""
        return data.decode("utf-8", errors="replace")
    except OSError:
        return ""


async def ingest_once() -> None:
    with Session(engine) as session:
        if not bus.get_settings(session).chat_bridge_enabled:
            # **Pausing clears the watermark.**
            #
            # `_last_at` protects against replaying old lines only the VERY FIRST time a server
            # is seen ("set a baseline, do NOT ingest the backlog"). Once an entry exists,
            # everything newer in the window counts as fresh. If the entry survived this early
            # return, switching the bridge off in the evening and on again in the morning would
            # inject the whole night's chat into every game at once. So after re-enabling, a
            # baseline is set just like the first time.
            if _last_at:
                log.info("bridge.ingest: bridge is off — watermarks discarded; re-enabling sets "
                         "a new baseline instead of ingesting the backlog")
                _last_at.clear()
            return
    for server, drv in servers_registry.list_enabled_with_driver():
        if not server.bridge_in:
            continue
        try:
            state = await docker.state(server.container_name)
            if not state.get("running"):
                continue
            # Chat source per game: MC = Docker log (stdout); PZ = DebugLog file (chat is not on
            # stdout). chat_log_path returns None for the Docker log, otherwise the file is tailed.
            path = drv.chat_log_path(server)
            if path:
                raw = await asyncio.to_thread(_read_tail, path)
            else:
                raw = await docker.logs(server.container_name, tail=400, timestamps=True)
            lines = drv.parse_chat(server, raw)
        except Exception:
            log.warning("bridge.ingest[%s]: round failed", server.key, exc_info=False)
            continue
        if not lines:
            continue
        newest = max(at for _, _, at in lines)
        prev = _last_at.get(server.key)
        if prev is None:
            # Set a baseline, do NOT ingest the backlog (incl. the line set of the boundary second).
            _last_at[server.key] = (newest, {(p, m) for p, m, at in lines if at == newest})
            continue
        prev_ts, prev_seen = prev
        # **Does the window even reach back to the watermark?**
        #
        # The source is fetched as an EXCERPT (64 KB file tail or `tail=400`), and its size is
        # not tied to the 25-second interval. If the source wrote more since the last poll than
        # the window holds, the lines in between dropped out BEFORE this job saw them, yet the
        # watermark still advances to the newest visible timestamp. Nothing can be recovered,
        # but it must be reported; otherwise a gap looks like silence.
        oldest = min(at for _, _, at in lines)
        if oldest > prev_ts:
            log.warning(
                "bridge.ingest[%s]: the window read starts AFTER the last watermark "
                "(%s > %s) — the chat lines in between slipped out and are lost. "
                "The source writes faster than the window holds.",
                server.key, oldest.isoformat(timespec="seconds"),
                prev_ts.isoformat(timespec="seconds"),
            )
        fresh = [
            (p, m, at) for (p, m, at) in lines
            if at > prev_ts or (at == prev_ts and (p, m) not in prev_seen)
        ]
        fresh.sort(key=lambda t: t[2])
        done_ts, done_seen = prev_ts, set(prev_seen)
        if fresh:
            with Session(engine) as session:
                for player, message, at in fresh:
                    try:
                        bus.record_inbound_chat(session, server, player, message)
                    except Exception:
                        # Persistence error (e.g. DB briefly gone): reset the session and abort
                        # the round WITHOUT advancing the watermark past the last success; the
                        # remaining lines are picked up again in the next poll.
                        try:
                            session.rollback()
                        except Exception:
                            pass
                        log.warning(
                            "bridge.ingest[%s]: persisting failed — rest of the round postponed",
                            server.key,
                        )
                        break
                    if at > done_ts:
                        done_ts, done_seen = at, set()
                    done_seen.add((player, message))
        _last_at[server.key] = (done_ts, done_seen)


async def fanout_once() -> None:
    with Session(engine) as session:
        settings = bus.get_settings(session)
        if not settings.chat_bridge_enabled:
            return
        cursor = _fanout_cursor.get("id")
        if cursor is None:
            maxid = session.exec(select(func.max(ChatBridgeMessage.id))).one()
            _fanout_cursor["id"] = maxid or 0  # baseline: do NOT fan out an existing backlog
            return
        msgs = list(
            session.exec(
                select(ChatBridgeMessage)
                .where(ChatBridgeMessage.kind == "chat", ChatBridgeMessage.id > cursor)
                .order_by(ChatBridgeMessage.id)
                .limit(200)
            )
        )
        fmt = settings.chat_format
    if not msgs:
        return

    all_sd = servers_registry.list_enabled_with_driver()
    names = {s.id: (s.display_name or s.key) for s, _ in all_sd}
    targets = [(s, d) for s, d in all_sd if s.bridge_out]

    # **Query the running state ONCE per target, not per (message x target).**
    #
    # The answer depends only on the target and does not change within a round, while each
    # `docker.state` costs a proxy round trip (roughly 0.26 s). Queried in the inner loop, a
    # handful of servers and ten chat lines in one tick would exceed this job's 12-second
    # interval.
    #
    # **`None` here means NOT queryable, not "not running".** Treating both alike would move
    # the cursor past a message that was never delivered.
    states = await asyncio.gather(
        *(docker.state(s.container_name) for s, _ in targets), return_exceptions=True
    )
    running: dict[str, bool | None] = {}
    for (srv, _), st in zip(targets, states, strict=True):
        if isinstance(st, BaseException):
            log.warning("bridge.fanout[%s]: run state cannot be queried", srv.key)
            running[srv.key] = None
        else:
            running[srv.key] = bool(st.get("running"))

    for msg in msgs:
        origin = names.get(msg.server_id, msg.server_key)
        text = _format(fmt, origin, msg.player, msg.message)
        unreachable = False
        for srv, drv in targets:
            # Source exclusion: never back to the origin. `server_key` is ALWAYS set, `server_id`
            # not necessarily, so additionally exclude by server_key.
            if (srv.id is not None and srv.id == msg.server_id) or srv.key == msg.server_key:
                continue
            if "announce" not in drv.capabilities():
                continue  # no broadcast verb (Reforger): chat cannot be injected
            state = running.get(srv.key)
            if state is False:
                continue                      # deliberately down: that is a result
            if state is None:
                unreachable = True           # unknown: the message stays open
                continue
            try:
                # Mark ONLY the formatted line as injected, NOT the bare body: a bare body like
                # "gg" would otherwise wrongly suppress a genuine "gg" chat from another server
                # within the 90 s window. Re-ingesting the injection is ruled out anyway
                # (announce = server message, not `<Name>` chat; anchored parser).
                loop_guard.mark_injected(text)
                await drv.announce(srv, text)
            except Exception:
                log.warning("bridge.fanout[%s]: injection failed", srv.key,
                            exc_info=False)
                unreachable = True
                continue  # swallow per target, never abort the loop
        # **The cursor only advances past what is done.**
        #
        # If it advanced regardless of whether the message arrived anywhere, a retry would be
        # impossible: the next round only selects `id > cursor`, so the message would be lost
        # for good. The ingest path above handles a persistence error the same way.
        if unreachable:
            log.info("bridge.fanout: message %s not delivered to all targets — cursor stays, "
                     "the next round retries", msg.id)
            break
        _fanout_cursor["id"] = msg.id


def _format(fmt: str, server: str, player: str, msg: str) -> str:
    try:
        return fmt.format(server=server, player=player, msg=msg)
    except Exception:
        return f"[{server}] {player}: {msg}"


def reset_state() -> None:
    """Tests only."""
    _last_at.clear()
    _fanout_cursor.clear()
