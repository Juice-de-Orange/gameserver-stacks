"""The rule logic of the firewall helper, without any I/O: desired state → rules → plan.

The helper owns exactly one chain, `GSS-ALLOW` in the `filter` table, and one rule outside it: the
jump `-A DOCKER-USER -j GSS-ALLOW`, first in `DOCKER-USER`. Everything else in the firewall
belongs to somebody else and is never written.

`GSS-ALLOW` holds ACCEPT rules only, one per (port, protocol, address, source network). A packet
that matches none of them leaves the chain at its end and continues in `DOCKER-USER` with the
next rule, exactly as if the helper were not there. So the helper can open a published game port
past a DROP rule of the admin's, but it can never block anything: SSH, other containers and the
admin's own rules behave as before.

Ports are matched on the ORIGINAL destination (`-m conntrack --ctorigdstport`), i.e. the host
port the packet was sent to, not the container port after Docker's DNAT — that is how Docker's
own documentation matches published ports in `DOCKER-USER`. `--ctstate DNAT` limits a rule to
connections that went through a port publication: a packet sent straight to a container address
on the same port number is not accepted by it, so Docker's own isolation between networks and
for unpublished ports stays in force.

The rule texts are written exactly as `iptables -S` prints them, so the current chain and the
desired one compare as plain strings. A rule the helper did not write in that form (or a
duplicate) shows up as "remove" and is replaced by the canonical one.
"""
from __future__ import annotations

import hashlib
import ipaddress
import json
import re
from collections import Counter
from dataclasses import dataclass, field

CHAIN = "GSS-ALLOW"
HOOK = "DOCKER-USER"
HOOK_RULE = f"-A {HOOK} -j {CHAIN}"
FAMILIES = ("ipv4", "ipv6")
PROTOS = ("tcp", "udp")
DEFAULT_PORT_RANGE = (1024, 65535)

# Limits of one desired state. A host with more published game ports than this is not the
# use case; a request above them is refused rather than truncated.
MAX_ALLOWS = 512
MAX_SOURCES = 32

# A server key as the dashboard writes it (`pz-1`, `mc-survival`). Only used in reports, never
# in a rule.
_LABEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


class Refused(ValueError):
    """The desired state asks for something the helper does not do. `problems` lists each."""

    def __init__(self, problems: list[str]):
        super().__init__("; ".join(problems))
        self.problems = problems


@dataclass(frozen=True, order=True)
class Allow:
    """One published game port to let through: port, protocol, the host address it is
    published on (empty: every address) and the server it belongs to (for reports only)."""

    port: int
    proto: str
    address: str = ""
    server: str = ""

    def key(self) -> tuple[int, str, str, str]:
        return (self.port, self.proto, self.address, self.server)

    def to_json(self) -> dict:
        return {"server": self.server, "port": self.port, "proto": self.proto, "address": self.address}


@dataclass(frozen=True)
class Desired:
    """What the dashboard wants open. `sources` empty: from anywhere."""

    allows: tuple[Allow, ...] = ()
    sources: tuple[str, ...] = ()

    def to_json(self) -> dict:
        return {"allows": [a.to_json() for a in self.allows], "sources": list(self.sources)}


def parse_port_range(text: str | None) -> tuple[int, int]:
    """`1024-65535` → (1024, 65535). Empty: the default. Anything else raises ValueError."""
    raw = (text or "").strip()
    if not raw:
        return DEFAULT_PORT_RANGE
    m = re.fullmatch(r"(\d{1,5})-(\d{1,5})", raw)
    if not m or not 1 <= int(m.group(1)) <= int(m.group(2)) <= 65535:
        raise ValueError(f"GSS_FIREWALL_PORT_RANGE must look like 1024-65535, not {raw!r}")
    return int(m.group(1)), int(m.group(2))


def family_of(address: str) -> str:
    return "ipv6" if ":" in address else "ipv4"


def _address(value: object, where: str, problems: list[str]) -> str:
    raw = str(value or "").strip()
    if raw in ("", "0.0.0.0", "::"):
        return ""
    try:
        addr = ipaddress.ip_address(raw)
    except ValueError:
        problems.append(f"{where}: address {raw[:64]!r} is not an IP address")
        return ""
    if addr.is_loopback or addr.is_unspecified or addr.is_multicast:
        problems.append(f"{where}: address {raw[:64]} cannot be reached from outside")
        return ""
    return str(addr)


def _source(value: object, problems: list[str]) -> str:
    raw = str(value or "").strip()
    try:
        return str(ipaddress.ip_network(raw, strict=False))
    except ValueError:
        problems.append(f"source {raw[:64]!r} is not an IP network (e.g. 198.51.100.0/24)")
        return ""


def parse_desired(payload: object, port_range: tuple[int, int] = DEFAULT_PORT_RANGE) -> Desired:
    """Validate a desired state from the API. Raises `Refused` with every problem found.

    The helper's positive list: it only ever opens `<port>/<tcp|udp>` within `port_range`
    (GSS_FIREWALL_PORT_RANGE). A port outside it — 22 for SSH, say — is refused, whatever the
    dashboard sends.
    """
    if not isinstance(payload, dict):
        raise Refused(["the desired state must be a JSON object"])
    raw_allows = payload.get("allows", [])
    raw_sources = payload.get("sources", [])
    if not isinstance(raw_allows, list) or not isinstance(raw_sources, list):
        raise Refused(["'allows' and 'sources' must be lists"])
    if len(raw_allows) > MAX_ALLOWS or len(raw_sources) > MAX_SOURCES:
        raise Refused([f"at most {MAX_ALLOWS} allows and {MAX_SOURCES} sources"])
    problems: list[str] = []
    allows: set[Allow] = set()
    low, high = port_range
    for i, item in enumerate(raw_allows):
        where = f"allows[{i}]"
        if not isinstance(item, dict):
            problems.append(f"{where}: must be an object")
            continue
        server = str(item.get("server") or "")
        port = item.get("port")
        proto = item.get("proto")
        if not _LABEL.fullmatch(server):
            problems.append(f"{where}: server {server[:64]!r} is not a server key")
        if proto not in PROTOS:
            problems.append(f"{where}: protocol {str(proto)[:16]!r} is not tcp or udp")
        if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
            problems.append(f"{where}: port {str(port)[:16]!r} is not a port number")
            continue
        if not low <= port <= high:
            problems.append(f"{where}: port {port} is outside the allowed range {low}-{high} "
                            f"(GSS_FIREWALL_PORT_RANGE)")
            continue
        address = _address(item.get("address"), where, problems)
        allows.add(Allow(port, str(proto), address, server))
    sources = sorted({s for s in (_source(v, problems) for v in raw_sources) if s})
    if problems:
        raise Refused(problems)
    return Desired(tuple(sorted(allows)), tuple(sources))


def _sources_for(desired: Desired, family: str) -> list[str] | None:
    """The source networks of one family: `[""]` = from anywhere, `None` = none of this family
    (then nothing is opened in it — the admin restricted access to the other family)."""
    if not desired.sources:
        return [""]
    mine = [s for s in desired.sources if family_of(s) == family]
    if not mine:
        return None
    if any(ipaddress.ip_network(s).prefixlen == 0 for s in mine):
        return [""]
    return mine


def rule_for(allow: Allow, source: str) -> str:
    """One rule, exactly as `iptables -S` prints it."""
    parts = [f"-A {CHAIN}"]
    if source:
        parts.append(f"-s {source}")
    parts.append(f"-p {allow.proto} -m conntrack --ctstate DNAT")
    if allow.address:
        parts.append(f"--ctorigdst {allow.address}")
    parts.append(f"--ctorigdstport {allow.port} --ctdir ORIGINAL -j ACCEPT")
    return " ".join(parts)


def rules_for(desired: Desired, family: str) -> list[str]:
    """The content of `GSS-ALLOW` for one family: sorted, without duplicates."""
    sources = _sources_for(desired, family)
    if sources is None:
        return []
    out: set[str] = set()
    for allow in desired.allows:
        if allow.address and family_of(allow.address) != family:
            continue
        for source in sources:
            out.add(rule_for(allow, source))
    return sorted(out)


def allows_by_rule(desired: Desired, family: str) -> dict[str, list[Allow]]:
    """Which allows a rule serves — to name the servers in a plan."""
    sources = _sources_for(desired, family)
    found: dict[str, list[Allow]] = {}
    if sources is None:
        return found
    for allow in desired.allows:
        if allow.address and family_of(allow.address) != family:
            continue
        for source in sources:
            found.setdefault(rule_for(allow, source), []).append(allow)
    return found


def chain_rules(listing: str, chain: str) -> list[str]:
    """The `-A <chain> …` lines of `iptables -S <chain>`, in order."""
    prefix = f"-A {chain} "
    return [line.strip() for line in listing.splitlines() if line.strip().startswith(prefix)]


@dataclass
class FamilyState:
    """What one family's `filter` table holds, as far as the helper cares."""

    hook_chain: bool                # DOCKER-USER exists (Docker's iptables firewall is on)
    chain: bool = False             # GSS-ALLOW exists
    rules: list[str] = field(default_factory=list)       # `-A GSS-ALLOW …`
    hook_lines: list[str] = field(default_factory=list)  # every `-A DOCKER-USER …`, in order

    def hook_positions(self) -> list[int]:
        """1-based positions of the helper's jump in DOCKER-USER."""
        return [i + 1 for i, line in enumerate(self.hook_lines) if line == HOOK_RULE]

    def to_json(self) -> dict:
        return {"hook_chain": self.hook_chain, "chain": self.chain, "rules": list(self.rules),
                "hook_positions": self.hook_positions()}


@dataclass
class FamilyPlan:
    create_chain: bool = False
    hook: str = "ok"                # ok | insert | move | dedupe
    add: list[str] = field(default_factory=list)
    remove: list[str] = field(default_factory=list)
    keep: list[str] = field(default_factory=list)

    def changes(self) -> int:
        return int(self.create_chain) + int(self.hook != "ok") + len(self.add) + len(self.remove)

    def to_json(self) -> dict:
        return {"create_chain": self.create_chain, "hook": self.hook, "add": list(self.add),
                "remove": list(self.remove), "keep": list(self.keep), "changes": self.changes()}


def plan_family(state: FamilyState, wanted: list[str]) -> FamilyPlan:
    """What turns `state` into `wanted` (and exactly one jump, first in DOCKER-USER).

    The order inside GSS-ALLOW does not matter (every rule is an ACCEPT), so the chain is
    compared as a multiset: a rule that is not wanted, and every second copy of one that is,
    goes; a wanted rule that is missing is added. Nothing else is listed, so nothing else is
    touched.
    """
    plan = FamilyPlan(create_chain=not state.chain)
    have = Counter(state.rules)
    want = set(wanted)
    for rule, count in have.items():
        extra = count - (1 if rule in want else 0)
        plan.remove.extend([rule] * extra)
    plan.add = sorted(want - set(have))
    plan.keep = sorted(want & set(have))
    positions = state.hook_positions()
    if positions == [1]:
        plan.hook = "ok"
    elif not positions:
        plan.hook = "insert"
    elif len(positions) == 1:
        plan.hook = "move"
    else:
        plan.hook = "dedupe"
    return plan


def restore_script(state: FamilyState, plan: FamilyPlan) -> str:
    """The plan as input for `iptables-restore --noflush`: one transaction, so the chain is
    never half-written and the jump is never missing while it moves.

    `-D` is given the full rule, never a rule number: a number would hit a different rule if
    Docker or the admin changed the chain in between.
    """
    lines = ["*filter"]
    if plan.create_chain:
        # Only for a chain that does not exist yet: `--noflush` FLUSHES an existing user chain
        # that is declared again.
        lines.append(f":{CHAIN} - [0:0]")
    lines += ["-D" + rule[2:] for rule in plan.remove]
    lines += plan.add
    if plan.hook != "ok":
        lines += [f"-D {HOOK} -j {CHAIN}"] * len(state.hook_positions())
        lines.append(f"-I {HOOK} 1 -j {CHAIN}")
    lines.append("COMMIT")
    return "\n".join(lines) + "\n"


def plan_id(desired: Desired, states: dict[str, FamilyState | None]) -> str:
    """A fingerprint of the desired state AND the current rules. Apply takes the id of the
    preview it confirms; if either side changed since, the ids differ and apply refuses."""
    blob = json.dumps(
        {"desired": desired.to_json(),
         "states": {f: (s.to_json() | {"hook_lines": s.hook_lines}) if s else None
                    for f, s in sorted(states.items())}},
        sort_keys=True,
    )
    return hashlib.sha256(blob.encode()).hexdigest()[:20]
