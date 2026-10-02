# Conventions for a game block

Every game lives in `games/<game>/` and is a **self-contained block**: you can copy the directory
to any Docker host, fill in `.env` and run `docker compose up -d` without anything else from this
repository. The [`stack`](../stack) helper and the CI only add convenience and checks on top.

Directory names are lower-case and hyphenated: `project-zomboid`, `minecraft`, `vintage-story`,
`arma-reforger`, `valheim`, `avorion`, `factorio`.

## Files

| File | Required | What |
|---|---|---|
| `docker-compose.yml` | yes | One game server service, plus optional helper services behind a profile (see RCON). |
| `.env.example` | yes | Every variable the compose file reads, each with a comment. Copied to `.env` by the user. |
| `README.md` | yes | See [README sections](#readme-sections). |
| `backup.list` | yes | Paths to back up, one per line, relative to the block directory. `#` comments allowed. |
| other files | optional | e.g. a config template (`config.example.json`) the README tells the user to copy. |

Runtime data never enters git: `data/`, `backups/` and `.env` are ignored by the root
`.gitignore`.

## docker-compose.yml

- **Image pinned to an explicit tag**, set through an env variable with that tag as the default:
  `image: factoriotools/factorio:${FACTORIO_TAG:-2.0.x}`. No `latest`, no floating major tag. The
  tag must exist in the registry when the block is written (checked, not assumed). Where the image
  tag is not the game version (Minecraft, Vintage Story, images that run SteamCMD at start), the
  README says what the tag means and how the game version is chosen.
- **One service named after the game** (`zomboid`, `minecraft`, `vintagestory`, `reforger`,
  `valheim`, `avorion`, `factorio`). No `container_name` — the compose project name (the directory
  name) keeps containers apart, and a second copy of the block can run next to the first.
- `restart: unless-stopped`.
- `stop_grace_period` long enough for the game to save on `SIGTERM` (state the reason in a
  comment). If the game does **not** save on `SIGTERM`, say so in the README and in `backup.list`
  comments.
- **Ports:** only the ports players need are published, each through an env variable with the
  upstream default: `"${FACTORIO_PORT:-34197}:34197/udp"`. If the game announces its own port to
  Steam or a master server, host and container port must be equal — then the same variable is used
  on both sides and passed to the server config, and the README says so.
- **RCON and admin ports are never published.** They stay on the block's default network. For
  Source-RCON games the block adds an `rcon` service under the `tools` profile, using
  `itzg/rcon-cli:${RCON_CLI_TAG:-1.7.7}` with `RCON_HOST`/`RCON_PORT`/`RCON_PASSWORD` set from the
  block's variables, so `docker compose run --rm rcon <command>` works without exposing anything.
- **Secrets are required, never defaulted:** `"${ZOMBOID_ADMIN_PASSWORD:?set ZOMBOID_ADMIN_PASSWORD in .env}"`.
  Compose then refuses to start with an empty value. `.env.example` leaves these empty and says how
  to generate one (`openssl rand -base64 24`).
- **Data in bind mounts under `./data/`** (relative to the block), so a plain `tar` backs it up and
  the user can see the files. If the image lets you choose the UID/GID, the block exposes
  `PUID`/`PGID` (default `1000`); the README states which UID owns `./data` after the first start.
- Healthcheck only if it tests something real (a port the game answers on, a query that proves the
  server is up). No `healthcheck: test: ["CMD", "true"]`.
- Memory: no hard `mem_limit` by default (a kill during a save corrupts worlds); the README gives a
  RAM recommendation and, where the image supports it, a heap variable.
- Comments explain *why*, not *what*; English.

## .env.example

```dotenv
# --- Image -------------------------------------------------------------------
# Tag of factoriotools/factorio. The tag IS the game version; clients must match it exactly.
FACTORIO_TAG=2.0.x

# --- Network -----------------------------------------------------------------
# UDP port players connect to.
FACTORIO_PORT=34197

# --- Secrets (required) ------------------------------------------------------
# RCON password. Generate one: openssl rand -base64 24
FACTORIO_RCON_PASSWORD=
```

- Variable names are prefixed with the game (`ZOMBOID_`, `MINECRAFT_`, `VINTAGESTORY_`, `REFORGER_`,
  `VALHEIM_`, `AVORION_`, `FACTORIO_`), except the shared `PUID`, `PGID`, `TZ`, `RCON_CLI_TAG`.
- Example values are neutral: `My Valheim Server`, `Dedicated world`, `TZ=Etc/UTC`. No real
  server names, hosts, IPs (use `203.0.113.10` / `example.com` in docs), Steam IDs or player names.
- Every variable used in `docker-compose.yml` appears here, and nothing else.
- An empty value means one of two things, and the comment above it says which: a **required
  secret** (compose uses `${VAR:?…}` and refuses to start), or an **optional setting** whose empty
  value has a stated meaning ("Empty = open server"). Never leave a value empty without saying so.

## backup.list

```text
# Stop → tar these paths → start. Mods belong to the world: without them it does not load.
data/saves
data/config
data/mods
```

`stack backup <game>` stops the server, archives exactly these paths into
`backups/<game>-<UTC timestamp>.tar.gz` (from the block directory), and starts the server again if
it was running. A consistent copy matters more than seconds of downtime.

## README sections

1. **Title and one-line summary**, the image used (link) and its licence.
2. **Quick start** — copy `.env.example`, fill the required values, `docker compose up -d`, how to
   tell it is ready (log line or query), how to connect.
3. **Ports** — table: port, protocol, purpose, published or internal.
4. **Configuration** — table of the `.env` variables; where the game's own config file lives in
   `./data`, and whether the server rewrites it at start (edit only while stopped?).
5. **Administration** — console/RCON (`docker compose run --rm rcon …`), admin lists, or "this game
   has no console" when that is the truth.
6. **Mods** — how mods are installed for this image; what must match on clients.
7. **Updates and versions** — what the tag means, how to update safely (backup first), what must
   never be downgraded.
8. **Backup and restore** — what `backup.list` contains and why; restore steps.
9. **Resources** — RAM/CPU/disk recommendation, first-start download size.
10. **Troubleshooting** — the gotchas that cost time (permissions/UID, config overwritten at start,
    client version mismatch, A2S/listing).
11. **Licences** — this block is MIT; the image has its own licence; the game server binary is
    proprietary and downloaded at runtime from Steam/the vendor; nothing of the game is in this
    repository.

Facts in a README come from a source (the image's documentation, the game's documentation, a
measured run). If something could not be verified, it is written as such or left out.
