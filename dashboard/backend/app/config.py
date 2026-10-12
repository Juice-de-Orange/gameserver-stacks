"""Central configuration from environment variables (`.env`)."""
import difflib
import ipaddress
import json
import logging
import os
import re
from collections.abc import Iterable, Mapping
from functools import lru_cache
from typing import Annotated

from pydantic import AliasChoices, AliasPath, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

# The secret the code ships with. It is public, so a token signed with it can be forged by anyone.
DEFAULT_JWT_SECRET = "dev-insecure-change-me"
JWT_SECRET_MIN_LENGTH = 32
SECRET_KEY_MIN_LENGTH = 32

_HOSTNAME_RE = re.compile(r"^(?!-)[A-Za-z0-9-]{1,63}(?<!-)(\.(?!-)[A-Za-z0-9-]{1,63}(?<!-))*$")


def _split_list(name: str, value: object) -> object:
    """Accept a list setting as a JSON array or as a comma-separated string.

    pydantic-settings decodes list fields as JSON only, so `A=/x,/y` used to abort the start with
    a decoding traceback that did not name the variable. The fields that use this helper are
    declared `NoDecode` and parsed here instead.
    """
    if not isinstance(value, str):
        return value
    text = value.strip()
    if not text:
        return []
    if text.startswith("["):
        try:
            parsed = json.loads(text)
        except ValueError as exc:
            raise ValueError(
                f"{name} looks like a JSON list but is not valid JSON ({exc}). "
                f'Use ["a","b"] or a comma-separated list: a,b'
            ) from exc
        if not isinstance(parsed, list) or not all(isinstance(p, str) for p in parsed):
            raise ValueError(f"{name} must be a list of strings")
        return parsed
    return [part.strip() for part in text.split(",") if part.strip()]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", case_sensitive=False)

    # --- Postgres ---
    postgres_user: str = "stacks"
    postgres_password: str = "changeme"
    postgres_db: str = "stacks"
    postgres_host: str = "console-db"     # compose service name, resolved within the project
    postgres_port: int = 5432

    # --- Auth / JWT ---
    # The default only exists so that tests and tooling can import the settings. The API refuses
    # to start with it or with anything shorter than JWT_SECRET_MIN_LENGTH (`jwt_secret_problem`).
    jwt_secret: str = DEFAULT_JWT_SECRET
    jwt_access_ttl_min: int = 30
    jwt_refresh_ttl_days: int = 14

    # --- Encryption of the secrets entered in the UI (`services/secrets.py`) ---
    # A game server's console password can be typed into the dashboard instead of being put into
    # the .env; it is stored encrypted with a key derived from this value. The API refuses to
    # start without it or with anything shorter than SECRET_KEY_MIN_LENGTH (`secret_key_problem`).
    # Losing or changing it makes the stored passwords unreadable (they have to be entered again).
    secret_key: str = Field(default="", validation_alias="GSS_SECRET_KEY")

    # --- Client address behind reverse proxies ---
    # Peers whose forwarding headers (`X-Forwarded-For`, `CF-Connecting-IP`) are believed. Each
    # entry is an IP address, a CIDR range or a host name that is resolved at runtime (the
    # compose file uses the service name of the bundled Caddy, because its address is assigned
    # by Docker). Empty: the headers are ignored and the TCP peer is the client.
    # JSON list or comma-separated. Used by `auth/ratelimit.client_ip`.
    trusted_proxies: Annotated[list[str], NoDecode] = []

    @field_validator("trusted_proxies", mode="before")
    @classmethod
    def _split_proxies(cls, v: object) -> object:
        return _split_list("TRUSTED_PROXIES", v)

    @field_validator("trusted_proxies")
    @classmethod
    def _check_proxies(cls, v: list[str]) -> list[str]:
        cleaned = []
        for entry in (e.strip() for e in v):
            if not entry:
                continue
            try:
                ipaddress.ip_network(entry, strict=False)
            except ValueError:
                # Not an address or range. A host name is fine; anything else (a range with a
                # typo, say) would silently never match, so it is rejected at start.
                if "/" in entry or not _HOSTNAME_RE.match(entry) or entry.replace(".", "").isdigit():
                    raise ValueError(
                        f"TRUSTED_PROXIES: {entry!r} is neither an IP address, a CIDR range "
                        f"nor a host name"
                    ) from None
            cleaned.append(entry)
        return cleaned

    # --- Bootstrap admin (seeded on the first start only) ---
    seed_admin_username: str = "admin"
    seed_admin_password: str = ""

    # --- Join page (public convenience information, optional) ---
    # Address under which the game servers are reachable from outside. Empty: the join page says
    # "no address configured" instead of showing a wrong one. The starting value: Settings →
    # Network overrides it (`services/install_settings.py`), and so does `extra.public_host` per
    # server.
    public_host: str = ""
    mod_collection_url: str = ""  # Steam Workshop collection: one click to subscribe to all mods
    community_url: str = ""        # Discord/community invitation

    # --- Reachability check from outside (`services/reachability/outside.py`) ---
    # Off by default: the check sends the server's public address (and port) to third-party
    # services. `on` offers the button; each service can then be switched off on its own. A
    # self-hosted instance of the open-source services can replace the public one by URL.
    reachability_probes: bool = Field(default=False, validation_alias="GSS_REACHABILITY_PROBES")
    reachability_steam: bool = Field(default=True, validation_alias="GSS_REACHABILITY_STEAM")
    reachability_mcstatus: bool = Field(default=True, validation_alias="GSS_REACHABILITY_MCSTATUS")
    reachability_mcstatus_url: str = Field(
        default="https://api.mcstatus.io", validation_alias="GSS_REACHABILITY_MCSTATUS_URL",
    )
    reachability_portcheck: bool = Field(default=True, validation_alias="GSS_REACHABILITY_PORTCHECK")
    reachability_portcheck_url: str = Field(
        default="https://portchecker.io", validation_alias="GSS_REACHABILITY_PORTCHECK_URL",
    )

    # --- Docker socket proxy (compose service name, resolved within the project) ---
    docker_proxy_url: str = "http://console-dockerproxy:2375"

    # --- Game servers on the host ---
    # Host directory that holds every game server's data. The compose file binds it into the API
    # container under the SAME path, because `container_spec` hands these paths to the Docker
    # API as host paths when it creates a game container.
    data_root: str = "/srv/gameservers"
    # Docker network that new game servers join by default (created by the compose file).
    game_network: str = "gameserver-net"

    # --- Host directories a dashboard-managed container may bind ---
    # A server's `data_dir` MUST lie below one of these roots (no traversal). Empty = [data_root].
    # JSON list or comma-separated.
    allowed_data_roots: Annotated[list[str], NoDecode] = []

    @field_validator("allowed_data_roots", mode="before")
    @classmethod
    def _split_roots(cls, v: object) -> object:
        return _split_list("ALLOWED_DATA_ROOTS", v)

    @field_validator("allowed_data_roots")
    @classmethod
    def _clean_roots(cls, v: list[str]) -> list[str]:
        # Fail closed: only non-empty absolute paths other than "/" (an empty or "/" entry would
        # open the allowlist, because every path matches it). An empty result falls back to
        # data_root.
        return [r.strip() for r in v if r and r.strip().startswith("/") and r.strip().strip("/")]

    @model_validator(mode="after")
    def _default_roots(self) -> "Settings":
        if not self.allowed_data_roots:
            self.allowed_data_roots = [self.data_root]
        return self

    # --- Firewall helper (compose profile `firewall`, `services/firewall.py`) ---
    # Shared secret of the API and `console-firewall`; empty = the helper is off (the default).
    # The helper refuses to start with fewer than 32 characters.
    firewall_token: str = Field(default="", validation_alias="GSS_FIREWALL_TOKEN")
    # The helper's Unix socket, in a volume that only the API and the helper mount.
    firewall_socket: str = Field(default="/run/console-firewall/helper.sock",
                                 validation_alias="GSS_FIREWALL_SOCKET")
    # Networks the opened game ports accept connections from (CIDR ranges, comma-separated or a
    # JSON list). Empty: from anywhere.
    firewall_sources: Annotated[list[str], NoDecode] = Field(default=[], validation_alias="GSS_FIREWALL_SOURCES")

    @field_validator("firewall_sources", mode="before")
    @classmethod
    def _split_firewall_sources(cls, v: object) -> object:
        return _split_list("GSS_FIREWALL_SOURCES", v)

    @field_validator("firewall_sources")
    @classmethod
    def _check_firewall_sources(cls, v: list[str]) -> list[str]:
        cleaned = []
        for entry in (e.strip() for e in v):
            if not entry:
                continue
            try:
                cleaned.append(str(ipaddress.ip_network(entry, strict=False)))
            except ValueError:
                raise ValueError(f"GSS_FIREWALL_SOURCES: {entry!r} is not an IP network "
                                 f"(e.g. 198.51.100.0/24)") from None
        return cleaned

    # --- Games this installation runs (`services/game_settings.py`) ---
    # Seeds the `game_settings` table on the first start only; after that the admin switches
    # games in the UI (Settings → Games) and this value changes nothing any more. A comma list
    # of game types (`minecraft,factorio`) or `all`. Empty: no game is active and the first-run
    # screen asks. A name that is not a game type stops every start (`game_settings.parse_games`).
    games: Annotated[list[str], NoDecode] = Field(default=[], validation_alias="GSS_GAMES")

    @field_validator("games", mode="before")
    @classmethod
    def _split_games(cls, v: object) -> object:
        return _split_list("GSS_GAMES", v)

    # Helper image for Steam Workshop downloads (`services/steam_download.py`). Pulled the first
    # time an active game needs a workshop item, never at install. Build it yourself with
    # `docker build -t <tag> dashboard/images/steamcmd` and set the tag here.
    steamcmd_image: str = Field(
        default="ghcr.io/juice-de-orange/stacks-console-steamcmd:1",
        validation_alias="GSS_STEAMCMD_IMAGE",
    )
    # Runner image for Vintage Story servers (`dashboard/images/vintagestory`), the starting value
    # of the "Add server" form (`services/server_presets.py`). Pulled when a container is created.
    # Build it yourself with `docker build -t <tag> dashboard/images/vintagestory` and set the tag
    # here; a server that exists keeps the image in its own row.
    vs_runner_image: str = Field(
        default="ghcr.io/juice-de-orange/stacks-console-vintagestory-runner:1",
        validation_alias="GSS_VS_RUNNER_IMAGE",
    )

    # --- Scheduler ---
    # Time zone of every fixed time of day (nightly jobs, restart and broadcast schedules).
    # Without an explicit zone APScheduler takes the local zone, which is UTC in a container
    # without `TZ` — and UTC has no daylight saving time, so "05:00" drifts against local time
    # twice a year. The default stays UTC because changing it shifts EVERY stored time by the
    # zone offset; that decision belongs to the operator.
    scheduler_tz: str = "UTC"
    # Retention of the job tables. Long on purpose: `player_events` feeds the leaderboard and
    # the death map, so a short period there is data loss rather than clean-up. `0` means
    # unlimited.
    incident_retention_days: int = 365
    player_event_retention_days: int = 730
    operation_retention_days: int = 180

    # --- Watchdog (RCON liveness; restarts a hung game process) ---
    watchdog_enabled: bool = True
    watchdog_fail_threshold: int = 3   # 60 s checks with dead RCON while the container runs
    watchdog_cooldown_min: int = 20    # minimum gap between automatic restarts

    # --- Automatic version change (hotfixes of the same minor version ONLY) ---
    # Off by default: an update that happens by itself is exactly the kind of silent change this
    # project avoids. Whoever sets it decides visibly in the `.env`.
    # Background and limits: `services/auto_update.py`.
    auto_update_enabled: bool = False

    # --- Metric history ---
    metrics_retention_days: int = 30
    # The audit log is security evidence, not display data — hence a year instead of the 30 days
    # of the metrics.
    audit_retention_days: int = 365
    # The chat bridge stores every chat line of every server plus three indexes. 14 days, because
    # the bridge needs the rows only as a hand-over buffer and for loop protection; the "who is
    # online" view reads the last minutes, not months.
    chat_retention_days: int = 14

    # --- Backup retention ---
    # Age alone is no limit once the weekly run covers every server: a large world can be
    # several GB per archive (4 GB measured for a Minecraft world, i.e. about 52 GB in 90 days
    # for one server). Hence an additional size limit per server, pruned oldest first.
    backup_retention_days: int = 90
    backup_max_gb_per_server: float = 12.0
    # How many archives ALWAYS stay, even beyond the size limit. A retention rule that may delete
    # the last backup is worse than none.
    backup_keep_min: int = 2

    @property
    def database_url(self) -> str:
        return (
            f"postgresql+psycopg://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )


def jwt_secret_problem(secret: str) -> str | None:
    """Why this JWT secret must not be used, or None if it is acceptable.

    With the built-in default or a short secret anybody can forge an admin token offline, so
    the API refuses to start (`main.lifespan`) — unconditionally, not only on a first start.
    """
    if secret == DEFAULT_JWT_SECRET:
        reason = "is still the built-in default, which is public"
    elif len(secret) < JWT_SECRET_MIN_LENGTH:
        reason = f"is only {len(secret)} characters long (minimum {JWT_SECRET_MIN_LENGTH})"
    else:
        return None
    return (
        f"JWT_SECRET {reason}. Tokens signed with it can be forged. Set a random JWT_SECRET of "
        f"at least {JWT_SECRET_MIN_LENGTH} characters in the .env, for example with "
        f"`openssl rand -hex 32`, and start again."
    )


def secret_key_problem(key: str) -> str | None:
    """Why this GSS_SECRET_KEY must not be used, or None if it is acceptable.

    Checked at every start (`main.lifespan`), like JWT_SECRET: a missing key would leave nothing
    to encrypt with, and a short one is guessable. There is no built-in default to fall back on —
    a default would be public, and everything encrypted with it readable for anybody.
    """
    if not key:
        reason = "is not set"
    elif len(key) < SECRET_KEY_MIN_LENGTH:
        reason = f"is only {len(key)} characters long (minimum {SECRET_KEY_MIN_LENGTH})"
    else:
        return None
    return (
        f"GSS_SECRET_KEY {reason}. It encrypts the game-server passwords entered in the "
        f"dashboard. Set a random GSS_SECRET_KEY of at least {SECRET_KEY_MIN_LENGTH} characters "
        f"in the .env, for example with `openssl rand -hex 32`, keep it with your backups, and "
        f"start again."
    )


# --- Unknown variables -------------------------------------------------------------------------
# `extra="ignore"` keeps a variable nobody reads from aborting the start — and so a misspelt
# setting (`JWT_ACCESS_TTL_MN`) used to vanish without a trace while its default took over. A name
# with one of these prefixes is meant for this project, so one that is neither a setting nor
# read elsewhere gets a warning at start (it is still ignored). Other names are not checked: the
# API's environment also holds the image's own variables and the game passwords, whose names
# the operator chooses (`rcon_password_env`).
CHECKED_ENV_PREFIXES = (
    "GSS_", "POSTGRES_", "JWT_", "SMTP_", "SEED_ADMIN_", "DASHBOARD_", "WATCHDOG_", "BACKUP_",
    "DB_",
)
# Names with those prefixes that are not settings of this class but are read elsewhere: by
# docker-compose.yml (it passes the whole .env to the API container), by the database dump
# sidecar (`ops/backup-loop.sh`), by `app.cli` and by the test suite.
OTHER_KNOWN_ENV = frozenset({
    "GSS_VERSION", "DASHBOARD_PORT", "DASHBOARD_BIND",
    "DASHBOARD_DOMAIN", "DASHBOARD_HTTP_PORT", "DASHBOARD_HTTPS_PORT",
    "DB_DATA_DIR", "DB_BACKUP_DIR", "DB_BACKUP_HOUR", "DB_BACKUP_MINUTE", "DB_BACKUP_KEEP_DAYS",
    "DB_BACKUP_OWNER",
    "GSS_GRACEFUL_RECORD", "GSS_SMOKE_BASE_URL", "GSS_SMOKE_ADMIN_TOKEN",
    # Settings of the firewall helper alone (dashboard/firewall), passed to it by compose.
    "GSS_FIREWALL_BACKEND", "GSS_FIREWALL_PORT_RANGE", "GSS_FIREWALL_RECONCILE_SECONDS",
})

# Settings that existed once and are read by nothing any more. They get a warning of their own
# instead of "unknown", so that whoever still sets them learns they have no effect. Mail: the
# sign-in by link they configured was never built (no code ever sent a mail), yet setting
# DASHBOARD_BASE_URL used to make SMTP settings mandatory at start.
RETIRED_ENV = {
    "DASHBOARD_BASE_URL": "sign-in by mail was never built",
    "SMTP_HOST": "sign-in by mail was never built",
    "SMTP_PORT": "sign-in by mail was never built",
    "SMTP_USER": "sign-in by mail was never built",
    "SMTP_PASSWORD": "sign-in by mail was never built",
    "MAIL_FROM": "sign-in by mail was never built",
}

_log = logging.getLogger("uvicorn.error")


def setting_env_names(model: type[BaseSettings] = Settings) -> set[str]:
    """Every environment name a field of `model` is read from (field name or its aliases)."""
    names: set[str] = set()
    prefix = model.model_config.get("env_prefix", "")
    for field_name, field in model.model_fields.items():
        alias = field.validation_alias or field.alias
        choices = alias.choices if isinstance(alias, AliasChoices) else [alias] if alias else []
        found = [c for c in choices if isinstance(c, str)]
        found += [c.path[0] for c in choices if isinstance(c, AliasPath) and isinstance(c.path[0], str)]
        names.update(n.upper() for n in found or [prefix + field_name])
    return names


def unknown_env_warnings(present: Iterable[str],
                         known: set[str] | frozenset[str] | None = None) -> list[str]:
    """One message per name in `present` that carries a checked prefix but is not known."""
    if known is None:
        known = setting_env_names() | OTHER_KNOWN_ENV
    messages = []
    for name in sorted({n.upper() for n in present}):
        if name in RETIRED_ENV and name not in known:
            messages.append(f"Setting {name} is no longer used ({RETIRED_ENV[name]}) — it is "
                            f"ignored. Remove it from the .env.")
            continue
        if not name.startswith(CHECKED_ENV_PREFIXES) or name in known:
            continue
        close = difflib.get_close_matches(name, sorted(known), n=1, cutoff=0.75)
        hint = f" Did you mean {close[0]}?" if close else ""
        messages.append(f"Unknown setting {name} in the environment — it is ignored.{hint}")
    return messages


def _present_env_names(environ: Mapping[str, str]) -> set[str]:
    """The names in the environment plus those in the `.env` file the settings read, if any."""
    names = set(environ)
    env_file = Settings.model_config.get("env_file")
    if isinstance(env_file, str) and os.path.isfile(env_file):
        from dotenv import dotenv_values

        names |= set(dotenv_values(env_file))
    return names


@lru_cache
def get_settings() -> Settings:
    loaded = Settings()
    for message in unknown_env_warnings(_present_env_names(os.environ)):
        _log.warning(message)
    return loaded


settings = get_settings()
