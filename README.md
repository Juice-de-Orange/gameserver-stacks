# gameserver-stacks

**Self-hostable game servers as small, readable Docker Compose blocks — one directory per game,
pinned images, no default passwords, consoles never exposed, and backups that are actually
consistent.**

Each block in [`games/`](games) is self-contained: copy the directory to any Docker host, fill in
`.env`, run `docker compose up -d`. The [`stack`](stack) helper adds the same commands for every
game — start, logs, console, backup, restore, clean shutdown — and CI checks every block on each
change.

The READMEs are the main value: they record what it takes to run each server for real — which
ports matter, where the RCON password actually comes from, which config file the server rewrites at
start, what belongs into a backup, and which mistakes cost a world.

## Games

| Game | Block | Image (licence) | Default ports | Console |
|---|---|---|---|---|
| Arma Reforger | [`arma-reforger`](games/arma-reforger) | [`ghcr.io/acemod/arma-reforger`](https://github.com/acemod/docker-reforger) (MIT) | 2001/udp, 17777/udp | BattlEye RCON, internal |
| Avorion | [`avorion`](games/avorion) | [`rfvgyhn/avorion`](https://github.com/rfvgyhn/docker-avorion) (MIT) | 27000/tcp+udp, 27003, 27020, 27021/udp | Source RCON, internal |
| Factorio 2.0 | [`factorio`](games/factorio) | [`factoriotools/factorio`](https://github.com/factoriotools/factorio-docker) (MIT) | 34197/udp | Source RCON, internal |
| Minecraft (Paper) | [`minecraft`](games/minecraft) | [`itzg/minecraft-server`](https://github.com/itzg/docker-minecraft-server) (Apache-2.0) | 25565/tcp (+ 24454/udp voice chat, optional) | Source RCON, internal |
| Project Zomboid (B42) | [`project-zomboid`](games/project-zomboid) | [`danixu86/project-zomboid-dedicated-server`](https://github.com/Danixu/project-zomboid-server-docker) (GPL-3.0) | 16261/udp, 16262/udp | Source RCON, internal |
| Valheim | [`valheim`](games/valheim) | [`ghcr.io/community-valheim-tools/valheim-server`](https://github.com/community-valheim-tools/valheim-server-docker) (Apache-2.0) | 2456–2458/udp | none (in-game admin console only) |
| Vintage Story | [`vintage-story`](games/vintage-story) | [`devidian/vintagestory`](https://github.com/Devidian/docker-vintagestory) (no licence file upstream) | 42420/tcp+udp | optional RCON mod, internal |

Every image is referenced by name only; no script from any image is copied into this repository.

## Quick start

Requirements: Linux with Docker Engine and the Compose plugin (`docker compose version` ≥ 2.20).

```bash
git clone https://github.com/Juice-de-Orange/gameserver-stacks.git
cd gameserver-stacks/games/valheim
cp .env.example .env
$EDITOR .env                 # set the required values (empty in the example)
docker compose up -d
docker compose logs -f       # the block's README says which line means "ready"
```

Or with the helper, from the repository root:

```bash
./stack list                 # games and their state
./stack up valheim
./stack logs valheim -f
./stack backup valheim       # stop → archive → start
./stack rcon factorio /players
```

`./stack help` lists every command.

## What every block promises

| | |
|---|---|
| **Pinned image** | An explicit tag through a `*_TAG` variable; never `latest`. Updates are a deliberate edit. |
| **No default passwords** | Required secrets have no fallback — Compose refuses to start until you set them. |
| **Console stays private** | RCON/admin ports are never published; `./stack rcon` reaches them from a throwaway container on the block's network. |
| **Your data is plain files** | Worlds and configs live in `games/<game>/data/`, ignored by git. |
| **Consistent backups** | `backup.list` names what belongs together; `./stack backup` stops the server for the copy. |
| **Upstream defaults** | Ports are the game's defaults and can be changed in `.env`. |

The contract is written down in [docs/CONVENTIONS.md](docs/CONVENTIONS.md).

## Running it for longer

[docs/OPERATIONS.md](docs/OPERATIONS.md) covers what matters after the first evening: why Docker's
published ports bypass UFW and how to allow-list them in the `DOCKER-USER` chain, stopping the
servers cleanly before a host reboot (with a systemd unit in [`examples/`](examples)), scheduling
and pruning backups, and updating without losing a world.

## Licences

The compose files, scripts and documentation in this repository are MIT-licensed
([LICENSE](LICENSE)). The container images are third-party projects with their own licences, linked
from each block. The game server software itself is proprietary; the images download it at runtime
from Steam or the vendor. Nothing of any game is contained in this repository. Some games require
you to accept their terms (for example Minecraft's EULA) — the blocks never do that for you.

## Contributing

Corrections from people who run these servers are the most valuable contribution. See
[CONTRIBUTING.md](CONTRIBUTING.md); security reports go through [SECURITY.md](SECURITY.md).
