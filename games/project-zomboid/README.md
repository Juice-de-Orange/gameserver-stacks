# Project Zomboid

A Project Zomboid **Build 42** dedicated server with RCON kept internal, Steam Workshop mods managed
in the server's `.ini`, and a clean save on stop.

Image: [`danixu86/project-zomboid-dedicated-server`](https://hub.docker.com/r/danixu86/project-zomboid-dedicated-server)
(source: [Danixu/project-zomboid-server-docker](https://github.com/Danixu/project-zomboid-server-docker),
GPL-3.0). This block only references the image by name.

## Quick start

```bash
cp .env.example .env
# fill ZOMBOID_ADMIN_PASSWORD and ZOMBOID_RCON_PASSWORD (openssl rand -base64 24),
# optionally ZOMBOID_PASSWORD (join password); avoid `$` in values, Compose would interpolate it
docker compose up -d
docker compose logs -f zomboid
```

The image is about 2.5 GB to download (compressed), because the game server is part of it. The
server is ready when the log shows `SERVER STARTED`:

```bash
docker compose logs zomboid | grep 'SERVER STARTED'
```

Connect from the game: **Join → Favourites**, enter the host address (`203.0.113.10` in these
docs), port `16261` and the join password, or use `steam://connect/203.0.113.10:16261`. Log in as
`ZOMBOID_ADMIN_USERNAME` with `ZOMBOID_ADMIN_PASSWORD` to get admin rights in game.

## Ports

| Port | Protocol | Purpose | Exposure |
|---|---|---|---|
| `16261` (`ZOMBOID_PORT`) | UDP | Game port (`DefaultPort`) | published |
| `16262` (`ZOMBOID_UDP_PORT`) | UDP | Direct connections (`UDPPort`) | published |
| `27015` (`ZOMBOID_RCON_PORT`) | TCP | RCON | internal only, never published |

Both UDP ports use the same number inside and outside the container, and the block passes them to
the server (`PORT`, `UDPPORT`), so changing a value in `.env` moves the listener and the published
port together. A second copy of this block on the same host needs two other free UDP ports.

The image README also lists `8766`/`8767` UDP and a TCP range per player slot. Those come from
older guides; this block publishes only the two UDP ports above, which is what Build 42 uses.

## Configuration

| Variable | Default | What |
|---|---|---|
| `ZOMBOID_TAG` | `42.21-release-2` | Image tag = game build. See [Updates and versions](#updates-and-versions). |
| `ZOMBOID_SERVER_NAME` | `servertest` | Internal name: file names and world folder. No spaces or special characters. |
| `ZOMBOID_DISPLAY_NAME` | `My Project Zomboid Server` | Name in the server browser (`PublicName`). |
| `ZOMBOID_PUBLIC` | `false` | List the server in the in-game browser (`Public`). |
| `ZOMBOID_PORT` | `16261` | UDP game port. |
| `ZOMBOID_UDP_PORT` | `16262` | UDP direct-connection port (`UDPPort`). |
| `ZOMBOID_RCON_PORT` | `27015` | RCON port for the `rcon` tool; must equal `RCONPort` in the `.ini`. |
| `ZOMBOID_MIN_MEMORY` / `ZOMBOID_MAX_MEMORY` | `2048m` / `4096m` | JVM heap (`-Xms` / `-Xmx`). |
| `ZOMBOID_ADMIN_USERNAME` | `admin` | Admin account created with the server data. |
| `ZOMBOID_ADMIN_PASSWORD` | — (required) | Admin password; sets or changes it at every start. |
| `ZOMBOID_RCON_PASSWORD` | — (required) | RCON password; also enables RCON. |
| `ZOMBOID_PASSWORD` | empty | Join password. Empty keeps the `.ini` value. |
| `RCON_CLI_TAG` | `1.7.7` | Tag of `itzg/rcon-cli` for the `rcon` tool. |

### Files in `./data`

| Path | Content |
|---|---|
| `data/zomboid/Server/<server name>.ini` | Server settings |
| `data/zomboid/Server/<server name>_SandboxVars.lua` | World rules (sandbox) |
| `data/zomboid/Server/<server name>_spawnregions.lua` | Spawn locations offered to players |
| `data/zomboid/Saves/Multiplayer/<server name>/` | The world |
| `data/zomboid/db/<server name>.db` | Player accounts, access levels, whitelist |
| `data/zomboid/Logs/` | Game logs (chat, joins, deaths are written here, not to `docker logs`) |
| `data/workshop/` | Steam Workshop downloads |

### Which `.ini` keys come from `.env`

At every start the image writes these keys into `<server name>.ini` from the environment. Change
them in `.env` and recreate the container (`docker compose up -d`); an edit in the `.ini` is
overwritten on the next start:

| `.ini` key | From |
|---|---|
| `Password` | `ZOMBOID_PASSWORD` (only when not empty) |
| `RCONPassword` | `ZOMBOID_RCON_PASSWORD` |
| `Public` | `ZOMBOID_PUBLIC` |
| `PublicName` | `ZOMBOID_DISPLAY_NAME` (only when not empty) |
| `UDPPort` | `ZOMBOID_UDP_PORT` |

The game port, admin user and admin password are passed as command-line arguments rather than
`.ini` keys. Since `SELF_MANAGED_MODS=true` is set, the image leaves `Mods` and `WorkshopItems`
alone. It does add maps it finds in downloaded Workshop items to `Map=` (see [Mods](#mods)).

Every other key (`PVP`, `MaxPlayers`, `Open`, `PublicDescription`, …) and the sandbox file are
yours: edit them while the server is stopped, then start it. On a new server the image creates the
`.ini` before the first start, so the values from `.env` already apply then; the game fills in all
other keys with its defaults.

To remove a join password, empty `ZOMBOID_PASSWORD` **and** clear `Password=` in the `.ini` while
the server is stopped — an empty variable alone leaves the old value in place.

## Administration

RCON is enabled by `ZOMBOID_RCON_PASSWORD` and reachable only on the block's network. The `rcon`
tool service runs one command and exits:

```bash
docker compose run --rm rcon players
docker compose run --rm rcon save
docker compose run --rm rcon servermsg "Restart in 5 minutes"
docker compose run --rm rcon kickuser "PlayerName"
docker compose run --rm rcon setaccesslevel "PlayerName" moderator
docker compose run --rm rcon addusertowhitelist "PlayerName"
docker compose run --rm rcon reloadoptions   # re-read the .ini while running
```

Access levels: `admin`, `moderator`, `overseer`, `gm`, `observer`, `none`.

Stop the server with `docker compose stop` (or `down`), not with the `quit` console command:
`quit` also saves and exits, but `restart: unless-stopped` then starts the container again.

The admin password is a command-line argument, and the game prints its arguments to the log at
every start. Anyone who can read `docker compose logs` can see it — the same people who can read
`.env`.

## Mods

Mods come from the Steam Workshop and are configured in `data/zomboid/Server/<server name>.ini`
(edit while stopped):

- `WorkshopItems=` — the numeric Workshop IDs, separated by `;`. The server downloads them into
  `data/workshop/` at start.
- `Mods=` — the mod IDs from each item's `mod.info`, separated by `;`. In Build 42 each entry
  starts with a backslash: `Mods=\ModA;\ModB`.
- `Map=` — map folders, `;`-separated, with `Muldraugh, KY` last. The image adds maps it finds in
  downloaded Workshop items at every start and keeps the base map last; it never removes one.

This block sets `SELF_MANAGED_MODS=true`, so the `.ini` is the only source for `Mods` and
`WorkshopItems`. The image can instead take them from `MOD_IDS` / `WORKSHOP_IDS`, but then it
rewrites both keys at every start and **clears** them when the variables are empty.

Things that cost time:

- A Workshop item that only ships a Build 41 version (`41/` folder, no `42/` or `common/`) does not
  load on Build 42, even though its files are downloaded.
- The server loads every **downloaded** map, listed or not. Removing a map mod means deleting its
  download from `data/workshop/content/108600/<id>/` and its entry in `Map=`.
- A world is tied to the maps it was generated with. Removing a map the world already used breaks
  those areas; switching maps means a fresh world. Back up before any mod change.
- The image README says new mods install on the second start. If a mod is missing after the first
  start, restart once (not verified by the maintainers on this tag).

Clients must run the same game build and the same mod set as the server.

## Updates and versions

The server files are downloaded into the image when the image is built, so **the tag is the game
build**: `42.21-release-2` is Build 42.21 from Steam's default (public) branch, image revision 2.
A pinned tag never changes; rebuilds of the same game version get a new `-N` suffix. Nothing in
this block updates the game on its own.

Steam's branch is chosen when the image is built (`STEAMAPPBRANCH`, `public` in `-release` tags).
At runtime the image only reads it together with `FORCEUPDATE`, which downloads the game at every
start and makes the running build differ from the tag — this block sets neither. Build 42 is on the
public branch, so use `-release` tags; `-unstable` tags come from a beta branch.

Why the update has to be deliberate: Steam updates players' clients automatically. Once a new
build is out, a server that still runs the previous one cannot be joined by updated clients — in
practice the join attempt hangs rather than reporting a version mismatch. So watch for new
`-release` tags after a game update and bump soon, but on purpose:

```bash
./stack backup project-zomboid      # or stop + tar the paths in backup.list
# set ZOMBOID_TAG in .env to the new *-release tag
docker compose pull && docker compose up -d
docker compose logs -f zomboid      # wait for SERVER STARTED, then join once
```

Never start a world with an older build than the one that last saved it. To roll back, restore the
backup taken before the update together with the old tag.

If the host runs an automatic image updater such as Watchtower, exclude this container (Watchtower:
label `com.centurylinklabs.watchtower.enable=false`), otherwise the pin means nothing.

## Backup and restore

`backup.list` contains `data/zomboid/Saves` (the world), `data/zomboid/Server` (configuration
including the mod lists) and `data/zomboid/db` (accounts and whitelist). `data/workshop` is left
out because the server downloads it again from `WorkshopItems=`; add it if you need to survive an
item disappearing from the Workshop.

Back up with the server stopped:

```bash
./stack backup project-zomboid
# or by hand, from this directory:
docker compose stop
mkdir -p backups
sudo tar -czf backups/zomboid-$(date -u +%Y%m%dT%H%M%SZ).tar.gz data/zomboid/Saves data/zomboid/Server data/zomboid/db
docker compose start
```

Restore (same or newer `ZOMBOID_TAG` than the backup was made with):

```bash
docker compose stop
sudo mv data/zomboid data/zomboid.before-restore
sudo tar -xzf backups/<archive>.tar.gz
docker compose start
```

or `./stack restore project-zomboid backups/<archive>.tar.gz`.

## Resources

- **RAM:** the heap is `ZOMBOID_MIN_MEMORY`/`ZOMBOID_MAX_MEMORY` (the image's own examples use
  `2048m`/`4096m`). Raise `ZOMBOID_MAX_MEMORY` with more players, a larger explored world and more
  mods; the host needs free memory beyond it for the JVM itself and the OS. The block sets no hard
  container memory limit, because a kill during a save corrupts the world.
- **Disk:** about 2.5 GB compressed for the image, plus the world (grows as players explore) and the
  Workshop downloads (map mods can be large).
- **CPU:** not measured for this block.

## Troubleshooting

- **Stop takes a while.** Expected: the entrypoint sends `quit` and waits for the save, up to the
  120 s grace period. A clean stop logs `Shutdown signal received` and
  `Server stopped cleanly with exit code 0`. Older image builds had no such handler and were killed
  after the grace period, falling back to the last autosave. Measured with `42.21-release-2` on a
  fresh world: `docker compose stop` took 12 s, the log showed `Saving finish` and
  `Server stopped cleanly with exit code 0`, exit code 0. A large world takes longer.
- **Host reboot.** When the Docker daemon itself shuts down it uses its own `shutdown-timeout`,
  which can be shorter than a save. Stop the block (or run `save` via RCON) before rebooting.
- **Setting in the `.ini` reverts.** It is one of the keys in
  [the table above](#which-ini-keys-come-from-env); change it in `.env`.
- **Permissions.** The container runs its entrypoint as root and hands `data/zomboid` and
  `data/workshop` to the image's `steam` user (UID 1000) at every start. Edit files as that UID or
  with `sudo`.
- **Updated clients cannot join / hang on connect.** The server lags behind the game build; see
  [Updates and versions](#updates-and-versions).
- **Not in the server browser.** `ZOMBOID_PUBLIC=true`, both UDP ports reachable from outside.
  Hidden is not closed: anyone with the address and join password can still connect directly.
- **Firewall.** Docker publishes ports through its own iptables rules, past UFW; restrict published
  ports in the `DOCKER-USER` chain (see [docs/OPERATIONS.md](../../docs/OPERATIONS.md)).
- **Admin login fails.** `ZOMBOID_SERVER_NAME` contains spaces or special characters, or the admin
  was created under a different `ZOMBOID_ADMIN_USERNAME` (renaming it later has no effect).
- **Very slow start on Windows/WSL2.** The image README warns that Docker Desktop on WSL2 makes
  start-up much slower, especially with many mods; use a Linux host or VM.

## Licences

This block (the files in this directory) is MIT, like the rest of the repository. The image
`danixu86/project-zomboid-dedicated-server` is GPL-3.0 and is only referenced, never copied. The
Project Zomboid dedicated server is proprietary software by The Indie Stone; it is downloaded from
Steam when the image is built and is not part of this repository. `itzg/rcon-cli` has its own
licence.
