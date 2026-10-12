"""Simple in-memory throttle against brute force on the public auth endpoints.

Valid **because console-api runs with exactly ONE worker** (see the Dockerfile CMD): the state
does not have to be shared across processes. With several workers it would have to move to a
shared place (database/Redis). It is also lost on a restart.

Failures are counted per key (IP and username separately). Too many within a 5-minute window
lock the key for a short time (429). A successful login resets the counters.
"""
from __future__ import annotations

import ipaddress
import logging
import socket
import time
from collections import defaultdict, deque
from collections.abc import Iterable

from fastapi import Request

from ..config import settings

log = logging.getLogger("uvicorn.error")

WINDOW_SEC = 300.0   # observation window
MAX_FAILURES = 8     # this many failures within the window ...
LOCKOUT_SEC = 300.0  # ... lock for this long (counted from the latest failure)

# Upper bound for the number of observed keys. The state is process-local and lives as long as
# the backend; without a cap it grows monotonically, and the key contains the username from the
# public login form. 4096 keys are out of reach in normal operation and cover any realistic
# attack without pushing a real user out of the lock.
MAX_KEYS = 4096

_failures: dict[str, deque] = defaultdict(deque)


def _prune(dq: deque, now: float) -> None:
    cutoff = now - WINDOW_SEC
    while dq and dq[0] < cutoff:
        dq.popleft()


def _forget_empty(keys: Iterable[str]) -> None:
    """Remove expired keys entirely — a deque that was pruned empty would otherwise stay forever.

    `reset()` only runs on a SUCCESSFUL login, i.e. exactly not while somebody is hammering the
    endpoint.
    """
    for k in keys:
        dq = _failures.get(k)
        if dq is not None and not dq:
            _failures.pop(k, None)


def retry_after(keys: Iterable[str]) -> int:
    """0 = free; otherwise seconds until release (maximum over all keys)."""
    now = time.monotonic()
    worst = 0
    for k in keys:
        dq = _failures.get(k)
        if not dq:
            continue
        _prune(dq, now)
        if len(dq) >= MAX_FAILURES:
            worst = max(worst, int(dq[-1] + LOCKOUT_SEC - now))
    _forget_empty(keys)
    return max(0, worst)


def record_failure(keys: Iterable[str]) -> None:
    now = time.monotonic()
    if len(_failures) >= MAX_KEYS:
        # Cap reached: drop the expired entries first. If that frees nothing, the OLDEST entries
        # go — whoever is attacking right now holds the newest ones and stays locked.
        for k in [k for k, dq in _failures.items() if not dq or dq[-1] < now - WINDOW_SEC]:
            _failures.pop(k, None)
        while len(_failures) >= MAX_KEYS:
            _failures.pop(min(_failures, key=lambda k: _failures[k][-1]), None)
    for k in keys:
        dq = _failures[k]
        _prune(dq, now)
        dq.append(now)


def reset(keys: Iterable[str]) -> None:
    for k in keys:
        _failures.pop(k, None)


# --- Client address ---------------------------------------------------------------------------

# How long a resolved host name from TRUSTED_PROXIES is reused. Docker assigns a new address
# when the proxy container is recreated, so the answer must not be kept forever.
_RESOLVE_TTL_SEC = 30.0

_resolved: dict[str, tuple[float, frozenset[str]]] = {}


def _as_ip(value: str | None) -> str | None:
    """Normalised IP address, or None if the text is not one.

    Free text must never become a throttle key: an attacker would send a fresh value with every
    attempt and bypass the IP throttle entirely. An IPv4-mapped IPv6 address is reduced to the
    IPv4 address so that both notations of one peer compare equal.
    """
    if not value:
        return None
    try:
        ip = ipaddress.ip_address(value.strip()[:64])
    except ValueError:
        return None
    mapped = getattr(ip, "ipv4_mapped", None)
    return str(mapped or ip)


def _resolve(name: str) -> frozenset[str]:
    """Addresses of a host name from TRUSTED_PROXIES; empty if it does not resolve (fail closed)."""
    now = time.monotonic()
    cached = _resolved.get(name)
    if cached and cached[0] > now:
        return cached[1]
    try:
        infos = socket.getaddrinfo(name, None, type=socket.SOCK_STREAM)
        addresses = frozenset(a for a in (_as_ip(info[4][0]) for info in infos) if a)
    except OSError:
        addresses = frozenset()
        log.warning("TRUSTED_PROXIES: host name %r does not resolve — not trusted", name)
    _resolved[name] = (now + _RESOLVE_TTL_SEC, addresses)
    return addresses


def _is_trusted_proxy(address: str) -> bool:
    ip = ipaddress.ip_address(address)
    for entry in settings.trusted_proxies:
        try:
            network = ipaddress.ip_network(entry, strict=False)
        except ValueError:
            if address in _resolve(entry):
                return True
            continue
        if ip.version == network.version and ip in network:
            return True
    return False


def client_ip(request: Request) -> str:
    """Address of the client for the throttle keys.

    Forwarding headers are believed only if the request arrives from a peer listed in
    TRUSTED_PROXIES. Any other caller can set them freely — in the default compose layout every
    game container shares a network with the API and reaches it directly — and would get a fresh
    throttle key per request. For such a caller the TCP peer is the client.

    From a trusted peer:
    * `CF-Connecting-IP`, if it is a valid address. The trusted proxy must set or strip this
      header itself; the bundled Caddy strips it.
    * otherwise `X-Forwarded-For`, read from the right: the first address that is not itself a
      trusted proxy. Entries further left were written by somebody outside the trusted chain.
    """
    peer = _as_ip(request.client.host if request.client else None)
    if peer is None:
        return "unknown"
    if not settings.trusted_proxies or not _is_trusted_proxy(peer):
        return peer
    headers = request.headers
    cf = _as_ip(headers.get("cf-connecting-ip"))
    if cf:
        return cf
    client = peer
    for part in reversed((headers.get("x-forwarded-for") or "").split(",")):
        hop = _as_ip(part)
        if hop is None:
            break  # malformed or empty: nothing left of this entry can be trusted either
        client = hop
        if not _is_trusted_proxy(hop):
            break
    return client
