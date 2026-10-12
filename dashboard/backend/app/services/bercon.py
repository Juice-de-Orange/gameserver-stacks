"""Async modified BattlEye RCON for Arma Reforger (berconpy v3).

Reforger speaks a **modified BattlEye RCON**, NOT Source RCON, so `services/rcon.py` (Source
RCON) is unusable here. `berconpy` wraps the BE UDP handshake (login packet, sequence numbers,
keep-alive). A short-lived client per call mirrors the per-call model of `rcon_command`; a
persistent client per `server_key` would be a later optimisation if latency becomes a problem.

Deliberate design decisions:
- The `berconpy` import is LAZY (inside the function), so driver import and the import smoke
  test stay green WITHOUT `berconpy` installed (unit tests inject a fake `console`; only the
  real deployment path needs the package). Mirrors the drivers' lazy
  `from ..rcon import rcon_command`.
- Thin, replaceable seam: if berconpy's handshake fails against Reforger's modification,
  switching to the `rcon` PyPI library is a one-file change.
- The BE login here is the PROTOCOL auth (RCON password from the `rcon` block of config.json),
  NOT the in-game `#login`.
"""
from __future__ import annotations

import asyncio
import socket

from .rcon import RconError


class BerconError(RconError):
    """BattlEye RCON unreachable / login rejected / timeout.

    Deliberately inherits from `RconError`, so the existing `except RconError` sites (console
    WebSocket, stats poller, restart/broadcast fan-out) swallow a Reforger RCON outage just
    like a Source RCON outage: no special path, no job-loop crash."""


async def bercon_command(
    host: str, port: int, password: str, command: str, *, timeout: float = 8.0
) -> str:
    """Run a (usually `#`-prefixed) Reforger RCON command and return the response.

    Raises `BerconError` on any connection/auth/timeout problem (one error type for callers).
    """
    try:
        import berconpy
    except ImportError as e:  # package missing (e.g. local test environment) → clear error
        raise BerconError(
            "berconpy is not installed — the BattlEye RCON transport is missing (requirements: berconpy>=3,<4)."
        ) from e

    # v3 provides `ArmaClient` for Arma/Reforger (speaks Reforger's modified protocol correctly);
    # the generic `AsyncRCONClient` is deprecated and trips over Reforger ("Invalid address").
    # Order: ArmaClient → AsyncRCONClient → RCONClient (backwards compatibility).
    # connect(ip, port, pw) + send_command are identical across them.
    client_cls = (
        getattr(berconpy, "ArmaClient", None)
        or getattr(berconpy, "AsyncRCONClient", None)
        or getattr(berconpy, "RCONClient", None)
    )
    if client_cls is None:
        raise BerconError("berconpy client class not found — incompatible version (Reforger needs v3).")

    client = client_cls()

    async def _run() -> str:
        # berconpy does NOT accept a host name: its connected UDP socket otherwise raises
        # "Invalid address: must be None or (ip, port)". So resolve the (container) name to an
        # IP on every call, which also covers an IP change after a container recreate.
        #
        # `loop.getaddrinfo` instead of `socket.gethostbyname`, and INSIDE `_run`, for two
        # reasons. First, `gethostbyname` is a blocking libc call without any timeout
        # parameter: if Docker's embedded resolver does not answer (exactly the situation the
        # re-resolution was built for), `resolv.conf`'s `timeout:5, attempts:2` apply and the
        # ONE uvicorn worker stalls for up to ten seconds, triggered by the stats poller every
        # minute without any human involved. Second, resolving inside `_run` puts it under the
        # `wait_for` below and thus within the documented 8-second budget.
        try:
            infos = await asyncio.get_running_loop().getaddrinfo(
                host, port, family=socket.AF_INET, type=socket.SOCK_DGRAM
            )
        except OSError as e:
            # Separate branch so that "name not resolvable" does not reach the operator as
            # "login rejected"; the catch-all `except` below cannot tell the two apart.
            raise BerconError(f"RCON host cannot be resolved ({host}): {e}") from e
        ip = infos[0][4][0]
        # connect() is an async context manager (logs in, keeps alive, logs out on exit).
        async with client.connect(ip, port, password):
            return await client.send_command(command)

    try:
        return await asyncio.wait_for(_run(), timeout=timeout)
    except asyncio.TimeoutError as e:
        raise BerconError(f"BattlEye RCON timed out after {timeout:.0f}s ({host}:{port}).") from e
    except BerconError:
        raise
    except Exception as e:  # login rejected / UDP unreachable / protocol error → wrap uniformly
        raise BerconError(f"BattlEye RCON unreachable/refused ({host}:{port}): {e}") from e
