"""End-to-end check of the helper against a real iptables — inside a THROWAWAY network namespace.

Never run this on a host: it creates a fake DOCKER-USER chain and foreign rules and changes
them. `netns-check.sh` (next to this directory) starts it in a container with a network namespace
of its own (`--network none`) and NET_ADMIN only; the capability then covers that namespace and
nothing else.

Each check prints `PASS <what>`; the first failure stops with `FAIL <what>` and exit code 1.
"""
from __future__ import annotations

import http.client
import json
import os
import secrets
import socket
import subprocess
import sys
import tempfile
import threading

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from gss_firewall import iptables  # noqa: E402
from gss_firewall.rules import parse_port_range  # noqa: E402
from gss_firewall.service import Helper, UnixHTTPServer, make_handler  # noqa: E402

TOOLS = {"ipv4": "iptables-nft", "ipv6": "ip6tables-nft"}


def sh(*cmd: str) -> str:
    done = subprocess.run(list(cmd), capture_output=True, text=True, check=False)
    if done.returncode:
        raise SystemExit(f"FAIL command {' '.join(cmd)}: {done.stderr.strip()}")
    return done.stdout


def listing(family: str) -> list[str]:
    """`iptables -S` of the whole filter table (rules have no counters there)."""
    return sh(TOOLS[family], "-S").splitlines()


def foreign(family: str) -> list[str]:
    """Every line that is not the helper's: what must never change."""
    return [line for line in listing(family) if "GSS-ALLOW" not in line]


def check(what: str, ok: bool, detail: object = "") -> None:
    if not ok:
        print(f"FAIL {what}" + (f": {detail}" if detail != "" else ""))
        sys.exit(1)
    print(f"PASS {what}")


class Client:
    def __init__(self, path: str, token: str):
        self.path, self.token = path, token

    def call(self, method: str, url: str, body: dict | None = None, token: str | None = None) -> tuple[int, dict]:
        conn = http.client.HTTPConnection("localhost")
        conn.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        conn.sock.connect(self.path)
        headers = {"Content-Type": "application/json"}
        tok = self.token if token is None else token
        if tok:
            headers["Authorization"] = f"Bearer {tok}"
        conn.request(method, url, body=json.dumps(body) if body is not None else None, headers=headers)
        res = conn.getresponse()
        data = json.loads(res.read() or b"{}")
        conn.close()
        return res.status, data


def allow(server: str, port: int, proto: str, address: str = "") -> dict:
    return {"server": server, "port": port, "proto": proto, "address": address}


def main() -> None:
    print("netns:", os.readlink("/proc/self/ns/net"))
    print("legacy tables in this namespace (/proc/net/ip_tables_names):",
          repr(iptables.read_file("/proc/net/ip_tables_names").strip()))

    # ---- what Docker and the admin would have: DOCKER-USER with a default-deny, foreign rules
    for family, tool in TOOLS.items():
        sh(tool, "-N", "DOCKER-USER")
        sh(tool, "-A", "FORWARD", "-j", "DOCKER-USER")
        sh(tool, "-A", "DOCKER-USER", "-p", "tcp", "--dport", "22", "-j", "ACCEPT")
        sh(tool, "-A", "DOCKER-USER", "-i", "eth0", "-j", "DROP")
        sh(tool, "-A", "DOCKER-USER", "-j", "RETURN")
        sh(tool, "-A", "INPUT", "-p", "tcp", "--dport", "22", "-j", "ACCEPT")
        # Only matches and targets that Docker uses itself. A LOG or REJECT target would make the
        # kernel load its module if it is not loaded yet, and modules are not per namespace.
        sh(tool, "-A", "INPUT", "-p", "tcp", "--dport", "22", "-s",
           "2001:db8:ff::/48" if family == "ipv6" else "192.0.2.0/24", "-j", "DROP")
    before = {f: foreign(f) for f in TOOLS}

    # ---- backend detection: auto finds nft; legacy is refused without asking the kernel for it
    check("auto-detect picks iptables-nft", iptables.detect("auto").name == "nft")
    try:
        iptables.detect("legacy")
        check("legacy is refused when the kernel has no legacy filter table", False)
    except iptables.BackendError as e:
        check("legacy is refused when the kernel has no legacy filter table", "legacy" in str(e), e)
        print("     message:", e)

    state_dir = tempfile.mkdtemp()
    sock = os.path.join(state_dir, "helper.sock")
    token = secrets.token_hex(32)
    helper = Helper("auto", parse_port_range("1024-65535"), os.path.join(state_dir, "desired.json"))
    server = UnixHTTPServer(sock, make_handler(helper, token))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    api = Client(sock, token)
    mode = os.stat(sock).st_mode & 0o777
    check("the socket is 0666 (the volume limits who sees it, the token who is answered)",
          mode == 0o666, oct(mode))

    # ---- the token
    desired = {"allows": [allow("mc-1", 25565, "tcp"), allow("vh-1", 2456, "udp"), allow("vh-1", 2457, "udp")],
               "sources": []}
    status, body = api.call("POST", "/v1/plan", {"desired": desired}, token="")
    check("no token -> 401", status == 401 and body["error"] == "token", (status, body))
    status, body = api.call("POST", "/v1/plan", {"desired": desired}, token="x" * 64)
    check("wrong token -> 401", status == 401, (status, body))
    status, body = api.call("GET", "/v1/state", token="")
    check("state without token -> 401", status == 401, (status, body))
    check("a refused request changed nothing", {f: foreign(f) for f in TOOLS} == before
          and not any("GSS-ALLOW" in line for f in TOOLS for line in listing(f)))

    # ---- preview: the diff, and nothing written
    snapshot = {f: listing(f) for f in TOOLS}
    status, plan = api.call("POST", "/v1/plan", {"desired": desired})
    check("plan -> 200", status == 200, (status, plan))
    p4 = plan["families"]["ipv4"]["plan"]
    print("     plan ipv4:", json.dumps(p4))
    check("plan: create chain, insert the jump, add 3 rules",
          p4["create_chain"] and p4["hook"] == "insert" and len(p4["add"]) == 3 and not p4["remove"], p4)
    check("plan: every port 'opens'", all(a["state"] == "opens" for a in plan["allows"]), plan["allows"])
    check("the preview wrote nothing", {f: listing(f) for f in TOOLS} == snapshot)

    # ---- apply needs the id of the preview it confirms
    status, body = api.call("POST", "/v1/apply", {"desired": desired, "plan_id": "0" * 20})
    check("apply with a stale plan id -> 409, nothing written",
          status == 409 and body["error"] == "plan_changed" and {f: listing(f) for f in TOOLS} == snapshot,
          (status, body))
    status, body = api.call("POST", "/v1/apply", {"desired": desired, "plan_id": plan["plan_id"]})
    check("apply -> 200", status == 200, (status, body))
    for family, tool in TOOLS.items():
        chain = sh(tool, "-S", "GSS-ALLOW").splitlines()
        print(f"     {family} GSS-ALLOW:", *chain, sep="\n       ")
        check(f"{family}: GSS-ALLOW holds exactly the 3 allow rules",
              chain[1:] == sorted(chain[1:]) and len(chain) == 4 and all(r.endswith("-j ACCEPT") for r in chain[1:]),
              chain)
        hook = sh(tool, "-S", "DOCKER-USER").splitlines()
        print(f"     {family} DOCKER-USER:", *hook, sep="\n       ")
        check(f"{family}: the jump is in DOCKER-USER once, first",
              [i for i, line in enumerate(hook) if line == "-A DOCKER-USER -j GSS-ALLOW"] == [1], hook)
        check(f"{family}: foreign rules (port 22 included) untouched", foreign(family) == before[family])

    # ---- idempotent: a second round is no change
    snapshot = {f: listing(f) for f in TOOLS}
    status, plan = api.call("POST", "/v1/plan", {"desired": desired})
    check("second plan: 0 changes, every port 'open'",
          plan["changes"] == 0 and all(a["state"] == "open" for a in plan["allows"]), plan)
    status, body = api.call("POST", "/v1/apply", {"desired": desired, "plan_id": plan["plan_id"]})
    check("second apply: 0 changes and the ruleset is identical",
          status == 200 and body["applied"] == 0 and {f: listing(f) for f in TOOLS} == snapshot, body)
    check("reconcile of the stored state: 0 changes", helper.reconcile() == 0)

    # ---- the positive list: port 22 (outside 1024-65535) is refused, whatever the API sends
    status, body = api.call("POST", "/v1/plan", {"desired": {"allows": [allow("evil", 22, "tcp")], "sources": []}})
    check("a desired port 22 -> 400 refused", status == 400 and body["error"] == "refused", (status, body))
    status, body = api.call("POST", "/v1/plan", {"desired": {"allows": [allow("x", 30000, "icmp")], "sources": []}})
    check("a protocol other than tcp/udp -> 400 refused", status == 400 and body["error"] == "refused", (status, body))
    check("refusals changed nothing", {f: listing(f) for f in TOOLS} == snapshot)

    # ---- a disabled/removed server: its ports close, the others stay
    smaller = {"allows": [allow("mc-1", 25565, "tcp")], "sources": []}
    status, plan = api.call("POST", "/v1/plan", {"desired": smaller})
    p4 = plan["families"]["ipv4"]["plan"]
    check("plan without vh-1: remove its 2 rules, closes names vh-1",
          len(p4["remove"]) == 2 and not p4["add"] and {c["server"] for c in plan["closes"]} == {"vh-1"}, plan)
    status, body = api.call("POST", "/v1/apply", {"desired": smaller, "plan_id": plan["plan_id"]})
    for family, tool in TOOLS.items():
        chain = sh(tool, "-S", "GSS-ALLOW").splitlines()[1:]
        check(f"{family}: only mc-1's port is left",
              len(chain) == 1 and "--ctorigdstport 25565" in chain[0] and "2456" not in " ".join(chain), chain)
        check(f"{family}: foreign rules untouched after closing", foreign(family) == before[family])

    # ---- the admin inserts a rule above the jump; the helper moves ITS jump back to the top
    sh("iptables-nft", "-I", "DOCKER-USER", "1", "-s", "203.0.113.0/24", "-j", "DROP")
    before["ipv4"] = foreign("ipv4")
    status, plan = api.call("POST", "/v1/plan", {"desired": smaller})
    check("plan: hook 'move' when a foreign rule sits above it",
          plan["families"]["ipv4"]["plan"]["hook"] == "move", plan["families"]["ipv4"])
    api.call("POST", "/v1/apply", {"desired": smaller, "plan_id": plan["plan_id"]})
    hook = sh("iptables-nft", "-S", "DOCKER-USER").splitlines()
    check("jump back at position 1, the admin's rule kept and in order",
          hook[1] == "-A DOCKER-USER -j GSS-ALLOW" and foreign("ipv4") == before["ipv4"], hook)

    # ---- a second jump (by hand) is removed, exactly one stays
    sh("iptables-nft", "-A", "DOCKER-USER", "-j", "GSS-ALLOW")
    status, plan = api.call("POST", "/v1/plan", {"desired": smaller})
    check("plan: hook 'dedupe' with two jumps", plan["families"]["ipv4"]["plan"]["hook"] == "dedupe")
    api.call("POST", "/v1/apply", {"desired": smaller, "plan_id": plan["plan_id"]})
    hook = sh("iptables-nft", "-S", "DOCKER-USER").splitlines()
    check("exactly one jump left, first", [i for i, line in enumerate(hook) if "GSS-ALLOW" in line] == [1], hook)

    # ---- drift: a rule deleted behind the helper's back comes back with the next reconcile
    sh("iptables-nft", "-F", "GSS-ALLOW")
    check("reconcile restores a flushed chain (1 change)", helper.reconcile() == 1)
    check("rule back", len(sh("iptables-nft", "-S", "GSS-ALLOW").splitlines()) == 2)
    check("foreign rules untouched after reconcile", foreign("ipv4") == before["ipv4"]
          and foreign("ipv6") == before["ipv6"])

    # ---- source networks and a specific host address, both families; idempotent as well
    scoped = {"allows": [allow("mc-1", 25565, "tcp", "192.0.2.10"), allow("v6-1", 30000, "udp", "2001:db8::10"),
                         allow("vh-1", 2456, "udp")],
              "sources": ["198.51.100.0/24", "2001:db8:1::/48"]}
    status, plan = api.call("POST", "/v1/plan", {"desired": scoped})
    api.call("POST", "/v1/apply", {"desired": scoped, "plan_id": plan["plan_id"]})
    for family, tool in TOOLS.items():
        print(f"     {family} GSS-ALLOW (scoped):", *sh(tool, "-S", "GSS-ALLOW").splitlines()[1:], sep="\n       ")
    v4 = sh("iptables-nft", "-S", "GSS-ALLOW")
    v6 = sh("ip6tables-nft", "-S", "GSS-ALLOW")
    check("ipv4: source and address in the rules, no IPv6 address",
          "-s 198.51.100.0/24" in v4 and "--ctorigdst 192.0.2.10" in v4 and "2001:db8" not in v4, v4)
    check("ipv6: its source and address, no IPv4 address",
          "-s 2001:db8:1::/48" in v6 and "--ctorigdst 2001:db8::10" in v6 and "192.0.2.10" not in v6, v6)
    status, plan = api.call("POST", "/v1/plan", {"desired": scoped})
    check("scoped rules are idempotent too (0 changes)", plan["changes"] == 0, plan["families"])

    # ---- the actual state, as the API reads it
    status, state = api.call("GET", "/v1/state")
    check("state: backend nft, stored desired = the last apply",
          status == 200 and state["backend"] == "nft" and len(state["stored"]["allows"]) == 3, state)

    # ---- without Docker's IPv6 firewall the family is reported absent and left alone
    sh("ip6tables-nft", "-D", "DOCKER-USER", "-j", "GSS-ALLOW")
    sh("ip6tables-nft", "-F", "GSS-ALLOW")
    sh("ip6tables-nft", "-X", "GSS-ALLOW")
    sh("ip6tables-nft", "-D", "FORWARD", "-j", "DOCKER-USER")
    sh("ip6tables-nft", "-F", "DOCKER-USER")
    sh("ip6tables-nft", "-X", "DOCKER-USER")
    status, plan = api.call("POST", "/v1/plan", {"desired": scoped})
    check("no IPv6 DOCKER-USER: ipv6 'absent', ipv4 still planned",
          plan["families"]["ipv6"]["status"] == "absent" and plan["families"]["ipv4"]["status"] == "ok", plan["families"])
    api.call("POST", "/v1/apply", {"desired": scoped, "plan_id": plan["plan_id"]})
    check("nothing was created in ipv6", not any("GSS-ALLOW" in line for line in listing("ipv6")))

    # ---- switching off: `remove` takes the jump and the chain out, nothing else
    server.shutdown()
    helper.remove()
    check("remove: no GSS-ALLOW left in ipv4", not any("GSS-ALLOW" in line for line in listing("ipv4")))
    check("remove: foreign rules untouched", foreign("ipv4") == before["ipv4"])
    check("remove: stored state forgotten", helper.stored() is None)

    # ---- the real entry point refuses to serve without a proper token
    env = {k: v for k, v in os.environ.items() if not k.startswith("GSS_")} | {
        "GSS_FIREWALL_TOKEN": "short", "PYTHONPATH": os.path.join(os.path.dirname(__file__), "..")}
    done = subprocess.run([sys.executable, "-m", "gss_firewall", "serve"], env=env, capture_output=True,
                          text=True, timeout=30, check=False)
    check("serve with a short token exits 2 and says why",
          done.returncode == 2 and "GSS_FIREWALL_TOKEN" in done.stderr, (done.returncode, done.stderr))
    print("ALL PASSED")


if __name__ == "__main__":
    main()
