# Reachability — can players reach a server?

Each server's page has a **Reachability** panel (admins only). It has three parts.

## Checklist

- The ports players need, with TCP/UDP, read from the server's settings.
- Where the ports are published (the server's bind address):
  - `0.0.0.0` publishes on every interface.
  - `127.0.0.1` keeps the server off the network on purpose, for a tunnel or a proxy on the same host.
  - A single LAN address needs the router to forward to exactly that address.
- The public address players connect to (Settings → Network, or per server).
- Ready-made firewall commands for each port:
  - `iptables` / `ip6tables` in the `DOCKER-USER` chain,
  - `nftables` in the host's `forward` chain,
  - `ufw route allow`.
- Hints for router port forwarding, carrier-grade NAT and cloud provider firewalls.

**Docker bypasses ufw.** Docker forwards a published port with its own NAT rules. That traffic
takes the forward path, and Docker's rules see it before ufw does:

- `ufw allow 25565/tcp` neither opens nor closes the port.
- A published port is usually open even while ufw says "deny".
- Filtering happens in `DOCKER-USER`, which Docker evaluates before its own rules, or in the
  host's forward chain.
- `ufw route allow …` only matters where ufw has been set up to filter container traffic
  (the `ufw-docker` approach).

## Check from inside

Nothing leaves the host. The dashboard checks two things:

- **Ports:** it asks Docker which ports the running container really publishes. This catches a
  container that was created before a port change.
- **The game:** it asks the game through its own console or query channel: RCON, BattlEye RCON, or
  the Steam query for Valheim. These are the same calls the fleet view and the watchdog use. Arma
  Reforger also gets a Steam query (A2S) on its `a2s` port.

## Check from outside (opt-in)

**Off by default.** Set `GSS_REACHABILITY_PROBES=on` in the `.env` and restart the dashboard
to offer it.

**What is sent:**

- The server's public **IPv4 address**. A DNS name is resolved on the dashboard host, and only
  the address is sent.
- The port numbers, for the two services that need them.

Nothing else is sent: no key, no server name, no password. The panel lists exactly this per
service before the button can be pressed. Each check is recorded in the audit log.

**When nothing is sent at all:**

- the server is bound to loopback,
- no public address is set,
- the address is not a public IPv4 internet address,
- the server is not running (every service would only report "unreachable").

Answers are cached for one minute, and every call has a 10-second timeout.

| Service | Checks | Receives | Terms and limits (checked 2026-10-12) | Switch |
|---|---|---|---|---|
| [Steam Web API](https://partner.steamgames.com/doc/webapi/ISteamApps) `ISteamApps/GetServersAtAddress/v1` (Valve) | Valheim, Project Zomboid, Avorion, Arma Reforger: is a server listed at this address on the game port or the Steam query port? | the IPv4 address | No API key needed (verified with a live call). [Steam Web API Terms of Use](https://steamcommunity.com/dev/apiterms): at most 100,000 calls per day; no use that degrades Steam; no claim of endorsement by Valve. | `GSS_REACHABILITY_STEAM` |
| [mcstatus.io](https://mcstatus.io/docs) `GET /v2/status/java/<ip>:<port>` | Minecraft: a status ping from the internet | IPv4 address and game port | Public API for developers. No key. 5 requests per second per client IP. Answers cached 1 minute. No published terms beyond the docs. The service is open source and can be self-hosted. | `GSS_REACHABILITY_MCSTATUS`, `GSS_REACHABILITY_MCSTATUS_URL` |
| [portchecker.io](https://portchecker.io/docs) `POST /api/query` | every other TCP port: a TCP connection from the internet | IPv4 address and the TCP ports | Open-source API "for checking port availability" ([GPL-3.0, self-hostable](https://github.com/dsgnr/portchecker.io)). No key. Public IPv4 addresses only. The request body is not logged. No published rate limit. | `GSS_REACHABILITY_PORTCHECK`, `GSS_REACHABILITY_PORTCHECK_URL` |

### What a result means

- **mcstatus.io "reachable"** and **portchecker.io "reachable"** are proof: something on the
  internet connected to that port.
- **Steam "listed"** means the server registered with Steam's master server from this address. The
  game runs and announces itself there. Players still connect directly, so a blocked game port can
  still stop them.
- **Steam "not listed"** is a warning, not a verdict:
  - A server set to "not public" is never listed.
  - A new server appears a few minutes after its start.
  - Arma Reforger announces its servers through Bohemia's own list, so Steam may never list it.
- **"Not checkable from outside"** is used for UDP ports without such a service: Factorio, and
  the extra UDP ports of several games. A UDP port that does not answer looks the same as a
  blocked one, so the check says so instead of showing green. Ask a player to connect.

All three switches default to `on` once `GSS_REACHABILITY_PROBES=on`. Turn any of them `off`
to stop that service from receiving anything. A port it would have checked then shows "off".
