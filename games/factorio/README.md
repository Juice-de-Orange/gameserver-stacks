# Factorio

A Factorio **2.0** headless server, running as UID 1000, with RCON kept internal, a save on stop,
and server settings that start private.

Image: [`factoriotools/factorio`](https://hub.docker.com/r/factoriotools/factorio)
(source: [factoriotools/factorio-docker](https://github.com/factoriotools/factorio-docker), MIT),
used in its `-rootless` variant. This block only references the image by name.

## Quick start

```bash
cp .env.example .env
# fill FACTORIO_RCON_PASSWORD (openssl rand -base64 24); avoid `$` in values, Compose would
# interpolate it
mkdir -p data && sudo chown 1000:1000 data   # the server runs as UID 1000 (no-op if that is you)
docker compose up -d
docker compose logs -f factorio
```

The image is about 210 MB to download (compressed, amd64); the game server is part of it, and
nothing else is downloaded at start (except mod updates, if you enable them). On the first start the server creates `data/config/` and a new map
`data/saves/_autosave1.zip`. It is ready when the log shows the state change to `InGame`:

```bash
docker compose logs factorio | grep 'to(InGame)'
```

Then stop it, set a name and description in `data/config/server-settings.json` (see
[Configuration](#configuration)), and start it again:

```bash
docker compose stop
"${EDITOR:-nano}" data/config/server-settings.json   # as UID 1000 or with sudo
docker compose start
```

Connect from the game: **Multiplayer → Connect to address** → `203.0.113.10:34197` (your host's
address). The client version must be exactly `FACTORIO_TAG`.

## Ports

| Port | Protocol | Purpose | Exposure |
|---|---|---|---|
| `34197` (`FACTORIO_PORT`) | UDP | Game | published |
| `27015` | TCP | RCON | internal only, never published |

The game protocol is UDP only (RCON is the one TCP port). Factorio reports its own port to the matching server (server browser), so the
block uses `FACTORIO_PORT` on both sides and passes it to the server (`PORT`). Mapping a different
host port onto `34197` works for direct connections but gives the server browser the wrong port.

## Configuration

| Variable | Default | What |
|---|---|---|
| `FACTORIO_TAG` | `2.0.77` | Game version; the compose file appends `-rootless` |
| `FACTORIO_PORT` | `34197` | UDP game port, host and container |
| `FACTORIO_LOAD_LATEST_SAVE` | `true` | `true`: load the newest file in `data/saves`; `false`: load `FACTORIO_SAVE_NAME` |
| `FACTORIO_GENERATE_NEW_SAVE` | `false` | `true`: create `data/saves/<FACTORIO_SAVE_NAME>.zip` if it does not exist |
| `FACTORIO_SAVE_NAME` | `world` | Save name without `.zip`, used by the two variables above |
| `FACTORIO_DLC_SPACE_AGE` | `true` | Space Age mods on (`true`), off (`false`) or a space-separated subset |
| `FACTORIO_UPDATE_MODS_ON_START` | `false` | Update enabled mods from the mod portal at every start |
| `FACTORIO_RCON_PASSWORD` | required | RCON password, written to `data/config/rconpw` at every start |
| `RCON_CLI_TAG` | `1.7.7` | Tag of `itzg/rcon-cli` for the `rcon` tools service |

### Files in `./data`

| Path | What | Rewritten at start? |
|---|---|---|
| `config/server-settings.json` | Name, description, visibility, credentials, game password, autosave | Created once, never overwritten |
| `config/rconpw` | RCON password | **Yes**, from `FACTORIO_RCON_PASSWORD` |
| `config/map-gen-settings.json`, `config/map-settings.json` | Settings for new maps | Created once from the game's examples |
| `config/server-adminlist.json`, `server-banlist.json`, `server-whitelist.json` | Player lists | Not created; you or the server write them |
| `mods/` | Mod zips and `mod-list.json` | `mod-list.json`: the Space Age entries are set from `FACTORIO_DLC_SPACE_AGE` |
| `saves/` | Maps, one `.zip` each, plus `_autosaveN.zip` | Leftover `*.tmp.zip` (interrupted saves) are deleted |

The server reads `server-settings.json` only at start: stop it, edit, start. Keys worth setting
(from the game's `server-settings.example.json`):

- `name`, `description` — shown in the server browser.
- `visibility.public` — `true` lists the server on the official matching server and **requires**
  `username` and `token` (below). The image's own default is `true`; with that and no
  credentials the server does not start (upstream issue
  [#507](https://github.com/factoriotools/factorio-docker/issues/507)), so this block creates the
  file with `false` on the first start. Hidden is not closed: anyone with the address can connect,
  unless you set `game_password` or a whitelist.
- `visibility.lan` — LAN broadcast; the image README says clients find LAN games only when the
  container uses host networking.
- `require_user_verification` — `true` (the game's default) admits only clients with a valid
  factorio.com account.
- `game_password` — join password.
- `autosave_interval` (minutes) and `autosave_slots` — the autosaves cycle through
  `_autosave1.zip` … `_autosaveN.zip`.

**Credentials (`username`, `token`).** Needed for `visibility.public` and for mod portal
downloads (`FACTORIO_UPDATE_MODS_ON_START`). Use your factorio.com username and the
authentication **token** from <https://factorio.com/profile> (also stored in your own game
install's `player-data.json`). The example file also has a `password`
field: leave it empty. The token "may be used instead of" the password (the game's own comment),
it can be revoked on its own, and the account password is never needed by the server. The file
holds the token in plain text; the block creates it with mode `0600`. Never commit it.

**Map generation.** A new map uses `config/map-gen-settings.json` and `config/map-settings.json`.
The image also accepts a `PRESET` variable (e.g. `death-world`, `rail-world`) when it creates a
map; to use it, add it under `environment:` yourself. To start a fresh map: stop, move the files
out of `data/saves`, start (or set `FACTORIO_GENERATE_NEW_SAVE=true` and a new
`FACTORIO_SAVE_NAME`, with `FACTORIO_LOAD_LATEST_SAVE=false`).

**Load an older save.** With `FACTORIO_LOAD_LATEST_SAVE=true` the newest file wins, and autosaves
are files too. To pin one map, set `FACTORIO_LOAD_LATEST_SAVE=false` and `FACTORIO_SAVE_NAME` to
its name without `.zip`, then `docker compose up -d`.

### Why the rootless variant

The regular image starts as root, changes the owner of the whole volume (`chown -R`) to its
`PUID`/`PGID` on every start, and runs Factorio under `runuser`. Two consequences:

- `docker stop` sends `SIGTERM` to `runuser`, which passes it on and kills Factorio two seconds
  later, in the middle of the save on stop (upstream issue
  [#599](https://github.com/factoriotools/factorio-docker/issues/599); visible in the log as
  `Session terminated, killing shell... ...killed.`). A longer `stop_grace_period` does not help.
- Setting `user:` on the regular image breaks its entrypoint; the image's permission guide warns
  about this, and it was observed by the maintainers.

The `-rootless` images run as UID 1000 from the start and execute Factorio as PID 1, so `SIGTERM`
reaches the server directly and the 120 s grace period applies. The price: the UID is fixed to
1000 (no `PUID`/`PGID`), `./data` must be writable by UID 1000 before the first start, and
upstream labels the variant experimental.

## Administration

RCON is real Source RCON on port 27015 inside the block network. The image reads the password only
from the file `config/rconpw` (it would generate a random one if the file were missing); this block
writes that file from `FACTORIO_RCON_PASSWORD` at every start, so the `rcon` tools service and the
server always agree. After changing the password in `.env`, run `docker compose up -d`.

```bash
docker compose run --rm rcon /players online
docker compose run --rm rcon /server-save
docker compose run --rm rcon /promote PlayerName
# or from the repository root: ./stack rcon factorio /players online
```

The image also contains its own client, which reads `rconpw` itself:
`docker compose exec factorio rcon /players online`.

Useful commands (from the Factorio wiki's console page): `/players [online|count]`, `/admins`,
`/promote <player>`, `/demote <player>`, `/kick <player> <reason>`, `/ban <player> <reason>`,
`/unban <player>`, `/whitelist add|remove|get|clear [player]`, `/server-save`, `/version`.
An empty whitelist lets everyone in. Bans go by account name, not IP. Admins, bans and the
whitelist can also be prepared as JSON arrays of names in `config/server-adminlist.json`,
`server-banlist.json` and `server-whitelist.json` while the server is stopped.

The container healthcheck runs `rcon /players count` with the image's client every minute; it
turns healthy once the map is loaded and RCON accepts the password.

**The RCON password appears in `docker compose logs`.** The image's entrypoint runs with shell
tracing (`set -x`) and prints the full server command line, including `--rcon-password <value>`, at
every start (seen with `2.0.77-rootless`). Anyone who can run `docker` on the host can read it
anyway (`docker inspect` shows the environment), but do not paste these logs anywhere and do not
ship them to an external log service without filtering that line.

## Mods

Factorio compares mods **exactly**: every client needs the same mods in the same versions as the
server, and the same game version. A missing or different mod does not show up as an error in the
server log; the player is refused when joining.

- **Install:** stop the server, copy the mod `.zip` files into `data/mods/`, make sure
  `data/mods/mod-list.json` lists them with `"enabled": true`, start. The simplest way to get a
  consistent set is to copy the zips and `mod-list.json` from a client where that exact set works.
- **Dependencies:** each mod's `info.json` declares the mods it needs. Nothing in this block or
  the image resolves them for you: install every required dependency yourself, in a compatible
  version.
- **Space Age:** `space-age`, `quality` and `elevated-rails` come with the game and are never
  files in `data/mods/`. `FACTORIO_DLC_SPACE_AGE` switches them in `mod-list.json` at every start.
  With them enabled, players need the expansion to join (not verified by the maintainers).
- **`FACTORIO_UPDATE_MODS_ON_START=true`** updates the mods that are already enabled to the newest
  release compatible with the game version, before the server starts. It needs `username` and
  `token` in `server-settings.json`; without them, the image README says, the server does not
  start. It does **not** install new mods or missing dependencies (it only checks the `base`
  version requirement), and it changes the mod set without you looking, after which players with
  the old set cannot join. Leave it off unless you update clients the same way.

## Updates and versions

The image tag is the game version (`2.0.77`). The image publishes `stable` (versions factorio.com
declared stable) and `latest` (may be experimental); at the time of writing `2.0.77` is the current
stable release and `2.1.x` is experimental. This block pins an exact version and appends
`-rootless`.

The game client usually updates itself (Steam does it automatically), and a client that is
even one patch ahead cannot join. Update the server when a new stable version comes out:

```bash
./stack backup factorio                  # from the repository root
# set FACTORIO_TAG in .env to the new version (the -rootless tag must exist)
docker compose pull && docker compose up -d
```

**Never downgrade a world.** Once a map has been saved by a newer version, the older server refuses
it:

```text
Map version 2.1.14-1 cannot be loaded because it is higher than the game version (2.0.77-0).
```

The way back is the backup from before the update, not a lower tag. Order matters, because the
server saves the map in *its* version when it stops:

1. `docker compose down`
2. set `FACTORIO_TAG` back to the old version
3. restore the backup from before the update (`./stack restore factorio <archive>`)
4. `docker compose up -d`

Treat a jump to a new major line (2.0 → 2.1) as one-way and take a backup first.

## Backup and restore

`backup.list` contains `data/saves`, `data/config` and `data/mods`. The mods belong to the world
(a save needs its mod set), and `config` holds the server settings, the player lists and the RCON
password. Because `config` contains the factorio.com token, keep the archives private.

```bash
./stack backup factorio                  # stop → archive → start, from the repository root
# or by hand, from this directory:
docker compose stop
sudo tar -czf "backups/factorio-$(date -u +%Y%m%dT%H%M%SZ).tar.gz" data/saves data/config data/mods
docker compose start
```

`rconpw` and `server-settings.json` are readable only by UID 1000; use `sudo` if you are another
user. Restore: `./stack restore factorio <archive>`, or stop, move `data/saves`, `data/config` and
`data/mods` aside, unpack the archive in this directory and start.

`/server-save` over RCON writes the map while the server keeps running, if you want a fresh save
before copying files.

## Resources

Not measured for this README. Memory and CPU grow with map size, entity count and mods; check
with `docker stats` as the factory grows. Disk: the image (about 210 MB compressed) plus the saves,
`autosave_slots` copies of the map on top of the named save. The block sets no memory limit: a kill
during a save loses the save.

## Troubleshooting

- **Container exits at once with `Permission denied` under `/factorio`.** `./data` is not writable
  by UID 1000 (Docker created it as root). `sudo chown -R 1000:1000 data`, then start again.
  Running Docker itself in rootless mode maps UIDs differently; see the image's
  [permission guide](https://github.com/factoriotools/factorio-docker/blob/master/PERMISSION_ISSUES_GUIDE.md).
- **`Error when creating server game: Missing token.`** `visibility.public` is `true` without
  `username`/`token` in `server-settings.json`. Add the credentials or set it to `false`.
- **Client cannot join, version mismatch.** Client and server versions differ; compare the client's
  version with `FACTORIO_TAG` (`docker compose run --rm rcon /version`).
- **Client cannot join, mod mismatch.** See [Mods](#mods); the server log looks healthy.
- **Listed in the server browser, but nobody can connect.** If the log shows
  `Own address is RIGHT IP:WRONG PORT`, Docker's userland proxy changed the UDP source port. The
  image README's fix is to run the Docker daemon with `--userland-proxy=false`.
- **A restored or chosen save is not loaded.** With `FACTORIO_LOAD_LATEST_SAVE=true` the newest
  file in `data/saves` wins, including autosaves; pin the map with `FACTORIO_SAVE_NAME` (see
  [Configuration](#configuration)).
- **RCON login fails.** `.env` changed but the container was not recreated; `docker compose up -d`
  rewrites `rconpw`. Avoid `$` in the password.
- **Firewall.** Docker publishes ports through its own iptables rules, past UFW; restrict published
  ports in the `DOCKER-USER` chain (see [docs/OPERATIONS.md](../../docs/OPERATIONS.md)).

## Licences

This block (the files in this directory) is MIT, like the rest of the repository. The image
`factoriotools/factorio` (factorio-docker) is MIT and is only referenced, never copied. The
Factorio headless server is proprietary software by Wube Software; it is downloaded from
factorio.com when the image is built and is not part of this repository. `itzg/rcon-cli` has its
own licence.
