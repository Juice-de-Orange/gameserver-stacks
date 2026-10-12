"""Can players reach a game server? A checklist, a check from inside and one from outside.

* `checklist` — what has to be open (ports with TCP/UDP from the registry row), the bind address
  and the public address, and ready-made firewall commands. Pure computation, no I/O.
* `inside` — from the API container: does Docker publish every port, and does the game answer on
  its console or query channel (the drivers' own RCON/A2S code)?
* `outside` — opt-in (`GSS_REACHABILITY_PROBES=on`): third-party services look at the public
  address from the internet. Only the public IPv4 address and the port numbers leave the host.

The router is `routers/reachability.py`; the panel on the server page shows all three.
"""
