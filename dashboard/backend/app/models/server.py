"""Registry of all managed game servers.

Single source of truth for every server. Rows are created through the UI or
`POST /api/servers`. New table → covered by create_all.

JSON fields (`game_ports`, `extra`) are deliberately stored as JSON strings
(house style, cf. `audit_log.payload`) — robust without SQLModel `sa_column` special cases.
"""
from __future__ import annotations

import datetime as dt
import enum

from sqlmodel import Field, SQLModel

from ..config import settings
from .player import utcnow_naive


class GameType(str, enum.Enum):
    pz = "pz"
    minecraft = "minecraft"
    vintagestory = "vintagestory"
    arma_reforger = "arma_reforger"
    valheim = "valheim"
    avorion = "avorion"
    factorio = "factorio"


class ManagementMode(str, enum.Enum):
    compose = "compose"      # container belongs to Docker Compose: only start/stop/restart + LIVE cgroup update
    dashboard = "dashboard"  # the dashboard owns the lifecycle: create/recreate via the Docker API


class ConsoleKind(str, enum.Enum):
    source_rcon = "source_rcon"  # PZ, Minecraft, Vintage Story (via RCON-Mod), Factorio, Avorion
    battleye_rcon = "battleye_rcon"  # Arma Reforger (modified BattlEye RCON, via services/bercon.py)
    stdin = "stdin"
    # Valheim: the dedicated server has NO remotely controllable console — neither RCON nor stdin.
    # The player list comes from the Steam query (services/a2s.py), moderation from files.
    none = "none"


class GameServer(SQLModel, table=True):
    __tablename__ = "game_servers"

    id: int | None = Field(default=None, primary_key=True)
    key: str = Field(index=True, unique=True)           # URL slug, e.g. "pz-1"
    game_type: GameType
    display_name: str = ""
    sort_order: int = 0
    enabled: bool = True
    management_mode: ManagementMode = Field(default=ManagementMode.dashboard)

    # --- container / connection ---
    container_name: str = ""
    data_dir: str = ""                 # host bind base — MUST be below an allowed root (hardening in container_spec)
    image_repo: str = ""
    image_tag: str = ""
    network: str = Field(default_factory=lambda: settings.game_network)   # GAME_NETWORK
    console_kind: ConsoleKind = Field(default=ConsoleKind.source_rcon)
    rcon_host: str = ""
    rcon_port: int = 0
    rcon_password: str = ""            # fallback; rcon_password_env (from the host's .env) is preferred
    rcon_password_env: str = ""
    game_ports: str = "{}"            # JSON string: {"game": "16261/udp", ...}
    # Host address the game ports are published on (`services/ports.py`): 0.0.0.0 = every
    # interface, 127.0.0.1 = this host only, or one address of the host.
    bind_address: str = "0.0.0.0"

    # --- version / rollback ---
    game_version: str = ""            # MC/VS: VERSION env; PZ: == image_tag
    server_type: str = ""             # MC TYPE (VANILLA/PAPER/FABRIC/FORGE/...)
    previous_image_tag: str = ""      # rollback source for PZ (tag = version)
    previous_game_version: str = ""   # rollback source for MC/VS (VERSION env)
    previous_type: str = ""

    # --- resources (cgroup + JVM) ---
    ram_mb: int = 0                   # HostConfig.Memory — the only unit the budget reserves (0 = no cap)
    cpu_cores: float = 0.0            # → NanoCpus (cpu_cores * 1e9); 0 = no limit
    heap_max_mb: int | None = None    # JVM -Xmx (NULL for VS/.NET)
    heap_min_mb: int | None = None    # JVM -Xms

    # --- idle auto-stop ---
    idle_autostop_enabled: bool = True
    idle_timeout_minutes: int | None = None   # NULL = global default (resource_budget)
    last_active_at: dt.datetime | None = None

    # --- cross-server participation (each switchable individually) ---
    # Mind the direction, the names read the other way round: `cross_server/bridge.ingest_once`
    # READS the chat of the `bridge_in` servers, `fanout_once` SENDS to the `bridge_out`
    # servers. Getting it wrong goes unnoticed because both default to `True`.
    # `tests/test_bridge_direction.py` pins the meaning to the code, not to this comment.
    bridge_in: bool = True            # SENDS: its chat is read and fed into the bridge
    bridge_out: bool = True           # RECEIVES: gets the other servers' chat announced
    # CAUTION, the name is misleading: for Project Zomboid this field switches the **Steam
    # visibility** (`PUBLIC` in `pz.container_env`, i.e. the entry in the server browser) — NOT
    # the who-is-where overview. `cross_server/features.presence()` does not read it at all; that
    # filters solely on `presence_enabled` in the cross-server settings.
    #
    # Whoever wants to hide presence with it takes PZ out of the Steam browser instead. It is
    # not renamed nonetheless: it sits in DB, API, frontend and `container_env`, for zero gain in
    # behaviour. Side effect to be aware of: a change only takes effect after the container is
    # recreated.
    presence_visible: bool = True
    announce_target: bool = True      # receives shared announcements

    extra: str = "{}"                 # JSON string for free-form/game-specific options
    created_at: dt.datetime = Field(default_factory=utcnow_naive)
    updated_at: dt.datetime = Field(default_factory=utcnow_naive)
