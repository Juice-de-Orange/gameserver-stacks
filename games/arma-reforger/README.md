# Arma Reforger

An Arma Reforger dedicated server with BattlEye RCON kept internal, required RCON and admin
passwords, PC-only by default, and saves, config and mods under `./data`.

Image: [`ghcr.io/acemod/arma-reforger`](https://github.com/acemod/docker-reforger/pkgs/container/arma-reforger)
(source: [acemod/docker-reforger](https://github.com/acemod/docker-reforger), MIT). This block only
references the image by name. The image installs the game server through SteamCMD (anonymous
login, Steam app `1874900`) and updates it **on every container start**.

## Quick start

```bash
cp .env.example .env
# fill REFORGER_RCON_PASSWORD and REFORGER_ADMIN_PASSWORD (openssl rand -hex 12; no spaces),
# set REFORGER_SERVER_NAME, and REFORGER_ADMINS if you want admins / Game Masters
docker compose up -d
docker compose logs -f reforger
```

The image itself is about 60 MB (compressed, amd64). The first start then downloads the dedicated
server from Steam: about 10.3 GB (SteamCMD reported 10 278 170 614 bytes in our test on
2026-10-02); the container turned `healthy` about 15 minutes after the start. The download lives
inside the container, so plan that disk space once per container. Mods listed in the config are downloaded by the server after that.

The image has a built-in health check that sends an A2S query to the server. It allows 15 minutes
for the first install and boot before failures count. The server is up when the container reports
`healthy`:

```bash
docker compose ps reforger      # STATUS ... (healthy)
```

Connect from the game: find the server by name in the server browser, or use direct join with
`203.0.113.10:2001` (your host's address and `REFORGER_PORT`).

## Ports

| Port | Protocol | Purpose | Published |
|---|---|---|---|
| `REFORGER_PORT` (2001) | UDP | Game traffic; this port is registered with the Bohemia backend | yes |
| `REFORGER_A2S_PORT` (17777) | UDP | A2S / Steam query (server info for tools and the health check) | yes |
| 19999 | UDP | BattlEye RCON | **no**, block network only |

The game port is announced to the backend (`publicPort`), so host and container port must be the
same number: the compose file uses `REFORGER_PORT` on both sides and passes it to the server as bind
and public port. The same is done for A2S.

If your host has a firewall, open both UDP ports for new inbound traffic. Docker-published ports
bypass UFW; see [docs/OPERATIONS.md](../../docs/OPERATIONS.md#firewall-docker-bypasses-ufw).

## Configuration

The server reads one JSON config. The image offers two ways to get it, selected by
`REFORGER_CONFIG` (the image's `ARMA_CONFIG`):

- **`docker_generated` (default).** On every start the image writes
  `data/config/docker_generated.json` from a bundled template and the environment variables below.
  Edits to that file are lost at the next start; change `.env` and recreate the container
  (`docker compose up -d`) instead.
- **A file name, e.g. `config.json`.** The image uses `data/config/<name>` exactly as it is and does
  not modify it. The game and RCON variables in `.env` are then ignored. See
  [Using your own config.json](#using-your-own-configjson).

| Variable | Default | What |
|---|---|---|
| `REFORGER_TAG` | `sha-d042142` | Image tag; see [Updates and versions](#updates-and-versions) |
| `REFORGER_PORT` | `2001` | UDP game port, same on host and container, announced to the backend |
| `REFORGER_A2S_PORT` | `17777` | UDP A2S query port |
| `REFORGER_PUBLIC_ADDRESS` | empty | Public IPv4 registered with the backend; empty = detected by the backend |
| `REFORGER_CONFIG` | `docker_generated` | Generated config, or a file name in `data/config/` used verbatim |
| `REFORGER_SERVER_NAME` | `My Arma Reforger Server` | Name in the server browser (up to 100 characters) |
| `REFORGER_SCENARIO_ID` | empty | Scenario `.conf` path; empty = image default, Conflict - Everon |
| `REFORGER_MAX_PLAYERS` | `32` | 1..128 |
| `REFORGER_VISIBLE` | `true` | Listed in the server browser |
| `REFORGER_GAME_PASSWORD` | empty | Join password; empty = open server |
| `REFORGER_ADMINS` | empty | Comma-separated Bohemia identity IDs and/or Steam IDs, no spaces |
| `REFORGER_SUPPORTED_PLATFORMS` | `PLATFORM_PC` | Allowed platforms; see [Crossplay](#crossplay) |
| `REFORGER_CROSS_PLATFORM` | `false` | `true` accepts all platforms |
| `REFORGER_MODS` | empty | Workshop mod IDs, `ID` or `ID=version`, comma-separated, no spaces |
| `REFORGER_MAX_FPS` | `120` | Server frame-rate cap |
| `REFORGER_PARAMS` | empty | Extra startup parameters, e.g. `-loadSessionSave` |
| `REFORGER_RCON_PASSWORD` | **required** | BattlEye RCON password, 3..32 characters, no spaces |
| `REFORGER_ADMIN_PASSWORD` | **required** | In-game admin password for `#login`, no spaces |

Avoid `$` in values: Compose would try to interpolate it.

The image exposes more settings as environment variables (view distance, BattlEye, persistence,
operating options, RCON allow/deny lists); they are listed in the
[image README](https://github.com/acemod/docker-reforger#parameters). To use one, add it to the
`environment:` list of the `reforger` service, or switch to your own `config.json`. Bohemia
documents every config field on the
[Server Config](https://community.bistudio.com/wiki/Arma_Reforger:Server_Config) wiki page.

### Scenario and Game Master

Only one scenario runs at a time. Official scenario IDs from the Bohemia wiki:

| Scenario | `REFORGER_SCENARIO_ID` |
|---|---|
| Conflict - Everon (image default) | `{ECC61978EDCC2B5A}Missions/23_Campaign.conf` |
| Game Master - Everon | `{59AD59368755F41A}Missions/21_GM_Eden.conf` |
| Game Master - Arland | `{2BBBE828037C6F4B}Missions/22_GM_Arland.conf` |
| Conflict - Arland | `{C41618FD18E9D714}Missions/23_Campaign_Arland.conf` |
| Combat Ops - Everon | `{DFAC5FABD11F2390}Missions/26_CombatOpsEveron.conf` |

The wiki page lists more. Scenarios that come from mods have their own IDs, documented by the mod.
Adding `-listScenarios` to `REFORGER_PARAMS` makes the server print scenario paths to its log; per the
wiki, Workshop scenarios are only listed when no scenario is loaded.

Keep the braces when you put an ID into `.env`, and quote it:
`REFORGER_SCENARIO_ID='{59AD59368755F41A}Missions/21_GM_Eden.conf'`.

A player gets the Game Master (Zeus) menu when **both** are true: the player is an admin (listed in
`REFORGER_ADMINS`, or logged in with `#login`), and a Game Master scenario is running. Without a
Game Master scenario there is no Game Master menu, admin or not.

### Crossplay

`REFORGER_SUPPORTED_PLATFORMS` takes `PLATFORM_PC`, `PLATFORM_XBL` (Xbox) and `PLATFORM_PSN`
(PlayStation). `REFORGER_CROSS_PLATFORM=true` adds the missing ones. According to the Bohemia wiki
a vanilla server can be PC only or PC with all consoles; PC with Xbox only is accepted on a modded
server.

**PlayStation and mods:** the Bohemia wiki (Server Config, state of early 2025) says that
`crossPlatform` does not add `PLATFORM_PSN` on modded servers. The maintainers' earlier setup kept
PlayStation off on its modded server so that mods stayed active. Newer game versions may allow
PlayStation on modded servers with restrictions on some mod types; that was **not verified by the
maintainers**. This block therefore defaults to PC only. If you run mods and add `PLATFORM_PSN`,
check the server log after the start and test with a console client.

### Using your own config.json

Use this when you need settings the image does not map to variables, or want the whole config in
one file:

```bash
docker compose stop                                 # if it is running
sudo mkdir -p data/config                           # data/ is owned by root once the server ran
sudo cp config.example.json data/config/config.json
sudo "${EDITOR:-nano}" data/config/config.json
# in .env: REFORGER_CONFIG=config.json
docker compose up -d
```

In the template, replace both placeholder passwords (`rcon.password`, `game.passwordAdmin`). They
contain spaces on purpose: Reforger does not accept spaces in these passwords, so an unedited copy
does not come up with a well-known password. The rejection was observed for the RCON password; for
`passwordAdmin` it was not verified. `game.admins` is an empty list; add your admins' Bohemia
identity IDs as strings.

Keep these in step with `.env`, because Compose still publishes the ports from `.env` and the
image's health check reads the A2S port from this file:

- `bindPort` and `publicPort` = `REFORGER_PORT`, `a2s.port` = `REFORGER_A2S_PORT`.
- `rcon.address` stays `0.0.0.0` and `rcon.port` stays `19999`, or other containers cannot reach
  RCON.

Compose still requires `REFORGER_RCON_PASSWORD` and `REFORGER_ADMIN_PASSWORD` in this mode, but the
server uses the passwords from the file. Setting the same values in both places avoids surprises
when you switch back. `REFORGER_MAX_FPS` and `REFORGER_PARAMS` still apply, because they are
startup parameters, not config fields.

### Files in `./data`

| Path | Container path | What |
|---|---|---|
| `data/config/` | `/reforger/Configs` | `docker_generated.json` (rewritten every start) or your own config |
| `data/profile/` | `/home/profile` | Server profile: logs, and the session saves in `data/profile/profile/` (hidden `.db` and `.save` entries) |
| `data/workshop/` | `/reforger/workshop` | Downloaded Workshop mods |

The game server itself is installed at `/reforger` inside the container and is **not** in `./data`.
It survives `docker compose restart` and `stop`/`start`, but recreating the container (for example
`docker compose up -d` after a change to `.env` or the image tag) downloads it again in full.

## Administration

Reforger's remote console is **BattlEye RCON** (UDP), not Source RCON. The `itzg/rcon-cli` helper
used by other blocks does not speak it, so this block has no `rcon` service, and `./stack rcon`
does not apply.

RCON listens on port 19999 inside the block's default network (`arma-reforger_default` when the
directory keeps its name). To use it, run a BattlEye RCON client that supports Arma Reforger in a
container attached to that network and connect to the `reforger` container on port 19999/udp with
`REFORGER_RCON_PASSWORD`. The maintainers' earlier setup used the Python library
[berconpy](https://github.com/thegamecracks/berconpy); its README says version 3 is required for Arma
Reforger. That library did not accept a host name, so pass the container's IP address:

```bash
docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' "$(docker compose ps -q reforger)"
```

Commands (from the Bohemia wiki, "Server Management"); RCON clients with `admin` permission may use
all of them:

| Command | What |
|---|---|
| `#players` | List players with their session `playerId` |
| `#kick <playerId>` | Kick; the player can rejoin |
| `#ban create <playerId or identityId> <seconds> [reason]` | Ban; `0` seconds = permanent. A `playerId` only works while the player is connected |
| `#ban remove <identityId>` | Lift a ban |
| `#ban list [page]` | List bans |
| `#restart` | Restart the running scenario; clients stay connected |
| `#shutdown` | Shut down the server |

There is no command to broadcast a message to all players and no save command; the server saves on
its autosave interval and when it stops. Spawning, weather and similar controls exist only in the
game, through the Game Master menu.

In the game, an admin opens the chat and types `#login <REFORGER_ADMIN_PASSWORD>`. Players listed in
`REFORGER_ADMINS` can use `#login` without a password. Players find their Bohemia identity ID in the
game under main menu, Profile. Since game version 1.3 the list holds at most 20 IDs.

## Mods

Mods are Workshop items identified by a 16-character hex GUID (on the mod's Workshop page). The
server downloads every listed mod, and its dependencies, from the Workshop at start into
`data/workshop/`. Clients download and activate the same mods automatically when they join.

- **Generated config:** `REFORGER_MODS=0123456789ABCDEF,FEDCBA9876543210=1.2.0`. Upper-case IDs, no
  spaces. Without `=version` the latest version is used.
- **Own config.json:** list them in `game.mods`:

  ```json
  "mods": [
    { "modId": "0123456789ABCDEF", "name": "Example mod", "version": "1.2.0" }
  ]
  ```

  `name` is only a label. `version` is optional. `required` (default `true`) decides whether the
  server refuses to start when the mod cannot be downloaded.

A mod change takes effect on the next start. Pin `version` if you want server and players to stay on
a known mod version; the GUID stays the same between versions. See [Crossplay](#crossplay) before
mixing mods and PlayStation players.

The image can also remove downloaded mods that are no longer referenced (`GAME_MODS_AUTO_PRUNE`, see
the image README); this block does not enable it.

## Updates and versions

Two things change independently:

| | Comes from | Changes when |
|---|---|---|
| Game server version | Steam, through SteamCMD | on **every** container start |
| Image (launcher, SteamCMD, Debian base) | `ghcr.io/acemod/arma-reforger:REFORGER_TAG` | only when you change the tag |

The image has no version tags. On 2026-10-02 the registry carried `latest`, `edge` and two
`sha-<commit>` tags; the image's build workflow tags each build of the source repository's `main`
branch as `latest` and `sha-<commit>`. This block pins `sha-d042142` (the build of
acemod/docker-reforger commit `d042142`, which was also `latest` when the block was written on
2026-10-02). Clients and server must run the same game version, and since the server updates itself
at every start, restart it after a game update so it is not left behind.

Tags can in principle be moved; for a fully reproducible image, pin the digest instead of the tag.
Read the digest of a tag from the registry:

```bash
docker buildx imagetools inspect ghcr.io/acemod/arma-reforger:sha-d042142 | grep Digest
```

and use it in place of the tag line in `docker-compose.yml`, e.g.
`image: ghcr.io/acemod/arma-reforger@sha256:<digest>`. Neither a tag nor a digest pins the game
version.

To update the image: back up, set `REFORGER_TAG` to a newer `sha-…` tag that exists, then
`docker compose up -d`. The container is recreated, so the game server is downloaded again.

## Backup and restore

`backup.list` contains:

- `data/profile` — the session saves (hidden `.db` and `.save` entries under `data/profile/profile/`)
  and the server logs. This is the world state.
- `data/config` — your config, or the generated one.

`data/workshop` is left out: the server downloads the listed mods again at start.

```bash
./stack backup arma-reforger          # from the repository root
# or by hand, from this directory:
docker compose stop
mkdir -p backups
tar czf "backups/arma-reforger-$(date -u +%Y%m%dT%H%M%SZ).tar.gz" data/profile data/config
docker compose start
```

`data/` belongs to root (see Troubleshooting), so the manual `tar` may need `sudo`. Copy with `tar`
or `cp -a`, not `cp data/profile/profile/*`: the save entries start with a dot and a shell glob skips
them.

Restore: `docker compose stop`, replace `data/profile` (and `data/config` if needed) from the archive,
`docker compose start`.

**Different scenarios:** the maintainers' earlier setup kept one copy of `data/profile/profile/.db`
and `.save` per scenario and swapped them while the server was stopped before changing the scenario.
Whether the game keeps the saves of different scenarios apart on its own was not verified; if you
switch scenarios on a server whose save matters, take a backup first.

## Resources

- **RAM:** depends on scenario, AI count and mods. A sample `-logStats` line on the Bohemia wiki shows
  about 3.2 GB in use for a session with 2 players and about 100 AI. Leave headroom above that; the
  maintainers have not benchmarked a recommendation. There is no memory limit in this block, because
  a kill during a save corrupts it.
- **CPU:** the server uses as much as it gets unless capped; `REFORGER_MAX_FPS` (60..120 recommended
  by Bohemia) limits it.
- **Disk:** the game server lives in the container's writable layer (downloaded at first start and
  after every recreate), plus `data/workshop/` for mods. Sizes were not measured by the maintainers.
- The image is published for `linux/amd64` only.

## Troubleshooting

- **`data/` belongs to root.** The image runs as root by design (SteamCMD requires it) and has no
  UID/GID option, so everything it creates under `./data` is owned by UID 0. Edit files with `sudo`.
- **Edits to `docker_generated.json` disappear.** It is rewritten from the environment on every
  start. Change `.env`, or switch to your own `config.json`.
- **Crash loop right after start.** Check the passwords first: no spaces, RCON password at least 3
  characters. In the maintainers' earlier setup an RCON password longer than 32 characters made the
  server reject the config on every boot. `openssl rand -hex 12` (24 characters) is safe.
- **Clients get "Session Error … Server unreachable".** The game port does not reach the container,
  usually a firewall. Docker's published ports bypass UFW; allow the UDP port in `DOCKER-USER` (see
  [docs/OPERATIONS.md](../../docs/OPERATIONS.md)).
- **Server is listed with a wrong address or cannot be joined from outside.** Set
  `REFORGER_PUBLIC_ADDRESS` to the host's public IPv4 address. The Bohemia wiki warns that inside
  Docker the server may otherwise register the container's address.
- **Client version mismatch.** The server always updates to the newest Steam build at start. After a
  game update, restart the server; players must update their game.
- **Health check stays `starting` for a long time.** The first start downloads the whole server; the
  image allows 15 minutes before failed checks count. Watch `docker compose logs -f reforger`.
- **Mods fail to load.** Mod IDs must be 16 upper-case hex characters. A pinned `version` must exist
  on the Workshop. With PlayStation in the platform list, see [Crossplay](#crossplay).

## Licences

This block (compose file, `.env.example`, `config.example.json`, this README) is MIT, like the rest
of the repository. The image `ghcr.io/acemod/arma-reforger` is MIT
([acemod/docker-reforger](https://github.com/acemod/docker-reforger/blob/main/LICENSE)) and contains
SteamCMD and Debian packages under their own licences. Arma Reforger and its dedicated server are
proprietary software by Bohemia Interactive; the server is downloaded from Steam at runtime under
Bohemia's terms. Workshop mods carry their authors' licences. Nothing of the game is in this
repository.
