"""The GameDriver abstraction.

Defines the game-agnostic interface behind which the game-specific quirks live. Routers and the
scheduler do NOT branch on the game type; they resolve a driver per `game_servers` row via
`get_driver(server)` (see `__init__.py`).

Deliberately lightweight and free of import side effects: only `GameType` is imported at
runtime; `GameServer` is a type hint only (`TYPE_CHECKING`), so this module pulls in no heavy
dependencies (DB/httpx) and creates no import cycle. Driver methods are STATELESS (each takes
`server`), which lets `get_driver` return one shared singleton per game type.
"""
from __future__ import annotations

import datetime as dt
import enum
import logging
import re
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, ClassVar

from ...errors import err
from ...models.player import utcnow_naive
from ...models.server import GameType

log = logging.getLogger("app.drivers")

if TYPE_CHECKING:  # type checkers only; no runtime import (avoids a cycle and the DB import)
    from ...models.server import GameServer

# Strip control characters, quotes, semicolons AND angle brackets from text that is embedded in
# an RCON string. `<`/`>` go too, so a bridged message injected via `say` can never look like
# player chat (`<Name> text`): loop and spoofing protection. This is the RCON injection guard for
# PZ `servermsg "…"`, MC `say …` and VS `/announce …`.
_RCON_UNSAFE = re.compile(r'[\x00-\x1f\x7f";<>]')


def sanitize_message(s: str) -> str:
    return _RCON_UNSAFE.sub("", s or "").strip()


# --- Secrets do not leave the server -----------------------------------------------------------
#
# `GET /api/servers/{key}/config` is gated by `can_edit_sandbox`, a permission handed out
# individually to config maintainers. Anyone who can read the RCON password through it has the
# full console of the game server as soon as they reach the internal network, which is in effect
# `can_console_full` across the permission boundary. So every driver masks secrets on read and
# marks them `env_managed` so `put_config` refuses to write them.

MASKED = "••••••"

_SECRET_HINTS = ("password", "passwort", "secret", "token", "apikey", "api_key")
# Short forms that appear as a WORD of their own in the key: `SERVER_PASS`, `rcon_pw`,
# `db-passwd`. As plain substrings they would be too greedy (`pass` is in `bypass`, `pw` in
# `pwd_hash`), hence the word boundaries.
# `key` is deliberately NOT in the list: `apikey`/`api_key` cover the real case, and a bare `key`
# would mask harmless structural keys (the registry itself has one).
_SECRET_WORDS = ("pass", "pw", "passwd", "pwd")
_SECRET_WORD_RE = re.compile(r"(?:^|[^a-z0-9])(?:" + "|".join(_SECRET_WORDS) + r")(?:[^a-z0-9]|$)")


def looks_like_secret(key: str) -> bool:
    """Heuristic on the key name.

    For Vintage Story this is the honest solution, not a convenience: `serverconfig.json` is a
    free-form JSON document, and the exact name of the password key of the "VintageRCon" mod is
    unverified. A fixed list of names would be a claim there.

    The short forms matter: Valheim keeps its configuration in the registry column `extra`,
    including `SERVER_PASS`, the join password. `extra` is part of every server row returned by
    `server_public()`, which only needs `can_view_status`. `server_pass` contains none of the
    long hints, so without the word match it would go out in clear text.
    `test_secrets_are_named_what_they_are.py` pins that EVERY field a driver marks as secret
    is recognised here.
    """
    low = (key or "").lower()
    if any(note in low for note in _SECRET_HINTS):
        return True
    return bool(_SECRET_WORD_RE.search(low))


def is_env_managed_key(key: str, structural: frozenset[str] = frozenset()) -> bool:
    """Must this key NOT be written through the config editor?

    Two reasons: it is a secret, or it is a structural value whose change would cut the dashboard
    off from its own server (RCON port, `enable-rcon`) or leave the backup definition pointing at
    nothing (`level-name` determines `world_subdirs`).
    """
    return key in structural or looks_like_secret(key)


def mask_if_secret(key: str, value: str) -> str:
    """Replace secrets with dots, leave structural values in clear text (they are read-only).

    Empty stays empty; otherwise "set" could not be told apart from "not set".
    """
    if not looks_like_secret(key):
        return value
    return MASKED if value not in (None, "") else ""


def _game_port_spec(server: GameServer) -> str:
    """The published game port from the registry, e.g. "16261/udp"."""
    import json

    try:
        return str(json.loads(server.game_ports or "{}").get("game") or "")
    except ValueError:
        return ""


def game_port(server: GameServer) -> int:
    spec = _game_port_spec(server)
    try:
        return int(spec.split("/")[0])
    except (ValueError, IndexError):
        return 0


def game_proto(server: GameServer) -> str:
    spec = _game_port_spec(server)
    return spec.split("/")[1] if "/" in spec else "udp"


# Replies with which a game REJECTS a command. RCON does not report these as errors:
# `rcon_command` only raises on transport and auth problems, a rejected command comes back as
# ordinary reply text. The word boundaries are required; otherwise `error` would also match a
# player name such as "Terrorbyte".
_REJECTED_RE = re.compile(
    r"\b(?:unknown\s+command|command\s+not\s+found|no\s+such\s+command|error|failed|"
    r"not\s+(?:a\s+|an\s+)?admin|permission\s+denied|not\s+allowed|usage)\b",
    re.I,
)


def check_console_response(out: str, *, command: str, game: str) -> str:
    """Did the server EXECUTE this command, or merely reply to it?

    `rcon_command` only raises on transport and auth errors; an `Unknown command: /save` or a
    permission error comes back as ordinary reply text. A caller that wraps a save command in
    `try`/`except` alone would therefore treat a rejected save as done, and a backup would pack
    the world without a save point, with no log line and no `QuiesceWarning`.

    Returns the reply unchanged so the function can be wrapped around a call. Raises
    `RuntimeError` when the reply looks like a rejection. An empty reply is fine: many verbs
    acknowledge silently.
    """
    text = (out or "").strip()
    if text and _REJECTED_RE.search(text):
        raise RuntimeError(f"{game}: `{command}` was not executed ({text.splitlines()[0][:120]!r})")
    return out


def write_config_file(path: str, content: str, *, mode: int | None = None) -> None:
    """Replace a configuration file, keeping a copy of the old one and swapping atomically.

    * Without the atomic swap an interrupted write (disk full, container died) leaves HALF a
      file. Factorio then refuses to start on unreadable JSON; Avorion rewrites its own default
      on the next start, including an empty `rconPassword`, and the console is gone.
    * Without the copy there is no way back.

    The `.bak` is deliberately simple: exactly ONE previous state, in the same directory, no
    rotation. It does not replace a backup; it covers the case that the write itself goes wrong.
    """
    import os
    import shutil

    os.makedirs(os.path.dirname(path), exist_ok=True)
    if os.path.exists(path):
        try:
            shutil.copy2(path, path + ".bak")
        except OSError as e:      # noqa: BLE001 — a missing backup copy must not prevent the write
            log.warning("Backup copy of %s could not be created (%s) — writing anyway", path, e)
    tmp = path + ".incoming"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(content)
        f.flush()
        os.fsync(f.fileno())
    if mode is not None:
        os.chmod(tmp, mode)
    os.replace(tmp, path)         # atomic: never expose half a file


def rcon_secret_from_file(server: GameServer, from_file: str) -> str:
    """Pick the RCON password when the server reads it from a FILE rather than from the env.

    This applies to Avorion (`galaxy/server.ini`) and Factorio (`config/rconpw`). Neither image
    has an env variable for it; whatever the file holds IS the running server's password. The
    `.env` is only a statement of intent.

    The file therefore wins over the `.env`. The two values drift apart in normal operation: a
    restore brings back Avorion's `server.ini` with the password it had then (the file lives
    INSIDE the galaxy directory), and the Factorio image generates one itself when none was
    provided at creation. If the `.env` won, `rcon_command` would fail on every call, the
    watchdog would count that as "RCON dead while the container runs" and restart a healthy
    server after every cooldown.

    When the two values differ this is logged as a warning rather than resolved silently: the
    operator has to know that the `.env` value has no effect.
    """
    from ..servers import resolve_secret

    file = (from_file or "").strip()
    env = (resolve_secret(server) or "").strip()
    if file and env and file != env:
        log.warning(
            "%s: the RCON password in the file differs from the one in the .env — the FILE "
            "applies, because that is the only one the server reads. To align them change the "
            "file (the server has to be stopped for that), not the .env.",
            server.key,
        )
    return file or env


def public_host(server: GameServer) -> str:
    """The address under which this server is reachable from outside.

    Per server from `extra.public_host`, otherwise the installation's address (Settings →
    Network, falling back to `PUBLIC_HOST`; `services/install_settings.py`). If nothing is
    configured it stays EMPTY, and the UI says so instead of showing a wrong address nobody can
    reach.
    """
    import json

    from .. import install_settings

    try:
        own = json.loads(server.extra or "{}").get("public_host")
    except ValueError:
        own = None
    return str(own or install_settings.public_host() or "")


@dataclass(frozen=True)
class ServerEvent:
    """A parsed game/server log event. Maps 1:1 onto models.PlayerEvent."""

    kind: str                 # "death" | "kill" | "level_up" | "join" | "leave" | "chat"
    username: str
    at: dt.datetime           # naive UTC (column convention, cf. log_parser._parse_ts)
    detail: str = ""          # chat body / target / free text
    raw: str = ""             # original log line (debugging/calibration)
    game_id: str = ""         # the game's own account id from the log line (PZ: SteamID64)
    x: int | None = None      # world coordinates where the log provides them (death map)
    y: int | None = None
    z: int | None = None


@dataclass(frozen=True)
class ConnectInfo:
    """How to join this server: public, no secrets.

    `uri` is set ONLY where the game really offers a protocol for it. An invented `minecraft://`
    link would be worse than none: it looks like a button and does nothing. So `hint` says what
    to do in the game instead, and the address to copy is always visible.
    """

    host: str
    port: int
    protocol: str = "udp"
    uri: str = ""                    # e.g. steam://connect/…; empty = no one-click join
    # `None` means **unknown**, not "no". An unreadable configuration file must not turn into
    # `password_required=False`: the join page would say "no password needed" for a server that
    # has one.
    password_required: bool | None = False
    hint: str = ""                   # what to do in the game when there is no URI
    browser_name: str = ""           # the name under which the server is listed in the browser


# The join password is the one secret that may be VISIBLE, and that is a distinction of
# substance, not a relaxation of the masking rule.
#
# An RCON or admin password is a control channel: whoever reads it has the full console of the
# game server as soon as they reach the internal network. The join password is known to
# everyone who plays anyway; it is in the invitation. Keeping it write-only protects nobody and
# costs the operator exactly the piece of information they need every day.
#
# Fields of this `kind` are delivered and written in clear text. An empty field really means
# "no password" here (not "unchanged" as with `password`), because the user sees the current
# value and must be able to clear it on purpose.
JOIN_PASSWORD = "join_password"


@dataclass(frozen=True)
class ConfigField:
    """An editable config entry for the generic config editor."""

    key: str
    label: str
    kind: str = "str"                 # str | int | float | bool | enum | text | password | join_password
    value: str = ""
    group: str = "General"
    choices: tuple[str, ...] = ()
    env_managed: bool = False         # True → read-only here (PZ: Public/RCONPassword/PublicName)
    help: str = ""
    # What THIS FIELD needs for a change to take effect; overrides `GameDriver.config_reload`.
    # For the case that a single field is more expensive than the rest: Project Zomboid re-reads
    # its `.ini` while running (`live`), but the join password sits in the container env and
    # needs a recreate. Empty = the driver default applies.
    reload: str = ""


# The help text of a join password that an environment variable sets (`join_password_field`).
JOIN_PASSWORD_FROM_ENV = (
    "Set by an environment variable (.env), which wins over the dashboard. Change it there."
)


def join_password_field(server: GameServer, *, key: str, group: str, help: str, reload: str = "",
                        label: str = "Join password") -> ConfigField:
    """The join password as a config field, from its encrypted slot (`services/secrets.py`).

    Visible (see `JOIN_PASSWORD`) — unless the variable named in `extra.password_env` is set:
    that wins over anything entered here, so the field is then system-managed and carries no
    value. An editable field whose change has no effect would be the trap.
    """
    import os

    from .. import secrets

    name = secrets.env_name(server, secrets.JOIN)
    if name and os.environ.get(name):
        return ConfigField(key=key, label=label, kind=JOIN_PASSWORD, value="", group=group,
                           env_managed=True, help=JOIN_PASSWORD_FROM_ENV, reload=reload)
    return ConfigField(key=key, label=label, kind=JOIN_PASSWORD, value=secrets.resolve(server, secrets.JOIN),
                       group=group, help=help, reload=reload)


class DriverError(ValueError):
    """A driver refuses, and the user gets to read why: `detail` is a coded text (`errors.err`).

    Still a `ValueError`, and `str()` is the English message: a caller that only knows
    `detail=str(e)` keeps working, one that passes `e.detail` on lets the frontend translate.
    Decisions hang on `code`, never on the wording.
    """

    def __init__(self, detail: dict[str, Any]) -> None:
        super().__init__(detail["message"])
        self.detail = detail

    @property
    def code(self) -> str:
        return str(self.detail["code"])


class DriverUnsupported(NotImplementedError):
    """The game cannot do this at all; the same coded `detail` as `DriverError`."""

    def __init__(self, detail: dict[str, Any]) -> None:
        super().__init__(detail["message"])
        self.detail = detail

    @property
    def code(self) -> str:
        return str(self.detail["code"])


class QuiesceWarning(RuntimeError):
    """Quiescing failed, but the archive is still usable.

    **`structural=True` means nothing failed at all.** Valheim has no save command from the
    outside; the hook can never succeed and only carries a hint text. Treating that as a fault
    (error log plus a `backup_quiesce_failed` incident on every backup) is noise that would
    eventually hide the one real quiesce failure.

    The difference from an ordinary error is why this class exists:

    * **Minecraft `save-off`/`save-all flush`** is load-bearing. Without it the server keeps
      writing during the tar run and the archive is torn. If it fails, no archive may be created:
      the hook raises a plain `RuntimeError` there, and because `create_backup` calls
      `pre_hook()` outside its `try`, the operation aborts.
    * **Vintage Story `genbackup`** is an extra: it asks the game to set a save point of its own.
      The actual archive is the tar over `Saves/`, which runs independently. Whether the
      "VintageRCon" mod knows the command at all is unverified. A hard abort would fail every
      backup as soon as the command is missing, which is worse than an archive without a save
      point.

    What does not happen is silence: the hint ends up in the log, in the result of
    `create_backup` and, if it reports a real failure, as an incident in the dashboard.
    """

    def __init__(self, *args, structural: bool = False) -> None:
        super().__init__(*args)
        #: `True` = the game cannot set this save point at all. The hint is then a property,
        #: not an incident.
        self.structural = structural


@dataclass(frozen=True)
class BackupSpec:
    """Drives `backup.create_backup`/`extract_backup` for one server.

    PZ: `data_dir=<server.data_dir>/data`, `world_subdirs=("Saves","Server","db")`.
    The pre/post hooks are where quiescing hooks in (e.g. MC save-off/save-all/save-on); PZ does
    not need them (None).
    """

    data_dir: str                                  # the directory that CONTAINS world_subdirs
    world_subdirs: tuple[str, ...]
    pre_hook: Callable[[], None] | None = None
    post_hook: Callable[[], None] | None = None
    # Which part is configuration ONLY, i.e. restorable on its own without touching the world.
    # Empty means "this game's archive contains no configuration", which is the case for
    # Minecraft, Vintage Story and Reforger: their `world_subdirs` are pure world directories,
    # and a config-only restore must not be offered for them.
    config_subdirs: tuple[str, ...] = ()
    # Does this game need a STOP for the archive to be worth anything?
    #
    # The quiesce hooks assume the server can be told from the outside to save. Valheim cannot:
    # it has neither a console nor stdin, and `save()` is a no-op. Without a stop the scheduled
    # backup would copy a world that is being WRITTEN.
    #
    # `True` means: the scheduled backup shuts the server down for the backup and starts it
    # again afterwards (`lifecycle.stop_and_snapshot`, the same path that applying mods, version
    # changes and restores use). That costs a few minutes of downtime per run.
    #
    # For all other games it stays `False`: where a save command exists the hook is the cheaper
    # way and a stop would be needless downtime.
    stop_for_consistency: bool = False


@dataclass(frozen=True)
class ModerationSpec:
    """Which moderation actions a game supports, **and why the others are missing**.

    Drives what the frontend shows and what the routers allow. Hiding a capability the game does
    not have at all is honest. Here, however, **subsets of an existing** capability are missing,
    and hiding those reads wrong: a missing "unban" for Vintage Story looks like "does not
    exist", while the truth is "unverified whether the RCON mod can do it".

    `reasons` brings the explanation to the UI: the key is the field name (`kick`, `ban`,
    `ban_ip`, `unban`, `access_levels`, `whitelist`), the value one plain sentence. An entry is
    also worthwhile for a supported action that has a condition ("target must be online").
    `asdict()` passes the field through to the frontend.
    """

    kick: bool = False
    ban: bool = False
    ban_ip: bool = False
    unban: bool = False
    access_levels: tuple[str, ...] = ()   # () → no access level control
    whitelist: bool = False
    # `field(default_factory=dict)` because `frozen=True` only forbids assignment, not sharing
    # one mutable class default between all instances.
    reasons: dict[str, str] = field(default_factory=dict)


# --- JSON configuration by dotted path ---------------------------------------------------------
#
# Shared by the drivers whose config is a JSON document with a curated field table (Reforger,
# Factorio `config/server-settings.json`).

def json_get(data: dict, dotted: str):
    """Read a nested value by dotted path (`game.gameProperties.battlEye`); None if missing."""
    cur = data
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def json_set(data: dict, dotted: str, value) -> None:
    """Set a nested value by dotted path; missing intermediate dicts are created."""
    parts = dotted.split(".")
    cur = data
    for part in parts[:-1]:
        nxt = cur.get(part)
        if not isinstance(nxt, dict):
            nxt = {}
            cur[part] = nxt
        cur = nxt
    cur[parts[-1]] = value


def coerce_value(kind: str, raw) -> object:
    """Convert a string from the config editor to the target type (bool/int/list/str)."""
    if kind == "bool":
        return str(raw).strip().lower() in ("true", "1", "yes", "on")
    if kind == "int":
        try:
            return int(str(raw).strip())
        except (TypeError, ValueError):
            return raw  # unparseable → set_config filters it out (no junk in numeric fields)
    if kind == "list":
        # The value arrives as a JSON array string from the chip editor; drop empty entries.
        # `json` is imported locally on purpose: this module stays lightweight (see the module
        # docstring); `_game_port_spec` and `public_host` do the same.
        import json

        try:
            v = json.loads(raw) if isinstance(raw, str) else raw
        except (TypeError, ValueError):
            return []
        return [str(x).strip() for x in v if str(x).strip()] if isinstance(v, list) else []
    return str(raw)


class ConfigReload(str, enum.Enum):
    """What it takes for a configuration change to take effect.

    * `live`: the server re-reads the file (PZ `reloadoptions`).
    * `restart`: the file is read at start (Minecraft, Vintage Story, Reforger, Factorio,
      Avorion).
    * `recreate`: the value sits in the container env, which can only be set when the container
      is CREATED (Valheim). A restart would not apply the change and would look as if it had.
    """

    live = "live"
    restart = "restart"
    recreate = "recreate"


class AutoUpdate(str, enum.Enum):
    """How far a server may update itself at night.

    The job in `services/auto_update.py` applies hotfixes WITHIN the running minor version, i.e.
    the range the vendor declares save-compatible. Without it a pinned server falls behind
    clients that Steam keeps updating, and nobody can join any more.

    * `patch`: hotfixes of the same minor version are applied at night (PZ, Factorio, Avorion,
      Minecraft, Vintage Story).
    * `none`: this server is never updated automatically, because its image fetches the
      game itself on every start anyway (Valheim, Reforger). This is the default.

    **The limit is the point:** a jump to a new minor version is manual work. The boot check
    catches "server does not start"; it does not catch "world gets migrated" or "mods broken",
    and both are the normal case there.
    """

    patch = "patch"
    none = "none"


class VersionSource(str, enum.Enum):
    """Where a game's version comes from; the question the version path really asks.

    The question is never "is this Project Zomboid" but "is the version in the image tag". It is
    answered on the driver, and callers ask `drivers.versioned_by_image_tag(server)` instead of
    comparing game types.

    * `image_tag`: the image tag IS the version (PZ, Factorio, Avorion). A change needs a new
      image, i.e. a pull before the swap; a rollback goes through `previous_image_tag`.
    * `env`: the image downloads the version at boot according to an env variable (Minecraft
      `VERSION`, Vintage Story). No pull needed, rollback through `previous_game_version`. This
      only holds if the image actually READS the env: on an image that bakes the game in, every
      change is a silent no-op (see `drivers/vintagestory.py`).
    * `runtime`: the image fetches the game from Steam on every start; nothing can be chosen
      (Arma Reforger, Valheim). Only the running server knows what runs, so the stats poller
      writes `detect_version()` back into `game_version`, and the version catalog greys the list
      out with a reason instead of offering an empty one.
    """

    image_tag = "image_tag"
    env = "env"
    runtime = "runtime"


def set_save_aside(source_folder: str, target_folder: str, files: tuple[str, ...]) -> dict:
    """Move a save's files out of the way: **moved, never deleted**.

    Same policy as `mods/apply.py` and `mods/foreign.remove_file`: a world is weeks of work,
    and a slip in the dashboard must not cost it for good. The timestamp in the target name
    keeps several removals of the same name apart.
    """
    import datetime as _dt
    import os as _os
    import shutil as _shutil

    stamp = _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%d-%H%M%S")
    moved: list[str] = []
    try:
        _os.makedirs(target_folder, exist_ok=True)
        for file in files:
            source = _os.path.join(source_folder, file)
            if not _os.path.exists(source):
                continue
            _shutil.move(source, _os.path.join(target_folder, f"{stamp}-{file}"))
            moved.append(file)
    except OSError as e:
        raise DriverError(err(
            "world.set_aside_failed", "The save could not be moved aside: {reason}", reason=str(e),
        )) from e
    if not moved:
        raise DriverError(err(
            "world.set_aside_nothing_found", "Not a single file of this save was there — nothing was touched.",
        ))
    return {"set_aside": target_folder, "files": moved, "stamp": stamp}


class GameDriver(ABC):
    """Game-agnostic driver interface. Implementations are stateless."""

    game_type: ClassVar[GameType]

    # Where the version comes from (see `VersionSource`). Default `env`, the case that works
    # without extra effort: no pull, no image rollback, no special path in the catalog.
    version_source: ClassVar[VersionSource] = VersionSource.env

    # What makes a config change take effect (see `ConfigReload`). Default `restart`: true for
    # every game that reads its configuration from a file.
    config_reload: ClassVar[ConfigReload] = ConfigReload.restart

    # Is `players()` good enough as proof of boot, even without Source RCON?
    #
    # `version_manager.verify_boot` uses `console_kind == source_rcon` as its condition. Where
    # there is no RCON the server counts as booted once the container has been running stably
    # for three rounds, which is weak proof: it shows "Docker did not restart it", not "the game
    # is up". For Valheim and Reforger the gap is large, because the image runs SteamCMD first
    # while the container has long been running; both set it to `True`.
    #
    # Default `False`.
    boot_proof_via_players: ClassVar[bool] = False

    # The line the game itself writes to its log once it accepts players, or `None`.
    #
    # `verify_boot` accepts it where the stronger proof (an answer from `players()`) is not to be
    # had, e.g. Vintage Story without the optional third-party RCON mod. Without it every restore,
    # version change and recreate of such a server waited its full 900 s and then rolled back a
    # healthy boot. The line counts only when it was written since the container's CURRENT start
    # and the container has been running stably for several rounds; an answer from `players()`
    # still wins as soon as there is one.
    boot_ready_line: ClassVar[re.Pattern[str] | None] = None

    def players_usable_as_boot_proof(self, server: GameServer) -> bool:
        """Is `players()` good enough for THIS server as proof that it is up?

        The ClassVar answers for the game, this method for the individual server. The difference
        matters for Valheim: there `players()` depends on the Steam query, and
        `SERVER_PUBLIC=false` (a freely editable config field) switches exactly that off. With it
        off, `verify_boot` would wait its full 900 s in vain and report `False`; `foreign_apply`
        would read that as "server did not come up", roll the mod selection back and wait another
        900 s, while the server was running the whole time.

        Returning `False` makes `verify_boot` fall back to the stability branch: several rounds
        running without a new restart. That is weaker proof, but attainable, and weak proof
        beats impossible proof.
        """
        return self.boot_proof_via_players

    def players_is_queryable(self, server: GameServer) -> bool:
        """Can `players()` answer at all for THIS server?

        **Not to be confused with `players_usable_as_boot_proof` above.** Mixing the two up
        switches the watchdog off for a server:

        * *Proof of boot* asks "is `players()` enough to PROVE the server is up?" For Project
          Zomboid the answer is `False`, not because the query would not work but because
          `verify_boot` uses the stronger RCON handshake there.
        * *Queryable* asks "do I get an answer at all?" That is the watchdog's question, and for
          Project Zomboid the answer is `True`.

        The two questions coincide only for Valheim, the only driver that overrides the default:
        with `SERVER_PUBLIC=false` the server no longer answers Steam queries, `players()` raises
        permanently, and the watchdog would consider the server sick and restart it every
        20 minutes.
        """
        return True

    # How far this server updates itself at night (see `AutoUpdate`). Default `none`: a
    # game server does not update itself unasked; a driver that wants it says so here.
    auto_update_limit: ClassVar[AutoUpdate] = AutoUpdate.none

    # Does the image refuse to start without a pinned version? Then the registry must not hold a
    # row with an empty `game_version`; otherwise the next recreate fails in a restart loop that
    # the rollback cannot escape either (it uses the same env). Such rows are therefore rejected
    # on create and update (`routers/servers.py`).
    version_required: ClassVar[bool] = False

    def version_valid(self, version: str) -> bool:
        """Does the image accept this version string? Only asked for drivers with
        `version_required`; the others do not pass the value on as a start condition anyway."""
        return True

    # Detected game version per `(server_key, started_at)`, see `_version_from_log`.
    # Process-local (single-worker invariant) and separate per driver class, because
    # `get_driver()` caches a singleton. Value: `(version, detected_at)`.
    _version_cache: ClassVar[dict[tuple[str, str], tuple[str, dt.datetime]]] = {}

    # Does the game update itself INSIDE the running container? Default no: everywhere else the
    # version only changes with the container, and `started_at` invalidates the cache by itself.
    #
    # Valheim is the exception: the image's `valheim-updater` runs in the main loop and pulls new
    # game builds through supervisor without touching the container. `started_at` stays the
    # same and the cache hit would stand; and because Valheim is `version_source = runtime`,
    # `detect_version` is the ONLY source, so the stale number would be shown as "running".
    #
    # If a period is set here, a cache entry additionally expires after that time.
    version_cache_ttl: ClassVar[dt.timedelta | None] = None

    @abstractmethod
    def capabilities(self) -> frozenset[str]:
        """Feature switches for nav/UI gating, e.g. {"console","mods","versions"}."""

    # --- Console / RCON ---
    @abstractmethod
    async def console(self, server: GameServer, command: str, *, timeout: float = 8.0) -> str:
        """Run a console/RCON command and return the (possibly multi-part) reply."""

    @abstractmethod
    async def players(self, server: GameServer) -> dict:
        """Online players: {"count": int, "names": list[str]}."""

    # --- Log/event parsing ---
    @abstractmethod
    def parse_events(self, server: GameServer, log_text: str) -> list[ServerEvent]:
        """Translate container log text into ServerEvents (game-specific patterns)."""

    # --- Joining: public connection details per game ---
    def connect_info(self, server: GameServer) -> ConnectInfo:
        """Address, port and the game's proper way in.

        The common part (address + published game port) comes from the registry; the drivers add
        the protocol URI and the hint text.
        """
        return ConnectInfo(host=public_host(server), port=game_port(server), protocol=game_proto(server))

    #: Where player events come from; `False` means stdout (the Docker log).
    #:
    #: This must be stated, not GUESSED from an empty list. `event_log_paths()` returning `[]`
    #: means "this game writes to stdout" for Minecraft, but for Project Zomboid the same empty
    #: list means "I did not find the files" (`PZDriver._newest_log` returns `None` both for an
    #: empty glob and on `OSError`). A permission problem, a missing mount or a renamed directory
    #: would then look like Minecraft, and the parser would read the Docker log, where PZ writes
    #: no events at all: no hits, no log line, no incident, and empty leaderboards.
    events_from_files: ClassVar[bool] = False

    def event_log_paths(self, server: GameServer) -> list[str]:
        """Files player events are read from, INSTEAD of the Docker log.

        Empty list = the events are on stdout (Minecraft). Project Zomboid overrides this: joins,
        leaves, deaths and skill levels are **only** in separate files under `data/Logs/`, never
        on stdout.
        """
        return []

    def event_log_obstacle(self, server: GameServer) -> str | None:
        """Why there is no event log file right now, or `None` when that is normal.

        `event_log_paths()` returns an empty list in two entirely different situations: "nobody
        played today, so the game created no file" and "I cannot get at the directory". The
        parser cannot tell them apart on its own, and reporting both as a configuration error
        floods the log with warnings until every real message is rotated out.

        The default is `None`: a game whose events are read from stdout does not have this
        question. Drivers that read files override it.
        """
        return None

    # --- Chat bridge: pull real PLAYER chat out of the log ---
    def chat_log_path(self, server: GameServer) -> str | None:
        """Optional file path the chat is read from, INSTEAD of the Docker log.

        Default `None` = the chat is in the Docker log (MC: stdout). PZ overrides this: Build 42
        does NOT write chat to stdout, only to the `DebugLog-server.txt` file (readable through
        the bind mount). The ingest job then reads that file instead of `docker.logs`."""
        return None

    def parse_chat(self, server: GameServer, log_text: str) -> list[tuple[str, str, "dt.datetime"]]:
        """Read real player chat from the (timestamp-prefixed) container log.

        Returns `(player, message, at)` tuples (at = naive UTC). DELIBERATELY separate from
        `parse_events`: chat is NOT persisted as a `PlayerEvent`, it is only fed into the
        cross-server bus. Default `[]` (no chat bridging); games opt in by overriding. Match ONLY
        real player chat (no system/server lines), so that the bridge's own injection (sent as a
        server message) is never re-ingested as chat."""
        return []

    async def announce(self, server: GameServer, text: str) -> None:
        """Send a message visible on the server (scheduled announcement / cross-server sink).

        Default = PZ `servermsg "…"`; MC/VS override with `say …` and `/announce …`. The text is
        made safe against RCON injection. Deliberately the sink of the chat bridge: a message
        sent this way is a SERVER message, not player chat, so the ingest parser (`parse_chat`)
        does not recognise it as player chat (loop protection)."""
        await self.console(server, f'servermsg "{sanitize_message(text)}"')

    async def save(self, server: GameServer) -> None:
        """Flush the world before a scheduled restart. Default = PZ `save`; MC/VS override
        (MC `save-all flush`, VS no-op: the game's own command instead of a hard-coded PZ `save`)."""
        await self.console(server, "save")

    async def graceful_shutdown(self, server: GameServer) -> bool:
        """Have the game shut down through its OWN console instead of hoping for signals.

        Background: the PZ server image starts the JVM via `su … ./start-server.sh` under a shell
        WITHOUT `trap`/`exec`. PID 1 is then a bash with no SIGTERM handler (`/proc/1/status`
        shows `SigCgt=0x10002`, i.e. SIGINT+SIGCHLD only). The kernel drops signals without a
        handler for PID 1, so the SIGTERM from `docker stop` **never** arrives; Docker kills the
        container after the timeout and the world falls back to the last autosave on EVERY stop.

        Returns `True` = a proper in-game shutdown was initiated. Errors (RCON dead, unknown
        command) are NEVER propagated: the stop has to happen regardless, and
        `lifecycle.stop_server` always issues a `docker stop` in parallel.
        Default `False` = this game shuts down cleanly on a signal (Reforger, and Minecraft,
        whose image turns SIGTERM into a console `stop`).
        """
        return False

    # --- Container specification ---
    @abstractmethod
    def container_env(self, server: GameServer, secret: str) -> dict[str, str]:
        """Game-specific env variables for the container create (KEY→VALUE)."""

    def container_user(self, server: GameServer) -> str:
        """The user the container should run as (`"uid:gid"`), or empty for the image default.

        The dashboard API runs as `APP_UID` (default 1000) and writes into the game servers'
        data directories: configuration, mod selection, backups. If the game server runs under a
        DIFFERENT uid, it owns all files after the first start and the config editor fails with
        "Permission denied" from then on. Minecraft and Vintage Story images run as 1000 by
        default; the Factorio image uses 845. Drivers that set the uid take the API process's
        own `os.getuid()`/`os.getgid()`, never a fixed 1000.

        Fixing permissions on the host does not last: the image resets the owners at start. The
        container therefore has to run under the same uid as the API.
        """
        return ""

    @abstractmethod
    def container_binds(self, server: GameServer) -> list[str]:
        """Docker bind mounts ("host:container") for this server."""

    # --- Backup ---
    @abstractmethod
    def backup_spec(self, server: GameServer) -> BackupSpec:
        """World/config backup specification (data_dir + world_subdirs + optional hooks)."""

    # --- Config editor ---
    @abstractmethod
    def config_fields(self, server: GameServer) -> list[ConfigField]:
        """Editable configuration fields for the generic config page."""

    def elevated_config_keys(self, server: GameServer) -> frozenset[str]:
        """Config keys that need a HIGHER permission than editing the configuration.

        Default empty. The case this exists for: Arma Reforger has `game.admins` and
        `game.passwordAdmin` as ordinary fields. Whoever may edit the configuration could add
        themselves as a game master (Game Master menu: spawn, control) or set the `#login`
        password. That is moderation power handed out through a config permission.

        Deliberately on the driver rather than a list in the router: only the game itself knows
        which keys carry power.
        """
        return frozenset()

    @abstractmethod
    def set_config(self, server: GameServer, updates: dict[str, str]) -> None:
        """Write configuration values (env-managed keys are ignored)."""

    # --- Mods/plugins ---
    @abstractmethod
    def list_mods(self, server: GameServer) -> dict:
        """Installed mods/plugins (game-specific structure)."""

    # --- Moderation: kick/ban/whitelist/access level per game ---
    def moderation_spec(self, server: GameServer) -> ModerationSpec:
        """Which moderation actions this game supports (default: none)."""
        return ModerationSpec()

    async def kick(self, server: GameServer, username: str, reason: str = "") -> str:
        raise NotImplementedError

    async def ban(self, server: GameServer, username: str, reason: str = "", ip: bool = False) -> str:
        raise NotImplementedError

    async def unban(self, server: GameServer, username: str) -> str:
        raise NotImplementedError

    async def set_access(self, server: GameServer, username: str, level: str) -> str:
        raise NotImplementedError

    async def whitelist_add(self, server: GameServer, username: str) -> str:
        raise NotImplementedError

    async def whitelist_remove(self, server: GameServer, username: str) -> str:
        raise NotImplementedError

    def whitelist_list(self, server: GameServer) -> dict:
        """Registered players (game-specific); default: not available."""
        return {"available": False, "users": []}

    # --- Per-game details for the console and the overview ---
    def safe_console_commands(self) -> frozenset[str]:
        """Command verbs allowed in safe mode (without can_console_full); game-specific."""
        return frozenset()

    # --- Registry env: for games whose configuration lives in the container env ---------------

    @staticmethod
    def registry_env(server: GameServer) -> dict[str, str]:
        """`extra.env` of the registry row: the stored env, not necessarily the RUNNING one.

        What is stored here is the intent. What the container really has set comes from
        `docker.state()["env"]`. Confusing the two hands out values (e.g. a join password) the
        server has never seen.
        """
        import json as _json

        try:
            return {str(k): str(v) for k, v in (_json.loads(server.extra or "{}").get("env") or {}).items()}
        except ValueError:
            return {}

    @staticmethod
    def write_registry_env(server: GameServer, env: dict[str, str]) -> None:
        """Write `extra.env` back; the only place that does so."""
        import json as _json

        from sqlmodel import Session, select

        from ...db import engine
        from ...models.server import GameServer as Row

        with Session(engine) as s:
            row = s.exec(select(Row).where(Row.key == server.key)).first()
            if row is None:
                raise DriverError(err(
                    "config.registry_row_gone", "Server '{key}' is no longer in the registry", key=server.key,
                ))
            try:
                extra = _json.loads(row.extra or "{}")
            except ValueError:
                extra = {}
            extra["env"] = env
            row.extra = _json.dumps(extra)
            s.add(row)
            s.commit()
        # The row passed in is a copy from another session; updating it as well keeps the same
        # call from returning the old state afterwards.
        try:
            before = _json.loads(server.extra or "{}")
        except ValueError:
            before = {}
        server.extra = _json.dumps({**before, "env": env})

    # --- Worlds / saves -----------------------------------------------------------------------
    #
    # Factorio keeps several `saves/*.zip` side by side, Valheim several
    # `worlds_local/<Name>.db`. These methods make them selectable from the dashboard instead of
    # editing the container env by hand.
    #
    # Deliberately NEXT TO Reforger's `save_slots` rather than replacing it: those slots answer
    # a different question ("which game mode has a save?"), and reinterpreting them would squeeze
    # two meanings into one name.

    def world_saves(self, server: GameServer) -> list[dict]:
        """Which worlds/saves are on disk for this server?

        Per entry: `name`, `active`, `size`, `modified` (ISO) and `files`. Empty means "this
        game has no switchable saves", not "none is there". A driver that needs the distinction
        raises (see the driver implementations).
        """
        return []

    def select_world(self, server: GameServer, name: str) -> str:
        """Make this save the active one. Returns what the operator has to do next.

        For Factorio and Valheim the active world depends on a container variable, so the change
        only takes effect with a **recreate**, not with a restart. The returned text says so
        explicitly.
        """
        raise DriverUnsupported(err(
            "world.switch_unsupported", "This game cannot switch worlds through the dashboard.",
        ))

    def remove_world(self, server: GameServer, name: str) -> dict:
        """Move a save out of the way: **moved, never deleted**."""
        raise DriverUnsupported(err(
            "world.remove_unsupported", "This game cannot remove worlds through the dashboard.",
        ))

    def overview_stats(self, server: GameServer) -> dict:
        """Extra figures for the overview (max_players, mods_count); game-specific."""
        return {"max_players": None, "mods_count": None}

    async def detect_version(self, server: GameServer) -> str:
        """Which game version is actually running? Empty if it cannot be determined.

        Needed where the registry does not know the version (e.g. a server running on the image's
        default without a pinned `VERSION`). Without this detection no catalog entry would be
        marked as "running" and `update_available` would always be empty: the version check
        would effectively be off without anyone noticing.
        """
        return ""

    async def _version_from_log(self, server: GameServer, pattern: re.Pattern[str]) -> str:
        """Read the version from the container log, cached until the next start.

        The cache key is `(key, started_at)`. `get_driver()` caches a singleton per game type, so
        a cache keyed by `server.key` alone would live for the whole process and keep reporting
        the old version after a version change. `started_at` comes from the Docker inspect and
        changes on **every** container start, so the cache invalidates itself. The inspect costs
        one extra call but is orders of magnitude cheaper than the full log it saves
        (`tail=1_000_000`).
        """
        from ..docker_client import docker

        try:
            state = await docker.state(server.container_name)
        except Exception:
            state = {}
        key = (server.key, str(state.get("started_at") or ""))
        now = utcnow_naive()
        cached = self._version_cache.get(key)
        if cached:
            version, detected_at = cached
            # `version_cache_ttl` is set when the game can update itself INSIDE the running
            # container (Valheim). `started_at` alone does not invalidate the cache there.
            if self.version_cache_ttl is None or now - detected_at < self.version_cache_ttl:
                return version
        try:
            text = await docker.logs(server.container_name, tail=1_000_000)
        except Exception:
            return ""
        found = pattern.findall(text)
        version = found[-1] if found else ""
        if version:
            # Cache hits only: an empty result means "not in the log yet", not "does not
            # exist"; the next call should search again.
            self._version_cache[key] = (version, now)
        return version

    def stop_timeout(self, server: GameServer) -> int:
        """Seconds the game server gets to shut down cleanly (SIGTERM → SIGKILL).

        Without a value Docker allows only **10 seconds**, too little for a game server that
        writes its world on stop.

        Measured from stop signal to process exit: Minecraft about 0.3 s, Vintage Story about
        0.6 s, Arma Reforger under 1 s, Project Zomboid about 15 s (a full save block, see
        `lifecycle.py`). The default of 60 s is generous by orders of magnitude, on purpose: the
        measurements were taken on **empty** worlds, Minecraft saves all loaded chunks on stop,
        and for the drivers with **no** `graceful_shutdown` of their own (Minecraft, Reforger)
        everything depends on this grace period.

        This does NOT help existing containers: `StopTimeout` can only be set at creation. For
        stops through the dashboard that does not matter, because `lifecycle` passes the period
        as `?t=`; a manual `docker stop` still gets only the 10 seconds until the container is
        recreated.
        """
        return 60
