# Operations

Running several game servers on one host for longer than a weekend. Everything here is optional;
each block runs on its own with `docker compose up -d`.

## Layout on a host

Clone the repository once (any directory; the examples use `/path/to/gameserver-stacks`) and work inside it. Each block keeps
its state next to its compose file:

```text
games/valheim/
├── docker-compose.yml   # from the repository
├── .env                 # yours: secrets and settings (chmod 600, never committed)
├── data/                # yours: world, config, mods (created on first start)
└── backups/             # yours: ./stack backup valheim
```

`git pull` updates compose files and docs without touching `.env`, `data/` or `backups/`.

## Firewall: Docker bypasses UFW

A published port (`ports:` in compose) is opened by Docker's own iptables rules, before UFW or
firewalld see the packet. "ufw deny 2456" does nothing for a container port. What does work is the
`DOCKER-USER` chain, which every forwarded packet passes first.

[`examples/firewall/docker-user-allowlist.sh`](../examples/firewall/docker-user-allowlist.sh) is an
allow-list for the external interface: only the listed ports pass, everything else Docker publishes
there is dropped. The systemd unit next to it re-applies the list after every Docker start (Docker
recreates the chain). Two details that cost time:

- Inside `DOCKER-USER` the destination port is already the **container** port. Match the port the
  client used with `-m conntrack --ctorigdstport`, not `--dport`.
- Remote consoles are not published by any block, so they need no rule. If you publish something
  yourself for testing, it is open to the internet until you remove it.

Test from a machine outside your network (e.g. `nc -vzu 203.0.113.10 2456` for UDP, a game client
for the real check) after every change.

## Clean shutdown and restart

Worlds are saved when the server process receives `SIGTERM`, and some take minutes. Each block sets
a `stop_grace_period` for that. On a host reboot, do not rely on Docker's own shutdown handling to
wait that long; stop the games explicitly first:

- `./stack stop-all` stops every running game (using each block's grace period) and records which
  ones were running in `.stack-running`.
- `./stack start-recorded` starts exactly those again.

[`examples/systemd/gameserver-stacks.service`](../examples/systemd/gameserver-stacks.service) runs
these two on shutdown and boot. Because the games were stopped with `docker compose stop`,
`restart: unless-stopped` does not start them on its own at boot; the unit does, and only the ones
that were running.

## Backups

`./stack backup <game>` stops the server, writes `games/<game>/backups/<game>-<UTC>.tar.gz` with the
paths listed in the block's `backup.list`, and starts the server again. Stopping is deliberate: an
archive of a world that is being written can be unusable, and you find out when you need it.

- **Schedule it** with cron or a systemd timer at a quiet hour, e.g.
  `15 4 * * 0 cd /path/to/gameserver-stacks && ./stack backup valheim`.
- **Prune old archives** yourself, by age and by total size — a large world produces gigabytes per
  archive. Example: `find games/*/backups -name '*.tar.gz' -mtime +28 -delete`.
- **Copy archives off the host.** A backup on the same disk does not survive the disk.
- **Treat archives as secrets.** They contain the game's config files, and those hold the join,
  admin and RCON passwords in plain text (e.g. `server.ini`, the generated `config.json`).
- **Back up before every change** of version, mods or config — and test a restore once:
  `./stack restore <game> <archive>` moves the current data to `backups/pre-restore-<UTC>/` before
  unpacking, so a failed restore is reversible.
- If `./data` belongs to the container's UID and not to you, run the backup with `sudo`.

## Updates

Every block pins its image with a `*_TAG` variable in `.env`. To update:

1. Read the image's release notes and the game's patch notes (world migrations, mod breakage).
2. `./stack backup <game>`.
3. Change the `*_TAG` value, `docker compose pull && docker compose up -d` in the block directory.
4. Watch the logs until the server is ready, then connect with an up-to-date client.

Never move a world back to an older game version after it has been opened by a newer one — most
games cannot load a save from the future, and some corrupt it. Restore the backup from step 2
instead.

Images that download the game at every start (Valheim, Arma Reforger) update the game without a
tag change; their README explains how to control that.

## Several servers of the same game

Copy the block's files without its state
(`rsync -a --exclude data --exclude backups games/valheim/ games/valheim-2/`), give the copy
different host ports and names in its `.env`, and start it. The compose project name comes from the directory name, so the
containers and networks do not collide.
