# Vintage Story

A [Vintage Story](https://www.vintagestory.at/) dedicated server in one Docker Compose block.

- Image: [`devidian/vintagestory`](https://hub.docker.com/r/devidian/vintagestory), built from the
  community repository `Devidian/docker-vintagestory` on GitHub. **Community image, no licence
  file upstream; check its terms before redistributing.** This block only references the image by
  name and copies nothing from it.
- Platform: the image is published for `linux/amd64` only.

## Quick start

```bash
cp .env.example .env        # the defaults work; nothing is required
docker compose up -d
docker compose logs -f vintagestory
```

The server is ready when the log shows a line like

```text
[Server Event] Dedicated Server now running on Port 42420 and all ips!
```

and `docker compose ps` reports the service as `healthy`. The first start creates the world, which
takes longer than later starts.

**Before anyone can join, deal with the whitelist.** Since game version 1.20 a dedicated server
only admits whitelisted players by default (`"WhitelistMode": 0` means "on" for dedicated servers),
so the first connection is refused. Either add players from the console
(`/player <playername> whitelist on`, see [Administration](#administration)) or turn the whitelist
off with `/serverconfig WhitelistMode off`.

Connect in the game: Multiplayer → add server → `203.0.113.10` (your host's address), or
`203.0.113.10:<port>` if you changed `VINTAGESTORY_PORT`.

## Ports

| Port | Protocol | Purpose | Published |
|---|---|---|---|
| 42420 (`VINTAGESTORY_PORT`) | TCP | Game connection | yes |
| 42420 (`VINTAGESTORY_PORT`) | UDP | Game traffic, used since game version 1.20 | yes |
| 42425 (`VINTAGESTORY_RCON_PORT`) | TCP | Remote console of the optional Vintage RCON mod | **no**, block network only |

The image itself only declares 42420/tcp. The official server guide asks for UDP on the same port
from version 1.20 on, so the block publishes both.

The server inside the container always listens on 42420. To use another host port, change
`VINTAGESTORY_PORT` only and leave `"Port"` in `serverconfig.json` at 42420. Players then add the
port to the address. Whether the public server list (`AdvertiseServer`) announces the host port
correctly in that case is not verified by the maintainers; keep 42420 if you advertise the server.

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `VINTAGESTORY_TAG` | `static-v1.22.6` | Image tag; the number is the game version inside the image. |
| `VINTAGESTORY_PORT` | `42420` | Host port for TCP and UDP. |
| `RCON_CLI_TAG` | `1.7.7` | Tag of `itzg/rcon-cli` for the optional `rcon` service. |
| `VINTAGESTORY_RCON_PORT` | `42425` | Port of the Vintage RCON mod; must equal `"Port"` in `vsrcon.json`. |
| `VINTAGESTORY_RCON_PASSWORD` | *(unset)* | Password of the Vintage RCON mod; must equal `"Password"` in `vsrcon.json`. |

The block sets `VS_DATA_PATH=/gamedata` and mounts `./data` there, so the server's data folder is
`./data` itself. After the first start it contains:

| Path | What |
|---|---|
| `data/serverconfig.json` | Server settings: name, `Password` (join password), `MaxClients`, `WhitelistMode`, `AdvertiseServer`, `StartupCommands`, world settings. Created on the first start. |
| `data/Saves/` | Worlds (`default.vcdbs` unless `WorldConfig.SaveFileLocation` says otherwise). |
| `data/Playerdata/` | Player roles, groups, whitelist and ban list. |
| `data/Mods/` | Server mods. |
| `data/ModConfig/` | Settings files written by mods. |
| `data/Logs/` | Server logs, including the chat log `server-chat.log`. |

`serverconfig.json` is read when the server starts. **Stop the server before editing it** and start
it again afterwards: changes to the file take effect only at the next start. While the server runs,
use the `/serverconfig ...` commands instead. All options are described on the
official wiki page [Server Config](https://wiki.vintagestory.at/Server_Config).

The image does not take settings from environment variables (apart from `VS_DATA_PATH`);
everything else is configured in `serverconfig.json` or with in-game commands.

## Administration

Vintage Story has **no built-in remote console (no RCON)**. There are three ways to run server
commands:

1. **In game**, as a player with the admin role: type commands with a leading slash in chat.
2. **The server console** on the Docker host. The block keeps stdin open, so you can attach to it:

   ```bash
   docker compose attach vintagestory
   /op <playername>
   ```

   Detach with `Ctrl-p Ctrl-q`. `Ctrl-c` stops the server. (The image publishes the same
   `tty`/`stdin_open` variant as an example; not verified by the maintainers.)
3. **The optional third-party mod "Vintage RCON"** (see below).

To make yourself admin without a console, set `"StartupCommands": "/op <playername>"` in
`serverconfig.json` (server stopped), start the server, join, and remove the entry again.

Useful commands: `/op <player>` (alias of `/player <player> role admin`),
`/player <player> whitelist on`, `/list clients`, `/announce <message>`, `/autosavenow`, `/stop`.
See the wiki's [List of server commands](https://wiki.vintagestory.at/List_of_server_commands).

### Optional: remote console with the Vintage RCON mod

[Vintage RCON](https://mods.vintagestory.at/vintagercon) (source:
[Shijikori/vintage-rcon](https://github.com/Shijikori/vintage-rcon), MIT) is a server mod by a
third party that adds a Source RCON server to Vintage Story. The `rcon` service of this block works
**only** when that mod is installed and configured; without it, the service has nothing to talk to.

Caveats, from the mod's own description:

- Source RCON is **not encrypted**. Every command runs with the privileges of the server console.
  The block never publishes the RCON port; keep it that way and use a VPN for remote access.
- The mod does not start its listener while its password is empty.
- Check the mod's game-version tags on its ModDB page before installing. At the time of writing the
  newest release (2.0.2) is tagged up to game version 1.22.0; whether it loads on newer patch
  versions is not verified by the maintainers.

Setup:

1. Download the release `.zip` from the ModDB page into `data/Mods/` and restart the server. The
   mod creates `vsrcon.json` with defaults and logs `vsrcon.json was not found in configs, has been
   created with default values.` Find the file with `find data -name vsrcon.json`
   (normally `data/ModConfig/vsrcon.json`).
2. Stop the server. In `vsrcon.json` set `"Password"` to the value of `VINTAGESTORY_RCON_PASSWORD`
   and keep `"Port"` equal to `VINTAGESTORY_RCON_PORT` (default 42425). Leave `"IP"` at `null`
   so the `rcon` container can reach it over the block network.
3. Start the server. The log shows `RCon Listener started on port 42425`.
4. Send commands **without** the leading slash (the mod adds it):

   ```bash
   docker compose run --rm rcon list clients
   docker compose run --rm rcon announce Restart in 5 minutes
   ```

`itzg/rcon-cli` together with this mod is not verified by the maintainers; the mod states that it
follows Valve's Source RCON specification.

## Mods

- **Install:** put the mod files (ModDB releases are `.zip`) into `data/Mods/` and restart. The
  server can also fetch mods itself with `/moddb install <modid>`.
- **Versions:** use the newest mod release whose ModDB "files" tab lists your game version. A mod
  built for a different game version will likely not work.
- **Clients:** when players join, the game offers to download the server's mod list; accepting
  installs the server-side and universal mods into the player's `ModsByServer` folder. Client-only
  mods stay the player's choice.
- **Removing a content mod** leaves its blocks and items broken in the world. Back up first
  (`../../stack backup vintage-story`); the wiki's `/bir` command helps to remap broken blocks.

## Updates and versions

The image contains the game: `static-v1.22.6` is game version 1.22.6, built from the vendor's
server archive. **There is no `VERSION` variable** or download at start; to change the game version
you change the tag. `latest` and `unstable` also exist upstream but move; this block pins a
`static-v...` tag on purpose. The image can lag behind game releases (when this block was
written, game version 1.22.7 was out and the newest image tag was `static-v1.22.6`) — check the
tag list on Docker Hub.

To update:

1. Read the game's patch notes and check that your mods have releases for the new version.
2. Back up: `../../stack backup vintage-story` (from the block directory) or the manual steps below.
3. Set `VINTAGESTORY_TAG` to the new `static-v<version>` tag, then
   `docker compose pull && docker compose up -d`.
4. Watch the log for `Game Version: v<version>` and the "now running" line, then join with a client
   of the same version.

Never start a world with an older game version after a newer one has opened it; restore the backup
from step 2 instead.

## Backup and restore

`backup.list` names `data/Saves`, `data/Playerdata`, `data/Mods`, `data/ModConfig`,
`data/serverconfig.json` and `data/servermagicnumbers.json` — the world, who may play and with
which role, and everything the world needs to load. Logs and the server's own backup folders
(`data/Backups`, `data/BackupSaves`) are left out.

The server saves the world when it stops, so a backup is taken with the server **stopped**:

```bash
../../stack backup vintage-story                 # stop → tar → start
# or by hand, in the block directory:
docker compose stop vintagestory
sudo tar -czf backups/vintage-story-$(date -u +%Y%m%dT%H%M%SZ).tar.gz \
  data/Saves data/Playerdata data/Mods data/ModConfig data/serverconfig.json data/servermagicnumbers.json
docker compose start vintagestory
```

Restore: `../../stack restore vintage-story backups/<archive>.tar.gz`, or by hand: stop the server,
move the listed paths aside, `sudo tar -xzf <archive>` in the block directory, start.

Alternative while the server runs: the in-game command `/genbackup [filename]` creates a full copy
of the current save game in the server's backups folder without pausing the server (official
command list). It copies only the save, not mods or config, and the copy stays on the same disk —
use it in addition to, not instead of, the archive above. Via the RCON mod the command is
`genbackup` without the slash.

## Resources

- **RAM/CPU:** the official server guide estimates 1 GB base RAM + 300 MB per player, 4 threads,
  and 1 GHz base + 100 MHz per player. The server is a .NET program; there is no heap setting, and
  the block sets no memory limit (a kill during a save can damage the world).
- **Self-protection:** `serverconfig.json` has `DieAboveMemoryUsageMb` (default 50000) and
  `DieBelowDiskSpaceMb` (default 400); the server shuts itself down past these limits.
- **Download:** about 380 MB compressed for the image (.NET runtime and game included); nothing
  is downloaded at start.
- **Disk:** the world grows as players explore; plan for archives of the same size per backup.

## Troubleshooting

- **"Not whitelisted" on the first join:** see the whitelist note in [Quick start](#quick-start).
- **Files in `./data` belong to root:** the image runs the server as root, so everything it creates
  in `./data` is owned by UID 0. Edit with `sudo`, and run backups with `sudo`.
- **Changes to `serverconfig.json` are lost or ignored:** edit only while the server is stopped.
- **Players cannot connect from outside:** open 42420 for **TCP and UDP** (or your
  `VINTAGESTORY_PORT`). Docker bypasses UFW; see [`docs/OPERATIONS.md`](../../docs/OPERATIONS.md).
- **Client version mismatch:** players need the game version of the image tag. Which mismatches the
  server tolerates is not verified by the maintainers; keep clients on the same version.
- **A mod does not load:** check its game-version tags and the log in `data/Logs/`.
- **No output from `docker compose run --rm rcon ...`:** the Vintage RCON mod is not installed,
  its password is empty, or the password/port in `vsrcon.json` differ from `.env`.

## Licences

- This block (compose file, `.env.example`, `backup.list`, this README) is MIT, like the rest of
  this repository.
- `devidian/vintagestory` is a community image whose upstream repository has no licence file; check
  its terms before redistributing it.
- `itzg/rcon-cli` and the Vintage RCON mod have their own licences.
- Vintage Story is proprietary software by Anego Studios. The image downloads the server files
  from the vendor when it is built; nothing of the game is in this repository. Players need their
  own game account.
