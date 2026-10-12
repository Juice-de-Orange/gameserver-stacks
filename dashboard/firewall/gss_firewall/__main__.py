"""`python -m gss_firewall [serve|state|remove]` — the helper's entry point.

serve   (default) the HTTP API on the Unix socket plus the reconcile loop
state   print what the helper finds in the firewall, change nothing
remove  take the helper's jump and its chain out of the firewall and forget the stored state
        (before switching the profile off: `docker compose run --rm console-firewall remove`)

Settings (environment):
  GSS_FIREWALL_TOKEN              shared with the API; at least 32 characters (required)
  GSS_FIREWALL_BACKEND            auto (default), nft or legacy
  GSS_FIREWALL_PORT_RANGE         ports the helper may open at all; default 1024-65535
  GSS_FIREWALL_RECONCILE_SECONDS  how often the applied state is restored; default 60, 0 = at start only
  GSS_FIREWALL_SOCKET             default /run/console-firewall/helper.sock
  GSS_FIREWALL_STATE              default /var/lib/console-firewall/desired.json
"""
from __future__ import annotations

import json
import logging
import os
import signal
import sys
import threading

from .rules import parse_port_range
from .service import TOKEN_MIN_LENGTH, Helper, UnixHTTPServer, make_handler, reconcile_loop


def _fail(message: str) -> int:
    print(f"console-firewall: {message}", file=sys.stderr)
    return 2


def main(argv: list[str]) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    command = argv[0] if argv else "serve"
    if command not in ("serve", "state", "remove"):
        return _fail(f"unknown command {command!r} (serve, state or remove)")
    try:
        port_range = parse_port_range(os.environ.get("GSS_FIREWALL_PORT_RANGE"))
        interval = float(os.environ.get("GSS_FIREWALL_RECONCILE_SECONDS") or 60)
    except ValueError as e:
        return _fail(str(e))
    state_path = os.environ.get("GSS_FIREWALL_STATE") or "/var/lib/console-firewall/desired.json"
    helper = Helper(os.environ.get("GSS_FIREWALL_BACKEND") or "auto", port_range, state_path)

    if command == "state":
        print(json.dumps(helper.state(), indent=2))
        return 0
    if command == "remove":
        helper.remove()
        print("console-firewall: chain GSS-ALLOW and its jump in DOCKER-USER removed")
        return 0

    token = os.environ.get("GSS_FIREWALL_TOKEN") or ""
    if len(token) < TOKEN_MIN_LENGTH:
        return _fail(f"GSS_FIREWALL_TOKEN is missing or shorter than {TOKEN_MIN_LENGTH} characters. "
                     "Set the same random value for the API and the helper in the .env, for "
                     "example with `openssl rand -hex 32`.")
    socket_path = os.environ.get("GSS_FIREWALL_SOCKET") or "/run/console-firewall/helper.sock"
    os.makedirs(os.path.dirname(socket_path), exist_ok=True)
    os.makedirs(os.path.dirname(state_path), exist_ok=True)

    server = UnixHTTPServer(socket_path, make_handler(helper, token))
    stop = threading.Event()
    threading.Thread(target=reconcile_loop, args=(helper, interval, stop), daemon=True).start()
    # `docker stop` sends SIGTERM: leave serve_forever and remove the socket.
    signal.signal(signal.SIGTERM, lambda *_: threading.Thread(target=server.shutdown).start())
    logging.getLogger("console-firewall").info("listening on %s (ports %d-%d)", socket_path, *port_range)
    try:
        server.serve_forever()
    finally:
        stop.set()
        server.server_close()
        if os.path.exists(socket_path):
            os.remove(socket_path)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
