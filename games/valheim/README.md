# Valheim

A Valheim dedicated server in one Docker Compose service, configured entirely through `.env`.

- Image: [`ghcr.io/community-valheim-tools/valheim-server`](https://github.com/community-valheim-tools/valheim-server-docker)
  (Apache-2.0), published for `linux/amd64` only.
- The image runs SteamCMD itself: it installs the Valheim server from Steam on the first start and
  keeps it up to date, also while the container is running.

## Quick start

```bash
cp .env.example .env
chmod 600 .env
# Set VALHEIM_SERVER_PASS (required) and VALHEIM_SERVER_NAME.
docker compose up -d
docker compose logs -f valheim
```

The first start downloads the server from Steam (`data/server` held 4.1 GB afterwards in our test
run, about 4 minutes including world generation) and then generates the world.
The server is ready when the log shows `Opened Steam server` and `docker compose ps` reports the
container as `healthy`. On the first start of a new world `Game server connected` comes earlier,
while the world is still being generated: we measured 76 s between the two lines, and the Steam
query (server browser, player count) answered only from `Opened Steam server` on. A restart of an
existing world logs both within a second.

Connect in the game with **Join Game → Join IP** and the host's address, e.g. `203.0.113.10`.
With the default port the port can be left out; with another port add it: `example.com:2556`.
Join IP also works when the server is not listed (`VALHEIM_PUBLIC=false`).

The password has two rules, and the server refuses to start if either is broken: at least
**5 characters**, and it must **not be part of the server name**.

## Ports

| Port (default) | Protocol | Purpose | Published |
|---|---|---|---|
| 2456 (`VALHEIM_PORT`) | UDP | Game traffic | yes |
| 2457 (`VALHEIM_QUERY_PORT`) | UDP | Steam query (A2S): server browser listing, player count | yes |
| 2458 (`VALHEIM_CROSSPLAY_PORT`) | UDP | Crossplay backend; some mods use game port + 2 | yes |

Valheim derives the query port from the game port (+1), and the Steam query reports the game
port to clients. Host and container therefore use the same numbers. To move the server, change all
three variables together and keep them consecutive (`2556`, `2557`, `2558`).

The image's optional supervisor web UI (9001/tcp) and status web server (80/tcp) are disabled and
not published. Docker publishes ports past UFW; see [`docs/OPERATIONS.md`](../../docs/OPERATIONS.md)
for a firewall that works.

## Configuration

Valheim has no configuration file for the server: the image builds the server command line from
environment variables. **Every change needs `docker compose up -d`**, which re-creates the
container. `docker compose restart` keeps the old values.

| Variable | Default | Meaning |
|---|---|---|
| `VALHEIM_TAG` | `1.4.0` | Image version (see [Updates](#updates-and-versions)). |
| `VALHEIM_PORT` / `VALHEIM_QUERY_PORT` / `VALHEIM_CROSSPLAY_PORT` | `2456` / `2457` / `2458` | See [Ports](#ports). |
| `VALHEIM_SERVER_NAME` | `My Valheim Server` | Name in the server browser. |
| `VALHEIM_WORLD_NAME` | `Dedicated` | World to load; created if it does not exist. |
| `VALHEIM_SERVER_PASS` | — (required) | Join password: at least 5 characters, not contained in the server name. |
| `VALHEIM_PUBLIC` | `true` | `true`: listed in the server browser. `false`: hidden **and** the query port stops answering. |
| `VALHEIM_CROSSPLAY` | `false` | Accept Xbox, Microsoft Store and other non-Steam clients (switches matchmaking from Steam to PlayFab). |
| `VALHEIM_BEPINEX` | `false` | Load BepInEx mods (see [Mods](#mods)). |
| `VALHEIM_SERVER_ARGS` | empty | Extra Valheim server arguments, passed through unchanged. |
| `VALHEIM_UPDATE_CRON` | `*/15 * * * *` | When to check Steam for a game update. Empty = off. |
| `VALHEIM_RESTART_CRON` | `10 5 * * *` | Daily server restart (image default). Empty = off. |
| `PUID` / `PGID` | `1000` / `1000` | User and group the game runs as; owner of `./data`. |
| `TZ` | `Etc/UTC` | Container time zone (log timestamps, the cron schedules). |

Things to know:

- **`VALHEIM_SERVER_PASS` is required.** If the variable were missing, the image would fall back to
  a default password that is printed in its public README — a listed server with that password is
  effectively open. Compose refuses to start this block without a value. An empty value is
  rejected as well, so this block cannot run a server without a password.
- **Changing `VALHEIM_WORLD_NAME` does not rename the world.** It loads or creates a different one;
  the old world stays in `data/config/worlds_local/` and comes back when you set the old name again.
- **`VALHEIM_PUBLIC=false` also turns off the Steam query.** The server still binds the query port
  but does not answer, so the server browser shows `Server is not responding` and status tools
  (A2S) report nothing. The join password protects a listed server just as well; "public" only
  means "findable".
- The schedules run only while nobody is online (`UPDATE_IF_IDLE` and `RESTART_IF_IDLE` keep their
  image default `true`).
- The image knows many more variables (Discord/hooks, ValheimPlus, log filters, syslog). They are
  documented in the [image README](https://github.com/community-valheim-tools/valheim-server-docker#environment-variables);
  add them to `docker-compose.yml` if you need them.

### Files in `./data`

```text
data/
├── config/                    → /config
│   ├── worlds_local/          the worlds (see Backup)
│   ├── adminlist.txt          admins
│   ├── bannedlist.txt         bans
│   ├── permittedlist.txt      whitelist
│   └── bepinex/               BepInEx config, plugins/, patchers/ (with VALHEIM_BEPINEX=true)
└── server/                    → /opt/valheim: game files from Steam, SteamCMD manifest cache
```

The list files are created by the server on its first start.

## Administration

**This server has no console and no RCON.** The dedicated server reads no commands from stdin and
offers no remote command port, so this block has no `rcon` service, and nothing can kick a player
or trigger a save from outside the game.

Moderation works through the three list files in `data/config/`:

| File | Effect |
|---|---|
| `adminlist.txt` | Admins. They can open the in-game console with **F5** and use admin commands such as `kick` and `ban`. Their game client must be started with the `-console` launch option. Admin rights give no cheat or debug commands. |
| `bannedlist.txt` | Banned players. |
| `permittedlist.txt` | Whitelist. An **empty** file is no whitelist: everyone with the password may join. |

Each entry is a player's SteamID64, one per line, with no comments or extra whitespace on the
line. Player names do nothing in these files. Players see their ID in the game with **F2**; the
server log shows it when they connect.

```text
// List admin players ID  ONE per line
<SteamID64 of the first admin>
<SteamID64 of the second admin>
```

Whether a running server re-reads the files was not verified by the maintainers: restart the
server after editing them (`docker compose restart valheim`).

The image also has `ADMINLIST_IDS`, `BANNEDLIST_IDS` and `PERMITTEDLIST_IDS` variables. When set,
they **overwrite** the files at every container start. This block does not use them, so the files
are the only source.

## Mods

Mods use [BepInExPack Valheim](https://valheim.thunderstore.io/package/denikson/BepInExPack_Valheim/),
which the image downloads and installs itself.

1. Set `VALHEIM_BEPINEX=true` and run `docker compose up -d`. The first start creates
   `data/config/bepinex/`.
2. Stop the server, copy plugin DLLs into `data/config/bepinex/plugins/` (patchers into
   `data/config/bepinex/patchers/`), start it again.

The image syncs these folders into the server installation at every start; a plugin you delete
from `data/config/bepinex/plugins/` is removed from the server at the next sync. Mod settings can
also come from `BEPINEXCFG_*` variables (see the image README).

- Many mods must be installed on every client as well, in a matching version. The mod's own page
  says which side it needs.
- BepInEx is updated by the same schedule as the game, and a game update can break mods at any
  time. After a Valheim patch, check the mods first.
- Some mods do not work with `VALHEIM_CROSSPLAY=true`.
- Plugin files with `VALHEIM_BEPINEX=false` are silently ignored: everything looks installed, and no
  mod is loaded.

## Updates and versions

**The tag is the image version, not the game version.** `VALHEIM_TAG=1.4.0` pins the container
(start scripts, SteamCMD, base OS). The game itself is not pinned:

- At **every container start**, before the server starts, the image installs the current Valheim
  build from Steam. `VALHEIM_UPDATE_CRON` only controls the additional checks while the container
  runs; an update found there is installed and the server restarted (when nobody is online).
- Turning the schedule off keeps the running server on its version until the next start — while
  players' Steam clients update. A client that is newer than the server cannot join, so an old
  server locks everyone out.
- The running game version is in the log, in the line `Valheim version: …`. The version field in
  the Steam query answer is not the game version (observed on a running server).

Updating the image:

1. Read the [image release notes](https://github.com/community-valheim-tools/valheim-server-docker/releases).
2. `./stack backup valheim` (or the manual steps under [Backup](#backup-and-restore)).
3. Set the new `VALHEIM_TAG`, then `docker compose pull && docker compose up -d`.

**Never move a world back to an older game version.** The first time a Valheim 1.0 server opens
an older world it converts it to the new directory format, and that is one-way: older servers
cannot read it afterwards. Keep a backup from before the conversion.

Do not pin an image older than `1.2.0` with a non-root `PUID`: older images set file permissions
on the 1.0 world directories, the server then cannot open its own world, and the process keeps
running without a world.

## Backup and restore

`backup.list` contains:

- `data/config/worlds_local` — the whole folder. A Valheim 1.0 world is a directory of many files
  (`worlds_local/<WORLD_NAME>/`); an older world is `<WORLD_NAME>.db` (the world) plus
  `<WORLD_NAME>.fwl` (seed and metadata), which only load as a pair. The `.old` files next to them
  are the server's own previous saves. Copying the folder keeps all of it together and still works
  after a change of `VALHEIM_WORLD_NAME`.
- `adminlist.txt`, `bannedlist.txt`, `permittedlist.txt` — without them a restored server has no
  admins and lets everyone in.
- `data/config/bepinex` — mod config and plugins; the world may not load correctly without the
  mods it was played with.

`data/server` is not backed up: it is downloaded again from Steam.

**Why the server is stopped:** Valheim saves every 20 minutes and on shutdown, and there is no way
to ask it to save from outside. An archive taken while it writes can capture a half-written world.
`./stack backup valheim` stops the container (the server saves on the way down), archives the
paths, and starts it again.

**The image's own backup feature is turned off** (`BACKUPS: "false"` in `docker-compose.yml`). It
archives `worlds_local` hourly while the server runs; its README states that files can be open
during the copy, and for 1.0 worlds it retries a few times and then keeps a possibly inconsistent
archive with a warning. It also writes into `data/config/backups` on the same disk. If you want it
in addition, change that line to `"true"` (see the image README for `BACKUPS_CRON` and retention).

Restore:

```bash
./stack restore valheim backups/valheim-<UTC timestamp>.tar.gz
```

or by hand: `docker compose stop`, move the paths from `backup.list` aside, unpack the archive in
the block directory (`tar -xzf backups/valheim-….tar.gz`), `docker compose start`. Ownership does
not need fixing: the image changes `./data/config` to `PUID:PGID` at every start. To bring in a
world from a game client, copy it from the client's `worlds_local` folder into
`data/config/worlds_local/` while the server is stopped and set `VALHEIM_WORLD_NAME` to its name.

## Resources

From the image README: about 2.8 GB RAM with nobody online; minimum 2 CPU cores and 4 GB RAM,
recommended 4 fast cores and 8 GB RAM. Most of the work runs in one thread, so a few high-clocked
cores beat many slow ones. Plan disk space for the game files (`data/server`: 4.1 GB after the first start in our
test with image 1.4.0; the image README says about 1 GB for the download) plus the worlds, which
grow with exploration and building. There is no memory limit in this block: a kill during a save can corrupt the world.

## Troubleshooting

- **Server does not start:** check the password rules (at least 5 characters, not in the server
  name).
- **Container runs but stays `unhealthy`:** the server process never opened its port. A common
  cause is a world it cannot read (log: `UnauthorizedAccessException`): check the ownership of
  `data/config/worlds_local` and the image version (see [Updates](#updates-and-versions)). The
  port looking open on the host proves nothing — that is Docker's proxy, not the server.
- **Not in the server browser:** `VALHEIM_PUBLIC=false` hides the server and silences the query
  port; use Join IP. With `true`, check that all three UDP ports reach the host. The in-game
  community list is long; filter by name.
- **Client cannot join after a Valheim patch:** the server picks up the new version at its next
  update check (only when nobody is online) or at the next start: `docker compose restart valheim`.
- **Changed `.env`, nothing happened:** run `docker compose up -d`, not `restart`.
- **Permission denied on `./data`:** after the first start the files belong to `PUID:PGID`
  (default `1000:1000`). Run backups as that user or with `sudo`.
- **`unhealthy` for a few minutes:** expected while the server restarts for an update.
- **World "gone" after changing `VALHEIM_WORLD_NAME`:** it is still in `worlds_local/`; set the old
  name again.

## Licences

This block (compose file, `.env.example`, `backup.list`, this README) is MIT-licensed like the rest
of the repository. The image is Apache-2.0, maintained by community-valheim-tools. The Valheim
dedicated server is proprietary software by Iron Gate; the image downloads it from Steam at
runtime, as it does BepInEx when enabled. Nothing of the game is contained in this repository.
