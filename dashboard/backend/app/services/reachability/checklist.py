"""The reachability checklist of one server: ports, addresses and firewall commands.

Everything here is computed from the registry row and the installation's public address; no
network, no Docker. The panel turns the facts into sentences (both languages), the commands are
shell text and stay as they are.

**Docker and the host firewall.** A published port is forwarded by Docker's own NAT rules to the
container. That traffic goes through the FORWARD path, not INPUT, so `ufw allow 25565/tcp` (an
INPUT rule) neither opens nor closes it: a published port is usually reachable even while ufw
says "deny". What does filter it is the `DOCKER-USER` chain, which Docker evaluates before its
own rules, and a FORWARD/forward chain of the host's own ruleset. The commands follow that:

* ufw: `ufw route allow …` — only has an effect where ufw filters forwarded traffic for
  containers (the `ufw-docker` style setup that hands `DOCKER-USER` to ufw).
* iptables: an ACCEPT in `DOCKER-USER`, matched on the original destination port with conntrack,
  as Docker's documentation does it — for a `DOCKER-USER` that ends in a DROP.
* nftables: an accept in the host's own `forward` chain, for a ruleset with policy drop there.

The game ports are published with host port == container port (`container_spec`), so a rule on
the port number holds before and after Docker's address translation.
"""
from __future__ import annotations

import ipaddress
import json
import re
from dataclasses import dataclass

from ...models.server import GameServer
from .. import install_settings
from ..ports import ANY

_SPEC_RE = re.compile(r"^([0-9]{1,5})/(tcp|udp)$")


@dataclass(frozen=True)
class Port:
    """One entry of `game_ports`: its name ("game", "a2s", …), the number and the protocol."""

    name: str
    port: int
    proto: str

    def as_dict(self) -> dict:
        return {"name": self.name, "port": self.port, "proto": self.proto}


def ports_of(server: GameServer) -> list[Port]:
    """The published ports of the row, in port order. Malformed entries are left out: the port
    check (`services/ports.py`) refuses to save or start them anyway."""
    try:
        raw = json.loads(server.game_ports or "{}")
    except ValueError:
        return []
    out: list[Port] = []
    for name, spec in (raw.items() if isinstance(raw, dict) else ()):
        m = _SPEC_RE.fullmatch(spec) if isinstance(spec, str) else None
        if m and 1 <= int(m.group(1)) <= 65535:
            out.append(Port(str(name), int(m.group(1)), m.group(2)))
    return sorted(out, key=lambda p: (p.port, p.proto, p.name))


def bind_scope(bind_address: str | None) -> str:
    """Where the ports are published: `all` (every interface), `loopback` (this host only —
    not reachable from outside by design), `private` (one LAN address: a router has to forward
    to exactly that address) or `public` (one internet address of the host)."""
    raw = (bind_address or "").strip()
    if raw in ("", ANY, "::"):
        return "all"
    try:
        ip = ipaddress.ip_address(raw)
    except ValueError:
        return "all"   # cannot be stored (`ports.normalize_bind_address`); Docker's default
    if ip.is_loopback:
        return "loopback"
    return "public" if ip.is_global else "private"


def public_address(server: GameServer) -> dict:
    """The address players are given, where it comes from, and what kind of address it is.

    `kind`: `name` (a DNS name — resolved only when a check runs), `public_ip`, `private_ip`
    (a LAN, loopback or reserved address: nobody on the internet can use it) or `none`.
    """
    try:
        own = json.loads(server.extra or "{}").get("public_host")
    except (ValueError, AttributeError):
        own = None
    own = str(own or "").strip()
    host = own or install_settings.public_host()
    source = "server" if own else ("installation" if host else "none")
    if not host:
        kind = "none"
    else:
        try:
            ip = ipaddress.ip_address(host.strip("[]"))
        except ValueError:
            kind = "name"
        else:
            kind = "public_ip" if ip.is_global else "private_ip"
    return {"host": host, "source": source, "kind": kind}


def commands(ports: list[Port], bind_address: str | None) -> dict[str, list[str]]:
    """Ready-made commands per firewall, one line per port. Empty for a loopback bind: such a
    server is meant to be reached through a tunnel or a proxy on this host, and opening a port
    for it would open nothing."""
    if bind_scope(bind_address) == "loopback" or not ports:
        return {"ufw": [], "iptables": [], "nftables": []}
    ufw = [f"sudo ufw route allow proto {p.proto} from any to any port {p.port}" for p in ports]
    iptables: list[str] = []
    for tool in ("iptables", "ip6tables"):
        iptables += [
            f"sudo {tool} -I DOCKER-USER -p {p.proto} -m conntrack --ctorigdstport {p.port} "
            f"--ctdir ORIGINAL -j ACCEPT"
            for p in ports
        ]
    nftables = [f"sudo nft insert rule inet filter forward {p.proto} dport {p.port} accept" for p in ports]
    return {"ufw": ufw, "iptables": iptables, "nftables": nftables}


def checklist(server: GameServer) -> dict:
    """Everything the panel shows before any check runs."""
    ports = ports_of(server)
    bind = (server.bind_address or "").strip() or ANY
    return {
        "key": server.key,
        "game_type": server.game_type.value,
        "ports": [p.as_dict() for p in ports],
        "bind": {"address": bind, "scope": bind_scope(bind)},
        "public": public_address(server),
        "commands": commands(ports, bind),
    }
