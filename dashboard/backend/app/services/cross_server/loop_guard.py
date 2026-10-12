"""Loop protection for the chat bridge. PROCESS-LOCAL (single-worker invariant).

The bridge reads genuine player chat from server A and injects it into servers B/C. Without
protection this creates an echo loop (our injection is read back as "chat" and distributed
again). Four structural layers prevent that:

  1. **Source exclusion** (in the fanout, not here): a message is NEVER sent back to its
     origin server.
  2. **Injected-hash TTL** (here): every line we inject is hashed and remembered with a TTL.
     On ingest, lines matching an injected hash are dropped: that is our own injection
     echoing in the chat/log, not genuine player chat.
  3. **dedup_key** (here): (server, player, message) is accepted only ONCE within a time
     window. This catches double reads (RCON + log) and fast repeats.
  4. **Parser discipline** (in the adapter): only genuine player chat is ingested; sinks send
     server/system messages. Even if layer 2 missed, the injection would not look like
     player chat.

All state is in memory with a TTL. With exactly ONE uvicorn worker (a hard invariant of the
dashboard) it is coherent. Do NOT scale to several workers without a shared store.
"""
from __future__ import annotations

import hashlib
import threading
import time

# How long an injected line counts as "do not ingest". Generously above the largest ingest
# latency (log tail/RCON poll ~25 s) so an echo is always still recognised as injected.
_INJECTED_TTL = 90.0
# Window in which (server, player, message) counts as a duplicate.
_DEDUP_TTL = 30.0

_lock = threading.Lock()
_injected: dict[str, float] = {}  # hash -> expiry (epoch s)
_dedup: dict[str, float] = {}     # dedup_key -> expiry (epoch s)


def _norm(text: str) -> str:
    """Collapse whitespace and lowercase, to be robust against formatting jitter."""
    return " ".join((text or "").split()).strip().lower()


def _hash(text: str) -> str:
    return hashlib.sha1(_norm(text).encode("utf-8")).hexdigest()


def make_dedup_key(server_key: str, player: str, msg: str) -> str:
    return f"{server_key}|{_norm(player)}|{_norm(msg)}"


def _purge(now: float) -> None:
    for store in (_injected, _dedup):
        for k in [k for k, exp in store.items() if exp <= now]:
            del store[k]


def mark_injected(text: str, now: float | None = None) -> None:
    """Remember a (formatted) line just injected into a server as "ours"."""
    now = time.time() if now is None else now
    with _lock:
        _purge(now)
        _injected[_hash(text)] = now + _INJECTED_TTL


def is_injected(text: str, now: float | None = None) -> bool:
    now = time.time() if now is None else now
    with _lock:
        _purge(now)
        return _hash(text) in _injected


def should_ingest(server_key: str, player: str, msg: str, now: float | None = None) -> bool:
    """True iff this chat line is NEW and genuine (not injected by us, not a duplicate).

    Side effect on True: marks the dedup_key (idempotent ingest: a second read of the same
    line returns False). Checks the incoming message both as bare text and as "player: msg"
    against the injected hashes (defence in depth on top of layer 4)."""
    now = time.time() if now is None else now
    line = f"{player}: {msg}"
    with _lock:
        _purge(now)
        if _hash(msg) in _injected or _hash(line) in _injected:
            return False
        key = make_dedup_key(server_key, player, msg)
        if key in _dedup:
            return False
        _dedup[key] = now + _DEDUP_TTL
        return True


def forget_dedup(server_key: str, player: str, msg: str) -> None:
    """Undo the dedup mark, for the case that persisting fails AFTER should_ingest: without
    the undo, the retry in the next poll would be rejected as a duplicate and the message
    would be lost for good."""
    with _lock:
        _dedup.pop(make_dedup_key(server_key, player, msg), None)


def reset() -> None:
    """Tests only: clear all in-memory state."""
    with _lock:
        _injected.clear()
        _dedup.clear()
