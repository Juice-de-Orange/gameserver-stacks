# Avorion

Avorion dedicated server in one Compose block, using the image
[`rfvgyhn/avorion`](https://hub.docker.com/r/rfvgyhn/avorion)
([source](https://github.com/rfvgyhn/docker-avorion), MIT licence).

The image has no environment variables. The game is configured through files in the galaxy folder
(`server.ini`, `modconfig.lua`), plus the few command-line flags this block passes (ports and the
first administrator).

## Quick start

```bash
cp .env.example .env
# Fill in AVORION_ADMIN (your SteamID64) and AVORION_RCON_PASSWORD (openssl rand -hex 24).

# The container runs as UID/GID 1000:1000 and cannot write to directories that Docker creates
# for it as root. Create them first:
mkdir -p data/galaxy data/backups
sudo chown -R 1000:1000 data

docker compose up -d
docker compose logs -f avorion
```

The server is ready when the log shows `Server startup complete`. On the first start it generates
a new galaxy and writes its default `server.ini` into `data/galaxy/`.

RCON is off at this point, because the default `rconPassword` is empty. To turn it on, follow
[Administration](#administration) once. It takes one stop and one start.

Players connect in the game via the multiplayer menu, connecting directly to the host's address
and the game port, for example `203.0.113.10:27000`. The default `server.ini` has
`isListed=false`, so the server does not show up in the public server list until you change that.

## Ports

| Port | Protocol | Purpose | Exposure |
|---|---|---|---|
| 27000 | TCP + UDP | Game traffic | published |
| 27003 | UDP | Query | published |
| 27020 | UDP | Steam query | published |
| 27021 | UDP | Steam master server | published |
| 27015 | TCP | RCON (Source RCON protocol) | internal only, reachable from the `rcon` service |

The Avorion wiki lists the four published ports as the ones a server needs. It does not say what
each one does. The purposes in this table come from the variable names in the
[Pelican egg for Avorion](https://github.com/pelican-eggs/games-steamcmd/tree/main/avorion),
which is also the source of the `--query-port`, `--steam-query-port` and `--steam-master-port`
flags.

Host and container ports are always equal, and each value is also passed to the server as a
command-line flag, because the server announces its ports to Steam. With the defaults, the flags
repeat the server's own defaults. A different set of ports, for example for a second copy of this
block, has not been tested by the maintainers. If you try it, change the value in `.env` only. Do
not edit the mapping.

## Configuration

### `.env`

| Variable | Default | Meaning |
|---|---|---|
| `AVORION_TAG` | `2.5.13.44140` | Image tag = game version (see [Updates](#updates-and-versions)). |
| `AVORION_PORT` | `27000` | Game port, TCP and UDP (`--port`). |
| `AVORION_QUERY_PORT` | `27003` | Query port (`--query-port`). |
| `AVORION_STEAM_QUERY_PORT` | `27020` | Steam query port (`--steam-query-port`). |
| `AVORION_STEAM_MASTER_PORT` | `27021` | Steam master port (`--steam-master-port`). |
| `AVORION_ADMIN` | required | SteamID64 of the first administrator (`--admin`). |
| `AVORION_RCON_PASSWORD` | required | Password the `rcon` tools service sends. Must equal `rconPassword` in `server.ini`. |
| `RCON_CLI_TAG` | `1.7.7` | Tag of `itzg/rcon-cli` for the `rcon` service. |

### The galaxy folder

Inside the container the galaxy lives at `/home/steam/.avorion/galaxies/avorion_galaxy`. The
image's entrypoint fixes the name with `--galaxy-name avorion_galaxy`. That folder is
`./data/galaxy` on the host:

| File | What |
|---|---|
| `server.ini` | All server settings, in `[Game]`, `[System]`, `[Networking]` and `[Administration]` sections. |
| `server.ini - readme.txt` | Written by the server: every `server.ini` key with an explanation. |
| `modconfig.lua` | Workshop mods (see [Mods](#mods)). You create it. |
| `sectors/`, `players/`, … | The galaxy itself. |
| `serverlog <date>.txt` | Server logs, including chat and logins. |

**Edit `server.ini` only while the server is stopped.** The server writes the file itself. The
Avorion wiki says it overwrites its settings on shutdown, and the maintainers saw it rewrite the
file at start. Either way, a change made while the server runs is silently lost. The safe order:

```bash
docker compose stop avorion
cp data/galaxy/server.ini data/galaxy/server.ini.bak   # keep a copy, see below
"$EDITOR" data/galaxy/server.ini
docker compose start avorion
```

Keep the copy. According to the Avorion wiki, a single mistake in `server.ini` makes the server
write a fresh default file with a **new seed**, which can damage the save. A broken file also
resets `rconPassword` to empty, which turns RCON off.

Commonly changed keys (the full list with explanations is in `server.ini - readme.txt`):

| Key | Default | Meaning |
|---|---|---|
| `[Administration] name` | `Avorion Server` | Name in server lists. |
| `[Administration] password` | empty | Join password; empty means none. |
| `[Administration] maxPlayers` | `10` | Player limit, plus one extra administrator. |
| `[Administration] accessListMode` | `Blacklist` | `Whitelist` makes the whitelist take effect. |
| `[Networking] isListed` | `false` | Show the server in public server lists. |
| `[Networking] rconPassword` | empty | RCON password; empty means RCON is off. |
| `[System] workerThreads` | set by the server | Simulation threads; must not exceed the host's CPU threads. |
| `[System] aliveSectorsPerPlayer` | `5` | Sectors kept in memory around each player (the main RAM lever). |
| `[System] backups` | `true` | Hourly in-game backups into `./data/backups`. |

## Administration

### RCON

Avorion speaks Source RCON on port 27015. RCON is enabled only when `rconPassword` in `server.ini`
is set. **That file is the only place the server reads the password from.** The image has no
variable for it, and there is no documented command-line flag. The `rcon` tools service, on the
other hand, can only read `.env`. So the same password lives in two places, and **you keep them
equal**:

1. Generate a password with letters and digits only, and put it in `.env` as
   `AVORION_RCON_PASSWORD`:

   ```bash
   openssl rand -hex 24
   ```

   Hex avoids `=`, `#` and other characters that could confuse the INI parser. A broken
   `server.ini` costs you the galaxy seed (see above).
2. Start the server once so that `server.ini` exists, then stop it:
   `docker compose stop avorion`.
3. In `data/galaxy/server.ini`, section `[Networking]`, set `rconPassword=` to the same value.
   Leave `rconPort=27015` and `rconIp=` (empty means all interfaces) as they are.
4. `docker compose start avorion`, then test:

   ```bash
   docker compose run --rm rcon /players
   ```

If you change the password later, change it in both places, with the server stopped. A restore
brings back the `server.ini` from the backup, **including the old `rconPassword`**. After a
restore, check that `.env` still matches.

Commands start with a slash. The commands below were checked against `/help` on a running
2.5.13 server:

```bash
docker compose run --rm rcon /help            # list all commands
docker compose run --rm rcon /status          # players, sectors, performance
docker compose run --rm rcon /players         # online players
docker compose run --rm rcon /save            # write everything to disk now
docker compose run --rm rcon "/say Restart in 5 minutes"
docker compose run --rm rcon /version
```

Moderation commands such as `/kick`, `/ban`, `/unban`, `/whitelist` and `/admin` exist as well.
`/help <command>` shows their arguments. Command lists on hosting websites are not reliable for
Avorion: several of the commands they name do not exist, for example `/tp` (the real command is
`/teleport`).

**Do not stop the server with `/stop`.** The container would exit and `restart: unless-stopped`
would start it again. Use `docker compose stop avorion`. It sends SIGTERM, and the server saves
and shuts down cleanly.

Without RCON there is a file-based fallback, documented in `server.ini - readme.txt` but not tested
by the maintainers. Write commands into `data/galaxy/commands.txt`, one per line (for example
`/save`). The server runs them once the content has been unchanged for 0.5 s, then deletes the
file.

### Administrators

The SteamID64 in `AVORION_ADMIN` is passed as `--admin` on every start and becomes a server
administrator. More administrators can be added in-game or over RCON with `/admin`
(`/help admin` for the syntax). The server keeps its administrators and permission groups in
files it writes into the galaxy folder. `server.ini` has no administrator key.

### Access lists

`accessListMode` in `server.ini` decides which list is active. The default is `Blacklist`, and in
that mode a maintained whitelist has no effect. The list files are in the galaxy folder. Change
the mode only while the server is stopped.

## Mods

Mods are Steam Workshop items. The server downloads them itself at start. Players who join get
them from the Workshop automatically. Stop the server, then create or edit
`data/galaxy/modconfig.lua`:

```lua
modLocation = ""
forceEnabling = false
mods = {
    {workshopid = "1234567890"},
    {workshopid = "2345678901"},
}
allowed = {
    {workshopid = "3456789012"},
}
```

- `mods` are the mods the server loads. Every player needs them, and they get them automatically.
- `allowed` lists client-side mods (mostly UI mods) that players may use on this server. It can
  stay empty (`allowed = {}`).
- A Workshop ID is the number in the item's Workshop address, not its title and not the URL.
- Leave `modLocation` empty and `forceEnabling` at `false` unless you know you need them.

Back up before changing mods. A galaxy that was played with a mod may not load cleanly without it.

## Updates and versions

The image installs the server with SteamCMD **at build time**, so the tag is the game version.
Nothing is downloaded at container start, except Workshop mods. Tags look like `2.5.13.44140`
(stable) and `2.5.13.44140-beta` (beta branch). `latest`, `stable` and `beta` are floating tags,
and this block does not use them. The image is rebuilt by its maintainer when a new Avorion
version comes out. After a game update it can take a while until a matching tag exists. The
pinned tag was checked on 2026-10-02. At that time it was the newest stable tag, built
2026-03-16.

To update:

1. Back up (see below). The Avorion wiki asks for a galaxy backup before every update.
2. Set the new tag in `AVORION_TAG`.
3. `docker compose pull && docker compose up -d`, then watch the log for `Server startup complete`.

**Never switch a galaxy from a beta tag back to a stable tag.** According to the Avorion wiki,
downgrading from beta to default "most certainly WILL corrupt your galaxy files". Treat any
downgrade the same way: restore the backup you took before the update instead.

Steam updates players' game clients on its own. If the server is older than the clients, players
may be unable to join until the image catches up. This has not been verified by the maintainers.

## Backup and restore

`backup.list` contains `data/galaxy`, the whole galaxy folder: the save, `server.ini`,
`modconfig.lua`, the access lists and the logs. A backup has to be taken **with the server
stopped**. On SIGTERM the server saves the galaxy and exits, so the stopped folder is consistent.

```bash
../../stack backup avorion          # stop → archive data/galaxy → start again
```

Without the helper:

```bash
docker compose stop avorion
tar -czf avorion-$(date -u +%Y%m%dT%H%M%SZ).tar.gz data/galaxy
docker compose start avorion
```

Restore:

```bash
docker compose stop avorion
mv data/galaxy data/galaxy.old
tar -xzf avorion-<timestamp>.tar.gz
sudo chown -R 1000:1000 data/galaxy
docker compose start avorion
```

`../../stack restore avorion <archive>` does the same, apart from the `chown`. After any restore,
check that `AVORION_RCON_PASSWORD` in `.env` still matches `rconPassword` in the restored
`server.ini`.

`./data/backups` holds the game's own hourly backups, written while the server runs. They are an
extra safety net, not a replacement for the stopped copy above.

## Resources

- **Architecture:** the image is published for `linux/amd64` only.
- **Download:** about 173 MB (compressed image). The game files are part of the image.
- **RAM:** there is no official figure for the dedicated server. The game's own requirements are
  4 GB minimum and 6 GB recommended. Memory grows with the number of players and with
  `aliveSectorsPerPlayer`, and lowering that value is the main lever. This block sets no memory
  limit on purpose: if the kernel kills the server while it writes the galaxy, the save can be
  lost.
- **CPU:** the Avorion wiki recommends as many `workerThreads` as the host has hardware threads,
  and at least 2 `generatorThreads`, but never as many as the host's hardware threads.
- **Disk:** a galaxy grows with every visited sector. The wiki says individual galaxies can take
  hundreds of megabytes and more. The hourly in-game backups in `./data/backups` add to that.

## Troubleshooting

- **The server cannot write its files / the container keeps restarting.** The log repeats
  `ini_parser_error … server.ini: cannot open file`: `./data/galaxy` did not exist before the first
  start, so Docker created it as root. `./data/galaxy` and `./data/backups` must belong to UID/GID
  1000. Under rootless Docker the container user is
  remapped, usually to 100999. Fix it with `sudo chown -R 1000:1000 data` (or the remapped ID).
- **A setting in `server.ini` did not stick.** It was edited while the server ran. Stop the
  server, edit, start.
- **`rcon` cannot connect or authentication fails.** `rconPassword` is empty (RCON is off), or it
  differs from `AVORION_RCON_PASSWORD`. A restore or a broken `server.ini` can cause either.
- **Galaxy reset or a new seed after editing `server.ini`.** The file had an error and the server
  replaced it with defaults. Stop the server and put back your `server.ini.bak`, or restore a
  backup.
- **The server restarts right after `/stop`.** That is the restart policy. Use
  `docker compose stop avorion`.
- **Nobody can find the server in the list.** `isListed=false` is the default. Players can still
  connect directly to `203.0.113.10:27000`. Also check that all four ports are open in the host
  firewall.

## Licences

This block (the files in this directory) is MIT-licensed like the rest of this repository. The
image `rfvgyhn/avorion` is MIT-licensed by its author. The Avorion server is proprietary software
by Boxelware. The image downloads it from Steam when the image is built, and it is not part of
this repository. Nothing of the game, its saves or its mods is stored here.
