"""An in-memory iptables for the firewall helper's tests: `-S <chain>` and `-restore --noflush`.

Just enough of the real behaviour that the helper's plan and apply can be checked without a
kernel: chains as lists of `-A …` lines, the restore commands the helper writes (`:CHAIN`, `-A`,
`-D <spec>`, `-I CHAIN 1`, `-F`, `-X`), one transaction per restore (all or nothing). The real
iptables is exercised by `dashboard/firewall/netns-check.sh` in a throwaway network namespace.
"""
from __future__ import annotations

import copy
import shlex
import sys
from pathlib import Path

# The helper is not part of the API image; its package lives next to the backend.
HELPER = Path(__file__).resolve().parents[2] / "firewall"
if str(HELPER) not in sys.path:
    sys.path.insert(0, str(HELPER))


class FakeIptables:
    def __init__(self, families=("ipv4", "ipv6"), backend="nft"):
        self.backend = backend
        self.tables = {f: {"FORWARD": [], "INPUT": []} for f in ("ipv4", "ipv6")}
        for f in families:
            self.tables[f]["DOCKER-USER"] = ["-A DOCKER-USER -j RETURN"]
        self.calls: list[list[str]] = []
        self.legacy_names = ""  # content of /proc/net/ip_tables_names

    def reader(self, path: str) -> str:
        return self.legacy_names if "ip_tables_names" in path or "ip6_tables_names" in path else ""

    def __call__(self, cmd: list[str], stdin: str | None) -> tuple[int, str, str]:
        self.calls.append(list(cmd))
        tool = cmd[0]
        family = "ipv6" if tool.startswith("ip6tables") else "ipv4"
        name = tool.split("-")[1]
        if name != self.backend:
            return 1, "", "iptables: No chain/target/match by that name."
        chains = self.tables[family]
        if tool.endswith("-restore"):
            return self._restore(family, stdin or "")
        chain = cmd[cmd.index("-S") + 1]
        if chain not in chains:
            return 1, "", "iptables: No chain/target/match by that name."
        return 0, "\n".join([f"-N {chain}", *chains[chain]]) + "\n", ""

    def _restore(self, family: str, script: str) -> tuple[int, str, str]:
        chains = copy.deepcopy(self.tables[family])
        for line in script.splitlines():
            if line in ("*filter", "COMMIT") or not line.strip():
                continue
            if line.startswith(":"):
                chains.setdefault(line[1:].split()[0], [])
                continue
            words = shlex.split(line)
            op, chain, rest = words[0], words[1], words[2:]
            if chain not in chains:
                return 1, "", f"iptables-restore: line failed: {line}"
            if op == "-A":
                chains[chain].append(" ".join(["-A", chain, *rest]))
            elif op == "-I":
                pos, spec = int(rest[0]), rest[1:]
                chains[chain].insert(pos - 1, " ".join(["-A", chain, *spec]))
            elif op == "-D":
                target = " ".join(["-A", chain, *rest])
                if target not in chains[chain]:
                    return 1, "", f"iptables-restore: line failed: {line}"
                chains[chain].remove(target)
            elif op == "-F":
                chains[chain] = []
            elif op == "-X":
                if any(f"-j {chain}" in r for rules in chains.values() for r in rules):
                    return 1, "", "chain is in use"
                del chains[chain]
            else:
                return 1, "", f"unsupported {op}"
        self.tables[family] = chains
        return 0, "", ""
