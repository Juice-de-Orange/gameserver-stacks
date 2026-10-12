# Stacks Console — security model

This document describes what the dashboard trusts, what it protects and how, and what you have to
provide when you deploy it. To report a vulnerability, see [SECURITY.md](../../SECURITY.md) in
the repository root.

## Trust model

**A dashboard admin is root-equivalent on the host.** The API controls Docker through the socket
proxy (`console-dockerproxy`), which is allowed to create, start, stop and remove containers and
to pull and delete images. An admin chooses a server's image and data directory, and the API
creates containers with host bind mounts from that. The restriction of `data_dir` to
`ALLOWED_DATA_ROOTS` (default: `DATA_ROOT`) is enforced by the API, not by the socket proxy —
whoever controls the API process is not bound by it. Treat the admin role, the `.env` file and
shell access to the API container like root on the host.

**Three permissions are code execution inside game containers**, by design:

| Permission | Why it is code execution |
|---|---|
| `can_console_full` | Unrestricted remote console. In Factorio, for example, `/c` runs Lua. |
| `can_manage_mods` | Uploads mod files (`.jar`, `.dll`, `.zip` or `.cs`, depending on the game; up to 100 MB) that the game server loads. |
| `can_manage_versions` | Changes the image tag of a server, i.e. pulls and runs another image. |

All three are admin-only in the default role matrix. Grant them only to people you would give a
shell in the game containers.

Game containers are on the same Docker network as the API (`GAME_NETWORK`) and can reach it
directly. They are treated as untrusted clients: they get nothing without a token, and forwarding
headers from them are ignored (see "Client addresses"). The bundled Caddy is not on that network
(it sits on `console-front` with the API), so game containers cannot reach it.

## Accounts and permissions

- **Sign-in** is by username and password. Passwords are hashed with argon2 (`argon2-cffi`,
  library defaults) and must be 8–128 characters long.
- **Self-registration** creates accounts in the state `pending`. They cannot sign in until
  someone with `can_manage_users` approves them. At most 25 registrations can be pending at a
  time, and registration is throttled per client address.
- **Roles** are `admin`, `trusted` and `member`. Sixteen permission flags can be granted per role
  (the role matrix, editable by admins) or per user; admins bypass all flags. Defaults: `member`
  may view the status; `trusted` may additionally view player statistics and logs and use the
  safe console; everything else is admin-only.
- Only admins can assign roles other than `member` and set per-user flags. Users with
  `can_manage_users` who are not admins cannot modify, reset or disable admin accounts.
- **Temporary passwords.** The seeded admin and every account whose password was reset must
  change the password first. Until then the API answers only `GET /api/auth/me` and
  `POST /api/auth/change-password` for that account, and WebSockets are refused.
- **Login throttle.** Eight failed attempts within five minutes lock the client address and,
  separately, the username for five minutes. The state is kept in memory: it is lost on a restart
  and requires the API to run as a single worker process (which the image does).
- **Audit log.** Sign-ins (including failed ones), user management and administrative actions are
  recorded and kept for `AUDIT_RETENTION_DAYS` (default 365). Only admins can read it.

## Tokens

**In short:** the browser holds an access and a refresh token in `localStorage` and sends the
access token as an `Authorization` header. The dashboard sets no cookies, so nothing is sent
automatically and there is no cross-site request forgery to defend against; for the same reason
there is no origin check. A WebSocket is opened with a one-time ticket instead of a token. What
this design does not protect against is script running in the dashboard's own page: it could
read the tokens. The Content-Security-Policy ("Browser hardening") is the guard against that.

The API issues signed JWTs (HS256, key `JWT_SECRET`):

| Token | Lifetime | Used for |
|---|---|---|
| access | `JWT_ACCESS_TTL_MIN` (30 min) | `Authorization: Bearer` on every API request |
| refresh | `JWT_REFRESH_TTL_DAYS` (14 days) | `POST /api/auth/refresh` → new access token |
| WebSocket ticket | 30 seconds, single use | `?ticket=` when opening one WebSocket |

**`JWT_SECRET` is mandatory.** The API refuses to start if it is the built-in default or shorter
than 32 characters. Generate one with `openssl rand -hex 32`. Changing it invalidates every token.

**Revocation.** Every account has a token version, and every token carries the version it was
issued under. A token whose version differs from the account's current one is rejected — on every
API request, on refresh and on WebSockets. The version is raised by:

- a password change (`POST /api/auth/change-password`; the response contains a new token pair so
  that the session that changed the password continues, all others end),
- a password reset by a user manager,
- approving, disabling or enabling an account, and changing its role,
- "sign out everywhere" (`POST /api/auth/logout-all`).

Independently of the version, the account is loaded from the database on every request: a
disabled account and a revoked permission take effect immediately.

There is no per-token revocation: signing out in the browser only deletes the tokens there.
"Sign out everywhere" on the account page (`POST /api/auth/logout-all`) ends every session of the
signed-in user.

**WebSockets.** A browser cannot send an `Authorization` header with a WebSocket, so a credential
has to travel in the URL, where proxies and access logs see it. The frontend therefore requests a
ticket (`POST /api/auth/ws-ticket`) right before each connection. A ticket is bound to the user,
expires after 30 seconds, is accepted once, and is not accepted by the HTTP API. The API also masks
`ticket=` and `token=` values in its own log. An open console or chat socket re-checks the account
on every command or poll and closes when the account is disabled, must change its password or has
its tokens revoked; the log stream checks at connection time only.

**Storage in the browser.** The frontend keeps the access and refresh token in `localStorage`.
Nothing is sent automatically by the browser, so cross-site request forgery does not apply;
script injection would expose the tokens. The Content-Security-Policy below is the guard against
that.

## Browser hardening

The bundled Caddy sends these headers with every response:

- `Content-Security-Policy`: `default-src 'self'`, `script-src 'self'`, `style-src 'self'`,
  `font-src 'self'`, `img-src 'self' data:`, `connect-src 'self'`, `object-src 'none'`,
  `base-uri 'self'`, `form-action 'self'`, `frame-ancestors 'none'` — no inline scripts, no
  third-party origins.
- `X-Content-Type-Options: nosniff`
- `X-Frame-Options: DENY` (for browsers that do not know `frame-ancestors`)
- `Referrer-Policy: no-referrer`

The interactive API documentation of FastAPI (`/docs`, `/openapi.json`) is not under `/api` and is
therefore not reachable through Caddy.

## Client addresses

The throttle counts per client address, so the API has to know it. Forwarding headers can be set
by anyone, so they are believed only from known proxies:

- `TRUSTED_PROXIES` (API) lists the peers whose `X-Forwarded-For` or `CF-Connecting-IP` header is
  used: IP addresses, CIDR ranges or host names. From any other peer the headers are ignored and
  the TCP peer address counts. The compose file sets it to `console-web`, the bundled Caddy.
- The bundled Caddy works out the client address itself, replaces `X-Forwarded-For` with that one
  address and removes `CF-Connecting-IP`. It believes an incoming `X-Forwarded-For` only from the
  peers in `CADDY_TRUSTED_PROXIES` (default: private and loopback addresses, i.e. a proxy or
  tunnel on the same host) and reads the header from the right, so entries a client supplied
  itself are skipped.

Consequences:

- Your reverse proxy must set or append `X-Forwarded-For`. If it passes a client-supplied header
  through unchanged, clients can choose their throttle key. The per-username throttle still
  applies in that case.
- Anything that reaches the bundled Caddy from a private address is treated as such a proxy:
  other containers on `console-front`, and clients on your LAN if you publish the dashboard there
  (`DASHBOARD_BIND`). Those can evade the per-address (not the per-username) throttle. Narrow
  `CADDY_TRUSTED_PROXIES` to your proxy's address if that matters to you. Game containers are
  not among them: they are not on Caddy's network.
- With the bundled HTTPS front (profile `https`), `console-tls` replaces `X-Forwarded-For` with
  the address it saw. Clients that reach it from a private or loopback address (your LAN) share
  one per-address throttle counter; the per-username throttle is unaffected.
- If you run the API behind a different proxy, list it in `TRUSTED_PROXIES` and make sure it
  overwrites both headers.

## Game servers

- **RCON is never published.** Containers created by the dashboard expose the RCON port inside
  the Docker network only; only game ports are published on the host.
- **Passwords** for the console (RCON, BattlEye), the in-game admin and joining the server come,
  in this order, from an environment variable the server names (if it is set, it wins), from the
  dashboard's secret store, or from the game's own files (Factorio and Avorion, where the file
  is what the running server uses). The API never returns them: it says only whether a password
  is set and where it comes from, and masks secret values in the server settings it returns.
- **What every member sees.** A server's status (`GET /api/servers/<key>/status` and
  `/overview`, open to `can_view_status`, which every member has by default) carries a fixed
  list of container state fields: whether it exists and runs, its status, restarts, start and
  end time, health. The container's environment, where the images get their passwords, is not
  among them. The player list shown there and on the fleet and join pages is shared between all
  viewers and can be up to 15 seconds old; the watchdog, the idle auto-stop, the auto-update and
  the boot check ask the game directly.
- **The secret store.** A password typed into the dashboard is stored in the table
  `server_secrets`, encrypted with Fernet (AES-128-CBC with HMAC-SHA256) under a key derived from
  `GSS_SECRET_KEY` with HKDF-SHA256. The API refuses to start without `GSS_SECRET_KEY` or with
  fewer than 32 characters; there is no default. A database dump therefore holds only
  ciphertext, and it is useless without the key. Losing or changing the key makes the stored
  passwords unreadable: the dashboard says so and asks for them again. Keep the key with your
  backups, but not inside the same archive as the database dump if you can avoid it.
- **Console.** Without `can_console_full`, only a per-game list of safe commands is accepted.
  Text that the dashboard inserts into console commands is stripped of control characters, `"`,
  `;`, `<` and `>`, and filtered further where a game needs it. No shell and no subprocess are
  involved anywhere. Workshop downloads (Project Zomboid) run in the separate steamcmd helper
  container, pulled only when needed, which accepts only a numeric app id and item id and always
  logs in anonymously. Copying a downloaded item out of the helper's cache skips symbolic links
  and checks that every resolved path stays inside the cache.
- **Containers.** Created with `no-new-privileges`, with Docker's default capabilities (none
  added, none dropped) and not privileged. Memory and CPU limits come from the server row.
  `no-new-privileges` has not been tested against every supported game image.
- **Which uid a game runs as.** Minecraft, Factorio and Valheim are told to run the game as the
  API's uid, so that the API can edit, back up and delete their files. The Vintage Story runner
  image starts as root only to create its two folders and hand entries with another owner to
  `PUID:PGID` (the API's uid); then it runs again as that uid (the download, the version cache
  and the game). Project Zomboid and Avorion run as their images' default user. The Arma
  Reforger image has no uid setting and runs as root, so its files belong to root; see "Helper
  containers".
- **Files.** Mod uploads are reduced to their base name, must have an extension allowed for the
  game, must not be dot-files, are size-checked while streaming and are stored disabled. Backup
  names must match a fixed pattern and resolve inside the server's backup directory. A restore
  unpacks with Python's `tarfile` `data` filter, which rejects absolute paths, path traversal and
  special files. It moves the current folders aside first and puts them back if unpacking fails.
- **Removing a server with its data** deletes the data directory only after checking its
  path as text and as its real path on disk: it must not be a symbolic link, must lie below an
  allowed data root, must not be the root itself and must not overlap another server's data
  directory. What the API's uid cannot delete there, the root helper deletes ("Helper
  containers"). The container is removed with its anonymous volumes; named volumes and bind
  mounts are never removed that way.

## Helper containers

Besides the game containers the API creates short-lived helpers through the same socket proxy.
They are part of the trust boundary above (whoever controls the API can create containers
anyway); what follows is what the dashboard's own code gives them.

- **The root helper** (`<container name>-cleanup`) deletes and copies files the API's uid
  cannot touch. It runs as root (`User 0:0`) with `no-new-privileges`, no network
  (`NetworkMode none`), all capabilities dropped except the ones its job needs, and a fixed
  command instead of the image's entry point. Its only mount is one folder of the server, at
  `/x`; it does not see the rest of the host, other servers or the Docker socket. It is removed
  after its run, together with its anonymous volumes (an image that declares `VOLUME`s gets
  them in the helper too). It uses the server's own image, which is already on the host, or
  `busybox:1.36` when that image is gone. Its jobs:
  - **Deleting** what the API cannot delete, with `DAC_OVERRIDE` and `FOWNER`: a server's data
    directory when it is removed with its data (Arma Reforger's files belong to root), and a
    `.restore-old-*` copy a restore set aside. The mount is the directory that passed the checks
    in "Removing a server with its data", or the server's own data directory for a restore
    copy, and only `.restore-old-*` entries inside it are named.
  - **Copying** SteamCMD out of the Arma Reforger image into the empty `steamcmd/` folder of the
    server's data directory (once, when its container is created), with `CHOWN` in addition:
    `cp -a` keeps the owner the image gave the files, which SteamCMD needs to update itself.
- **The steamcmd helper** (Project Zomboid workshop downloads, see "Game servers") runs as the
  API's uid with every capability dropped and `no-new-privileges`, on Docker's default bridge
  (it has to reach Steam), with the download cache as its only mount.
- **The save swap of Arma Reforger** (switching the game mode) moves save folders with a
  helper from the server's image: root, no network, the server's data directory as its only
  mount, all capabilities dropped except `CHOWN`, `DAC_OVERRIDE` and `FOWNER`,
  `no-new-privileges`, removed with its anonymous volumes.

## Backups contain passwords

A world backup contains the game's configuration files, and several games keep their join and
RCON passwords there **in clear text**. For that reason downloading a backup, listing its contents
and restoring it require the admin role, not just `can_manage_backups` (which covers creating
and listing). The nightly database dump of `console-backup` contains the user table with
the password hashes and the encrypted game passwords. Protect both like the `.env` file.

## Firewall helper (optional)

Off unless you switch it on (compose profile `firewall` plus `GSS_FIREWALL_TOKEN`, see
`.env.example`). Docker publishes container ports past ufw and firewalld; whoever blocks them
anyway puts a default-deny into Docker's `DOCKER-USER` chain. The helper `console-firewall` then
opens the published game ports of the enabled servers in front of that rule.

**What it is allowed to do, by the kernel.** The helper runs with `network_mode: host` and the
capability `NET_ADMIN`, nothing else: all other capabilities dropped, `no-new-privileges`, a
read-only root file system, no Docker socket, no published or listening network port. In the
host's network namespace `NET_ADMIN` is enough to change **every** firewall rule, interface and
route of the host. The kernel does not hold it to less. Treat the helper's image, its container
and the `firewall_state` volume like root on the host's network.

**What it does, by its own code** (`dashboard/firewall/`):

- It writes only its own chain `GSS-ALLOW` in the `filter` table and one jump to it, first in
  `DOCKER-USER`, for IPv4 and IPv6 (IPv6 only where Docker created `DOCKER-USER`). Every change is
  one `iptables-restore --noflush` transaction; a rule is removed by its full text, never by its
  position, so a rule of yours is never hit by mistake.
- `GSS-ALLOW` holds ACCEPT rules only: protocol tcp or udp, the ORIGINAL destination port of a
  connection Docker translated for a published port (`-m conntrack --ctstate DNAT
  --ctorigdstport`), optionally the host address and the source networks. A packet that matches
  none of them returns to `DOCKER-USER` and meets your rules as before. The helper never blocks
  anything, so it cannot lock you out of SSH.
- It only ever opens ports inside `GSS_FIREWALL_PORT_RANGE` (default 1024–65535). A port outside
  it — 22, say — is refused, whatever the dashboard asks for.
- It uses the iptables variant whose `filter` table holds Docker's `DOCKER-USER` (nf_tables or
  legacy). Legacy iptables is only asked if the kernel already has its table, so looking does not
  load a kernel module. If both or neither have the chain it changes nothing and says why.
  `GSS_FIREWALL_BACKEND` settles a tie. Legacy iptables also needs the capability `NET_RAW`
  (add it in a `docker-compose.override.yml`), and its lock file is the container's own, not the
  host's `/run/xtables.lock`. Only the nf_tables variant has been tested.

**Who can ask it.** The helper answers on a Unix socket in the volume `firewall_socket`, which
only `console-api` and `console-firewall` mount, and every request needs
`Authorization: Bearer <GSS_FIREWALL_TOKEN>` (at least 32 characters, compared in constant time;
without it the helper does not start). The socket itself is open to every process that sees the
volume (mode 0666): handing it to the API's uid would take `CAP_CHOWN`, and the helper keeps
`NET_ADMIN` as its only capability. Do not mount `firewall_socket` into anything else. A container
in the host's network namespace cannot join `console-internal`, hence the socket instead of a
network.

The API sends a desired state: the published ports of the enabled servers of active games
(ports bound to a loopback address are left out) and `GSS_FIREWALL_SOURCES`. Whoever controls
the API can therefore have ACCEPT rules for ports in the allowed range written — no more. The
admin role is root-equivalent anyway (see "Trust model"); the port range and "ACCEPT only" hold
even against a compromised API.

**Preview, then apply.** `GET /api/settings/firewall` (admin only) shows the helper's plan: the
rules it would add and remove and what happens to the jump. Nothing is written.
`POST /api/settings/firewall/apply` sends back the plan's id; the helper applies it only if the
rules and the desired state are still exactly what the plan was made from, and answers 409
otherwise. Every apply is in the audit log.

**Closing does not wait.** Once a minute the API compares the last applied state with the
servers. Ports of a server that was switched off, removed or moved to another port, or whose game
was deactivated, are closed right away (audit log entry by `system`). That only removes ACCEPT
rules; a new port is never opened without an admin's apply.

**Drift and reboots.** The helper stores the last applied state in the volume `firewall_state`
and applies it again at start and every `GSS_FIREWALL_RECONCILE_SECONDS` (default 60): after a
reboot or a firewall reload the rules come back by themselves.

**Switching it off.** `docker compose run --rm console-firewall remove` deletes the jump and the
chain and forgets the stored state. Then remove the profile from `COMPOSE_PROFILES` and run
`docker compose rm -sf console-firewall`. Stopping the helper alone leaves the last rules in place.

**Tested** only in a throwaway network namespace, never against a host firewall:
`sh dashboard/firewall/netns-check.sh` runs the helper against a real iptables (nf_tables) in a
container with its own network namespace and `NET_ADMIN` only, with a fake `DOCKER-USER`.

## Deployment checklist

- Put HTTPS in front of the dashboard: the bundled front (profile `https`) or your own
  TLS-terminating reverse proxy or tunnel ([operations.md](operations.md#https)). The compose
  file publishes the dashboard on `127.0.0.1` only and serves plain HTTP; without TLS, passwords
  and tokens travel in clear text.
- Set a random `JWT_SECRET` and `GSS_SECRET_KEY` (at least 32 characters each),
  `POSTGRES_PASSWORD` and `SEED_ADMIN_PASSWORD`; keep `.env` readable only by its owner
  (`chmod 600`). Remove
  `SEED_ADMIN_PASSWORD` after the first start if you like — it is only used while no account named
  `SEED_ADMIN_USERNAME` exists.
- Make sure your proxy sets `X-Forwarded-For` (see "Client addresses").
- If you switch on the firewall helper, read "Firewall helper" above first; it runs in the
  host's network namespace with `NET_ADMIN`.
- Never publish the socket proxy (`console-dockerproxy`) or the database. Both sit on an internal
  Docker network without a route to the outside; the socket proxy has no authentication of its
  own.
- Run the API as one process (the image's default). The login throttle, the role-matrix cache and
  the list of used WebSocket tickets are per process.
- Keep the dashboard images up to date: `docker compose pull && docker compose up -d`, or
  `docker compose up -d --build` if you build them yourself, so that dependency upgrades take
  effect.
