"""Host ports of a game server: the bind address, the format of `game_ports`, and conflicts.

`container_spec.build_create_payload` publishes every entry of `game_ports` with host port ==
container port, on the server's `bind_address`. This module checks those values before they
reach Docker, so the admin gets a clear answer instead of Docker's "port is already allocated"
at the first start:

* **Format** (`check_format`): every entry is `<port>/<tcp|udp>` with a port from 1 to 65535,
  and no port appears twice in one server.
* **Other servers** (`check_servers`): no other registry row publishes the same port, protocol
  and an overlapping address. Every row counts, a switched-off one too: two servers on one port
  can never run side by side, and the admin finds out now rather than at the next start.
* **Other containers** (`check_containers`): no RUNNING container publishes it (the Docker
  container list through the socket proxy), the dashboard's own containers included. The
  server's own container is left out: it holds its ports already.

Where: create and save (`routers/servers.py`), "Create container" (`provisioning.provision`)
and start (`routers/server.perform_action`). A program on the host that is not a container is
invisible from here; Docker's own refusal at start is translated by `docker_error`.

**Bind address.** `0.0.0.0` (the default) leaves `HostIp` unset, which is Docker's own default:
every interface, IPv4 and IPv6. `127.0.0.1` (or `::1`) publishes the port on loopback only,
for a server that is reached through a tunnel or a proxy on the same host; any other address
publishes it on that one interface. The address must exist on the host, otherwise Docker
refuses to start the container.
"""
from __future__ import annotations

import ipaddress
import json
import logging
import re
from dataclasses import dataclass

from fastapi import HTTPException, status
from sqlmodel import Session, select

from ..errors import err
from ..models.server import GameServer

log = logging.getLogger("uvicorn.error")

# Docker's default: no `HostIp` → every interface.
ANY = "0.0.0.0"


def normalize_bind_address(value: str | None) -> str:
    """The bind address as stored: a plain IPv4 or IPv6 address, `0.0.0.0` when empty.

    A host name is refused: Docker takes an IP address here, and a name would have to be
    resolved on the host, not in the API container.
    """
    raw = (value or "").strip()
    if not raw:
        return ANY
    try:
        return str(ipaddress.ip_address(raw))
    except ValueError:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail=err(
                "server.bind_address_invalid",
                "'{value}' is not an IP address. Use 0.0.0.0 (all interfaces), 127.0.0.1 (this "
                "host only) or one address of the host.",
                value=raw[:64],
            ),
        ) from None


def host_ip(bind_address: str | None) -> str:
    """The `HostIp` for Docker: empty for "all interfaces" (Docker's default, IPv4 and IPv6)."""
    addr = (bind_address or "").strip()
    return "" if addr in ("", ANY) else addr


# ------------------------------------------------------------------ format

_SPEC_RE = re.compile(r"^([0-9]{1,5})/(tcp|udp)$")


def _format_error(name: str, spec: object) -> HTTPException:
    return HTTPException(
        status.HTTP_400_BAD_REQUEST,
        detail=err(
            "server.port_format",
            "Port '{name}' is '{spec}': write it as <port>/<tcp|udp> with a port from 1 to 65535, "
            "e.g. 25565/tcp.",
            name=str(name)[:32], spec=str(spec)[:32],
        ),
    )


def check_format(game_ports: dict) -> None:
    """Every entry `<port>/<tcp|udp>` (1–65535), no port and protocol twice in one server."""
    seen: dict[tuple[int, str], str] = {}
    for name, spec in (game_ports or {}).items():
        m = _SPEC_RE.fullmatch(spec) if isinstance(spec, str) else None
        if not str(name).strip() or m is None or not 1 <= int(m.group(1)) <= 65535:
            raise _format_error(name, spec)
        key = (int(m.group(1)), m.group(2))
        if key in seen:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                detail=err(
                    "server.port_duplicate",
                    "Ports '{name}' and '{other}' are both {port}: one host port can be published "
                    "only once.",
                    name=str(name)[:32], other=seen[key], port=spec,
                ),
            )
        seen[key] = str(name)[:32]


# ------------------------------------------------------------------ conflicts

_ANY_IPS = frozenset({"", ANY, "::"})


@dataclass(frozen=True)
class Binding:
    """One published host port: address (empty = every interface), port, protocol."""

    ip: str
    port: int
    proto: str

    def overlaps(self, other: Binding) -> bool:
        """Same port and protocol on a shared address. "Every interface" overlaps every address;
        two different specific addresses (127.0.0.1 and 192.0.2.10) do not."""
        if self.port != other.port or self.proto != other.proto:
            return False
        return self.ip in _ANY_IPS or other.ip in _ANY_IPS or self.ip == other.ip

    def label(self) -> str:
        """`0.0.0.0:25565/tcp`, `[::1]:2456/udp` — the same in every language."""
        ip = self.ip or ANY
        return f"{f'[{ip}]' if ':' in ip else ip}:{self.port}/{self.proto}"


def bindings_of(server: GameServer) -> list[Binding]:
    """What this row publishes. Malformed entries are skipped — `check_format` reports them."""
    try:
        game_ports = json.loads(server.game_ports or "{}")
    except ValueError:
        return []
    ip = host_ip(server.bind_address)
    out: list[Binding] = []
    for spec in game_ports.values() if isinstance(game_ports, dict) else ():
        m = _SPEC_RE.fullmatch(spec) if isinstance(spec, str) else None
        if m:
            out.append(Binding(ip, int(m.group(1)), m.group(2)))
    return out


def _in_use(mine: Binding, theirs: Binding, *, server: str = "", container: str = "") -> HTTPException:
    if server:
        detail = err(
            "server.port_in_use_server",
            "Port {port} is already used by server '{holder}' ({theirs}). Choose another port, or "
            "change or remove that server first.",
            port=mine.label(), holder=server, theirs=theirs.label(),
        )
    else:
        detail = err(
            "server.port_in_use_container",
            "Port {port} is already published by the running container '{holder}' ({theirs}). "
            "Choose another port, or stop that container first.",
            port=mine.label(), holder=container, theirs=theirs.label(),
        )
    return HTTPException(status.HTTP_409_CONFLICT, detail={**detail, "holder": server or container})


def check_servers(server: GameServer, session: Session) -> None:
    """No other registry row publishes an overlapping port (switched-off rows included)."""
    mine = bindings_of(server)
    if not mine:
        return
    others = session.exec(select(GameServer).where(GameServer.key != server.key)).all()
    for other in sorted(others, key=lambda o: o.key):
        for theirs in bindings_of(other):
            for b in mine:
                if b.overlaps(theirs):
                    raise _in_use(b, theirs, server=other.key)


def published_by_containers(containers: list[dict], *, exclude: set[str]) -> list[tuple[Binding, str]]:
    """`(binding, container name)` for every published port of the listed containers.

    `containers` is Docker's `GET /containers/json`. Exposed-only ports (no `PublicPort`) are
    not on the host and are skipped, as are the containers named in `exclude`.
    """
    out: list[tuple[Binding, str]] = []
    for c in containers or []:
        names = [str(n).lstrip("/") for n in (c.get("Names") or [])]
        if exclude & set(names):
            continue
        holder = names[0] if names else str(c.get("Id") or "")[:12]
        for p in c.get("Ports") or []:
            if not p.get("PublicPort"):
                continue
            out.append((Binding(str(p.get("IP") or ""), int(p["PublicPort"]), str(p.get("Type") or "tcp")), holder))
    return out


async def check_containers(server: GameServer, *, exclude: set[str] = frozenset(), strict: bool) -> None:
    """No running container (other than this server's own) publishes an overlapping port.

    `strict=False` (create and save of a row): if Docker cannot be asked, the check is skipped
    and logged — saving a row needs no Docker, and the check runs again before the container is
    created and started. `strict=True` (container create): Docker is needed for the next step
    anyway, so an unanswered question stops here.
    """
    mine = bindings_of(server)
    if not mine:
        return
    from .docker_client import docker

    try:
        containers = await docker.running_containers()
    except Exception as e:  # noqa: BLE001 — strict callers get the reason, the others a log line
        if strict:
            raise HTTPException(
                status.HTTP_502_BAD_GATEWAY,
                detail=err(
                    "server.port_check_failed",
                    "Docker could not be asked which ports are in use ({reason}).",
                    reason=str(e)[:300],
                ),
            ) from None
        log.warning("ports[%s]: the container list is not available (%s) — checked against the "
                    "registry only", server.key, e)
        return
    names = {n for n in ({server.container_name} | set(exclude)) if n}
    for theirs, holder in published_by_containers(containers, exclude=names):
        for b in mine:
            if b.overlaps(theirs):
                raise _in_use(b, theirs, container=holder)


async def check(server: GameServer, session: Session, *, exclude: set[str] = frozenset(),
                strict: bool = False) -> None:
    """Format, other servers, running containers — in this order (the clearest answer first)."""
    try:
        game_ports = json.loads(server.game_ports or "{}")
    except ValueError:
        game_ports = None
    if not isinstance(game_ports, dict):
        raise _format_error("game_ports", server.game_ports)
    check_format(game_ports)
    check_servers(server, session)
    await check_containers(server, exclude=exclude, strict=strict)


_DOCKER_PORT_ERRORS = ("port is already allocated", "address already in use")


def docker_error(server: GameServer, error: Exception) -> HTTPException | None:
    """Docker's refusal to bind a port as a coded answer — for the holder this module cannot
    see: a program on the host that is not a container. `None` for any other error."""
    text = str(error)
    if not any(marker in text.lower() for marker in _DOCKER_PORT_ERRORS):
        return None
    return HTTPException(
        status.HTTP_409_CONFLICT,
        detail=err(
            "server.port_in_use_host",
            "A port of '{key}' is already in use on the host ({reason}). Another program, not a "
            "container, holds it: choose another port or stop that program.",
            key=server.key, reason=text[:300],
        ),
    )
