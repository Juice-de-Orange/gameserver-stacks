# Security Policy

## Reporting a vulnerability

Please **do not** open a public issue for security problems. Report them privately through
GitHub's private vulnerability reporting on this repository: **Security → Report a
vulnerability**. You will receive an acknowledgement within **7 days**.

## Scope

In scope: the compose files and their defaults (published ports, secrets handling, privileges,
mounts), the `stack` helper (backup and restore handle your data), the example firewall and
systemd files, and the documentation where following it would leave a server exposed.

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
