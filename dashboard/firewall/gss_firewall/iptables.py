"""Talking to iptables: which backend the host uses, reading the two chains, applying a plan.

Docker writes its rules with the host's `iptables`, which is either the nf_tables variant
(`iptables-nft`, the default of current distributions) or the old `iptables-legacy`. Both can be
present at the same time, and rules written with the wrong one end up in a ruleset Docker does
not look at. So the backend is the one whose `filter` table has Docker's `DOCKER-USER` chain:

* legacy counts only if the kernel has the legacy `filter` table already
  (`/proc/net/ip_tables_names`). Asking `iptables-legacy` otherwise would make the kernel load
  the legacy table module — a change to the host just by looking.
* nft counts if `iptables-nft -S DOCKER-USER` answers.
* Neither, or both: the helper says so and changes nothing. `GSS_FIREWALL_BACKEND` (nft or
  legacy) settles the second case.

IPv6 is handled the same way with `ip6tables-*`. Without a `DOCKER-USER` chain there (Docker's
IPv6 firewall is off) the family is reported as absent and left alone.
"""
from __future__ import annotations

import subprocess
from collections.abc import Callable
from dataclasses import dataclass

from .rules import CHAIN, HOOK, FamilyState, chain_rules

Runner = Callable[[list[str], str | None], tuple[int, str, str]]
Reader = Callable[[str], str]

_TABLE_NAMES = {"ipv4": "/proc/net/ip_tables_names", "ipv6": "/proc/net/ip6_tables_names"}


class BackendError(RuntimeError):
    """iptables cannot be used as the helper needs it; the text says why, for the admin."""


def run(cmd: list[str], stdin: str | None = None) -> tuple[int, str, str]:
    """Run one iptables command (argument list, never a shell)."""
    try:
        done = subprocess.run(cmd, input=stdin, capture_output=True, text=True, timeout=30, check=False)
    except FileNotFoundError:
        return 127, "", f"{cmd[0]}: not installed"
    except subprocess.TimeoutExpired:
        return 124, "", f"{cmd[0]}: no answer within 30 seconds"
    return done.returncode, done.stdout, done.stderr


def read_file(path: str) -> str:
    try:
        with open(path, encoding="ascii", errors="replace") as fh:
            return fh.read()
    except OSError:
        return ""


@dataclass(frozen=True)
class Backend:
    name: str  # nft | legacy

    def tool(self, family: str, restore: bool = False) -> list[str]:
        base = ("ip6tables" if family == "ipv6" else "iptables") + f"-{self.name}"
        cmd = [base + ("-restore" if restore else "")]
        if self.name == "legacy":
            # Docker serialises its own legacy changes with the xtables lock; wait for it.
            cmd += ["-w", "5"]
        if restore:
            cmd.append("--noflush")
        return cmd


def _explain(name: str, family: str, stderr: str) -> str:
    text = stderr.strip().splitlines()[-1] if stderr.strip() else "no output"
    low = stderr.lower()
    if "permission denied" in low:
        need = "NET_ADMIN" + (" and NET_RAW (legacy iptables opens a raw socket)" if name == "legacy" else "")
        return f"{family}: iptables-{name} is not allowed to read the firewall ({text}). The container needs {need}."
    if "incompatible" in low:
        return (f"{family}: the filter table was written by the nft tool, not by iptables ({text}). "
                "Docker's nftables firewall backend has no DOCKER-USER chain; this helper works "
                "with Docker's iptables backend only.")
    return f"{family}: iptables-{name}: {text}"


def _has_hook(backend: Backend, family: str, runner: Runner, reader: Reader) -> tuple[bool, str]:
    if backend.name == "legacy" and "filter" not in reader(_TABLE_NAMES[family]).split():
        return False, f"{family}: the kernel has no legacy iptables filter table"
    rc, _out, err = runner(backend.tool(family) + ["-S", HOOK], None)
    if rc == 0:
        return True, ""
    if "no chain" in err.lower() or "does not exist" in err.lower():
        return False, f"{family}: iptables-{backend.name} has no {HOOK} chain"
    return False, _explain(backend.name, family, err)


def detect(setting: str, runner: Runner = run, reader: Reader = read_file) -> Backend:
    """The backend Docker uses on this host (see the module text). Raises BackendError."""
    setting = (setting or "auto").strip().lower()
    if setting in ("nft", "legacy"):
        backend = Backend(setting)
        found, why = _has_hook(backend, "ipv4", runner, reader)
        if not found:
            raise BackendError(f"GSS_FIREWALL_BACKEND={setting}, but {why}. Is Docker running "
                               f"with its iptables firewall, and does it use iptables-{setting}?")
        return backend
    if setting != "auto":
        raise BackendError(f"GSS_FIREWALL_BACKEND must be auto, nft or legacy, not {setting!r}")
    results = {name: _has_hook(Backend(name), "ipv4", runner, reader) for name in ("nft", "legacy")}
    found = [name for name, (ok, _why) in results.items() if ok]
    if len(found) == 1:
        return Backend(found[0])
    if len(found) == 2:
        raise BackendError("Both iptables-nft and iptables-legacy have a DOCKER-USER chain. Set "
                           "GSS_FIREWALL_BACKEND to the one Docker uses (`iptables --version` on "
                           "the host says nf_tables or legacy).")
    reasons = "; ".join(why for _ok, why in results.values())
    raise BackendError(f"No DOCKER-USER chain found ({reasons}). The helper needs Docker's iptables "
                       "firewall on this host (Docker creates DOCKER-USER when it starts).")


def read_family(backend: Backend, family: str, runner: Runner = run,
                reader: Reader = read_file) -> FamilyState:
    """The current DOCKER-USER and GSS-ALLOW of one family. Read-only."""
    found, why = _has_hook(backend, family, runner, reader)
    if not found:
        if family == "ipv4":
            raise BackendError(why)
        return FamilyState(hook_chain=False)
    rc, hook_out, err = runner(backend.tool(family) + ["-S", HOOK], None)
    if rc != 0:
        raise BackendError(_explain(backend.name, family, err))
    state = FamilyState(hook_chain=True, hook_lines=chain_rules(hook_out, HOOK))
    rc, chain_out, err = runner(backend.tool(family) + ["-S", CHAIN], None)
    if rc == 0:
        state.chain = True
        state.rules = chain_rules(chain_out, CHAIN)
    elif "no chain" not in err.lower() and "does not exist" not in err.lower():
        raise BackendError(_explain(backend.name, family, err))
    return state


def apply_script(backend: Backend, family: str, script: str, runner: Runner = run) -> None:
    """Feed one restore transaction to the backend. All or nothing."""
    rc, _out, err = runner(backend.tool(family, restore=True), script)
    if rc != 0:
        raise BackendError(_explain(backend.name, family, err))
