# Stacks Console

A web dashboard for game servers on your own Docker host. You add a server in the browser, and
the dashboard downloads the image, creates the container and starts it. After that it runs the
server for you: console, backups and restore, version changes, mods, scheduled restarts, player
statistics and a watchdog. Everything runs on one host, as one Docker Compose project.

Supported games: Minecraft (Paper), Factorio, Valheim, Project Zomboid, Arma Reforger, Vintage
Story and Avorion. The starting values of each game come from its block in [`games/`](../games).

- [Requirements](#requirements)
- [Install](#install)
- [First sign-in](#first-sign-in)
- [Your first server](#your-first-server)
- [Day to day](#day-to-day)
- [Configuration](#configuration)
- [A second installation on the same host](#a-second-installation-on-the-same-host)
- [Updating](#updating)
- [Uninstalling](#uninstalling)
- [Languages](#languages)
- [Relation to the game blocks](#relation-to-the-game-blocks)
- [Games](#games)
- [Known limitations](#known-limitations)

More documents:
[operations](docs/operations.md) (HTTPS, firewall, backups and restore, logs, host reboots),
[security model](docs/security.md), [reachability](docs/reachability.md).

## Requirements

- Linux with Docker Engine and the Compose plugin, `docker compose version` 2.24 or newer.
- `git`. Nothing needs `sudo` if your user may run `docker`.
- Disk and memory for the games you run. The dashboard itself is small: on our test host its
  API image is about 330 MB and its web image about 100 MB, plus `postgres:16-alpine` and the
  socket proxy. A game image is often more than a gigabyte, and a world can grow to several.
- A browser that can reach the host. The dashboard listens on `127.0.0.1` only by default; see
  [Reaching the dashboard](#reaching-the-dashboard).

## Install

```bash
git clone https://github.com/Juice-de-Orange/gameserver-stacks.git
cd gameserver-stacks/dashboard
./setup
docker compose up -d
```

What the steps do:

| Step | Why |
|---|---|
| `./setup` | Copies `.env.example` to `.env` (mode 600), where every setting lives. Generates the four secrets: `POSTGRES_PASSWORD`, `JWT_SECRET`, `GSS_SECRET_KEY` and the one-time `SEED_ADMIN_PASSWORD`. Sets `DATA_ROOT`, the host directory for every server's files, to `data/servers` in this directory and creates it, owned by you. Prints the address and the one-time password. |
| `docker compose up -d` | Starts the stack, with the images built from this checkout until a release is published (below). |

`./setup` is safe to run again: an existing `.env` is never replaced, only values that are
empty or still the template's `change-me…` are filled in, and the database password is not
touched once the database exists. It needs no network and no `sudo`. Its options:

```bash
./setup --data-root /srv/gameservers   # another directory for the servers' files
./setup --games minecraft,factorio     # games switched on at the first start (or `all`)
./setup --port 8081                    # another host port for the web UI
```

**Your uid.** The API writes into `DATA_ROOT` as the uid it runs as (`APP_UID`), so the directory
must belong to that uid. The prebuilt images run as 1000. If your uid is different, `./setup`
writes yours to `APP_UID` (and to `DB_BACKUP_OWNER`, the owner of the database dumps) and tells
you to start with `docker compose up -d --build`, which builds the images with your uid; build
again after every update ([Updating](#updating)). Run as root, it hands `DATA_ROOT` to uid 1000
instead. A `--data-root` that belongs to somebody else is refused with the `chown` command that
fixes it.

To write `.env` by hand instead: [`.env.example`](.env.example) explains every setting, and the
API refuses to start while `JWT_SECRET` or `GSS_SECRET_KEY` is missing or shorter than 32
characters.

**Images.** The compose file names prebuilt images on GHCR
(`ghcr.io/juice-de-orange/stacks-console-api` and `-web`, tag `GSS_VERSION`, default `latest`).
Once a release is published, `docker compose up -d` without `--build` pulls them. Until then the
pull fails with `denied` and Compose builds the images from the checkout instead; `--build`
does that directly.

The two secrets you must keep:

- `GSS_SECRET_KEY` encrypts the game-server passwords you enter in the dashboard. A database
  restored without the same key has unreadable passwords; you then enter them again.
- `JWT_SECRET` signs the sign-in tokens. Changing it signs everybody out.

Check that everything runs:

```bash
docker compose ps                 # five services: console-api, -web, -db, -dockerproxy, -backup
docker compose logs console-api   # contains "Application startup complete."
```

`console-api` shows `(healthy)` once its start-up database changes are done and the database
answers. `docker compose up -d --wait` returns only then.

### Reaching the dashboard

The dashboard is published on `127.0.0.1:8080` (`DASHBOARD_PORT`), plain HTTP. Open
<http://127.0.0.1:8080> on the host itself. From another computer, use an SSH tunnel for a
first look:

```bash
ssh -L 8080:127.0.0.1:8080 you@your-server   # then open http://127.0.0.1:8080 locally
```

For regular use, put HTTPS in front of it. The stack can do that itself (compose profile
`https`, with a certificate from Let's Encrypt), or you use your own reverse proxy or tunnel:
[docs/operations.md](docs/operations.md#https) has both, with examples for Caddy, Traefik,
nginx and Cloudflare Tunnel.

## First sign-in

1. Take the one-time password that `./setup` printed. It is also in `.env`:
   `grep '^SEED_ADMIN_PASSWORD=' .env`.
2. Sign in as `admin` with it.
3. The dashboard asks for a password of your own right away; until you set one, nothing else
   works for this account.
4. You can then delete the `SEED_ADMIN_PASSWORD` line. It is only read while no account named
   `SEED_ADMIN_USERNAME` exists.

On a fresh installation no game is active yet, so the first page asks **"Which games do you want
to run?"** Pick the games and press **Continue**. Only active games appear when you add a
server, get version lookups and download anything. You can change the choice later under
**Settings → Games**; a game that still has an enabled server cannot be switched off.
`GSS_GAMES=minecraft,factorio` (or `all`) in `.env` makes the choice before the first start
instead.

## Your first server

The whole flow happens in the browser. We ran it for Minecraft:

1. **Add server.** On the empty fleet page press **Add your first server**, or **Add server**
   in the side bar. Choose the game.
2. **Check the values.** The form is filled from the game's block. What you usually look at:
   - **Key**: part of every address of this server, cannot be changed later.
   - **Data directory**: suggested as `<DATA_ROOT>/<key>`. It must lie below `DATA_ROOT` (or a
     root listed in `ALLOWED_DATA_ROOTS`).
   - **Ports** and **bind address**. `0.0.0.0` publishes the game ports on every interface;
     `127.0.0.1` keeps them on the host. The dashboard refuses a port that another server or
     a running container already uses, and says which one.
   - **Container name**: defaults to the key. 2–128 characters: letters, digits, `_`, `.`
     and `-`, starting with a letter or digit. It is also the console's host name on the game
     network.
   - **Console password**: type it in (stored encrypted, never shown again) or name a variable
     in `.env` that holds it. A variable that is set wins over a stored value. Project
     Zomboid's in-game admin password (edit page) and the join passwords of Project Zomboid and
     Valheim (**Configuration**, once the server exists) are stored the same way.
   - **Licence**: Minecraft needs the EULA accepted; the checkbox links it.
3. **Create server.** The server is now registered, switched off, and its page opens.
4. **Create container.** This downloads the image (often more than a gigabyte, with a progress
   bar), creates the data directories, the configuration and the container. Nothing starts yet;
   a message says when the container exists. The same step without a browser, in the
   `dashboard/` directory:
   ```bash
   docker compose exec console-api python -m app.cli create-container <key>
   ```
5. **Enable and start.** The page shows the server starting. Minecraft took a bit over a
   minute until it answered. If the start is refused (a port that a program on the host holds,
   for instance, which only Docker finds out), the server is switched off again and the reason
   stays on the page; fix it under **Edit server** and try again.

To let players in, set the address they connect to under **Settings → Network** (or per server
on its edit page), open the game ports in your router or cloud firewall, and use the
**Reachability** panel on the server page. It lists the ports, gives firewall commands for your
host, and can optionally ask outside services whether the port is reachable from the internet.
Details: [docs/reachability.md](docs/reachability.md).

## Day to day

- **Edit** (server page → **Edit server**): name, image, version, ports, bind address, memory,
  game settings. A change that needs a new container is recreated for you: backup, new
  container, boot check, and the old one comes back if the new one does not start. In that
  case the edit page warns that the saved values are not applied until a recreate succeeds.
- **Switch off** stops the container and takes the server out of every job (watchdog, backups,
  restarts, announcements). Its data stays.
- **Remove** (only when switched off) deletes the container and the server's records in the
  dashboard. You choose whether its data directory stays on disk or is deleted; the audit log
  stays either way.
- **Backups**: every enabled server is backed up weekly (Sunday 04:00, `SCHEDULER_TZ`), and
  **Back up now** on the server's **Backups** page takes one at any time. Valheim cannot be
  told to save, so it is stopped for both and started again (a scheduled restart with a backup
  takes it while the server is down anyway). **Restore** stops the server,
  takes a safety backup of the current state, **replaces** the world with the archived one
  (files created since the backup do not survive), starts the server and checks that it comes
  up. The dashboard database has its own nightly dump. Both, and how to restore them:
  [docs/operations.md](docs/operations.md#backups-and-restore).
- **Console**: the live console with a list of safe commands for everyone allowed to use it, and
  the full console for admins.
- **Memory**: the fleet and the server page show a server's memory use against its container's
  **Memory limit** (edit page). Without a limit they show the amount only; for Minecraft the
  **Status** page also names the Java heap ceiling.
- **Users and roles**: people can register; an admin approves them. Roles `admin`, `trusted` and
  `member`, with permissions per role or per user (**Users & roles**). Every sign-in and admin
  action is in the **Audit log**.

## Configuration

Everything is in `.env`, and [`.env.example`](.env.example) explains every setting. The ones
you are most likely to change:

| Setting | Default | What |
|---|---|---|
| `DATA_ROOT` | `data/servers` here (set by `./setup`) | Host directory for all server data, owned by `APP_UID`. Absolute path. |
| `DASHBOARD_PORT`, `DASHBOARD_BIND` | `8080`, `127.0.0.1` | Where the web UI is published. |
| `GSS_GAMES` | empty | Games switched on at the very first start. |
| `PUBLIC_HOST` | empty | The address players connect to; **Settings → Network** overrides it. |
| `SCHEDULER_TZ` | `UTC` | Time zone of every scheduled job and of the times you enter. |
| `COMPOSE_PROJECT_NAME`, `GAME_NETWORK` | `stacks-console`, `gameserver-net` | Needed for a [second installation](#a-second-installation-on-the-same-host). |
| `GSS_VERSION` | `latest` | Release of the prebuilt images. |
| `DB_BACKUP_*`, `DB_DATA_DIR` | nightly at 02:30 UTC, 30 days | The database dump; see [operations](docs/operations.md#the-dashboard-database). |
| `APP_UID` | `1000`, or yours (set by `./setup`) | uid of the API and of the game processes it creates. Another value than 1000 needs `--build`. |

A setting with one of the project's prefixes (`GSS_`, `POSTGRES_`, `JWT_`, `DASHBOARD_`,
`WATCHDOG_`, `BACKUP_`, `DB_`, `SEED_ADMIN_`, `SMTP_`) that the dashboard does not know is
reported at start, with a suggestion if it looks like a typo:

```text
WARNING:  Unknown setting GSS_GAMS in the environment — it is ignored. Did you mean GSS_GAMES?
```

**Server data in more than one directory**: copy
[`docker-compose.override.example.yml`](docker-compose.override.example.yml) to
`docker-compose.override.yml` and follow its comments. Compose reads that file by itself.

**Container names**: no service of the stack has a fixed container name. The containers are
called `<project>-<service>-1`; address them with `docker compose exec <service> …` from the
`dashboard/` directory. The game containers the dashboard creates are named after the
**Container name** field of each server (default: its key; 2–128 characters, letters, digits,
`_`, `.` and `-`, starting with a letter or digit). The short-lived helper that deletes
root-owned files is named after it (`<container name>-cleanup`).

## A second installation on the same host

Each installation is its own checkout with its own `.env`. Three values must differ from the
first one:

```bash
COMPOSE_PROJECT_NAME=stacks-console-2
DASHBOARD_PORT=8081
GAME_NETWORK=gameserver-net-2
```

Also give it its own `DATA_ROOT` (`./setup` in the second checkout does, with `data/servers` of
that checkout). The two installations share nothing, but they share the host:
game container names and published ports must not collide. The port check sees the other
installation's running containers and refuses a port they already publish.

## Updating

Back up first (the world backups and the database dump are taken automatically, but a fresh
dump costs nothing: see [operations](docs/operations.md#the-dashboard-database)). Then, in the
`dashboard/` directory:

```bash
git pull
docker compose pull          # new prebuilt images, once releases are published
docker compose up -d
```

If you build the images yourself (no release yet, or `APP_UID` other than 1000):

```bash
git pull
docker compose up -d --build
```

The game servers keep running; only the dashboard's containers are replaced. Database changes
are applied by the API at start. Read [`CHANGELOG.md`](../CHANGELOG.md) and the
[upgrade notes](docs/operations.md#upgrade-notes) before an update.

## Uninstalling

1. In the dashboard, switch off and **Remove** every server. Choose whether each data directory
   is deleted or kept. (The game containers are not part of the compose project: `docker
   compose down` does not remove them, and it cannot remove the game network while they are
   attached to it.)
2. In the `dashboard/` directory:
   ```bash
   docker compose --profile https --profile firewall down -v --rmi local
   ```
   This removes the containers, networks, volumes and the images built here. The two
   `--profile` options make it include the volumes of the optional services even if you
   switched them off earlier; without them, `down -v` leaves those volumes behind.
3. Delete what is left on disk. Some of it is not yours: `data/db` belongs to the database's
   uid, and a kept Arma Reforger data directory belongs to root. A container deletes it without
   `sudo`. From the `dashboard/` directory:
   ```bash
   cd ../..                     # the directory that holds your gameserver-stacks checkout
   docker run --rm -v "$PWD:/p" busybox:1.36 rm -rf /p/gameserver-stacks
   ```
   That removes the checkout with `data/` (the database and, as `./setup` sets it, `DATA_ROOT`)
   and `db-backups/`; copy the dumps elsewhere first if you want to keep them. A `DATA_ROOT`
   outside the checkout goes the same way, through its parent directory: for `/mnt/games`,
   `docker run --rm -v /mnt:/p busybox:1.36 rm -rf /p/games`.
4. The game images the dashboard pulled (often several GB each) and a Vintage Story runner you
   built yourself stay in Docker's image store. List them with `docker image ls` and remove the
   ones you no longer need by name, for example
   `docker image rm stacks-console/vintagestory-runner:1`.

## Languages

The interface is in English and German. It follows the browser's language; the **EN/DE** button
in the top bar switches and remembers the choice per browser. Error messages from the server are
translated too. Log files and the `.env` comments are English only.

## Relation to the game blocks

The blocks in [`games/`](../games) and the dashboard are independent:

- The dashboard does not need the `games/` directory at runtime. Its presets were taken from the
  blocks: image, tag, ports and settings.
- The dashboard does **not** take over a container started from a block. It only manages the
  containers it created. A block that runs on the same host keeps working, and the port check
  refuses a port the block already publishes.
- There is no migration tool from a block to the dashboard. Moving a world means copying the
  block's data into the new server's data directory before **Create container**; that has not
  been tested for every game, so take a backup and check the result.

## Games

Default ports are the ones the **Add server** form suggests. An editable port can be changed
there and later on the edit page; the dashboard tells the game to listen on it. A fixed port
cannot be changed, so that game runs only once per host, also across two installations.

| Game | Default ports | Ports editable | Console |
|---|---|---|---|
| Minecraft (Paper) | 25565/tcp | yes | Source RCON |
| Factorio | 34197/udp | yes | Source RCON |
| Valheim | 2456, 2457, 2458/udp | yes | none (in-game admin console only) |
| Project Zomboid | 16261, 16262/udp | yes | Source RCON |
| Arma Reforger | 2001, 17777/udp | yes | BattlEye RCON |
| Vintage Story | 42420/tcp+udp | no: the runner image starts the server without a port, and the game writes its `serverconfig.json` at the first start | Source RCON through the "Vintage RCON" mod |
| Avorion | 27000/tcp+udp, 27003, 27020, 27021/udp | no: its query and Steam ports exist only as command-line arguments, and the dashboard starts the image without any | Source RCON |

## Known limitations

- **What has been tested through the UI.** All seven games have been run end to end through
  this dashboard on a test host: create, start, console where the game has one, backup and
  restore, edit, remove. Not tested yet: a real game client joining (only Minecraft), the
  firewall helper on a running host, and the Reachability panel's check from outside.
- **Vintage Story's console** needs the third-party "Vintage RCON" server mod. Without it the
  server runs, but console, player list and moderation are not available.
- **Arma Reforger runs as root** in its container (the image has no PUID/PGID setting), so its
  files in the data directory belong to root. The dashboard deletes them through a short-lived
  helper container; to delete them by hand, use `sudo` or a container
  ([Uninstalling](#uninstalling) shows one).
- **Arma Reforger checks its whole install at every start.** The image runs SteamCMD's
  validation over the ~10 GB game before the server starts, so a restart, restore or recreate
  takes about five minutes of downtime even though nothing is downloaded again.
- **One host.** No remote nodes, no clustering.
- **The admin role is root on the host**, through the Docker socket proxy. Give it only to
  people you would give root. See [docs/security.md](docs/security.md).
- **The check from outside** in the Reachability panel is IPv4 only.
- **The firewall helper** (profile `firewall`) has only been tested with nf_tables iptables in a
  throwaway network namespace; legacy iptables is untested.
- **Architectures**: the API, web and firewall helper images are built for amd64 and arm64.
  The steamcmd helper (Project Zomboid workshop mods) and the Vintage Story runner are amd64
  only.
- The operation progress (pull, create, restore) is polled every two seconds; a progress bar can
  jump back while Docker reports layer sizes late.
