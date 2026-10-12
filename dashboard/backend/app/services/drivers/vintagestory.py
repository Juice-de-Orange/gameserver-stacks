"""VintageStoryDriver for the project's own runner image (`images/vintagestory/`, published as
`ghcr.io/juice-de-orange/stacks-console-vintagestory-runner`; setting GSS_VS_RUNNER_IMAGE).

VS is .NET (no JVM heap → `heap_*` = NULL, RAM only through the cgroup limit `ram_mb`). The
version comes from the `VERSION` env. That requires an image that actually READS it: an image
that bakes the game in at BUILD time ignores `VERSION`, and version change, rollback and
auto-update silently do nothing. The runner image (`images/vintagestory/`) downloads the version
at boot, md5-checked and cached under `gamedata/server-bin/`.

Config is `serverconfig.json`. The console comes from the **optional** third-party "Vintage RCON"
mod, which speaks Source RCON on the internal network (→ `rcon_command` unchanged). The mod and
its RCON password have to be set up on the server; until the patterns have been calibrated
against the real mod output, console, players and events are best effort. Without the mod the
server still runs and is fully managed: the boot proof is the server's own ready line
(`boot_ready_line`), the console page says why it cannot work, and the watchdog does not read the
silent RCON port as a hang.
Backup: `genbackup` (no slash, via the mod) as pre-hook; a fully consistent backup otherwise
needs stop-then-copy.

File access (config/mods) requires the server's data directory to be mounted into the API
container; all file paths are best effort (no mount → empty, no crash).
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
import posixpath
import re
from typing import TYPE_CHECKING, ClassVar

from ...errors import err
from ...models.server import GameType
from ..rcon import RconError
from .base import (
    JOIN_PASSWORD, AutoUpdate, BackupSpec, ConfigField, DriverError, GameDriver, ModerationSpec,
    QuiesceWarning,
    ServerEvent, VersionSource, looks_like_secret, sanitize_message,
)

if TYPE_CHECKING:
    from ...models.server import GameServer

log = logging.getLogger("uvicorn.error")

# VS player list from the RCON mod output; the patterns still need calibrating against the real
# mod output.
_VS_COUNT_RE = re.compile(r"(\d+)\s+player", re.I)
_VS_EVENTS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"Player (?P<u>[\w.\- ]{1,32}) joins", re.I), "join"),
    (re.compile(r"Player (?P<u>[\w.\- ]{1,32}) (?:left|disconnect)", re.I), "leave"),
]

# Chat bridge: VS writes player chat to `…/Logs/server-chat.log` (not stdout).
# Format (verified on real logs): `24.1.2026 16:30:32 [Chat] 0 | Alice: hello`.
# `[Chat] <group> | <player>: <text>`; group 0 is the general/public channel (only that one is
# bridged; private group chats with another id stay local).
_VS_CHAT_RE = re.compile(r"\[Chat\]\s+(?P<gid>\d+)\s+\|\s+(?P<p>[^:]+):\s+(?P<m>.*)$")
_VS_TS_RE = re.compile(r"^(\d{1,2})\.(\d{1,2})\.(\d{4})\s+(\d{1,2}):(\d{2}):(\d{2})")
_VS_BRIDGE_GROUP = "0"


def _parse_vs_ts(line: str):
    """`D.M.YYYY H:M:S` → naive datetime (for the ingest watermark). None if malformed."""
    import datetime as dt

    m = _VS_TS_RE.match(line)
    if not m:
        return None
    d, mo, y, h, mi, s = (int(x) for x in m.groups())
    try:
        return dt.datetime(y, mo, d, h, mi, s)
    except ValueError:
        return None


def parse_vs_players(out: str) -> dict:
    """Best effort: players from the VS RCON output. Not yet calibrated on the real mod output."""
    text = out or ""
    m = _VS_COUNT_RE.search(text)
    # Names best effort: lines starting with a bullet; otherwise empty until calibrated.
    names = [n.strip("-• \t") for n in text.splitlines() if n.strip().startswith(("-", "•"))]
    count = int(m.group(1)) if m else len(names)
    return {"count": count, "names": names}


# The same form the runner image accepts (`images/vintagestory/entrypoint.sh`). What fails here
# would otherwise be rejected by the image only AFTER stop, backup and container swap.
_VS_VERSION_FORM = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+([.-][0-9A-Za-z.-]+)?$")


# Vintage Story has no remote console; the dashboard's comes from an optional third-party mod.
_NO_RCON_MOD = err(
    "console.vintagestory_rcon_mod_missing",
    "Vintage Story has no remote console of its own. The console, the player list and the "
    "moderation commands need the third-party server mod “Vintage RCON”, and it is not installed "
    "on this server. The game itself runs normally without it.",
)


class VsRconModMissing(RconError):
    """RCON does not answer and the mod is not there. Carries the coded reason in `detail`.

    An `RconError`, so every caller that treats an unreachable RCON as "unknown" (idle stop,
    player count, boot proof) keeps doing so.
    """

    def __init__(self, detail: dict) -> None:
        super().__init__(detail["message"])
        self.detail = detail


# The one key in `serverconfig.json` that holds the join password. Deliberately a set and not a
# heuristic: `looks_like_secret` would also match the mod's RCON password, and that is a control
# channel, not part of an invitation.
_VS_JOIN = frozenset({"Password"})


class VintageStoryDriver(GameDriver):
    game_type = GameType.vintagestory
    # Through an env variable, like Minecraft. The runner image requires `VERSION` to be set.
    version_source = VersionSource.env
    # Like Minecraft: hotfixes of the same minor version at night, no jumps.
    auto_update_limit = AutoUpdate.patch
    # The runner image deliberately has no default version (`images/vintagestory/entrypoint.sh`).
    version_required = True

    def version_valid(self, version: str) -> bool:
        return bool(_VS_VERSION_FORM.match(version))

    def pull_failed_detail(self, server: GameServer, image: str, reason: str) -> dict:
        """The runner image could not be pulled: say how to build it locally.

        The default runner lives on GHCR; where that is not reachable (packages not public yet, a
        registry outage, no internet on the host) the raw registry error ("denied") gives no clue
        that the image can be built from this repository in a minute.
        """
        return err(
            "provision.vintagestory_runner_pull_failed",
            "The Vintage Story runner image {image} could not be downloaded ({reason}). Build it "
            "on the Docker host instead: docker build -t stacks-console/vintagestory-runner:1 "
            "dashboard/images/vintagestory — then enter stacks-console/vintagestory-runner as "
            "Image and 1 as Image tag on this server's edit page and create the container again.",
            image=image, reason=reason,
        )

    def capabilities(self) -> frozenset[str]:
        return frozenset({"console", "players", "events", "config", "mods", "backup", "versions", "announce"})

    # The server's own line once it accepts players (verified on 1.22.6):
    #   "12.10.2026 11:37:42 [Server Event] Dedicated Server now running on Port 42420 and all ips!"
    # The boot proof without the optional RCON mod (`GameDriver.boot_ready_line`).
    boot_ready_line = re.compile(r"Dedicated Server now running")

    def rcon_mod_installed(self, server: GameServer) -> bool | None:
        """Is the third-party "Vintage RCON" mod there? `None` = cannot tell (no data mount).

        Evidence is the mod in the active `Mods/` folder (file or folder name with `vintagercon`
        or its mod id `vsrcon`) or the `ModConfig/vsrcon.json` it writes on its first start.
        Only names are read: `players()` runs through here every minute.
        """
        if not os.path.isdir(self._gamedata(server)):
            return None
        if os.path.isfile(self.data_path(server, "ModConfig", "vsrcon.json")):
            return True
        try:
            names = os.listdir(self.mods_dir(server))
        except FileNotFoundError:
            return False
        except OSError:
            return None
        return any("vintagercon" in n.lower() or "vsrcon" in n.lower() for n in names)

    def console_notice(self, server: GameServer) -> dict | None:
        """Why the console cannot work on this server, sent when the console opens. `None` = no
        known reason."""
        if self.rcon_mod_installed(server) is False:
            return _NO_RCON_MOD
        return None

    def players_is_queryable(self, server: GameServer) -> bool:
        """Without the mod nobody answers on the RCON port; the watchdog would read that as a hung
        game and restart a healthy server every 20 minutes."""
        return self.rcon_mod_installed(server) is not False

    # --- Console / RCON (via the "Vintage RCON" mod; no proxy exec/attach) ---
    async def console(self, server: GameServer, command: str, *, timeout: float = 8.0) -> str:
        from ..rcon import rcon_command
        from ..servers import resolve_secret

        secret = resolve_secret(server)
        try:
            return await rcon_command(server.rcon_host, server.rcon_port, secret, command, timeout=timeout)
        except RconError as e:
            # The bare "RCON unreachable" says nothing about the usual cause: the mod is optional
            # and missing. Only where that is certain; otherwise the real error stays.
            if self.rcon_mod_installed(server) is False:
                raise VsRconModMissing(_NO_RCON_MOD) from e
            raise

    async def players(self, server: GameServer) -> dict:
        return parse_vs_players(await self.console(server, "list clients"))

    async def announce(self, server: GameServer, text: str) -> None:
        # VS: `announce <text>` WITHOUT a leading slash. The "Vintage RCON" mod injects commands
        # as in the server console (no `/` prefix; a `/` would produce `//announce` and fail).
        # Broadcasts to group 0; does NOT show up as a `[Chat] <gid> | <player>:` line → loop-safe.
        await self.console(server, f"announce {sanitize_message(text)}")

    async def save(self, server: GameServer) -> None:
        # VS saves automatically on shutdown; the RCON mod has no save command of its own. So
        # this is deliberately a no-op before a restart; a raw PZ `save` would arrive as "/save"
        # and do nothing.
        return None

    def connect_info(self, server: GameServer):
        """Vintage Story has no URL scheme; joining goes through the server browser."""
        from .base import ConnectInfo, game_port, game_proto, public_host

        cfg = self._config(server) if hasattr(self, "_config") else {}
        return ConnectInfo(
            host=public_host(server), port=game_port(server), protocol=game_proto(server),
            uri="",
            password_required=bool(cfg.get("Password")),
            hint="In Vintage Story: “Multiplayer” → “Add Server” → enter the address.",
            browser_name=str(cfg.get("ServerName") or server.display_name or server.key),
        )

    # No `graceful_shutdown` of its own: the `docker stop` IS the clean stop. The runner's
    # dotnet process handles the signal (STOPSIGNAL SIGINT in `images/vintagestory/Dockerfile`)
    # and saves the world. A `stop` through the "Vintage RCON" mod was tried here before, but
    # whether the mod knows that command is undocumented, it answers unknown commands with plain
    # text, and the attempt made `lifecycle` wait its head start and log "falling back to the
    # signal path" on every stop although that path was the right one.

    # --- Chat bridge: VS reads from …/Logs/server-chat.log (not Docker log/RCON) ---
    def chat_log_path(self, server: GameServer) -> str | None:
        import os

        # Fixed paths, no recursive glob. This runs synchronously on every bridge round, and the
        # runner image keeps up to three game installations under `gamedata/server-bin/`: a
        # `glob(data_dir/**/Logs/server-chat.log)` would walk tens of thousands of entries per
        # round in the single uvicorn worker. There are exactly two places: the data path and
        # the old layout above it.
        candidates = (
            self.data_path(server, "Logs", "server-chat.log"),
            posixpath.join(self._gamedata(server), "Logs", "server-chat.log"),
        )
        try:
            files = [f for f in candidates if os.path.isfile(f)]
            return max(files, key=os.path.getmtime) if files else None
        except OSError:
            return None

    def parse_chat(self, server: GameServer, log_text: str):
        import datetime as dt

        out: list[tuple[str, str, dt.datetime]] = []
        for line in log_text.splitlines():
            cm = _VS_CHAT_RE.search(line)
            if not cm or cm.group("gid") != _VS_BRIDGE_GROUP:
                continue
            # Skip lines WITHOUT a parseable timestamp (instead of a now() fallback): the log is
            # in server LOCAL time, and a UTC wall-clock fallback would undermine the ingest
            # watermark (re-ingest old fragments as "new" or drop real messages).
            at = _parse_vs_ts(line)
            if at is None:
                continue
            out.append((cm.group("p").strip(), cm.group("m"), at))
        return out

    def parse_events(self, server: GameServer, log_text: str) -> list[ServerEvent]:
        import datetime as dt

        from ..log_parser import _TS_RE, _parse_ts

        out: list[ServerEvent] = []
        for line in log_text.splitlines():
            line = line.strip()
            if not line:
                continue
            m = _TS_RE.match(line)
            if m:
                at = _parse_ts(m.group("ts"))
                content = m.group("rest")
            else:
                at = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
                content = line
            for pattern, kind in _VS_EVENTS:
                em = pattern.search(content)
                if em:
                    out.append(ServerEvent(kind=kind, username=em.group("u").strip(), at=at, raw=line))
                    break
        return out

    # --- Container specification (VERSION env image; RAM through the cgroup only) ---
    def container_env(self, server: GameServer, secret: str) -> dict[str, str]:
        extra = json.loads(server.extra or "{}")
        # The RCON password lives in serverconfig.json (read by the mod), NOT in the env.
        # Set VERSION ONLY when a target version is pinned. An empty game_version must NOT act as
        # a version change (it could migrate the world). The runner image deliberately has NO
        # default version: without VERSION it aborts before the game starts instead of silently
        # running some version. Further env via extra.env.
        env: dict[str, str] = {str(k): str(v) for k, v in (extra.get("env") or {}).items()}
        # AFTER `extra.env`, and a `VERSION` in there is dropped: otherwise an old pin would win
        # over every version change, exactly the silent no-op the runner image was built against.
        env.pop("VERSION", None)
        # The same uid as the dashboard API, which edits serverconfig.json and installs mods
        # under the data path. The runner chowns that path to `PUID:PGID` and drops to them
        # (`images/vintagestory/entrypoint.sh`); its default is 1000, so an API built with
        # another `APP_UID` would lose write access. Set unless `extra.env` names them itself.
        env.setdefault("PUID", str(os.getuid()))
        env.setdefault("PGID", str(os.getgid()))
        if server.game_version:
            env["VERSION"] = server.game_version
        return env

    # Pattern verified on a running server:
    #   "5.7.2026 20:42:59 [Server Notification] Game Version: v1.21.6 (Stable)"
    _VERSION_RE = re.compile(r"Game Version:\s*v?([0-9]+(?:\.[0-9]+)*)")

    # A cache dict of its own per driver class; otherwise VS and Reforger would share a namespace.
    _version_cache: ClassVar[dict[tuple[str, str], str]] = {}

    async def detect_version(self, server: GameServer) -> str:
        """Read the running version from the container log.

        The line "Game Version: v1.21.6" is in the START BANNER. A small `tail` does not find it:
        on a server that has been running for a while it is hundreds of thousands of characters
        back. So the whole log is fetched and the result remembered; the cache is handled in
        `_version_from_log`.
        """
        return await self._version_from_log(server, self._VERSION_RE)

    def container_binds(self, server: GameServer) -> list[str]:
        return [f"{server.data_dir}/gamedata:/gamedata"]

    # --- Backup: genbackup as pre-hook (if the mod is there); create_backup tars the saves ---
    def backup_spec(self, server: GameServer) -> BackupSpec:
        import asyncio

        def _running() -> bool:
            """Is the container running right now? When in doubt YES; then a quiesce error is
            real. Same helper as in `minecraft.py`."""
            from ..docker_client import docker

            try:
                return bool(asyncio.run(docker.state(server.container_name)).get("running"))
            except Exception:      # noqa: BLE001 — proxy unreachable means "when in doubt it is running"
                return True

        def _genbackup() -> None:
            try:
                # WITHOUT a leading slash (like `announce`): the RCON mod adds it itself,
                # "/genbackup" would arrive as "//genbackup" and fail silently.
                asyncio.run(self.console(server, "genbackup"))
            except Exception as e:
                # A stopped server is not a failed quiesce. Without this distinction EVERY
                # scheduled backup of a stopped container would record a
                # `backup_quiesce_failed` incident although the archive is correct: pure noise
                # in the incident list that eventually hides the one real failure.
                if not _running():
                    log.info("vs[%s]: genbackup skipped — the container is not running", server.key)
                    return
                # `QuiesceWarning` instead of `pass`: the tar over `Saves/` runs independently
                # and is the actual archive, so it is still created, but nobody takes the
                # failure for success. See the class in `base.py` for the contrast with
                # Minecraft, where quiescing is load-bearing and has to fail hard.
                log.error("vs[%s]: genbackup failed: %s", server.key, e)
                raise QuiesceWarning(f"The Vintage Story save point (genbackup) failed: {e}") from e

        # `data_dir` deliberately stays `gamedata`: `backups/` lives there, and moving it would
        # make the existing archives invisible. Only the world directory is resolved relative to
        # it: `vs/Saves` instead of `Saves`. `extract_backup` restores to the same place.
        base = self._gamedata(server)
        world = posixpath.relpath(self.data_path(server, "Saves"), base)
        if self.rcon_mod_installed(server) is False:
            # No mod, no console: the save point can never be set, and a copy of the running
            # SQLite world (with its `-wal`) is not worth trusting. Stop for the archive, as
            # for Valheim. `None` (no data mount, cannot tell) keeps the save-command path.
            return BackupSpec(data_dir=base, world_subdirs=(world,), stop_for_consistency=True)
        return BackupSpec(
            data_dir=base,
            world_subdirs=(world,),
            pre_hook=_genbackup,
        )

    # --- Config: serverconfig.json (JSON file; needs the data directory mounted) ---
    def _config(self, server: GameServer) -> dict:
        """Read serverconfig.json; empty if it is not there. Never raises."""
        path = self.data_path(server, "serverconfig.json")
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def config_fields(self, server: GameServer) -> list[ConfigField]:
        fields: list[ConfigField] = []
        data = self._config(server)
        if not data:
            return fields
        for k, v in data.items():
            # A `null` means "not set", not "does not exist". In a real `serverconfig.json`
            # `Password` is JSON `null` when unset and would fall through the scalar check
            # below, so the editor would hide it exactly when someone wants to SET it.
            #
            # Deliberately only for the join key and not for every `null`: for the other fields
            # the type would be guessed, and a wrongly typed field writes nonsense into the file
            # on the next save.
            if v is None and k in _VS_JOIN:
                v = ""
            if isinstance(v, (str, int, float, bool)):
                kind = "bool" if isinstance(v, bool) else ("int" if isinstance(v, int) else "str")
                # `serverconfig.json` contains BOTH passwords: the join password (`Password`,
                # cf. `connect_info`) and that of the RCON mod.
                # WRITE-ONLY rather than read-only. Unlike PZ and Minecraft, `serverconfig.json`
                # is the ONLY source of these values for Vintage Story; they come from no
                # container env. Marking them `env_managed` would mean the passwords could never
                # be changed again. The pattern is the same as Reforger's (`game.password`): the
                # value is never delivered, an empty field means "unchanged".
                # `Password` is the JOIN password, the invitation every player knows anyway, so
                # it is visible and can be cleared. Everything else that looks like a secret
                # (above all the RCON password of the "VintageRCon" mod) stays write-only: that
                # is a control channel.
                if k in _VS_JOIN:
                    fields.append(ConfigField(
                        key=k, label=k, group="serverconfig.json",
                        kind=JOIN_PASSWORD, value="" if v is None else str(v),
                    ))
                    continue
                secret = looks_like_secret(k)
                fields.append(ConfigField(
                    key=k, label=k, group="serverconfig.json",
                    kind="password" if secret else kind,
                    value="" if secret else str(v),
                ))
        return fields

    def set_config(self, server: GameServer, updates: dict[str, str]) -> None:
        # Empty password field = "unchanged" (write-only semantics, as with Reforger). Without
        # this, saving the config page would wipe the passwords, because the field is delivered
        # empty.
        updates = {
            k: v for k, v in updates.items()
            # The join password is exempt: there empty really means "open server", because the
            # user sees the current value.
            if k in _VS_JOIN or not (looks_like_secret(k) and not str(v).strip())
        }
        path = self.data_path(server, "serverconfig.json")
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            # See `minecraft.set_config`: a silent `return` becomes 200 + audit in the endpoint
            # for a change that never happened. Broken JSON explicitly counts too; otherwise
            # the next successful write would overwrite a file nobody was able to read.
            raise DriverError(err(
                "config.vintagestory_unreadable",
                "serverconfig.json cannot be read ({path}): {reason}", path=path, reason=str(e),
            )) from e
        for k, v in updates.items():
            old = data.get(k)
            # Edit existing SCALAR keys only; do not create unknown keys or overwrite nested
            # (dict/list) ones (structure and type protection for serverconfig.json).
            if isinstance(old, (dict, list)):
                continue
            if old is None:
                # Same case as in the read path, on the write side. The type protection skips
                # keys without a current value: right for unknown keys, wrong for the join
                # password, which is `null` precisely because it was never set. Only there is
                # the target type known (string); anywhere else it would be guessed, and a
                # wrongly typed value writes nonsense into the file.
                if k not in _VS_JOIN:
                    continue
                data[k] = str(v)
                continue
            if isinstance(old, bool):
                data[k] = str(v).lower() in ("true", "1", "yes", "on")
            elif isinstance(old, int):  # bool is handled above
                try:
                    data[k] = int(v)
                except (TypeError, ValueError):
                    data[k] = v
            elif isinstance(old, float):
                try:
                    data[k] = float(v)
                except (TypeError, ValueError):
                    data[k] = v
            else:
                data[k] = str(v)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=1)

    # --- The DATA PATH: `gamedata/vs`, not `gamedata` ---------------------------------------
    @staticmethod
    def _gamedata(server: GameServer) -> str:
        return posixpath.join(server.data_dir, "gamedata")

    @classmethod
    def data_path(cls, server: GameServer, *parts: str) -> str:
        """A path INSIDE the real data path, with a fallback to the old layout.

        CAREFUL: the server runs with `VS_DATA_PATH=/gamedata/vs` and therefore works in
        `gamedata/vs/…`, NOT in `gamedata/…`. This applies to mods, world and configuration
        alike. An installation can have a stale `gamedata/Saves/` next to the real
        `gamedata/vs/Saves/`; a backup that packs the stale one looks entirely plausible by size
        and contains an old world, including every mandatory backup taken as the way back before
        applying mods, restoring or changing versions.

        `extra.vs_data_subdir` allows a different sub-directory. The fallback only applies when
        the expected path does not exist at all but the old one does, so a deviating
        installation stays usable instead of silently pointing at nothing.
        """
        extra = json.loads(server.extra or "{}")
        sub = str(extra.get("vs_data_subdir", "vs")).strip("/")
        base = cls._gamedata(server)
        candidate = posixpath.join(base, sub, *parts) if sub else posixpath.join(base, *parts)
        legacy = posixpath.join(base, *parts)
        if not os.path.exists(candidate) and os.path.exists(legacy):
            return legacy
        return candidate

    # --- Mods: ZIP/DLL/CS in the Mods directory of the DATA PATH ---
    @classmethod
    def mods_dir(cls, server: GameServer) -> str:
        return cls.data_path(server, "Mods")

    def list_mods(self, server: GameServer) -> dict:
        try:
            names = os.listdir(self.mods_dir(server))
        except OSError:
            return {"mods": []}
        return {"mods": sorted(f for f in names if not f.startswith("."))}

    # No `stop_timeout` override: the inherited 60 s are sufficient by measurement. From "Server
    # termination event received" to "All threads gracefully shut down" took 0.6 seconds (two
    # independent measurements), exit code 0.

    # --- Moderation: VintageRcon can kick/ban (no leading slash). Unban/whitelist/access levels
    # depend on the mod and are not verified yet → deliberately NOT offered. ---
    def moderation_spec(self, server: GameServer) -> ModerationSpec:
        # The reasons are shown in the UI. The wording matters: not "does not exist" but "has
        # not been verified"; that is the difference between a limit of the game and an open
        # measurement.
        unset = (
            "Not offered because it is unverified whether the VintageRCon mod can do it. That is a "
            "matter of measuring, not of programming: the commands have to be tried through the "
            "console of a running Vintage Story server (without a leading slash) — whatever "
            "answers gets built."
        )
        return ModerationSpec(
            kick=True, ban=True,
            reasons={"unban": unset, "ban_ip": unset, "access_levels": unset,
                     "whitelist": unset},
        )

    async def kick(self, server: GameServer, username: str, reason: str = "") -> str:
        return await self.console(server, f"kick {sanitize_message(username)}")

    async def ban(self, server: GameServer, username: str, reason: str = "", ip: bool = False) -> str:
        return await self.console(server, f"ban {sanitize_message(username)}")

    # --- Safe console verbs (VS commands without slash) + overview figures ---
    def safe_console_commands(self) -> frozenset[str]:
        return frozenset({"list", "stats"})

    def overview_stats(self, server: GameServer) -> dict:
        max_players = None
        try:
            for f in self.config_fields(server):
                if f.key in ("MaxClients", "MaxPlayers"):
                    max_players = int(f.value)
                    break
        except (ValueError, OSError):
            pass
        try:
            mods_count = len(self.list_mods(server).get("mods", []))
        except OSError:
            mods_count = None
        return {"max_players": max_players, "mods_count": mods_count}
