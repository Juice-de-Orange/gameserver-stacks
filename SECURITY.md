# Security Policy

## Reporting a vulnerability

Please **do not** open a public issue for security problems. Report them privately through
GitHub's private vulnerability reporting on this repository: **Security → Report a
vulnerability**. You will receive an acknowledgement within **7 days**.

## Supported versions

Until 1.0, only the latest release (and the `main` branch) receives security fixes. Update
before you report: the issue may already be fixed.

## Scope

In scope: the compose files and their defaults (published ports, secrets handling, privileges,
mounts), the `stack` helper (backup and restore handle your data), the example firewall and
systemd files, the Stacks Console dashboard (`dashboard/`, including its helper images), and the
documentation where following it would leave a server exposed.

Out of scope: vulnerabilities in the game servers themselves or in the third-party images the
blocks use — report those to the game vendor or the image maintainers (each block's README links
its image).

## What the blocks promise

- **Remote consoles are never published.** RCON and admin ports stay on the block's internal
  Docker network; `./stack rcon` reaches them through a short-lived helper container.
- **No default passwords.** Required secrets have no fallback; Compose refuses to start until you
  set them in `.env`.
- **Your `.env`, worlds and backups never enter git** (`.gitignore`).

## Running a public game server

Published game ports are reachable from the internet by design. Docker publishes ports through
its own iptables rules, which bypass host firewalls such as UFW; see
[docs/OPERATIONS.md](docs/OPERATIONS.md) for an allow-list in the `DOCKER-USER` chain. Keep the
images up to date (back up first), use join passwords or allow-lists where the game supports
them, and keep `.env` readable only by you (`chmod 600`).

## Stacks Console: trust model in short

The dashboard in [`dashboard/`](dashboard) controls Docker on its host. The full model is in
[dashboard/docs/security.md](dashboard/docs/security.md); the points that matter most:

- **A dashboard admin is root on the host.** The API creates, starts and removes containers
  and pulls images through a Docker socket proxy, and an admin chooses images and data
  directories. Give the admin role, the `.env` file and shell access to the API container only
  to people you would give root.
- **Three permissions run code in game containers**: the full console, mod uploads and version
  changes. They are admin-only by default.
- **Roles** `admin`, `trusted` and `member`, with sixteen permission flags per role or per user.
  New registrations wait for approval. Passwords are hashed with argon2; failed sign-ins are
  throttled per address and per username.
- **Tokens, not cookies.** The browser keeps signed tokens in `localStorage` and sends them as a
  header, so cross-site request forgery does not apply. WebSockets open with a single-use ticket
  that expires after 30 seconds. A strict Content-Security-Policy (no inline or third-party
  scripts) guards the tokens against injected script.
- **Game passwords** typed into the dashboard are stored encrypted with a key derived from
  `GSS_SECRET_KEY`. The API refuses to start with a missing or short `JWT_SECRET` or
  `GSS_SECRET_KEY`.
- **Nothing is exposed by default.** The web UI listens on `127.0.0.1`; the database and the
  socket proxy are on an internal network without a route outside; remote consoles are never
  published. HTTPS comes from the optional `https` profile or your own proxy.
- **The optional firewall helper** runs in the host's network namespace with `NET_ADMIN`. It only
  ever adds ACCEPT rules for ports in a configured range, in a chain of its own.
