# CLAUDE.md

Guidance for AI coding agents (and humans) working in this repository.

## What this is

A collection of self-contained Docker Compose blocks for game servers, one directory per game under
`games/`, plus a small `stack` helper. Each block must keep working on its own with plain
`docker compose` — the helper and CI are conveniences, never dependencies.

## Rules

- **Follow [docs/CONVENTIONS.md](docs/CONVENTIONS.md)** for every block: pinned image tag through a
  `*_TAG` variable, required secrets with `${VAR:?…}` and no defaults, RCON/admin ports never
  published, data under `./data`, `backup.list` present.
- **Facts need a source.** README statements come from the image documentation, the game's
  documentation, or a run you actually did. Do not invent ports, variables, log lines or behaviour;
  if you cannot verify something, leave it out or mark it as unverified.
- **Verify the tag exists** before pinning it (`docker buildx imagetools inspect <image>:<tag>`).
- **No real infrastructure in examples:** `203.0.113.10`, `example.com`, `My Valheim Server`.
- Never copy scripts out of the third-party images (some are GPL); reference images by name.
- English, plain and precise. Comments explain why.

## Verify before you say it works

```bash
./stack check                                   # every block: validates with values, fails closed without
docker run --rm -v "$PWD:/mnt:ro" -w /mnt koalaman/shellcheck:v0.11.0 stack examples/firewall/*.sh
```

For a block change, also start it (`cp .env.example .env`, fill the secrets, `docker compose up -d`),
watch the logs until the server reports ready, and stop it again; say what you observed.
"Should work" is not a result.

## Layout

| Path | What |
|---|---|
| `games/<game>/` | One block: `docker-compose.yml`, `.env.example`, `README.md`, `backup.list` |
| `stack` | Helper: up/down/logs/rcon/backup/restore/stop-all/check |
| `docs/CONVENTIONS.md` | The block contract |
| `docs/OPERATIONS.md` | Firewall, clean shutdown, backups, updates |
| `examples/` | systemd units and the `DOCKER-USER` allow-list script |
