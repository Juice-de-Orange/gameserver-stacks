"""PZDriver: wraps the existing Project Zomboid services.

Reuse instead of new code: RCON through `rcon.rcon_command`, events through patterns calibrated
on real log lines, config/mods through `ini_editor`. `ini_editor`, `rcon`, `servers` and
`mods.state` are imported INSIDE the methods; that is no longer required (see the docstring of
`drivers/__init__.py`), the form simply stays.
"""
from __future__ import annotations

import json
import os
import re
from typing import TYPE_CHECKING

from ...errors import err
from ...models.server import GameType
from ..player_parse import parse_players as _parse_players
from .base import (
    AutoUpdate, BackupSpec, ConfigField, ConfigReload, DriverError, GameDriver,
    ModerationSpec, ServerEvent, VersionSource, join_password_field, looks_like_secret, sanitize_message,
)

if TYPE_CHECKING:
    from ...models.server import GameServer

# Chat bridge: PZ (Build 42) does NOT write player chat to stdout, only to the
# `DebugLog-server.txt` file. Format (verified on real logs):
#   [DD-MM-YY HH:MM:SS.mmm][info] Got message:ChatMessage{chat=Local, author='Name', text='Message'}.
# The "Got message:" line is matched (not the "sent to chat" line, so nothing is ingested twice).
_PZ_CHAT_RE = re.compile(
    r"Got message:ChatMessage\{chat=(?P<ch>\w+),\s*author='(?P<p>[^']*)',\s*text='(?P<m>.*)'\}"
)
# PZ's own timestamp [DD-MM-YY HH:MM:SS.mmm] (NOT Docker RFC3339, because the file is read).
_PZ_TS_RE = re.compile(r"^\[(\d{2})-(\d{2})-(\d{2})\s+(\d{2}):(\d{2}):(\d{2})\.(\d{3})\]")
# Bridge PUBLIC channels only (Admin/PM/Whisper/Server/Faction/Safehouse deliberately NOT).
_PZ_BRIDGE_CHANNELS = {"General", "Say", "Local", "Shout"}

# --- Player events (calibrated on real lines, fixture tests/fixtures/pz_user_log.txt) ---
# `user.txt`:
#   [TS] 76561198… "Name" fully connected (6715,5331,0).
#   [TS] 76561198… "Name" disconnected player (6714,5323,1).
#   [TS] user Some Player died at (3594,10923,0) (non pvp).
# Careful with deaths: the name is NOT quoted and may contain spaces, hence `.+?` up to the
# literal " died at" instead of a \S+ pattern.
_PZ_CONNECT_RE = re.compile(
    r"(?P<sid>\d{17})\s+\"(?P<u>[^\"]*)\"\s+(?P<what>fully connected|disconnected player)\s+"
    r"\((?P<x>-?\d+),(?P<y>-?\d+),(?P<z>-?\d+)\)"
)
_PZ_DEATH_RE = re.compile(
    r"\buser\s+(?P<u>.+?)\s+died at\s+\((?P<x>-?\d+),(?P<y>-?\d+),(?P<z>-?\d+)\)\s+\((?P<pvp>[^)]*)\)"
)
# `PerkLog.txt`: [TS] [76561198…][Name][x,y,z][Login|Died|Cooking=0, Fitness=5, …][Hours Survived: N].
_PZ_PERK_RE = re.compile(
    r"\[(?P<sid>\d{17})\]\[(?P<u>[^\]]*)\]\[(?P<x>-?\d+),(?P<y>-?\d+),(?P<z>-?\d+)\]\[(?P<body>[^\]]*)\]"
)
_PZ_SKILL_RE = re.compile(r"(?P<name>[A-Za-z]+)=(?P<lvl>\d+)")


def _parse_skills(body: str) -> dict[str, int]:
    return {m.group("name"): int(m.group("lvl")) for m in _PZ_SKILL_RE.finditer(body)}


def _level_ups_from(snapshots) -> list[ServerEvent]:
    """Derive level-ups from consecutive skill snapshots.

    PZ writes **no** level-up line of its own. `PerkLog.txt` only contains snapshots of all
    skills, and not on every level-up either (a skill can go from 1 to 3 between two snapshots).
    What can honestly be said is "between these two points in time these levels were gained";
    the event time is that of the later snapshot.

    Comparison happens only WITHIN the section that was read; a player's first snapshot has no
    predecessor and deliberately produces nothing (otherwise every login would be a "level-up
    from 0").

    **If any value drops, the whole comparison is discarded.** That is not progress but a new
    character after a death, which simply has different values, some higher, some lower. Without
    this rule a freshly rolled character would be reported as e.g. "Carving 0→3".
    """
    out: list[ServerEvent] = []
    last: dict[str, dict[str, int]] = {}
    for sid, name, at, skills in sorted(snapshots, key=lambda s: s[2]):
        key = sid or name
        before = last.get(key)
        last[key] = skills
        if before is None:
            continue
        if any(lvl < before.get(skill, 0) for skill, lvl in skills.items()):
            continue  # new character, not progress
        increased = [
            f"{skill} {before.get(skill, 0)}→{lvl}"
            for skill, lvl in skills.items()
            if lvl > before.get(skill, 0)
        ]
        if increased:
            out.append(ServerEvent(kind="level_up", username=name, at=at, game_id=sid,
                                   detail=", ".join(sorted(increased))))
    return out


def branch_for_tag(image_tag: str) -> str:
    """The Steam branch that belongs to the image tag.

    The server image's tags are named `<version>-release` (built from the Steam branch `public`)
    or `<version>-unstable`. The game files are baked in at image build time; `STEAMAPPBRANCH`
    only has an effect at runtime together with `FORCEUPDATE` (which is deliberately NOT set), so
    keeping the env consistent with the tag is explicitness, not a behaviour change. Without a
    recognisable tag it stays `public` (the image's own default). A full image reference
    ("repo:tag") is fine too; only the end is checked.
    """
    return "unstable" if (image_tag or "").strip().endswith("-unstable") else "public"


def _parse_pz_ts(line: str):
    """[DD-MM-YY HH:MM:SS.mmm] → naive datetime (server local time; the ingest watermark only
    needs monotonicity per server). None if there is no valid timestamp."""
    import datetime as dt

    m = _PZ_TS_RE.match(line)
    if not m:
        return None
    d, mo, y, h, mi, s, ms = (int(x) for x in m.groups())
    try:
        return dt.datetime(2000 + y, mo, d, h, mi, s, ms * 1000)
    except ValueError:
        return None


# What the image writes from the container env into the `.ini` on every boot. Editing these keys
# in the file has no effect (and still looks like success); they come from the registry.
#
# `RCONPassword` is deliberately missing here: it is the dashboard's control channel and stays
# read-only.
#
# `PublicName` is missing as well. It depends on the same registry value as `SERVERNAME`
# (`extra.servername`), which for Project Zomboid determines not only the name in the browser but
# also the FILE NAMES (`<name>.ini`, `<name>_SandboxVars.lua`, `<name>_spawnregions.lua`) and the
# WORLD DIRECTORY (`Saves/Multiplayer/<name>`). A "name in the server browser" field that
# switches the world on the next recreate would look like a cosmetic change and be the most
# expensive kind of operating error.
#
# A separate `DISPLAYNAME` would be the right way, but it is unverified whether the image fills
# `PublicName` from it; that can only be found out by a recreate. Until that has been measured
# the key stays read-only.
_FROM_REGISTRY = frozenset({"Password", "Public"})


class PZDriver(GameDriver):
    game_type = GameType.pz
    # For PZ the image tag IS the version (`…:42.20.0-release`); `branch_for_tag` derives the
    # Steam branch from it.
    version_source = VersionSource.image_tag
    # A pinned server falls behind the clients as soon as a hotfix is released, and PZ then
    # lets nobody in.
    auto_update_limit = AutoUpdate.patch
    # PZ re-reads the .ini at runtime via `reloadoptions`.
    config_reload = ConfigReload.live

    def capabilities(self) -> frozenset[str]:
        return frozenset(
            {
                "console",
                "players",
                "events",
                "config",
                "sandbox",
                "mods",
                # "Restore the configuration only": for the other games the archive contains no
                # configuration at all, so the button is hidden there instead of ending in an error.
                "backup-config-only",
                "backup",
                "versions",
                "announce",  # servermsg → announcements/broadcasts (Reforger cannot do this)
            }
        )

    # --- Console / RCON ---
    async def console(self, server: GameServer, command: str, *, timeout: float = 8.0) -> str:
        from ..rcon import rcon_command
        from ..servers import resolve_secret

        secret = resolve_secret(server)
        return await rcon_command(server.rcon_host, server.rcon_port, secret, command, timeout=timeout)

    async def players(self, server: GameServer) -> dict:
        return _parse_players(await self.console(server, "players"))

    async def graceful_shutdown(self, server: GameServer) -> bool:
        """PZ only shuts down cleanly through its own console: `save`, then `quit`.

        According to the game files `quit` itself is "Save and quit the server"
        (`media/lua/shared/Translate/EN/UI.json` → `UI_ServerOptionDesc_Quit`). The preceding
        `save` is deliberate redundancy: it writes the world before anything else happens.

        Measured: `save` → "World saved", `quit` → "Quit", then a complete save block in the log
        and **exit code 0 after about 22 s** instead of SIGKILL after 120 s.

        Generous timeouts, because `save` may take a while on a large world; a timeout is not an
        error here, just a reason to carry on regardless.
        """
        try:
            await self.console(server, "save", timeout=60.0)
        except Exception:
            # DELIBERATELY broad: whatever goes wrong while saving, `quit` must still be sent
            # (it saves by itself). An `except (RconError, OSError)` would let e.g. an error
            # from `resolve_secret` through and skip the decisive command.
            pass
        try:
            await self.console(server, "quit", timeout=30.0)
        except Exception:
            # Not by itself a failure: `quit` ends the server, and the connection can close
            # before its reply arrives. Reported as a failure, `lifecycle` logged "could not be
            # shut down through the console" on normal stops (the same rule as Factorio's
            # `/quit`). The container going down by itself decides; `False` only when it is
            # still running, i.e. RCON is really dead and only the signal path is left.
            return await self._goes_down(server)
        return True

    async def _goes_down(self, server: GameServer, *, within: int = 45) -> bool:
        """Has the container stopped running, or does it within about `within` seconds?

        45 s, because PZ writes its world after `quit` and exits about 22 s later (measured).
        An unreadable state counts as still running: the claim "shut down cleanly" needs proof.
        Mirrors `FactorioDriver._goes_down`.
        """
        import asyncio

        from ..docker_client import docker

        for attempt in range(within + 1):
            if attempt:
                await asyncio.sleep(1.0)
            try:
                if not (await docker.state(server.container_name)).get("running"):
                    return True
            except Exception:
                pass
        return False

    def overview_stats(self, server: GameServer) -> dict:
        """Figures for the overview, read for THIS server (`ini_editor.ini_path(server)`), so a
        second PZ server shows its own player limit and mod count."""
        from .. import ini_editor as ini
        from ..mods import state as mod_state

        out: dict = {"max_players": None, "mods_count": None}
        try:
            out["max_players"] = int(ini.get_ini_at(ini.ini_path(server)).get("MaxPlayers", "0") or 0) or None
        except Exception:
            pass
        try:
            out["mods_count"] = len(mod_state.live_plan(server).workshop_items)
        except Exception:
            pass
        return out

    def connect_info(self, server: GameServer):
        """PZ offers a real one-click join (`steam://connect/…`)."""
        from .base import ConnectInfo, game_port, game_proto, public_host

        extra = json.loads(server.extra or "{}")
        host, port = public_host(server), game_port(server)
        return ConnectInfo(
            host=host, port=port, protocol=game_proto(server),
            uri=f"steam://connect/{host}:{port}" if host and port else "",
            password_required=bool(self._join_password(server)),
            hint="In the game: “Join server” → “Favorites” → enter the address.",
            browser_name=extra.get("servername") or server.display_name or server.key,
        )

    # --- Log/event parsing: from the files under data/Logs, NOT from the Docker log ---
    # PZ writes player events ONLY to files under `data/Logs/`, never to stdout. An empty list
    # therefore means "not found" here, not "it is in the Docker log".
    events_from_files = True

    def event_log_paths(self, server: GameServer) -> list[str]:
        """The two files that actually contain player events.

        `<date>_user.txt` has joins, leaves and deaths, with SteamID64 and world coordinates.
        `<date>_PerkLog.txt` has skill levels, from whose differences level-ups can be derived.
        **None** of this is on stdout.

        Both files only come into existence when somebody actually plays, and at the daily
        rollover (07:00) they are moved to `logs_<date>/`; hence the newest one in the root
        directory is taken.
        """
        return [p for p in (self._newest_log(server, "*_user.txt"),
                            self._newest_log(server, "*_PerkLog.txt")) if p]

    def event_log_obstacle(self, server: GameServer) -> str | None:
        """Is there a REASON why nothing can be found here, or did simply nobody play?

        `<date>_user.txt` is only created when somebody joins and moves to `logs_<date>/` at the
        daily rollover (07:00). On a running but empty server it regularly does not exist, and
        that normal case must not be reported as a fault on every parser round.

        Only what can be fixed is reported: a missing directory (mount, wrong `data_dir`) or
        missing permissions. `X_OK` is checked in addition to `R_OK`: without search permission
        a `glob` in the directory returns nothing although it looks readable.
        """
        directory = os.path.join(server.data_dir, "data", "Logs")
        if not os.path.isdir(directory):
            return f"{directory} does not exist — check the mount or data_dir."
        if not os.access(directory, os.R_OK | os.X_OK):
            return f"{directory} cannot be read or searched — check the permissions."
        return None

    @staticmethod
    def _newest_log(server: GameServer, pattern: str) -> str | None:
        import glob

        try:
            files = glob.glob(os.path.join(server.data_dir, "data", "Logs", pattern))
            return max(files, key=os.path.getmtime) if files else None
        except OSError:
            return None

    def parse_events(self, server: GameServer, log_text: str) -> list[ServerEvent]:
        """Events from `user.txt` + `PerkLog.txt`.

        The patterns are calibrated on real lines (fixture `tests/fixtures/pz_user_log.txt`), not
        guessed. Two traps:

        * **Player names contain spaces** (`user Some Player died at …`); in the death line the
          name is unquoted. A `\\S+` pattern would capture "Some" and lose the rest.
        * The timestamps are **server local time**; the container runs without `TZ`, i.e. UTC,
          which matches the column convention. If a time zone is ever set on the container, all
          events shift and a conversion belongs here.

        Deliberately NOT produced: zombie kills. They are in no log file of the server
        (`pvp.txt` logs hits, not kills). Better no number than an invented one.
        """
        out: list[ServerEvent] = []
        snapshots: list = []  # (steamid, name, at, {skill: level})

        for line in log_text.splitlines():
            line = line.strip()
            if not line:
                continue
            at = _parse_pz_ts(line)
            if at is None:
                continue  # no reliable timestamp, no event (no now() fallback)

            m = _PZ_DEATH_RE.search(line)
            if m:
                out.append(ServerEvent(
                    kind="death", username=m.group("u").strip(), at=at, raw=line,
                    detail="PvP" if m.group("pvp").strip() == "pvp" else "no PvP",
                    x=int(m.group("x")), y=int(m.group("y")), z=int(m.group("z")),
                ))
                continue

            m = _PZ_CONNECT_RE.search(line)
            if m:
                out.append(ServerEvent(
                    kind="join" if m.group("what") == "fully connected" else "leave",
                    username=m.group("u"), at=at, raw=line, game_id=m.group("sid"),
                    x=int(m.group("x")), y=int(m.group("y")), z=int(m.group("z")),
                ))
                continue

            m = _PZ_PERK_RE.search(line)
            if m and "=" in m.group("body"):
                skills = _parse_skills(m.group("body"))
                if skills:
                    snapshots.append((m.group("sid"), m.group("u"), at, skills))

        out.extend(_level_ups_from(snapshots))
        return out

    # --- Chat bridge: PZ reads from the DebugLog file (not the Docker log) ---
    def chat_log_path(self, server: GameServer) -> str | None:
        import glob

        logs_dir = os.path.join(server.data_dir, "data", "Logs")
        try:
            files = glob.glob(os.path.join(logs_dir, "*DebugLog-server.txt"))
            return max(files, key=os.path.getmtime) if files else None
        except OSError:
            return None

    def parse_chat(self, server: GameServer, log_text: str):
        import datetime as dt

        out: list[tuple[str, str, dt.datetime]] = []
        for line in log_text.splitlines():
            cm = _PZ_CHAT_RE.search(line)
            if not cm or cm.group("ch") not in _PZ_BRIDGE_CHANNELS:
                continue
            # Skip lines WITHOUT a parseable timestamp (instead of a now() fallback): the log is
            # in server LOCAL time, and a UTC wall-clock fallback would undermine the ingest
            # watermark (re-ingest old fragments as "new" or drop real messages).
            at = _parse_pz_ts(line)
            if at is None:
                continue
            out.append((cm.group("p"), cm.group("m"), at))
        return out

    # --- Container specification (derived from the registry row) ---
    def container_env(self, server: GameServer, secret: str) -> dict[str, str]:
        from .. import secrets

        extra = json.loads(server.extra or "{}")
        name = extra.get("servername") or server.display_name or server.key
        # PZ uses THREE separate secrets: ADMINPASSWORD (in-game admin), PASSWORD (server join,
        # public!) and RCONPASSWORD. Each has its own slot in `services/secrets.py`: the variable
        # named in `extra.admin_password_env` / `extra.password_env` wins, otherwise the value
        # entered in the dashboard (stored encrypted). Without either, the admin password is the
        # console password and PASSWORD is left out.
        admin_pw = secrets.resolve(server, secrets.ADMIN) or secret
        join_pw = secrets.resolve(server, secrets.JOIN)
        env = {
            "STEAMAPPBRANCH": extra.get("branch") or branch_for_tag(server.image_tag),
            "SELF_MANAGED_MODS": "true",
            "SERVERNAME": name,
            "DISPLAYNAME": name,
            "ADMINUSERNAME": extra.get("admin_user", "admin"),
            "ADMINPASSWORD": admin_pw,
            "RCONPASSWORD": secret,
            "PUBLIC": "true" if server.presence_visible else "false",
            "MIN_MEMORY": f"{server.heap_min_mb}m" if server.heap_min_mb else "2048m",
            "MAX_MEMORY": f"{server.heap_max_mb}m" if server.heap_max_mb else "4096m",
        }
        # Set PASSWORD only when a source exists; otherwise DELIBERATELY omit it (image default)
        # rather than clearing it by accident. This lets the preflight detect a missing join
        # password.
        if join_pw:
            env["PASSWORD"] = join_pw
        # The registry's ports: the image passes PORT and UDPPORT to the server and writes them
        # into the .ini (DefaultPort, UDPPort) at every start; host port == container port.
        ports = json.loads(server.game_ports or "{}")
        for var, name in (("PORT", "game"), ("UDPPORT", "direct")):
            number = str(ports.get(name, "")).split("/")[0]
            if number.isdigit():
                env[var] = number
        return env

    def stop_timeout(self, server: GameServer) -> int:
        """PZ writes the whole world on a clean stop: 120 s.

        With the Docker default (10 s) the JVM is killed in the middle of saving.
        """
        return 120

    def container_binds(self, server: GameServer) -> list[str]:
        return [
            f"{server.data_dir}/data:/home/steam/Zomboid",
            f"{server.data_dir}/workshop-mods:/home/steam/pz-dedicated/steamapps/workshop",
        ]

    # --- Backup ---
    def backup_spec(self, server: GameServer) -> BackupSpec:
        return BackupSpec(
            data_dir=os.path.join(server.data_dir, "data"),
            world_subdirs=("Saves", "Server", "db"),
            # `Saves` is the world, `Server` the configuration (`.ini`, SandboxVars,
            # spawnregions). Project Zomboid is the only game whose archive contains
            # configuration, which is why "restore the configuration only" exists only here.
            #
            # `db` holds `<servername>.db`: the player accounts with their passwords, access
            # levels, bans and the whitelist. Without it a restore brings the world back and
            # every account is gone; on a server with `Open=false` nobody can get in any more.
            # It is not configuration, so it is not part of "restore the configuration only".
            # Archives from before it was added simply have no `db` folder, and a restore of
            # one leaves the current `db` alone (`extract_backup` replaces what the archive holds).
            config_subdirs=("Server",),
        )

    # --- Config editor (generic; the curated editor lives in routers/config.py) ---
    #
    # These methods must use the server-scoped `ini_editor` functions (`ini_path(server)`):
    # `/{key}/config`, `/{key}/mods` and the join page are served through them, and an unscoped
    # call would read and write the `.ini` of a different PZ server.
    def config_fields(self, server: GameServer) -> list[ConfigField]:
        from .. import ini_editor as ini

        # `env_managed` means "read-only here"; it does not hold the value back.
        #
        # `Password` is the named exception, on a different path: it stays read-only in the
        # `.ini` (the image overwrites it from the container env on every boot, so writing there
        # would have no effect and still look like success). Instead it is filled as a separate,
        # VISIBLE field from the registry and written back there. `RCONPassword` stays hidden:
        # it is the control channel.
        #
        # Decide secrecy by NAME, not by env membership. `ENV_MANAGED` says WHERE a value comes
        # from, not whether it is a secret. `DiscordToken` is a standard key in every PZ
        # `server.ini`, neither env-managed nor mod-managed, and a Discord bot token is a control
        # channel.
        #
        # Secret fields are write-only (`kind="password"`, empty value), as with Vintage Story.
        # MASKING them would be wrong here: the field stays editable, and saving would write the
        # dots into the file as the value.
        fields = [
            ConfigField(
                key=k, label=k, env_managed=(k in ini.ENV_MANAGED),
                kind="password" if looks_like_secret(k) else "str",
                value="" if looks_like_secret(k) else v,
            )
            for k, v in ini.get_ini_at(ini.ini_path(server)).items()
            if k not in _FROM_REGISTRY
        ]
        # The values the image writes from the container env into the `.ini`. Editing them THERE
        # has no effect; on the next boot the env value is back. So they come from the registry
        # and go back there; `reload=recreate` tells the UI that a restart is not enough.
        fields.append(join_password_field(
            server, key="Password", group="Server", reload=ConfigReload.recreate.value,
            help="The password for joining the server. Shown in plain text because every player "
                 "knows it anyway. It is NOT in the .ini (the image overwrites that on every boot) "
                 "but in the container environment — so a change only takes effect after the "
                 "container has been recreated.",
        ))
        fields.append(ConfigField(
            key="Public", label="Listed publicly", kind="bool", group="Server",
            value="true" if server.presence_visible else "false",
            reload=ConfigReload.recreate.value,
            help="Visibility in the Steam server browser. Off does NOT mean sealed off — whoever "
                 "has the address and the password still gets in through “Direct Connect”.",
        ))
        return fields

    @staticmethod
    def _extra(server: GameServer) -> dict:
        import json as _json

        try:
            return _json.loads(server.extra or "{}")
        except ValueError:
            return {}

    def _join_password(self, server: GameServer) -> str:
        """The value the server REALLY runs with — the same source as `container_env`."""
        from .. import secrets

        return secrets.resolve(server, secrets.JOIN)

    def set_config(self, server: GameServer, updates: dict[str, str]) -> None:
        from .. import ini_editor as ini

        # The join password does NOT go into the `.ini`: the image overwrites it from the
        # container env on every boot. A write there would have no effect and still look like
        # success, so it goes to the registry, which `container_env` reads. It takes effect on
        # the next recreate of the container.
        from_registry = {k: v for k, v in updates.items() if k in _FROM_REGISTRY}
        if from_registry:
            self._write_registry(server, from_registry)

        # The lock lives in the DRIVER, not only in the router: `set_config` is reachable from
        # other paths too, and a lock that hangs in one place only is gone with the next caller.
        locked = ini.ENV_MANAGED | ini.MOD_MANAGED
        # Empty password field = "unchanged" (write-only semantics, as with Vintage Story and
        # Reforger). Without this every save of the config page would wipe the secrets, because
        # the field always arrives empty on load. The join password is exempt: it goes through
        # `_FROM_REGISTRY` and was already split off above.
        safe = {
            k: v for k, v in updates.items()
            if k not in locked and not (looks_like_secret(k) and not str(v).strip())
        }
        if safe:
            ini.set_ini_keys_at(ini.ini_path(server), safe)

    def _write_registry(self, server: GameServer, values: dict[str, str]) -> None:
        """Set the env-bound values in the registry, the only place that reaches the image.

        `Password` goes into the encrypted `join` slot (`services/secrets.py`), `PublicName`
        lives in `extra`, `Public` is a column of its own (`presence_visible`). For the text
        fields empty really means empty: the user sees the current value and must be able to
        switch it off. A variable named in `extra.password_env` still wins over the stored
        password; the field is then system-managed (`join_password_field`).
        """
        import json as _json

        from sqlmodel import Session, select

        from ...db import engine
        from ...models.server import GameServer as Row
        from .. import secrets

        for k, v in values.items():
            if any(c in str(v) for c in "\r\n\x00"):
                raise DriverError(err(
                    "config.pz_control_chars", "{key} must not contain control characters.", key=k,
                ))

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
            if "Password" in values:
                secrets.store(s, row.id, str(values["Password"] or ""), secrets.JOIN)
                extra.pop("password", None)  # clear text of an older version
            if "PublicName" in values:
                extra["servername"] = str(values["PublicName"] or "")
            if "Public" in values:
                row.presence_visible = str(values["Public"]).strip().lower() in ("true", "1", "yes", "on")
            row.extra = _json.dumps(extra)
            s.add(row)
            s.commit()
            new_extra, new_public = row.extra, row.presence_visible
        # Update the copy passed in as well, so the same call does not read the old state afterwards.
        server.extra = new_extra
        server.presence_visible = new_public

    # --- Mods ---
    def list_mods(self, server: GameServer) -> dict:
        from .. import ini_editor as ini

        return ini.get_mods(server)

    # --- Safe console verbs ---
    def safe_console_commands(self) -> frozenset[str]:
        return frozenset({"players", "save", "servermsg"})

    # --- Moderation ---
    _ACCESS_LEVELS = ("admin", "moderator", "overseer", "gm", "observer", "none")

    def moderation_spec(self, server: GameServer) -> ModerationSpec:
        return ModerationSpec(
            kick=True, ban=True, ban_ip=True, unban=True,
            access_levels=self._ACCESS_LEVELS, whitelist=True,
            # PZ supports everything; hints exist only where a condition applies.
            reasons={"kick": "Only works for someone who is online right now."},
        )

    async def kick(self, server: GameServer, username: str, reason: str = "") -> str:
        u, r = sanitize_message(username), sanitize_message(reason)
        return await self.console(server, f'kickuser "{u}"' + (f' -r "{r}"' if r else ""))

    async def ban(self, server: GameServer, username: str, reason: str = "", ip: bool = False) -> str:
        u, r = sanitize_message(username), sanitize_message(reason)
        return await self.console(server, f'banuser "{u}"' + (" -ip" if ip else "") + (f' -r "{r}"' if r else ""))

    async def unban(self, server: GameServer, username: str) -> str:
        return await self.console(server, f'unbanuser "{sanitize_message(username)}"')

    async def set_access(self, server: GameServer, username: str, level: str) -> str:
        if level not in self._ACCESS_LEVELS:
            raise DriverError(err(
                "moderation.pz_invalid_access_level", "Invalid access level: {level}", level=level,
            ))
        return await self.console(server, f'setaccesslevel "{sanitize_message(username)}" {level}')

    async def whitelist_add(self, server: GameServer, username: str) -> str:
        return await self.console(server, f'addusertowhitelist "{sanitize_message(username)}"')

    async def whitelist_remove(self, server: GameServer, username: str) -> str:
        return await self.console(server, f'removeuserfromwhitelist "{sanitize_message(username)}"')

    def whitelist_list(self, server: GameServer) -> dict:
        """Registered players from the PZ SQLite DB (read-only, defensive)."""
        import sqlite3

        extra = json.loads(server.extra or "{}")
        name = extra.get("servername") or server.display_name or server.key
        path = os.path.join(server.data_dir, "data", "db", f"{name}.db")
        rows: list[dict] = []
        try:
            con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            try:
                con.row_factory = sqlite3.Row
                tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if "whitelist" in tables:
                    for r in con.execute("SELECT * FROM whitelist LIMIT 500"):
                        d = dict(r)
                        rows.append({
                            "username": d.get("username") or d.get("displayName") or "",
                            "accesslevel": d.get("accesslevel") or "",
                            "banned": bool(d.get("banned")),
                            "steamid": str(d.get("steamid") or ""),
                        })
            finally:
                con.close()
        except Exception:
            return {"available": False, "users": []}
        return {"available": True, "users": rows}
