"""Check from inside: does Docker publish the ports, and does the game answer?

Nothing leaves the host. Two questions, both from the API container:

1. **Published.** Docker's container list (through the socket proxy, the same read the port
   check uses) says which host ports the running container really holds. A container created
   before a port change still has the old ones until it is recreated.
2. **Answers.** The driver's own liveness probe, `players()` — Source RCON, BattlEye RCON or the
   Steam query (Valheim), the same call the fleet and the watchdog make. A server with an `a2s`
   entry whose console is RCON (Arma Reforger) also gets an A2S info query on that port, with
   the same code (`services/a2s.py`).

Both run on the internal Docker network, so they say "the game is up and Docker forwards its
ports", not "the internet can reach it". That is the outside check's question.
"""
from __future__ import annotations

import asyncio
import time

from ...errors import detail_of, note
from ...models.server import ConsoleKind, GameServer
from .. import a2s
from ..docker_client import docker
from ..drivers import get_driver
from ..ports import Binding, bindings_of, published_by_containers
from .checklist import ports_of

TIMEOUT_S = 5.0


async def _published(server: GameServer) -> tuple[list[Binding] | None, dict | None]:
    """The bindings the running container holds, or `(None, reason)`."""
    try:
        containers = await docker.running_containers()
    except Exception as e:  # noqa: BLE001 — a missing answer is a result, not a 500
        return None, note(
            "reachability.docker_unavailable",
            "Docker could not be asked which ports the container publishes ({reason}).",
            reason=str(e)[:200],
        )
    own = [c for c in containers or [] if server.container_name in {str(n).lstrip("/") for n in c.get("Names") or []}]
    if not own:
        return [], None
    return [b for b, _ in published_by_containers(own, exclude=set())], None


_ANY = ("", "0.0.0.0", "::")


def _same_address(held: str, wanted: str) -> bool:
    """Docker lists an every-interface port twice (`0.0.0.0` and `::`); the row says `""`."""
    return held in _ANY if wanted in _ANY else held == wanted


def _published_rows(server: GameServer, held: list[Binding]) -> list[dict]:
    out = []
    for want in bindings_of(server):
        same_port = [h for h in held if h.port == want.port and h.proto == want.proto]
        ok = any(_same_address(h.ip, want.ip) for h in same_port)
        name = next((p.name for p in ports_of(server) if p.port == want.port and p.proto == want.proto), "")
        if ok:
            detail = None
        elif same_port:
            detail = note(
                "reachability.port_other_address",
                "The container publishes {port} on {actual}, but the server is set to {wanted}: "
                "recreate the container (edit page) so the change takes effect.",
                port=f"{want.port}/{want.proto}",
                actual=", ".join(sorted({h.ip or "0.0.0.0" for h in same_port})),
                wanted=want.ip or "0.0.0.0",
            )
        else:
            detail = note(
                "reachability.port_not_published",
                "The container does not publish {port}. It was probably created before the port "
                "was changed: recreate the container (edit page) so the change takes effect.",
                port=f"{want.port}/{want.proto}",
            )
        out.append({"name": name, "port": want.port, "proto": want.proto, "ok": ok, "detail": detail})
    return out


async def _probe(coro) -> tuple[bool, float | None, dict | None]:
    """`(ok, latency in ms, why not)`. "Why not" is coded like every other text of the panel:
    a driver's own coded `detail` passes through (Valheim's "query not open yet"); anything else
    is foreign text (an OS or protocol error) and travels as a parameter."""
    t0 = time.perf_counter()
    try:
        await asyncio.wait_for(coro, timeout=TIMEOUT_S)
    except asyncio.TimeoutError:
        return False, None, note("reachability.probe_timeout", "no answer within {seconds} s",
                                 seconds=f"{TIMEOUT_S:g}")
    except Exception as e:  # noqa: BLE001 — every failure of the probe is the answer "no"
        detail = detail_of(e)
        if isinstance(detail, dict):
            return False, None, detail
        return False, None, note("reachability.probe_failed", "{reason}",
                                 reason=detail[:300] or type(e).__name__)
    return True, round((time.perf_counter() - t0) * 1000, 1), None


def _channel(server: GameServer) -> dict:
    """How `players()` reaches this game, for the panel's wording."""
    if server.console_kind == ConsoleKind.source_rcon:
        return {"channel": "rcon", "port": server.rcon_port or None}
    if server.console_kind == ConsoleKind.battleye_rcon:
        return {"channel": "battleye", "port": server.rcon_port or None}
    return {"channel": "query", "port": None}


async def check(server: GameServer) -> dict:
    """Published ports and the game's answer. A stopped container stops here with a reason."""
    try:
        state = await docker.state(server.container_name)
    except Exception as e:  # noqa: BLE001
        return {"ran": False, "reason": note(
            "reachability.docker_unavailable",
            "Docker could not be asked which ports the container publishes ({reason}).",
            reason=str(e)[:200],
        ), "published": [], "answers": []}
    if not state.get("running"):
        return {"ran": False, "reason": note(
            "reachability.not_running",
            "The server is not running. Start it, wait until it is up, then check again.",
        ), "published": [], "answers": []}

    held, reason = await _published(server)
    published = _published_rows(server, held) if held is not None else []

    answers = []
    ok, latency, error = await _probe(get_driver(server).players(server))
    answers.append({**_channel(server), "ok": ok, "latency_ms": latency, "error": error})
    a2s_port = next((p.port for p in ports_of(server) if p.name == "a2s" and p.proto == "udp"), None)
    if a2s_port and server.console_kind != ConsoleKind.none:
        host = server.rcon_host or server.container_name
        ok, latency, error = await _probe(a2s.info(host, a2s_port, timeout=TIMEOUT_S))
        answers.append({"channel": "a2s", "port": a2s_port, "ok": ok, "latency_ms": latency, "error": error})

    return {"ran": True, "reason": reason, "published": published, "answers": answers}
