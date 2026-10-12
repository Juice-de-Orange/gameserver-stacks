# Stacks Console — operations

Running the dashboard for longer than a first evening. Every command runs in the `dashboard/`
directory of your checkout, where `docker-compose.yml` and `.env` are. Install and first steps
are in the [README](../README.md).

- [HTTPS](#https)
- [Firewall](#firewall)
- [Backups and restore](#backups-and-restore)
- [Updating](#updating)
- [Logs](#logs)
- [Host reboots](#host-reboots)

## HTTPS

The dashboard itself speaks plain HTTP on `127.0.0.1:8080` (`DASHBOARD_BIND`, `DASHBOARD_PORT`).
Passwords and tokens must not cross a network that way, so put HTTPS in front of it. There are
two ways.

### The bundled HTTPS front (profile `https`)

An extra Caddy (`console-tls`) answers on ports 80 and 443 of every host interface, gets and
renews a Let's Encrypt certificate for your domain, redirects HTTP to HTTPS and sends HSTS.

Requirements: the domain resolves to this host from the internet, and ports 80 and 443 reach it
(router, cloud firewall). The certificate authority checks the name over them. Nothing else on
the host may use ports 80 and 443.

Add three lines to `.env`, then start:

```bash
COMPOSE_PROFILES=https
DASHBOARD_DOMAIN=dashboard.example.com
ACME_EMAIL=admin@example.com
```

```bash
docker compose up -d
docker compose logs console-tls   # look for "certificate obtained successfully"
```

If you already use another profile, list both: `COMPOSE_PROFILES=https,firewall`. A missing
setting stops `console-tls` with a sentence that names it (`docker compose logs console-tls`).

The local HTTP port stays as it is. For a test without a public name, `ACME_CA=internal` takes
the certificate from Caddy's own authority: no internet access is needed, and every browser
warns. Never use it for real.

**Switching it off**: remove the lines from `.env`, then

```bash
docker compose up -d
docker compose rm -sf console-tls   # `up -d` alone leaves console-tls running
```

### Your own reverse proxy or tunnel

Keep the dashboard on `127.0.0.1` and point your proxy at `http://127.0.0.1:8080`. Whatever you
use, it must:

- pass **WebSockets** (`/ws/…`: live console, logs, chat),
- **set** `X-Forwarded-For` to the client address it saw, or append to it. The bundled Caddy
  believes the header from private and loopback addresses only (`CADDY_TRUSTED_PROXIES`) and
  reads it from the right. If your proxy connects from a public address, list that address in
  `CADDY_TRUSTED_PROXIES`;
- accept request bodies of at least 100 MB (mod uploads).

The security headers (CSP and others) come from the bundled Caddy and pass through any proxy.

**Caddy** on the host (`/etc/caddy/Caddyfile`):

```caddyfile
dashboard.example.com {
	reverse_proxy 127.0.0.1:8080
}
```

Caddy handles certificates, WebSockets and `X-Forwarded-For` by itself.

**nginx** (`/etc/nginx/conf.d/dashboard.conf`, certificate from certbot or similar):

```nginx
map $http_upgrade $connection_upgrade {
    default upgrade;
    ''      close;
}

server {
    listen 80;
    server_name dashboard.example.com;
    return 301 https://$host$request_uri;
}

server {
    listen 443 ssl;
    http2 on;
    server_name dashboard.example.com;

    ssl_certificate     /etc/letsencrypt/live/dashboard.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/dashboard.example.com/privkey.pem;

    add_header Strict-Transport-Security "max-age=31536000" always;
    # Mod uploads are up to 100 MB.
    client_max_body_size 110m;

    location / {
        proxy_pass http://127.0.0.1:8080;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        # Replace, do not append: the client address nginx saw is the one that counts.
        proxy_set_header X-Forwarded-For $remote_addr;
        proxy_set_header X-Forwarded-Proto https;
        # WebSockets (live console, logs, chat).
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection $connection_upgrade;
        proxy_read_timeout 1h;
    }
}
```

**Traefik** in Docker, with the Docker provider: Traefik reaches `console-web` over a shared
network instead of the host port. The example expects an external network `traefik` that your
Traefik container is on, an entry point `websecure` and a certificate resolver `letsencrypt`;
use your own names. Put this into `docker-compose.override.yml` next to `docker-compose.yml`
(Compose reads it by itself), then `docker compose up -d`:

```yaml
services:
  console-web:
    networks: [console-front, traefik]
    labels:
      traefik.enable: "true"
      traefik.docker.network: traefik
      traefik.http.routers.stacks-console.rule: Host(`dashboard.example.com`)
      traefik.http.routers.stacks-console.entrypoints: websecure
      traefik.http.routers.stacks-console.tls.certresolver: letsencrypt
      traefik.http.services.stacks-console.loadbalancer.server.port: "80"

networks:
  traefik:
    external: true
```

Traefik's container has a private address, which the bundled Caddy trusts by default.

**Cloudflare Tunnel**: run `cloudflared` on the host and route the public hostname to
`http://127.0.0.1:8080`. In a locally managed tunnel (`config.yml`):

```yaml
tunnel: <tunnel-id>
credentials-file: /etc/cloudflared/<tunnel-id>.json
ingress:
  - hostname: dashboard.example.com
    service: http://127.0.0.1:8080
  - service: http_status:404
```

In a tunnel managed in the Cloudflare dashboard, the same goes into the public hostname: type
`HTTP`, URL `127.0.0.1:8080`. Cloudflare terminates TLS. The bundled Caddy takes the client
address from `X-Forwarded-For` (cloudflared connects from loopback) and drops
`CF-Connecting-IP`. If `cloudflared` runs in a container, it cannot reach the host's
`127.0.0.1`; attach it to `console-front` with an override, like Traefik above, and use
`http://console-web:80`.

**What was tested** (with a self-signed certificate or Caddy's internal authority, in
containers, against a running installation: page load, sign-in, a console command over the
WebSocket): the bundled profile with `ACME_CA=internal` (also the HTTP → HTTPS redirect and
HSTS), Caddy, nginx 1.29 with exactly the configuration above, and Traefik 3.6 with the labels
above but its default certificate instead of `letsencrypt`. The Cloudflare Tunnel example was
not tested.

## Firewall

Docker publishes container ports with its own iptables rules. That traffic does not pass ufw or
firewalld: `ufw deny 25565` does not close a published game port. Filtering for containers
happens in Docker's `DOCKER-USER` chain. The **Reachability** panel on each server page lists
the server's ports and gives ready-made commands for `iptables`, `nftables` and `ufw route`
([reachability.md](reachability.md)).

The usual pattern is a default deny in `DOCKER-USER` for the external interface, plus one
ACCEPT per published game port. [`examples/firewall/`](../../examples/firewall) in the
repository root has such an allow-list script and a systemd unit that re-applies it after every
Docker start; [`docs/OPERATIONS.md`](../../docs/OPERATIONS.md#firewall-docker-bypasses-ufw)
explains the details. Remote consoles are never published, so they need no rule.

### The firewall helper (profile `firewall`)

With a default deny in place, the optional helper `console-firewall` keeps the ACCEPT rules in
step with the dashboard. It opens exactly the published ports of the enabled servers, in a chain
of its own (`GSS-ALLOW`), with ACCEPT rules only. It never blocks anything and never touches
other rules. Read its trust boundary in [security.md](security.md#firewall-helper-optional)
first: it runs in the host's network namespace with `NET_ADMIN`.

1. In `.env`: add `firewall` to `COMPOSE_PROFILES` and set
   `GSS_FIREWALL_TOKEN=` to the output of `openssl rand -hex 32`. Optional:
   `GSS_FIREWALL_SOURCES` (allowed source networks) and `GSS_FIREWALL_PORT_RANGE` (default
   1024–65535).
2. `docker compose up -d`.
3. **Settings → Network** shows what the helper would add and remove. Nothing is opened until an
   admin applies it. Ports of a server that is switched off, removed or moved close by
   themselves within a minute.

To switch it off: `docker compose run --rm console-firewall remove` (takes its chain and jump
out), remove the lines from `.env`, then `docker compose rm -sf console-firewall`.

Its image is published with every release as `ghcr.io/juice-de-orange/stacks-console-firewall`
(amd64 and arm64, tag `GSS_VERSION` like the API and web images). Until then, `docker compose
up -d` builds it from `firewall/` when the pull is refused.

The helper has been tested in a throwaway network namespace with nf_tables iptables, not
against a real host firewall, and not with legacy iptables. CI repeats that check on every pull
request (job `firewall-netns` in `.github/workflows/dashboard.yml`: the helper in a container
with `--network none` and `NET_ADMIN` only), and the job `helper-images` builds the firewall,
steamcmd and Vintage Story runner images there, so a broken Dockerfile shows before a release.

## Backups and restore

There are two kinds of backup, and you want both:

| What | Made by | Where |
|---|---|---|
| Game worlds and their configuration | the dashboard, weekly and on demand | a `backups/` folder in each server's data directory (Minecraft: `<data_dir>/data/backups/`) |
| The dashboard database: users, roles, servers, stored passwords (encrypted), schedules, statistics, audit log | the `console-backup` sidecar, nightly | `DB_BACKUP_DIR`, default `./db-backups/` |

Neither leaves the host. Copy both off it regularly, together with `.env` (it holds
`GSS_SECRET_KEY`, without which the stored game passwords cannot be read). Treat all three like
passwords: world archives contain the games' config files with join and console passwords in
clear text, and the database dump contains the password hashes.

### World backups

- **Weekly**: every enabled server, Sunday 04:00 in `SCHEDULER_TZ`.
- **On demand**: server page → **Backups** → **Back up now**.
- **Before risky changes** the dashboard takes one by itself: before a restore, a version
  change, a container recreate, applying mods, an edit of Project Zomboid's config files, and a
  scheduled restart whose schedule asks for a backup first.
- **Games that cannot save on command** (Valheim) are stopped for the weekly and the on-demand
  backup and started again, because the running server has written only its last autosave.
  A scheduled restart that asks for a backup first takes it while the server is stopped for
  the restart. Players are disconnected for that time. The other games are archived while they run, after a
  save command where the game has one.
- **Names**: `<date>-<time>-<label>.tar.gz` (UTC), the label says why: `weekly`, `manual`,
  `pre-restore`, `pre-version-<version>`, `pre-recreate-<version>`, `pre-restart`, and other
  `pre-…` labels for mod changes, config edits and Arma Reforger's game-mode switch. Archives
  named `pre-version-*` from before recreates had a label of their own are listed and restored
  like any other.
- **Retention**: `BACKUP_RETENTION_DAYS` (90), `BACKUP_MAX_GB_PER_SERVER` (12) and
  `BACKUP_KEEP_MIN` (2), oldest first. The last `BACKUP_KEEP_MIN` archives always stay.

What an archive holds is each game's world and configuration folders. For Project Zomboid that
is `Saves/`, `Server/` and `db/` (accounts, access levels, bans, whitelist). Archives made
before `db/` was added restore without it and leave the current `db/` as it is.

**Restore** (admins only): **Backups** → **Restore** next to an archive. The dashboard stops the
server, takes a `pre-restore` backup of the current state, replaces the archived folders with
their archived version (anything created since the backup is gone), starts the server and
checks that it comes up. **Configuration only**, where a game offers it, restores the config
files and keeps the world (Project Zomboid: `Server/` only).

The replaced folders are first moved aside as `<folder>.restore-old-<time>` and deleted once the
restore has succeeded. Files a game wrote as root are deleted through a short-lived helper
container ([security.md](security.md#helper-containers)). If a copy still cannot be deleted, the
restore result names its path; delete it by hand, it only takes up space.

We ran this for a Minecraft server: backup, a file added to the world afterwards, restore. The
file was gone, the world was the archived one, and a `…-pre-restore.tar.gz` archive appeared
next to the restored one.

### The dashboard database

The sidecar dumps the database once at start (if there is no dump from today) and then daily at
`DB_BACKUP_HOUR:DB_BACKUP_MINUTE` UTC (default 02:30), keeps `DB_BACKUP_KEEP_DAYS` (30) days and
only ever removes its own files (`stacks-console-*.sql.gz`). Check that it works:

```bash
docker compose logs console-backup   # "ok: stacks-console-<date>-<time>.sql.gz, <n> bytes"
ls -l db-backups/
```

A dump right now, before an update or an intervention (kept until you delete it):

```bash
docker compose exec -T console-db sh -c 'pg_dump -U "$POSTGRES_USER" --clean --if-exists "$POSTGRES_DB"' \
  | gzip > db-backups/manual-$(date -u +%Y%m%d-%H%M%S).sql.gz
```

**Restore a dump** into the running stack. The API is stopped so that nothing writes in between;
the game servers keep running:

```bash
docker compose stop console-api
gunzip -c db-backups/<file>.sql.gz \
  | docker compose exec -T console-db sh -c 'psql -q -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB"' > /dev/null
docker compose start console-api
```

The dump replaces every table (`--clean`): accounts, servers and settings are those of the dump
afterwards. We tested exactly these commands: a registration made after the dump was gone after
the restore, the server and the admin account were back. Restore with the same
`GSS_SECRET_KEY` the dump was made with; otherwise the stored game passwords are unreadable and
the server pages ask for them again.

## Updating

Take a database dump first (above). Then:

```bash
git pull
docker compose pull          # prebuilt images, once releases are published
docker compose up -d
```

`GSS_VERSION` in `.env` pins a release (`0.2.0`); without it you follow `latest`. If you build
the images yourself (before the first release, or with `APP_UID` other than 1000), replace the
last two commands with `docker compose up -d --build`. Until the first release `docker compose
pull` reports `denied` for the two dashboard images; that is expected.

The game servers keep running during the update. Read [`CHANGELOG.md`](../../CHANGELOG.md)
first. Updating a game is a separate step in the dashboard (server → **Version**), with a backup
before and a boot check after.

The API applies database changes when it starts, and its healthcheck answers only after that
and while the database responds. In a script, wait for it before you use the CLI:

```bash
docker compose up -d --wait          # returns once every service with a healthcheck is healthy
docker compose ps console-api        # STATUS "Up … (healthy)"
```

### Upgrade notes

- **Arma Reforger servers created before the installed game moved into the data directory**
  keep the game inside their container: every recreate downloads it again (about 10 GB). They
  move to the new layout when their container is created anew. Switch the server off, change
  something on its edit page that needs a new container (the memory limit, for example) and
  save; with the server switched off, saving replaces the stopped container instead of
  recreating it. The first start after that downloads the game once more, into `install/` in
  the data directory; later recreates keep it. A recreate while the server is switched on keeps
  the old layout.

## Logs

```bash
docker compose logs -f console-api       # the dashboard's own log: jobs, warnings, errors
docker compose logs console-backup       # database dumps
docker compose logs console-tls          # certificates (profile https)
```

The logs of a game server are on its **Logs** page, or with `docker logs -f <container name>`.
Sign-ins and admin actions are in the **Audit log** (admins only), kept for
`AUDIT_RETENTION_DAYS` (365).

Docker keeps container logs without a size limit unless you set one. Configure rotation for the
whole host in `/etc/docker/daemon.json` (`"log-opts": {"max-size": "10m", "max-file": "3"}`).

## Host reboots

When the host shuts down, Docker stops every container itself and gives each about 15 seconds,
whatever the container is configured with. That is too short for a large world. Stop the game
servers the dashboard's way before Docker goes down instead (announcement, save, each game's own
grace period). A server stopped like that counts as stopped by hand, so Docker's restart policy
does not bring it back at boot; the record does:

```bash
docker compose exec -T console-api python -m app.cli stop-all        # records and stops the running servers
docker compose exec -T console-api python -m app.cli start-recorded  # starts exactly those again
```

[`examples/systemd/stacks-console.service`](../examples/systemd/stacks-console.service) runs
these two around every reboot. Set its `WorkingDirectory` to your `dashboard/` directory, then:

```bash
sudo cp examples/systemd/stacks-console.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now stacks-console.service
```

At boot the unit first runs `docker compose up -d --wait`, so the database is healthy and the
API has finished its start-up migrations (its healthcheck answers) before the servers start. At
shutdown it is stopped before `docker.service`. `TimeoutStopSec` (900 s) must cover all your
servers' saves together, since they stop one after another.

Tested: `stop-all` and `start-recorded` against a running Minecraft server (stopped in about
eight seconds, started again from the record), `docker compose up -d --wait`, and
`systemd-analyze verify` of the unit (systemd 257, in a container). The unit itself has not run
through a real reboot.
