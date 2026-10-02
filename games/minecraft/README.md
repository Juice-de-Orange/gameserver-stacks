# Minecraft (Java Edition, Paper)

A [Paper](https://papermc.io/) server for Minecraft: Java Edition, run with the
[`itzg/minecraft-server`](https://github.com/itzg/docker-minecraft-server) image
(Apache-2.0, documentation at <https://docker-minecraft-server.readthedocs.io>). Bedrock clients
cannot connect.

## Quick start

```bash
cp .env.example .env
```

Edit `.env`:

- Read the [Minecraft EULA](https://www.minecraft.net/en-us/eula). If you accept it, set
  `MINECRAFT_EULA=TRUE`. This block does not accept it for you: Compose refuses to start while the
  value is empty, and the image refuses to start unless it is `TRUE`.
- Set `MINECRAFT_RCON_PASSWORD` (`openssl rand -hex 24`).

```bash
docker compose up -d
docker compose logs -f minecraft
```

The first start downloads Paper and generates the world. The server is ready
when the log shows a line starting with `Done (`. `docker compose ps` then reports the container as
`healthy` (the image's own healthcheck queries the server status).

The whitelist is on by default (`MINECRAFT_ENABLE_WHITELIST=true`), so add yourself before you
connect, and make yourself an operator if you want in-game admin commands:

```bash
docker compose run --rm rcon whitelist add <your Java profile name>
docker compose run --rm rcon op <your Java profile name>
```

Connect with the Minecraft launcher set to the same version as `MINECRAFT_VERSION`:
**Multiplayer → Add Server**, address `203.0.113.10` (your host), or `203.0.113.10:25566` if you
changed `MINECRAFT_PORT`. Minecraft Java has no join link; the address is entered by hand.

## Ports

| Port | Protocol | Purpose | Published |
|---|---|---|---|
| 25565 (`MINECRAFT_PORT`) | TCP | Game | yes |
| 25575 | TCP | RCON | **no**, block network only |
| 24454 (`MINECRAFT_VOICE_PORT`) | UDP | Simple Voice Chat plugin | only with the voice chat override, see [Mods and plugins](#mods-and-plugins) |

Only the host side of the game port changes with `MINECRAFT_PORT`; inside the container the server
always listens on 25565. Do not change `rcon.port` in `server.properties`: the image and its
`rcon-cli` expect 25575.

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `MINECRAFT_TAG` | `2026.9.2-java25` | Image tag: image release + Java runtime. Not the game version. |
| `MINECRAFT_VERSION` | `26.2` | Minecraft version Paper runs (image `VERSION`). |
| `MINECRAFT_EULA` | *(required)* | `TRUE` once you accept the Minecraft EULA (image `EULA`). |
| `MINECRAFT_MEMORY` | `4G` | Java heap, sets both `-Xms` and `-Xmx` (image `MEMORY`). |
| `MINECRAFT_MOTD` | `A Paper Minecraft server` | Text under the server in the multiplayer list. |
| `MINECRAFT_DIFFICULTY` | `easy` | `peaceful`, `easy`, `normal` or `hard`. |
| `MINECRAFT_ENABLE_WHITELIST` | `true` | `true` sets `white-list` and `enforce-whitelist`; `false` lets any account join. |
| `MINECRAFT_PORT` | `25565` | Host TCP port for players. |
| `MINECRAFT_RCON_PASSWORD` | *(required)* | RCON password, used by the server and by the `rcon` service. |
| `PUID` / `PGID` | `1000` | UID/GID the server runs as (image `UID`/`GID`). |
| `TZ` | `Etc/UTC` | Time zone for logs. |
| `RCON_CLI_TAG` | `1.7.7` | Tag of `itzg/rcon-cli` for the `rcon` tools service. |

The server is fixed to `TYPE=PAPER` and RCON is always on.

**`server.properties`** lives in `./data/server.properties`. At every start the image writes the
properties that have an environment variable set (here: `motd`, `difficulty`, `white-list`,
`enforce-whitelist`, `enable-rcon`, `rcon.password`, `rcon.port`) and leaves every other property
as it is in the file. So:

- change the mapped properties in `.env`, then `docker compose up -d`;
- change any other property in the file **while the server is stopped**
  (`docker compose stop`, edit, `docker compose start`).

Every known `server.properties` key has an image variable (for example `MAX_PLAYERS`, `VIEW_DISTANCE`,
`SEED`, `LEVEL`); the full table is in the image's
[server properties documentation](https://docker-minecraft-server.readthedocs.io/en/latest/configuration/server-properties/).
To set one, add it under `environment:` in a `docker-compose.override.yml` next to the compose
file (Compose merges that file automatically). If you set `LEVEL` (the world folder name), change
`world` in `backup.list` to the same name.

Paper's own settings are in `./data/config/paper-global.yml` and `./data/config/paper-world-defaults.yml`,
plus `./data/bukkit.yml` and `./data/spigot.yml`; edit them while the server is stopped.

**Ownership of `./data`:** the server runs as `PUID` (default 1000). On start the image changes the
owner of `./data` to that UID when it differs, so files in `./data` belong to UID 1000 after the
first start. Edit them as that user, or with `sudo`.

## Administration

The server console is reached over RCON. Three ways, all without publishing a port:

```bash
docker compose run --rm rcon list                  # the rcon tools service (itzg/rcon-cli)
./stack rcon minecraft list                        # the same through the stack helper
docker compose exec minecraft rcon-cli list        # rcon-cli built into the server image
docker compose exec minecraft rcon-cli             # interactive console
```

Useful commands: `list`, `say <text>`, `save-all flush`, `stop`,
`whitelist add|remove <name>`, `whitelist list`, `op <name>`, `deop <name>`,
`kick <name> [reason]`, `ban <name> [reason]`, `ban-ip <name>`, `pardon <name>`.

Use Minecraft: Java Edition profile names, not Xbox gamertags; they can differ for the same
Microsoft account. Operators, whitelist and bans are stored in `./data/ops.json`,
`./data/whitelist.json`, `./data/banned-players.json` and `./data/banned-ips.json`.

Instead of RCON commands you can let the image maintain these lists from variables: `OPS` and
`WHITELIST` take user names separated by commas or newlines and are merged into the existing files
at start. Set them in `docker-compose.override.yml`; the image documentation describes the merge
modes (`EXISTING_OPS_FILE`, `EXISTING_WHITELIST_FILE`).

## Mods and plugins

Paper runs **plugins** (Bukkit/Paper `.jar` files), not Fabric or Forge mods. Plugins run only on
the server; players do not install anything for them. Plugin jars, their configuration and their
data live in `./data/plugins/`.

Ways to install plugins:

- **By hand:** put the `.jar` into `./data/plugins/` and restart (`docker compose restart minecraft`).
  Plugins added while the server runs are not loaded until the restart.
- **From Modrinth:** list project slugs or IDs in `MODRINTH_PROJECTS` (in
  `docker-compose.override.yml`). At every start the image downloads the newest version that
  matches Paper and `MINECRAFT_VERSION`, and removes plugins whose line you deleted. A trailing `?`
  (`bluemap?`) marks a plugin as optional: if no compatible version exists, the server starts
  without it.
- **From Hangar or any URL:** the image has no Hangar-specific variable. Put the plugin's direct
  download URL (from its Hangar version page) into `PLUGINS`, one per line, or download the jar
  into `./data/plugins/` by hand.
- **From SpigotMC:** `SPIGET_RESOURCES` takes a comma-separated list of SpigotMC resource IDs.

Do not set `MODRINTH_PROJECTS` or `PLUGINS` to an empty value: an empty list removes the plugins
that list installed. Leave the variable out instead.

Plugins are built against a server API. Before you change `MINECRAFT_VERSION`, check that every
plugin supports the new version (the "game versions" on Modrinth, the Paper platform versions on
Hangar).

### Simple Voice Chat (optional)

[Simple Voice Chat](https://modrinth.com/plugin/simple-voice-chat) adds proximity voice chat. It
needs its own UDP port, which this block publishes only when you enable it:

```bash
cp voice-chat.override.example.yml docker-compose.override.yml
docker compose up -d
```

The override installs the plugin from Modrinth and publishes `24454/udp`. Open that UDP port in the
host firewall as well. If you change the port, set `MINECRAFT_VOICE_PORT` in `.env` **and** `port`
in `./data/plugins/voicechat/voicechat-server.properties` to the same value. For Paper the config
lives there, not in `config/voicechat/` (that is the Fabric/Forge location).

Players who want voice install the Simple Voice Chat client mod (for example with Fabric) in the
same Minecraft version. Players without it can still join, unless `force_voice_chat` is enabled in
the plugin config. The plugin authors recommend the same plugin/mod version on server and clients.

## Updates and versions

Two independent values decide what runs:

- **`MINECRAFT_TAG`** is the image: `<image release>-<Java runtime>`, for example
  `2026.9.2-java25`. Releases are listed at
  <https://github.com/itzg/docker-minecraft-server/releases>. Tags without a release (`java25`,
  `stable`, `latest`) move with every image build; this block pins a release.
- **`MINECRAFT_VERSION`** is the game version. At every start the image installs the newest Paper
  build of the default (stable) channel for that version. To pin a build, set `PAPER_BUILD` in
  `docker-compose.override.yml`.

The Java runtime must fit the game version:

| Minecraft / Paper | Java (PaperMC recommendation) | Tag suffix |
|---|---|---|
| 1.20 – 1.21.11 | 21 | `-java21` |
| 26.1 and newer | 25 | `-java25` |

A runtime that is too old fails at start with `UnsupportedClassVersionError`. The minimum Java of a
Paper version is in the PaperMC API: `https://fill.papermc.io/v3/projects/paper/versions/<version>`,
field `java.version.minimum`.

To update:

1. Back up: `./stack backup minecraft` (see below).
2. Check that every plugin supports the new version.
3. Set the new `MINECRAFT_VERSION` in `.env`, and a new `MINECRAFT_TAG` if the Java requirement
   changes.
4. `docker compose pull && docker compose up -d`
5. Read the start log, not just the health status:
   `docker compose logs minecraft | grep -E "Enabling|Done \(|ERROR|Could not load"`.
   Every plugin needs an `Enabling` line; a plugin that is only loaded does not run.
6. Players switch their launcher to the same version; a different client version cannot join.

**Never downgrade a world.** Paper states that a world upgraded to 26.1 cannot go back to an
older version: on the first start with 26.1 or newer, Paper moves the Nether and the End from
`world_nether/` and `world_the_end/` into `world/dimensions/` and removes the old folders. Take the
backup before the update.

The image only installs builds of Paper's default channel. For a version without such a build the
image documentation names the error `No build found for version … with channel 'default'` (at the
time of writing, the PaperMC API lists only beta builds for 26.3). Use a version with a stable
build, or set `PAPER_CHANNEL=experimental` if you accept experimental builds.

## Backup and restore

`backup.list` contains:

- the world: `data/world` (since 26.1 it holds all dimensions), plus `data/world_nether` and
  `data/world_the_end` for worlds in the older layout (skipped when absent);
- `data/plugins`: plugin jars **and** everything plugins store (configs, player data, economy,
  claims, homes). A world restored without its plugin data is not the server you backed up;
- `data/config` (Paper settings) and the root configuration files (`server.properties`,
  `bukkit.yml`, `spigot.yml`, `commands.yml`, `permissions.yml`, `help.yml`);
- `ops.json`, `whitelist.json`, `banned-players.json`, `banned-ips.json`.

Not included, because the image recreates them: the Paper jar, libraries, caches and logs.

```bash
./stack backup minecraft      # stop → tar the paths → start; writes backups/minecraft-<UTC>.tar.gz
```

The server saves the world on `SIGTERM`, so a stopped server gives a consistent copy. To copy
without stopping, pause saving around the copy:
`docker compose exec minecraft rcon-cli save-off`, then `save-all flush`, copy, then `save-on`.
Run `save-on` even if the copy fails, or the server stops autosaving until the next restart.

Restore:

```bash
./stack restore minecraft backups/minecraft-<timestamp>.tar.gz
```

or by hand: `docker compose stop`, move the paths from `backup.list` out of `./data`,
`tar -xzf backups/minecraft-<timestamp>.tar.gz` in this directory, `docker compose start`. Restore
with the same `MINECRAFT_VERSION` as the backup, or a newer one, never an older one.

## Resources

- **RAM:** `MINECRAFT_MEMORY` sets the Java heap (image default without this block: 1 GB). Paper's
  getting-started guide uses 4 GB in its example, which is this block's default. The container needs
  more than the heap; the image documentation recommends about 25 % extra for non-heap memory. The
  block sets no hard memory limit: a kill during a save can corrupt the world.
- **Disk:** the image is about 355 MB compressed (linux/amd64, `2026.9.2-java25`); the Paper jar
  for 26.2 is about 65 MB. The world grows as players explore it.
- **CPU:** not measured for this block.

## Troubleshooting

- **Container exits right away, log asks to accept the EULA:** `MINECRAFT_EULA` is not `TRUE`. Once
  accepted, the image writes `./data/eula.txt`.
- **`UnsupportedClassVersionError`:** the Java runtime in `MINECRAFT_TAG` is too old for
  `MINECRAFT_VERSION` (see the table above).
- **`No build found for version …`:** no stable Paper build for that version yet (see Updates).
- **Players are rejected:** the whitelist is on; `docker compose run --rm rcon whitelist add <name>`.
- **A setting in `server.properties` keeps reverting:** it is one of the properties the image
  writes from `.env` at every start; change it in `.env`.
- **Permission denied in `./data`:** the files belong to `PUID` (default 1000). Use that user or
  `sudo`; `./stack backup` needs the same if your user is a different UID.
- **RCON password in `server.properties` looks different from `.env`:** values in that file are
  Java-properties escaped (`!` is written as `\!`). Test RCON instead of comparing strings:
  `docker compose exec minecraft rcon-cli list`.
- **A plugin is in `plugins/` but does nothing:** look for its `Enabling` line in the log; a missing
  line usually means it does not support the running version.
- **Voice chat "not connected":** the UDP port is not published or not open in the host firewall.
- **Exit code 137 when stopping:** the save took longer than `stop_grace_period` (90 s). Raise it
  in `docker-compose.override.yml` together with the image's `STOP_DURATION`.

## Licences

This block is MIT licensed, like the rest of this repository. The image
[`itzg/minecraft-server`](https://github.com/itzg/docker-minecraft-server) and
[`itzg/rcon-cli`](https://github.com/itzg/rcon-cli) are Apache-2.0. Paper is downloaded at runtime
from PaperMC and has its own licence. The Minecraft server software is proprietary to Mojang/Microsoft
and is used under the Minecraft EULA, which you accept yourself. Plugins have their own licences.
Nothing of Minecraft, Paper or any plugin is contained in this repository.
