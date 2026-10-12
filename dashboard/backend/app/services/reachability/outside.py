"""Check from outside: third-party services look at the server's public address.

**Off by default** (`GSS_REACHABILITY_PROBES=on` offers it). Each service can be switched off on
its own, and the two open-source ones can be pointed at a self-hosted instance. What leaves the
host is the server's public **IPv4 address** (a DNS name is resolved here first, so the name is
not sent) and, for two of the services, port numbers — nothing else: no key, no server name, no
password. The panel lists this per service before the button is pressed.

| Service | Used for | Sends |
|---|---|---|
| Steam Web API `ISteamApps/GetServersAtAddress` (Valve, no key needed) | the game port of Valheim, Project Zomboid, Avorion, Arma Reforger | the IPv4 address |
| mcstatus.io (open source, 5 requests/s per client, answers cached 1 min) | the Minecraft port | IPv4 address and port |
| portchecker.io (open source, IPv4 and public addresses only) | every other TCP port | IPv4 address and ports |

What each answer means — and what it does not:

* **Steam** lists a server that has registered with Steam's master server from that address.
  That proves the game runs and is announced at this address; whether its game port is open is
  only likely, not proven (players connect directly). A server set to "not public" is never
  listed, and Arma Reforger servers are announced through Bohemia's own list, not necessarily
  Steam's — "not listed" is a warning, not a verdict.
* **mcstatus.io** performs a real Minecraft status ping from the internet: "online" is proof.
* **portchecker.io** opens a TCP connection from the internet: "open" is proof that something
  answers on that port.
* **UDP without such a service** (Factorio, the extra ports of several games) cannot be checked
  from outside: a UDP port that does not answer looks the same as a blocked one. The result says
  so instead of showing green.

Answers are cached for `CACHE_TTL_S` per service, address and port, so repeated clicks do not
reach the services again; every call has a timeout of `TIMEOUT_S`.
"""
from __future__ import annotations

import asyncio
import ipaddress
import socket
import time
from dataclasses import dataclass

import httpx

from ...config import settings
from ...errors import note
from ...models.server import GameServer, GameType
from ..docker_client import docker
from .checklist import Port, bind_scope, ports_of, public_address

TIMEOUT_S = 10.0
CACHE_TTL_S = 60.0
USER_AGENT = "stacks-console reachability check"
STEAM_URL = "https://api.steampowered.com/ISteamApps/GetServersAtAddress/v1/"

# Games whose server registers with Steam's master server. The check looks for the UDP entry
# on the game port (Avorion: `game_udp`, the same number as its TCP `game` port) and for the
# Steam query port, which Steam's answer carries in `addr`.
STEAM_GAMES = frozenset({GameType.valheim, GameType.pz, GameType.avorion, GameType.arma_reforger})
STEAM_QUERY_NAMES = frozenset({"a2s", "steam_query"})

# Tests replace it with an `httpx.MockTransport`; `None` is the real network.
_transport: httpx.AsyncBaseTransport | None = None

_cache: dict[tuple, tuple[float, object]] = {}


@dataclass(frozen=True)
class Service:
    id: str
    name: str
    url: str          # what the panel shows: the address the data goes to
    sends: str        # `ip` | `ip_port` | `ip_ports`
    enabled: bool


def services() -> list[Service]:
    """The three services with their switch, for the disclosure in the panel."""
    return [
        Service("steam", "Steam Web API", STEAM_URL, "ip", settings.reachability_steam),
        Service("mcstatus", "mcstatus.io", _base(settings.reachability_mcstatus_url) + "/v2/status/java/",
                "ip_port", settings.reachability_mcstatus),
        Service("portcheck", "portchecker.io", _base(settings.reachability_portcheck_url) + "/api/query",
                "ip_ports", settings.reachability_portcheck),
    ]


def _base(url: str) -> str:
    return (url or "").strip().rstrip("/")


def _enabled(service_id: str) -> bool:
    return next(s.enabled for s in services() if s.id == service_id)


def method_for(server: GameServer, port: Port) -> str:
    """Which service checks this port, before switches: `steam`, `mcstatus`, `portcheck` or
    `none` (UDP without a game protocol a service could speak)."""
    if port.proto == "tcp":
        if server.game_type == GameType.minecraft and port.name == "game":
            return "mcstatus"
        return "portcheck"
    if server.game_type in STEAM_GAMES and (port.port == _game_port(server) or port.name in STEAM_QUERY_NAMES):
        return "steam"
    return "none"


def _game_port(server: GameServer) -> int:
    return next((p.port for p in ports_of(server) if p.name == "game"), 0)


def plan(server: GameServer) -> list[dict]:
    """Per port: the service that would check it (after the switches) — what the panel shows
    before the first check. A switched-off Minecraft service falls back to the TCP check."""
    out = []
    for p in ports_of(server):
        method = method_for(server, p)
        if method == "mcstatus" and not _enabled("mcstatus"):
            method = "portcheck"
        state = "none" if method == "none" else ("on" if _enabled(method) else "off")
        out.append({**p.as_dict(), "service": method, "state": state})
    return out


def disclosure(server: GameServer) -> dict:
    """What the GET of the panel needs: on or off, the services, and the plan per port."""
    return {
        "enabled": settings.reachability_probes,
        "services": [s.__dict__ for s in services()],
        "plan": plan(server),
        "cache_s": int(CACHE_TTL_S),
    }


# ------------------------------------------------------------------ preconditions

async def _resolve_ipv4(host: str) -> list[str]:
    """IPv4 addresses of a name or literal (resolved by this host, the name is not sent)."""
    try:
        return [str(ipaddress.IPv4Address(host))]
    except ValueError:
        pass
    loop = asyncio.get_running_loop()
    infos = await asyncio.wait_for(
        loop.getaddrinfo(host, None, family=socket.AF_INET, type=socket.SOCK_STREAM), TIMEOUT_S,
    )
    seen: list[str] = []
    for info in infos:
        addr = str(info[4][0])
        if addr not in seen:
            seen.append(addr)
    return seen


async def target(server: GameServer) -> tuple[str | None, dict | None]:
    """`(ipv4, None)` when a check can run, otherwise `(None, reason)` — and then nothing is sent.

    A loopback bind, a missing public address, an address that is not on the internet: in all of
    these the services would either say something wrong or receive a private address.
    """
    if bind_scope(server.bind_address) == "loopback":
        return None, note(
            "reachability.bound_to_loopback",
            "The ports are bound to {address}: this server is not reachable from outside by "
            "design (for a tunnel or a proxy on this host). Nothing was sent.",
            address=server.bind_address,
        )
    host = public_address(server)["host"]
    if not host:
        return None, note(
            "reachability.no_public_address",
            "No public address is set (Settings → Network, or on the server's edit page), so "
            "there is nothing to check from outside. Nothing was sent.",
        )
    try:
        addresses = await _resolve_ipv4(host.strip("[]"))
    except (OSError, asyncio.TimeoutError) as e:
        return None, note(
            "reachability.unresolvable",
            "The public address {host} does not resolve to an IPv4 address on this host ({reason}). "
            "Nothing was sent.",
            host=host, reason=str(e)[:200],
        )
    usable = [a for a in addresses if ipaddress.IPv4Address(a).is_global]
    if not usable:
        return None, note(
            "reachability.not_on_internet",
            "The public address {host} has no public IPv4 address ({addresses}): the services "
            "check IPv4 internet addresses only. Nothing was sent.",
            host=host, addresses=", ".join(addresses) or "-",
        )
    return usable[0], None


# ------------------------------------------------------------------ the services

def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=TIMEOUT_S, headers={"User-Agent": USER_AGENT},
                             transport=_transport, follow_redirects=False)


async def _cached(key: tuple, fetch):
    now = time.monotonic()
    hit = _cache.get(key)
    if hit is not None and now - hit[0] < CACHE_TTL_S:
        return hit[1]
    value = await fetch()
    _cache[key] = (time.monotonic(), value)
    return value


def clear_cache() -> None:
    _cache.clear()


async def _steam(ip: str) -> list[dict]:
    """Servers Steam lists at this IPv4 address: `[{"gameport", "appid", …}]`."""
    async def fetch():
        async with _client() as c:
            r = await c.get(STEAM_URL, params={"addr": ip})
            r.raise_for_status()
            body = r.json().get("response") or {}
        if not body.get("success"):
            raise ValueError(str(body.get("message") or "the answer says success=false")[:200])
        return list(body.get("servers") or [])
    return await _cached(("steam", ip), fetch)


async def _mcstatus(ip: str, port: int) -> bool:
    """Does a Minecraft status ping from the internet get an answer?"""
    async def fetch():
        async with _client() as c:
            r = await c.get(f"{_base(settings.reachability_mcstatus_url)}/v2/status/java/{ip}:{port}")
            r.raise_for_status()
            return bool(r.json().get("online"))
    return await _cached(("mcstatus", ip, port), fetch)


async def _portcheck(ip: str, ports: tuple[int, ...]) -> dict[int, bool]:
    """Which TCP ports accept a connection from the internet."""
    async def fetch():
        async with _client() as c:
            r = await c.post(f"{_base(settings.reachability_portcheck_url)}/api/query",
                             json={"host": ip, "ports": list(ports)})
            r.raise_for_status()
            body = r.json()
        if body.get("error"):
            raise ValueError(str(body.get("msg") or "the answer says error=true")[:200])
        return {int(c["port"]): bool(c["status"]) for c in body.get("check") or []}
    return await _cached(("portcheck", ip, ports), fetch)


def _service_error(service: str, e: BaseException) -> dict:
    reason = "timeout" if isinstance(e, (httpx.TimeoutException, asyncio.TimeoutError)) else str(e)[:200]
    return note(
        "reachability.service_failed",
        "{service} did not give a usable answer ({reason}). Try again later, or switch the "
        "service off.",
        service=service, reason=reason or type(e).__name__,
    )


# ------------------------------------------------------------------ the check

async def _not_running(server: GameServer) -> dict | None:
    try:
        state = await docker.state(server.container_name)
    except Exception as e:  # noqa: BLE001 — unknown is a reason to send nothing
        return note(
            "reachability.state_unknown",
            "Docker could not be asked whether the server is running ({reason}). Nothing was sent.",
            reason=str(e)[:200],
        )
    if state.get("running"):
        return None
    return note(
        "reachability.not_running",
        "The server is not running. Start it, wait until it is up, then check again.",
    )


def _row(p: Port, service: str, status: str, detail: dict) -> dict:
    return {**p.as_dict(), "service": service, "status": status, "detail": detail}


async def check(server: GameServer) -> dict:
    """Run the check for every port. The caller has checked `GSS_REACHABILITY_PROBES`.

    Per port `status`: `reachable`, `unreachable`, `listed`, `not_listed`, `not_checkable`,
    `service_off` or `error`, with a coded `detail` that says what it means.
    """
    ip, reason = await target(server)
    if ip is None:
        return {"ran": False, "reason": reason, "address": None, "ports": []}
    reason = await _not_running(server)
    if reason is not None:
        # A stopped server answers nobody: every service would report "unreachable".
        return {"ran": False, "reason": reason, "address": None, "ports": []}

    rows = plan(server)
    ports = {(r["port"], r["proto"], r["name"]): Port(r["name"], r["port"], r["proto"]) for r in rows}
    wanted = {r["service"] for r in rows if r["state"] == "on"}

    async def run(service, coro):
        try:
            return service, await coro, None
        except Exception as e:  # noqa: BLE001 — a failed service is a result, not a 500
            return service, None, e

    jobs = []
    if "steam" in wanted:
        jobs.append(run("steam", _steam(ip)))
    mc_port = next((r["port"] for r in rows if r["service"] == "mcstatus" and r["state"] == "on"), None)
    if mc_port is not None:
        jobs.append(run("mcstatus", _mcstatus(ip, mc_port)))
    tcp = tuple(sorted({r["port"] for r in rows if r["service"] == "portcheck" and r["state"] == "on"}))
    if tcp:
        jobs.append(run("portcheck", _portcheck(ip, tcp)))
    answers = {service: (value, error) for service, value, error in await asyncio.gather(*jobs)}

    names = {s.id: s.name for s in services()}
    out: list[dict] = []
    for r in rows:
        p = ports[(r["port"], r["proto"], r["name"])]
        service = r["service"]
        if service == "none":
            out.append(_row(p, service, "not_checkable", note(
                "reachability.udp_not_checkable",
                "UDP port {port} cannot be checked from outside: a UDP port that does not answer "
                "looks the same as a blocked one. Ask a player to connect.",
                port=p.port,
            )))
            continue
        if r["state"] == "off":
            out.append(_row(p, service, "service_off", note(
                "reachability.service_off",
                "Not checked: {service} is switched off in this installation.",
                service=names[service],
            )))
            continue
        value, error = answers.get(service, (None, None))
        if error is not None:
            out.append(_row(p, service, "error", _service_error(names[service], error)))
        elif service == "steam":
            out.append(_steam_row(server, p, value or [], ip))
        elif service == "mcstatus":
            out.append(_row(p, service, "reachable", note(
                "reachability.minecraft_online",
                "mcstatus.io reached the Minecraft server at {address} from the internet.",
                address=f"{ip}:{p.port}",
            )) if value else _row(p, service, "unreachable", note(
                "reachability.minecraft_offline",
                "mcstatus.io got no answer from {address}. Check the firewall, the router's port "
                "forwarding and whether the server is fully started.",
                address=f"{ip}:{p.port}",
            )))
        else:
            is_open = (value or {}).get(p.port)
            out.append(_row(p, service, "reachable", note(
                "reachability.tcp_open",
                "portchecker.io connected to {address} from the internet.",
                address=f"{ip}:{p.port}",
            )) if is_open else _row(p, service, "unreachable", note(
                "reachability.tcp_closed",
                "portchecker.io could not connect to {address}. Check the firewall and the "
                "router's port forwarding.",
                address=f"{ip}:{p.port}",
            )))
    return {"ran": True, "reason": None, "address": ip, "ports": out}


def _steam_row(server: GameServer, p: Port, listed: list[dict], ip: str) -> dict:
    if any(int(s.get("gameport") or 0) == p.port or str(s.get("addr") or "").endswith(f":{p.port}")
           for s in listed):
        return _row(p, "steam", "listed", note(
            "reachability.steam_listed",
            "Steam lists this server at {address}: it runs and is announced at this address. "
            "Players connect directly, so a blocked port can still stop them.",
            address=f"{ip}:{p.port}",
        ))
    others = sorted({int(s.get("gameport") or 0) for s in listed} - {0})
    if server.game_type == GameType.arma_reforger:
        return _row(p, "steam", "not_listed", note(
            "reachability.steam_not_listed_reforger",
            "Steam lists no server at {address}. Arma Reforger announces its servers through "
            "Bohemia's own list, so this proves nothing either way.",
            address=f"{ip}:{p.port}",
        ))
    return _row(p, "steam", "not_listed", note(
        "reachability.steam_not_listed",
        "Steam lists no server at {address} (other game ports at this address: {others}). A "
        "server set to public shows up a few minutes after its start; if it does not, players "
        "on the internet probably cannot see it either.",
        address=f"{ip}:{p.port}", others=", ".join(map(str, others)) or "-",
    ))
