"""Starting values for a new registry row, one preset per game type.

The "Add server" page fills its form from here (`GET /api/servers/presets`); every value stays
editable there, and `POST /api/servers` validates the result like any other request. Nothing in
this module is applied behind the admin's back.

**Every value has a source, and it is named next to it.** Two kinds of sources only:

* the driver (`services/drivers/<game>.py`) — what `container_env`, `container_binds`,
  `ensure_config` and the class constants already assume;
* the game's block in the repository (`games/<game>/docker-compose.yml`, `.env.example`) — the
  image, the pinned tag, the ports and the memory figures a plain `docker compose` setup uses.
  The secret names come from `dashboard/.env.example`.

Where neither says anything, the preset says nothing either (`ram_mb = 0` means "no cap", the
registry default). `tests/test_server_presets.py` checks the presets against the drivers.

**Ports.** `container_spec.build_create_payload` publishes every port with host port ==
container port. A port can therefore only be changed where the game is told to LISTEN on it:
through the driver (Factorio `PORT`, Valheim `SERVER_PORT`, Project Zomboid `PORT`/`UDPPORT`,
the Reforger `config.json`) or through a container variable the driver passes on (`port.env`,
Minecraft). Every other port is `editable=False`, and the preset says why: such a game can run
only once per host, which a second installation on the same host has to know.
"""
from __future__ import annotations

from collections.abc import Collection
from dataclasses import asdict, dataclass, field

from ..config import settings
from ..models.server import ConsoleKind, GameType
from .drivers import get_driver


def _split_ref(ref: str) -> tuple[str, str]:
    """`repo:tag` or `repo@sha256:…` → (repo, tag). The inverse of `container_spec.image_ref`.

    A colon before the last `/` belongs to a registry port (`registry.example.com:5000/x`), not
    to a tag.
    """
    if "@" in ref:
        repo, digest = ref.split("@", 1)
        return repo, digest
    head, _, last = ref.rpartition("/")
    if ":" in last:
        name, tag = last.rsplit(":", 1)
        return (f"{head}/{name}" if head else name), tag
    return ref, ""


@dataclass(frozen=True)
class PresetPort:
    name: str                 # key in `game_ports` ("game" is the one players connect to)
    port: int
    proto: str                # "tcp" | "udp"
    editable: bool
    env: str = ""             # container variable that must carry the same number (`extra.env`)
    help: str = ""


@dataclass(frozen=True)
class PresetValue:
    """A value the form shows as a field of its own and writes into `extra` or `extra.env`."""

    name: str
    value: str
    target: str               # "env" → `extra.env[name]`, "extra" → `extra[name]`
    secret: bool = False      # a password: input hidden, never shown again
    required: bool = False    # the form refuses to create without a value
    help: str = ""


@dataclass(frozen=True)
class Acknowledgement:
    """Something the admin has to accept in person before the server may be created."""

    id: str
    url: str
    help: str


@dataclass(frozen=True)
class Preset:
    game_type: GameType
    image_repo: str
    image_tag: str
    image_help: str
    ports: tuple[PresetPort, ...]
    console_kind: ConsoleKind
    rcon_port: int = 0
    secret_env: str = ""          # suggested name of the env variable with the console password
    secret_required: bool = False
    secret_help: str = ""
    game_version: str = ""
    version_help: str = ""
    server_type: str = ""
    ram_mb: int = 0
    heap_max_mb: int | None = None
    heap_min_mb: int | None = None
    memory_help: str = ""
    values: tuple[PresetValue, ...] = ()
    acknowledgements: tuple[Acknowledgement, ...] = ()
    version_required: bool = field(init=False, default=False)

    def __post_init__(self) -> None:
        # From the driver, not written twice: `routers/servers._check_version_required` asks
        # the same attribute on create.
        from ..models.server import GameServer

        required = get_driver(GameServer(key="preset", game_type=self.game_type)).version_required
        object.__setattr__(self, "version_required", required)


# Why a port is fixed, per game. The edit page shows it under the field.
_FIXED_PORT_VS = (
    "Fixed: the Vintage Story runner starts the server without a port, so it listens on the port "
    "in its serverconfig.json (42420). Only one Vintage Story server per host, across "
    "installations."
)
_FIXED_PORT_AVORION = (
    "Fixed: Avorion takes its query and Steam ports only as command-line arguments, and the "
    "dashboard starts the image without any. Only one Avorion server per host, across "
    "installations."
)

PRESETS: tuple[Preset, ...] = (
    # ---------------------------------------------------------------- Minecraft
    Preset(
        game_type=GameType.minecraft,
        # Image: driver docstring (`itzg/minecraft-server`); tag: games/minecraft/.env.example
        # (MINECRAFT_TAG).
        image_repo="itzg/minecraft-server",
        image_tag="2026.9.2-java25",
        image_help=(
            "Tag of itzg/minecraft-server: <image release>-<Java runtime>. It is NOT the game "
            "version. Paper 26.1 and newer need Java 25, so keep a -java25 tag for those versions."
        ),
        # games/minecraft/docker-compose.yml publishes container port 25565/tcp. `SERVER_PORT` is
        # the itzg image's variable for the port the server listens on; the driver passes
        # `extra.env` through to the container (`MinecraftDriver.container_env`).
        ports=(
            PresetPort(
                "game", 25565, "tcp", editable=True, env="SERVER_PORT",
                help=(
                    "TCP port players connect to. The image writes it into server.properties on "
                    "the first start; a later change on the edit page updates that file too."
                ),
            ),
        ),
        # Driver: Source RCON, `rcon_port or 25575`; the block's rcon service uses 25575 too.
        console_kind=ConsoleKind.source_rcon,
        rcon_port=25575,
        # dashboard/.env.example: MC_RCON_PW. The driver passes it as RCON_PASSWORD; without a
        # value the console stays closed.
        secret_env="MC_RCON_PW",
        secret_required=True,
        secret_help="RCON password (RCON is never published). Generate one: openssl rand -hex 24",
        # games/minecraft/docker-compose.yml: TYPE PAPER; .env.example: MINECRAFT_VERSION.
        server_type="PAPER",
        game_version="26.2",
        version_help=(
            "Minecraft version Paper runs. Clients must use exactly this version. At every start "
            "the image installs the newest default-channel Paper build for it."
        ),
        # games/minecraft/.env.example: MINECRAFT_MEMORY=4G sets -Xms and -Xmx ("The host needs
        # this plus about 25 %") → heap 4096 MB, cgroup limit 4096 × 1.25 = 5120 MB.
        ram_mb=5120,
        heap_max_mb=4096,
        heap_min_mb=4096,
        memory_help="Java heap for the server. The host needs this plus about 25 %.",
        # games/minecraft/.env.example and docker-compose.yml (MOTD, DIFFICULTY,
        # ENABLE_WHITELIST), passed through `extra.env`.
        values=(
            PresetValue("MOTD", "A Paper Minecraft server", "env",
                        help="Text shown under the server in the multiplayer list."),
            PresetValue("DIFFICULTY", "easy", "env", help="peaceful, easy, normal or hard."),
            PresetValue(
                "ENABLE_WHITELIST", "true", "env",
                help=(
                    "true: only players on the whitelist can join (add them with \"whitelist add "
                    "<name>\"). false: anyone with a Minecraft account can join."
                ),
            ),
        ),
        # games/minecraft/docker-compose.yml: "The image refuses to start until the Minecraft
        # EULA is accepted; the user does that." The driver sets EULA=TRUE for every container
        # it creates, so the acceptance happens here, in the form.
        acknowledgements=(
            Acknowledgement(
                "eula", "https://www.minecraft.net/en-us/eula",
                "The server does not start without accepting the Minecraft EULA. The dashboard "
                "sets EULA=TRUE for this container.",
            ),
        ),
    ),
    # ---------------------------------------------------------------- Factorio
    Preset(
        game_type=GameType.factorio,
        # Image: `version_catalog.FACTORIO_IMAGE_REPO`; tag: games/factorio/.env.example
        # (FACTORIO_TAG) plus the `-rootless` suffix the block appends and
        # `FactorioDriver.container_user` requires.
        image_repo="factoriotools/factorio",
        image_tag="2.0.77-rootless",
        image_help=(
            "Game version = image tag. Clients must run exactly this version. Keep the -rootless "
            "variant: it runs as the dashboard's uid, so the config editor can write its files."
        ),
        # Driver: PORT = game port (`container_env`); block: FACTORIO_PORT=34197/udp.
        ports=(
            PresetPort("game", 34197, "udp", editable=True,
                       help="UDP port players connect to. Factorio announces it to the server browser."),
        ),
        # Driver: Source RCON, `rcon_port or 27015`; block: RCON_PORT 27015.
        console_kind=ConsoleKind.source_rcon,
        rcon_port=27015,
        # dashboard/.env.example: FACTORIO_RCON_PW, written to config/rconpw on creation
        # (`FactorioDriver.ensure_config`). Without it the image generates its own password and
        # the driver reads it back from that file, so it is not required.
        secret_env="FACTORIO_RCON_PW",
        secret_help=(
            "Written to config/rconpw when the server is created. Empty: the image generates one "
            "on first start and the dashboard reads it from the file."
        ),
    ),
    # ---------------------------------------------------------------- Project Zomboid
    Preset(
        game_type=GameType.pz,
        # Image: `version_catalog.PZ_IMAGE_REPO`; tag: games/project-zomboid/.env.example.
        image_repo="danixu86/project-zomboid-dedicated-server",
        image_tag="42.21-release-2",
        image_help="The tag IS the game build; players' clients must run the same build.",
        # games/project-zomboid: 16261/udp (DefaultPort) and 16262/udp (UDPPort), passed as PORT
        # and UDPPORT; the driver passes the registry's numbers the same way.
        ports=(
            PresetPort("game", 16261, "udp", editable=True,
                       help="UDP port players connect to (DefaultPort). Written into the server's .ini at every start."),
            PresetPort("direct", 16262, "udp", editable=True,
                       help="Second UDP port (UDPPort), for direct connections. Written into the .ini at every start."),
        ),
        console_kind=ConsoleKind.source_rcon,
        rcon_port=27015,                       # block: ZOMBOID_RCON_PORT
        secret_env="PZ_RCON_PW",               # dashboard/.env.example
        secret_required=True,
        secret_help="RCON password. Generate one: openssl rand -base64 24",
        # Driver defaults MIN_MEMORY 2048m / MAX_MEMORY 4096m, same as the block.
        # `resource_budget._effective_demand`: PZ runs without a cgroup cap on purpose.
        heap_max_mb=4096,
        heap_min_mb=2048,
        memory_help="JVM heap. The host needs free RAM above the maximum for the JVM itself and the OS.",
        # `PZDriver.container_env` resolves both names from the environment, otherwise the value
        # stored in the dashboard (`services/secrets.py`); dashboard/.env.example names them
        # PZ_ADMIN_PW and PZ_SERVER_PW.
        values=(
            PresetValue("admin_password_env", "PZ_ADMIN_PW", "extra",
                        help="Name of the env variable with the in-game admin password; it wins while it is "
                             "set. Or leave it empty and store the password on the server's edit page "
                             "(encrypted). Neither: the RCON password is used."),
            PresetValue("password_env", "PZ_SERVER_PW", "extra",
                        help="Name of the env variable with the join password; it wins while it is set. "
                             "Or leave it empty and set the password under Configuration once the server "
                             "exists (stored encrypted). Neither: no join password."),
        ),
    ),
    # ---------------------------------------------------------------- Vintage Story
    Preset(
        game_type=GameType.vintagestory,
        # The driver works with the project's own runner image (`images/vintagestory/Dockerfile`),
        # published on GHCR by `.github/workflows/release-images.yml`; which tag comes from the
        # setting GSS_VS_RUNNER_IMAGE (`config.vs_runner_image`).
        image_repo=_split_ref(settings.vs_runner_image)[0],
        image_tag=_split_ref(settings.vs_runner_image)[1],
        image_help=(
            "The project's own runner image, downloaded when the container is created. To build it "
            "yourself instead: docker build -t <name> dashboard/images/vintagestory on the Docker "
            "host, then enter that name here (or set GSS_VS_RUNNER_IMAGE for every new server)."
        ),
        # games/vintage-story: the server listens on 42420, TCP and UDP. The game reads `Port`
        # from serverconfig.json, but the runner image passes no port and the file is written
        # by the server at its first start, so the dashboard cannot set it beforehand.
        ports=(
            PresetPort("game", 42420, "tcp", editable=False, help=_FIXED_PORT_VS),
            PresetPort("game_udp", 42420, "udp", editable=False, help=_FIXED_PORT_VS),
        ),
        # Console through the "Vintage RCON" mod; block: VINTAGESTORY_RCON_PORT=42425.
        console_kind=ConsoleKind.source_rcon,
        rcon_port=42425,
        secret_env="VS_RCON_PW",               # dashboard/.env.example
        secret_help=(
            "Only used with the Vintage RCON server mod installed; put the same value into "
            "\"Password\" in the mod's vsrcon.json."
        ),
        # games/vintage-story/.env.example: VINTAGESTORY_TAG=static-v1.22.6. The runner image has
        # no default version (`VintageStoryDriver.version_required`).
        game_version="1.22.6",
        version_help="Game version the runner downloads at start; players need a matching client.",
    ),
    # ---------------------------------------------------------------- Arma Reforger
    Preset(
        game_type=GameType.arma_reforger,
        # games/arma-reforger: image and REFORGER_TAG.
        image_repo="ghcr.io/acemod/arma-reforger",
        image_tag="sha-d042142",
        image_help=(
            "The tag pins the wrapper (launcher, SteamCMD, base OS), NOT the game: the game server "
            "is downloaded from Steam and updated on every container start."
        ),
        # `ReforgerDriver.initial_config` writes bindPort/publicPort from "game" and the A2S port
        # from "a2s"; block: 2001/udp and 17777/udp.
        ports=(
            PresetPort("game", 2001, "udp", editable=True,
                       help="UDP game port, announced to the Bohemia backend."),
            PresetPort("a2s", 17777, "udp", editable=True,
                       help="UDP port for A2S (Steam query) requests."),
        ),
        # Driver: BattlEye RCON, `rcon_port or 19999`; block: RCON_PORT 19999.
        console_kind=ConsoleKind.battleye_rcon,
        rcon_port=19999,
        secret_env="REFORGER_RCON_PW",         # dashboard/.env.example
        secret_required=True,
        secret_help="BattlEye RCON password: 3..32 characters, no spaces. Generate one: openssl rand -hex 12",
    ),
    # ---------------------------------------------------------------- Avorion
    Preset(
        game_type=GameType.avorion,
        # Image: `version_catalog.AVORION_IMAGE_REPO`; tag: games/avorion/.env.example.
        image_repo="rfvgyhn/avorion",
        image_tag="2.5.13.44140",
        image_help="The tag IS the game version. Never move a galaxy from a beta tag back to a stable one.",
        # games/avorion: game 27000 tcp+udp, query 27003, Steam query 27020, Steam master 27021.
        # The block passes them as command arguments (`--port` …); the dashboard passes no
        # command line, so they cannot be changed from here. (`server.ini` holds only the game
        # port, measured on a generated file; the other three exist only as arguments.)
        ports=(
            PresetPort("game", 27000, "tcp", editable=False, help=_FIXED_PORT_AVORION),
            PresetPort("game_udp", 27000, "udp", editable=False, help=_FIXED_PORT_AVORION),
            PresetPort("query", 27003, "udp", editable=False, help=_FIXED_PORT_AVORION),
            PresetPort("steam_query", 27020, "udp", editable=False, help=_FIXED_PORT_AVORION),
            PresetPort("steam_master", 27021, "udp", editable=False, help=_FIXED_PORT_AVORION),
        ),
        # Driver: `rcon_port or 27015` (written into server.ini by `ensure_config`).
        console_kind=ConsoleKind.source_rcon,
        rcon_port=27015,
        secret_env="AVORION_RCON_PW",          # dashboard/.env.example
        secret_help=(
            "Written into server.ini when the server is created. Empty: the dashboard generates "
            "one and reads it from the same file."
        ),
    ),
    # ---------------------------------------------------------------- Valheim
    Preset(
        game_type=GameType.valheim,
        # games/valheim: image and VALHEIM_TAG.
        image_repo="ghcr.io/community-valheim-tools/valheim-server",
        image_tag="1.4.0",
        image_help=(
            "This is the image version, NOT the game version: the image installs the current "
            "Valheim server from Steam at every start."
        ),
        # Driver: SERVER_PORT = game port, Steam query = "a2s" or game + 1
        # (`ValheimDriver._query_port`); block: 2456, 2457, 2458 (all udp).
        ports=(
            PresetPort("game", 2456, "udp", editable=True,
                       help="UDP game port. Valheim announces it to Steam."),
            PresetPort("a2s", 2457, "udp", editable=True,
                       help="UDP Steam query port. Valheim always uses the game port + 1; keep the two in step."),
            PresetPort("crossplay", 2458, "udp", editable=True,
                       help="UDP port for crossplay and for mods that talk over game port + 2."),
        ),
        # Driver / model: Valheim has no remote console at all.
        console_kind=ConsoleKind.none,
        # games/valheim/.env.example: SERVER_PASS is required. The form sends it in `extra.env`;
        # `POST /api/servers` moves it into encrypted storage (`services/secrets.split_literals`),
        # where `ValheimDriver.container_env` reads it.
        values=(
            PresetValue(
                "SERVER_PASS", "", "env", secret=True, required=True,
                help=(
                    "Join password, at least 5 characters, must not appear in the server name. "
                    "Every player needs it, so make it typeable."
                ),
            ),
        ),
    ),
)


def preset_list(games: Collection[GameType] | None = None) -> list[dict]:
    """The presets as plain JSON (enums as their values).

    `games` limits the list to those game types — the router passes the active games
    (`services/game_settings.py`); `None` means every preset.
    """
    out = []
    for p in PRESETS:
        if games is not None and p.game_type not in games:
            continue
        d = asdict(p)
        d["game_type"] = p.game_type.value
        d["console_kind"] = p.console_kind.value
        out.append(d)
    return out


def preset_for(game_type: GameType) -> Preset | None:
    return next((p for p in PRESETS if p.game_type == game_type), None)
