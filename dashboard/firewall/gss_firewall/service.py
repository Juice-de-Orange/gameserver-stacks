"""The helper process: a small HTTP API on a Unix socket, and the reconcile loop.

The dashboard API is the only client. It sends the desired state (the game ports of the enabled
servers, optional source networks); the helper answers with a plan (`POST /v1/plan`), applies a
plan the admin confirmed (`POST /v1/apply` with the plan's id) and reports what is there
(`GET /v1/state`). Every request needs `Authorization: Bearer <GSS_FIREWALL_TOKEN>`.

The last applied desired state is kept in a file and applied again every
GSS_FIREWALL_RECONCILE_SECONDS (and at start): after a host reboot the rules are gone, and a
firewall reload may drop them as well. The loop only ever restores what an admin applied.
"""
from __future__ import annotations

import hmac
import json
import logging
import os
import socketserver
import threading
from http.server import BaseHTTPRequestHandler

from . import iptables
from .rules import (
    FAMILIES,
    Desired,
    FamilyPlan,
    FamilyState,
    Refused,
    allows_by_rule,
    parse_desired,
    plan_family,
    plan_id,
    restore_script,
    rules_for,
)

log = logging.getLogger("console-firewall")

MAX_BODY = 64 * 1024
TOKEN_MIN_LENGTH = 32


class PlanChanged(RuntimeError):
    """The rules or the desired state changed between preview and apply."""


class Helper:
    """Plan, apply and report — serialised by one lock (requests and the loop share it)."""

    def __init__(self, backend_setting: str, port_range: tuple[int, int], state_path: str,
                 runner: iptables.Runner = iptables.run, reader: iptables.Reader = iptables.read_file):
        self.backend_setting = backend_setting
        self.port_range = port_range
        self.state_path = state_path
        self.runner = runner
        self.reader = reader
        self.lock = threading.Lock()

    # ------------------------------------------------------------ stored desired state

    def stored(self) -> Desired | None:
        try:
            with open(self.state_path, encoding="utf-8") as fh:
                return parse_desired(json.load(fh), self.port_range)
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as e:
            log.warning("stored desired state %s is unreadable (%s) — ignored", self.state_path, e)
            return None

    def _store(self, desired: Desired) -> None:
        tmp = self.state_path + ".tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(desired.to_json(), fh, sort_keys=True)
        os.replace(tmp, self.state_path)

    # ------------------------------------------------------------ plan / apply

    def _read(self) -> tuple[iptables.Backend, dict[str, FamilyState]]:
        backend = iptables.detect(self.backend_setting, self.runner, self.reader)
        states = {f: iptables.read_family(backend, f, self.runner, self.reader) for f in FAMILIES}
        return backend, states

    def _plan(self, desired: Desired) -> tuple[iptables.Backend, dict[str, FamilyState],
                                               dict[str, FamilyPlan], str]:
        backend, states = self._read()
        plans = {f: plan_family(s, rules_for(desired, f)) for f, s in states.items() if s.hook_chain}
        return backend, states, plans, plan_id(desired, states)

    def _report(self, desired: Desired, backend: iptables.Backend, states: dict[str, FamilyState],
                plans: dict[str, FamilyPlan], pid: str) -> dict:
        """The plan as JSON, with each desired port marked `open` or `opens`, and the ports of
        the previous state that this one closes (named by their server, if known)."""
        families: dict[str, dict] = {}
        for family, state in states.items():
            if not state.hook_chain:
                families[family] = {"status": "absent", "state": state.to_json()}
                continue
            families[family] = {"status": "ok", "state": state.to_json(), "plan": plans[family].to_json()}
        allows = []
        for allow in desired.allows:
            rules = [r for f in plans for r, owners in allows_by_rule(desired, f).items() if allow in owners]
            pending = [r for f in plans for r in plans[f].add if r in rules]
            # `skipped`: no managed family gets a rule for it (the source networks are all of the
            # other family, or its address is IPv6 and Docker's IPv6 firewall is off).
            state = "skipped" if not rules else "opens" if pending else "open"
            allows.append(allow.to_json() | {"state": state})
        previous = self.stored()
        wanted = {a.key()[:3] for a in desired.allows}
        closes = [a.to_json() for a in (previous.allows if previous else ()) if a.key()[:3] not in wanted]
        return {
            "plan_id": pid,
            "backend": backend.name,
            "desired": desired.to_json(),
            "families": families,
            "allows": allows,
            "closes": closes,
            "changes": sum(p.changes() for p in plans.values()),
        }

    def plan(self, payload: object) -> dict:
        desired = parse_desired(payload, self.port_range)
        with self.lock:
            return self._report(desired, *self._plan(desired))

    def apply(self, payload: object, expected: str) -> dict:
        desired = parse_desired(payload, self.port_range)
        with self.lock:
            backend, states, plans, pid = self._plan(desired)
            if not hmac.compare_digest(pid, str(expected or "")):
                raise PlanChanged(pid)
            changed = self._apply(backend, states, plans)
            self._store(desired)
            log.info("applied: %d allows, %d sources, %d changes", len(desired.allows),
                     len(desired.sources), changed)
            report = self._report(desired, *self._plan(desired))
            report["applied"] = changed
            return report

    def _apply(self, backend: iptables.Backend, states: dict[str, FamilyState],
               plans: dict[str, FamilyPlan]) -> int:
        changed = 0
        for family, plan in plans.items():
            if plan.changes():
                iptables.apply_script(backend, family, restore_script(states[family], plan), self.runner)
                changed += plan.changes()
        return changed

    def reconcile(self) -> int:
        """Apply the stored desired state again (the loop). Returns the number of changes."""
        desired = self.stored()
        if desired is None:
            return 0
        with self.lock:
            backend, states, plans, _pid = self._plan(desired)
            changed = self._apply(backend, states, plans)
        if changed:
            log.warning("reconcile: %d rule(s) had drifted and were restored", changed)
        return changed

    def state(self) -> dict:
        with self.lock:
            backend, states = self._read()
        stored = self.stored()
        return {
            "backend": backend.name,
            "stored": stored.to_json() if stored else None,
            "families": {f: ({"status": "ok"} if s.hook_chain else {"status": "absent"}) | {"state": s.to_json()}
                         for f, s in states.items()},
        }

    def remove(self) -> None:
        """Take the helper's jump and chain out of every family (for switching it off)."""
        with self.lock:
            backend, states = self._read()
            for family, state in states.items():
                if not state.hook_chain or not state.chain:
                    continue
                lines = ["*filter"] + ["-D DOCKER-USER -j GSS-ALLOW"] * len(state.hook_positions())
                lines += ["-F GSS-ALLOW", "-X GSS-ALLOW", "COMMIT"]
                iptables.apply_script(backend, family, "\n".join(lines) + "\n", self.runner)
            try:
                os.remove(self.state_path)
            except FileNotFoundError:
                pass


# ---------------------------------------------------------------- HTTP on a Unix socket


def make_handler(helper: Helper, token: str) -> type[BaseHTTPRequestHandler]:
    expected = f"Bearer {token}".encode()

    class Handler(BaseHTTPRequestHandler):
        server_version = "console-firewall"
        sys_version = ""

        def address_string(self) -> str:  # Unix socket: there is no client address
            return "unix"

        def log_message(self, fmt: str, *args) -> None:
            log.info("%s %s", self.command, fmt % args)

        def _send(self, status: int, body: dict) -> None:
            data = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _drain(self) -> None:
            """Read a body that will not be used: a client still sending it when the answer
            comes and the connection closes would see a broken pipe instead of the answer."""
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                return
            if 0 < length <= MAX_BODY:
                self.rfile.read(length)

        def _authorised(self) -> bool:
            got = (self.headers.get("Authorization") or "").encode()
            if hmac.compare_digest(got, expected):
                return True
            self._drain()
            self._send(401, {"error": "token", "message": "missing or wrong token"})
            return False

        def _body(self) -> dict | None:
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = -1
            if not 0 < length <= MAX_BODY:
                self._send(400, {"error": "bad_request", "message": f"a JSON body of at most {MAX_BODY} bytes is required"})
                return None
            try:
                body = json.loads(self.rfile.read(length))
            except ValueError:
                self._send(400, {"error": "bad_request", "message": "the body is not JSON"})
                return None
            if not isinstance(body, dict):
                self._send(400, {"error": "bad_request", "message": "the body must be a JSON object"})
                return None
            return body

        def _run(self, fn) -> None:
            try:
                self._send(200, fn())
            except Refused as e:
                self._send(400, {"error": "refused", "message": str(e), "problems": e.problems})
            except PlanChanged as e:
                self._send(409, {"error": "plan_changed", "plan_id": str(e),
                                 "message": "the rules or the desired state changed since the preview"})
            except iptables.BackendError as e:
                self._send(503, {"error": "backend", "message": str(e)})
            except Exception as e:  # noqa: BLE001 — the client gets a reason, the log the trace
                log.exception("request failed")
                self._send(500, {"error": "internal", "message": str(e)[:300]})

        def do_GET(self) -> None:  # noqa: N802 — BaseHTTPRequestHandler's naming
            if not self._authorised():
                return
            if self.path == "/v1/state":
                self._run(helper.state)
            else:
                self._send(404, {"error": "not_found", "message": self.path[:64]})

        def do_POST(self) -> None:  # noqa: N802
            if not self._authorised():
                return
            if self.path not in ("/v1/plan", "/v1/apply"):
                self._drain()
                self._send(404, {"error": "not_found", "message": self.path[:64]})
                return
            body = self._body()
            if body is None:
                return
            if self.path == "/v1/plan":
                self._run(lambda: helper.plan(body.get("desired")))
            else:
                self._run(lambda: helper.apply(body.get("desired"), body.get("plan_id", "")))

    return Handler


class UnixHTTPServer(socketserver.UnixStreamServer):
    """One request at a time: the helper is asked a few times a minute, never in parallel.

    The socket is writable for everyone who can see it (0666; connecting needs write access).
    Who can see it is decided by the volume it lives in, which only the API and the helper mount;
    the token decides who is answered. Handing the socket to the API's uid instead would need
    CAP_CHOWN, and the helper keeps NET_ADMIN as its only capability.
    """

    def __init__(self, path: str, handler: type[BaseHTTPRequestHandler]):
        if os.path.exists(path):
            os.remove(path)  # a stale socket of the previous run
        super().__init__(path, handler)
        os.chmod(path, 0o666)


def reconcile_loop(helper: Helper, interval: float, stop: threading.Event) -> None:
    """At start, then every `interval` seconds (`interval` <= 0: at start only)."""
    while True:
        try:
            helper.reconcile()
        except Exception as e:  # noqa: BLE001 — the loop must survive a bad minute
            log.warning("reconcile failed: %s", e)
        if interval <= 0 or stop.wait(interval):
            return
