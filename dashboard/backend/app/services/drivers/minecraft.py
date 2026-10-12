"""MinecraftDriver for the itzg/minecraft-server image.

Controlled through **Source RCON** (itzg `ENABLE_RCON=TRUE`, port 25575). RCON replies can exceed
4096 bytes (`list`, `help`), hence `rcon_command(..., multi=True)`. Version and type come from
the `VERSION`/`TYPE` env (the image downloads at boot, no image pull). Config is
`server.properties` (file edit), mods/plugins are jars under `/data/mods` and `/data/plugins`.

File access (config/mods) requires the server's `data_dir` to be mounted into the API container.
All file paths are best effort (no mount → empty list, no crash).
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
import posixpath
import re
from typing import TYPE_CHECKING

from ...errors import err
from ...models.server import GameType
from .base import (
    AutoUpdate, BackupSpec, ConfigField, DriverError, GameDriver, ModerationSpec, ServerEvent,
    VersionSource,
    is_env_managed_key, mask_if_secret, sanitize_message,
)

if TYPE_CHECKING:
    from ...models.server import GameServer

log = logging.getLogger("uvicorn.error")

# Keys in `server.properties` the config editor must not write. Secrets are recognised by name
# in `is_env_managed_key` (`rcon.password`); these are the STRUCTURAL values: the three ports and
# `enable-rcon` are the dashboard's control channel, and `level-name` determines which
# directories `backup_spec` below packs.
_MC_ENV_MANAGED = frozenset({
    "enable-rcon", "rcon.port", "server-port", "query.port", "level-name",
})

# "There are 2 of a max of 20 players online: Alice, Bob"
_MC_LIST_RE = re.compile(r"There are (\d+)\s+of a max of \d+ players online:?\s*(.*)", re.I)

# Log prefix, ANCHORED, and matching BOTH real forms:
#
#   [10:00:00] [Server thread/INFO]: …   → `logs/latest.log` (file, older servers)
#   [20:36:01 INFO]: …                   → stdout, i.e. what `docker.logs` returns
#
# A running Paper server writes the SECOND form to stdout. Anchoring on `[Server thread/INFO]:`
# alone, the obvious reading of the documentation, would switch event detection off entirely.
#
# `+` over the bracket blocks covers both forms; the closing `:` is NOT part of the block, so
# `[20:36:01 INFO]` matches just like `[10:00:00] [Server thread/INFO]`.
_MC_PREFIX = r"^(?:\[[^\]]{1,40}\]\s*)+:\s*"

# ALL patterns start with the prefix. Without it `search` would run over the whole line and any
# text could create an event: `<Steve> Friend2 joined the game` would record a real join for
# "Friend2", and the bridge's own cross-server injection (`say [Project Zomboid] Bob: …`) would
# create Minecraft events from PZ chat. Both would end up in `player_events` and from there in
# session times, leaderboard and event feed. `loop_guard.mark_injected` only protects the chat
# ingest, not `parse_events`.
#
# DELIBERATELY no "chat" entry: chat is NOT a PlayerEvent (driver contract in base.py). It flows
# through `_MC_CHAT_RE`/`parse_chat` into the cross-server bus; a "chat" event here would be
# persisted by `log_parser` as an empty `PlayerEvent(kind="chat")`.
#
# `what` stays in the death alternation although it is broad on its own: Minecraft has dozens of
# death messages of this form (`was pricked to death`, `was impaled`, `was fireballed`, …) and
# an enumeration would certainly be incomplete. The anchoring takes the edge off: the name has
# to be the FIRST word after the log prefix, which is the case for real server messages.
# These are starter patterns and should be calibrated against real server logs.
_MC_EVENTS: list[tuple[re.Pattern, str]] = [
    (re.compile(_MC_PREFIX + r"(?P<u>[A-Za-z0-9_]{1,16}) joined the game\b"), "join"),
    (re.compile(_MC_PREFIX + r"(?P<u>[A-Za-z0-9_]{1,16}) left the game\b"), "leave"),
    (re.compile(
        _MC_PREFIX + r"(?P<u>[A-Za-z0-9_]{1,16}) (?:was|drowned|blew up|hit the ground|fell|"
        r"burned|went up in flames|starved|suffocated|withered|died)\b"), "death"),
]

# Chat bridge: player chat with BODY. ANCHORED on the real log prefix followed by `<Name> …`
# (optionally `[Not Secure]`), so a `<…>` in the MIDDLE of the text (e.g. in the bridge's own
# `say` injection `[Server] [A] Alice: hi <Bob> x`) is NOT mistaken for chat; otherwise player
# name and origin could be spoofed and an echo loop built. `say` output appears as a
# `[Server]`/`[Rcon]` line without `<Name>` right after the prefix → no match.
#
# The prefix must cover the stdout form (`[20:36:01 INFO]:`), not only the `logs/latest.log`
# form: `chat_log_path` returns None for MC, so the ingest reads `docker.logs`.
_MC_CHAT_RE = re.compile(
    _MC_PREFIX + r"(?:\[Not Secure\]\s*)?<(?P<p>[A-Za-z0-9_]{1,16})>\s(?P<m>.+)$"
)


def parse_mc_list(out: str) -> dict:
    """MC `list` output → {"count": int, "names": list[str]}."""
    m = _MC_LIST_RE.search(out or "")
    if not m:
        return {"count": 0, "names": []}
    count = int(m.group(1))
    names = [n.strip() for n in m.group(2).split(",") if n.strip()]
    return {"count": count, "names": names}


class MinecraftDriver(GameDriver):
    game_type = GameType.minecraft
    # The itzg image downloads the version at boot according to `VERSION`/`TYPE`.
    version_source = VersionSource.env
    # Hotfixes of the same minor version are applied at night (1.21.10 → 1.21.11). A jump to
    # 1.22 stays manual work: the world gets migrated there, and the boot check sees none of it.
    auto_update_limit = AutoUpdate.patch

    def capabilities(self) -> frozenset[str]:
        return frozenset({"console", "players", "events", "config", "mods", "backup", "versions", "announce"})

    # --- Console / RCON (multi-packet: MC replies can exceed 4096 bytes) ---
    async def console(self, server: GameServer, command: str, *, timeout: float = 8.0) -> str:
        from ..rcon import rcon_command
        from ..servers import resolve_secret

        secret = resolve_secret(server)
        return await rcon_command(
            server.rcon_host, server.rcon_port, secret, command, timeout=timeout, multi=True
        )

    async def players(self, server: GameServer) -> dict:
        return parse_mc_list(await self.console(server, "list"))

    # --- Log/event parsing (strip the Docker RFC3339 prefix, then MC patterns) ---
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
            for pattern, kind in _MC_EVENTS:
                em = pattern.search(content)
                if em:
                    out.append(ServerEvent(kind=kind, username=em.group("u"), at=at, raw=line))
                    break
        return out

    def parse_chat(self, server: GameServer, log_text: str):
        import datetime as dt

        from ..log_parser import _TS_RE, _parse_ts

        out: list[tuple[str, str, dt.datetime]] = []
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
            cm = _MC_CHAT_RE.search(content)
            if cm:
                out.append((cm.group("p"), cm.group("m").strip(), at))
        return out

    async def announce(self, server: GameServer, text: str) -> None:
        # MC: `say <text>` (no `servermsg`). Shows up in the log as a [Server] line, not as
        # "<player> text", so the ingest does not read it back as player chat.
        await self.console(server, f"say {sanitize_message(text)}")

    async def save(self, server: GameServer) -> None:
        # MC: `save-all flush` (not the PZ `save`); flushes the world to disk before the restart.
        await self.console(server, "save-all flush")

    def connect_info(self, server: GameServer):
        """Minecraft Java has **no** URL scheme, so there is no one-click join.

        An invented `minecraft://` link would look like a button and do nothing. The hint gives
        the real way in the game instead; the address is shown next to it for copying.
        """
        from .base import ConnectInfo, game_port, game_proto, public_host

        return ConnectInfo(
            host=public_host(server), port=game_port(server), protocol=game_proto(server),
            uri="",
            password_required=False,  # Minecraft has no server password; it uses a whitelist instead
            hint="In Minecraft: “Multiplayer” → “Add Server” → enter the address.",
            browser_name=server.display_name or server.key,
        )

    # No `graceful_shutdown` of its own: the signal path IS the clean stop here. On `docker
    # stop` the itzg image's `mc-server-runner` sends `stop` to the server console itself —
    # measured on Paper 26.2: all chunks saved, exit code 0, about one second. An RCON `stop`
    # after the stop call (the order `lifecycle` needs, see its module docstring) arrives at a
    # container that is already gone; one before it would let the game exit before Docker marks
    # the container as stopped, and `unless-stopped` would start it again.

    # --- Container specification (itzg env) ---
    def container_env(self, server: GameServer, secret: str) -> dict[str, str]:
        extra = json.loads(server.extra or "{}")
        env = {
            "EULA": "TRUE",
            "TYPE": server.server_type or "VANILLA",
            "VERSION": server.game_version or "LATEST",
            "ENABLE_RCON": "TRUE",
            "RCON_PORT": str(server.rcon_port or 25575),
            "RCON_PASSWORD": secret,
            "OVERRIDE_SERVER_PROPERTIES": "false",  # our server.properties editor wins
            # The same uid as the dashboard API, which writes server.properties, the whitelist and
            # the mods into the same directory. The image runs as 1000 by default and chowns
            # `/data` to its uid at every start, so an API built with another `APP_UID` would lose
            # write access after the first start. The image's start script switches its
            # `minecraft` user to `UID`/`GID` (measured on 2026.9.2: "Changing uid of minecraft
            # to …"). The process's own ids, never a fixed 1000.
            "UID": str(os.getuid()),
            "GID": str(os.getgid()),
        }
        if server.heap_max_mb:
            env["MAX_MEMORY"] = f"{server.heap_max_mb}M"
        if server.heap_min_mb:
            env["INIT_MEMORY"] = f"{server.heap_min_mb}M"
        for k, v in (extra.get("env") or {}).items():  # pass MOTD/DIFFICULTY/... through from extra
            env[str(k)] = str(v)
        return env

    def container_binds(self, server: GameServer) -> list[str]:
        return [f"{server.data_dir}/data:/data"]

    def _level_name(self, server: GameServer) -> str:
        """`level-name` from server.properties (default `world`).

        Read-only in the editor, but the value still has to be READ, otherwise the backup
        definition points at nothing. An unreadable file deliberately falls back to `world`: it
        is the game's default, and a backup that packs the default world beats none at all.
        """
        try:
            for f in self.config_fields(server):
                if f.key == "level-name" and f.value.strip():
                    return f.value.strip()
        except Exception:
            log.warning("mc[%s]: level-name cannot be read — the backup takes 'world'", server.key)
        return "world"

    # --- Backup: save-off/save-all before the tar, save-on afterwards (consistent world) ---
    def backup_spec(self, server: GameServer) -> BackupSpec:
        import asyncio

        def _running() -> bool:
            from ..docker_client import docker

            try:
                return bool(asyncio.run(docker.state(server.container_name)).get("running"))
            except Exception:
                return True   # when in doubt it is running, so the quiesce error is real

        def _rcon(cmd: str, *, timeout: float = 8.0) -> None:
            """Send a quiesce command. If it fails, it RAISES.

            Swallowing the error here would defeat two safeguards at once:

            * `create_backup` deliberately calls `pre_hook()` **outside** its `try`, so a raising
              hook aborts before an archive is created. If the error is swallowed, a torn
              snapshot of the world is created and logged as a successful backup. That includes
              the mandatory backups before applying mods and changing versions, i.e. exactly
              the archives meant as rollback points.
            * `asyncio.run` raises inside an already running loop. That is why the scheduled
              backup must use `asyncio.to_thread`; swallowed, a violation of that invariant
              would be invisible.
            """
            try:
                # The hook runs in the thread pool, so a loop of its own is fine.
                asyncio.run(self.console(server, cmd, timeout=timeout))
            except Exception as e:
                # A STOPPED server has nothing to quiesce: it is not writing and its files are
                # consistent. Without this distinction no backup of a stopped server would be
                # created at all. When in doubt (state cannot be queried) it counts as running
                # and the error is real.
                if not _running():
                    log.info("mc[%s]: %r skipped — the container is not running", server.key, cmd)
                    return
                log.error("mc[%s]: quiesce command %r failed: %s", server.key, cmd, e)
                raise RuntimeError(f"Quiescing Minecraft ({cmd}) failed: {e}") from e

        def _before_packing() -> None:
            _rcon("save-off")
            try:
                # 60 s: the save time scales with the loaded world, and the 8 s default would
                # make failure the normal case.
                _rcon("save-all flush", timeout=60.0)
            except BaseException as e:
                # `save-off` has already gone through. `create_backup` deliberately calls
                # `pre_hook()` OUTSIDE its `try`, but the `post_hook` (`save-on`) hangs on the
                # `finally` of exactly that `try` and is NEVER reached from here. Without this
                # undo the autosave of the RUNNING server would stay off until the next restart:
                # a failed backup would turn into silent world loss on every crash.
                try:
                    _rcon("save-on")
                except Exception as revert_error:
                    # The autosave now really stays off. That is worse than the failure that
                    # got us here, so it has to be reported more loudly: the message goes into
                    # the exception that `create_backup` passes on and `scheduled_backup`
                    # records as an incident. With `log.error` alone the graver of the two
                    # errors would be the one nobody sees.
                    log.error(
                        "mc[%s]: save-on failed after the failed flush — the autosave stays OFF "
                        "until the server restarts.", server.key,
                    )
                    raise RuntimeError(
                        f"Quiescing Minecraft (save-all flush) failed: {e} — and so did undoing "
                        f"it (save-on): {revert_error}. CAUTION: the autosave of this server is OFF "
                        f"until the next restart."
                    ) from revert_error
                raise

        # `level-name` is READ, not guessed. The key is env-managed precisely because it
        # determines these directory names (see `_MC_ENV_MANAGED`). With a hard-coded `world`
        # and `level-name=MyWorld` the backup would pack directories that do not exist;
        # `create_backup` skips missing directories SILENTLY and returns an archive consisting
        # of the bare gzip header, which then becomes the "latest backup", also as the mandatory
        # way back before applying mods and changing versions.
        #
        # The name can change without the config editor too: `container_env` passes `extra.env`
        # through unchecked, and itzg maps `LEVEL=<name>` onto `level-name`.
        world = self._level_name(server)
        base = posixpath.join(server.data_dir, "data")
        # Since Minecraft 26.1 the two side directories no longer exist. On the first start
        # Paper moves all dimensions to `<world>/dimensions/minecraft/{overworld,the_nether,
        # the_end}` (`WorldFolderMigration`) and removes `<world>_nether`/`<world>_the_end`.
        #
        # Hard-coded, the two would be dead paths from then on. The archive would stay complete
        # (missing entries are skipped and `<world>` now contains everything), but the "not a
        # single world directory" guard would depend on a single entry, and the definition would
        # name something that is no longer on disk.
        #
        # So this is decided by what is on disk rather than by version: a server on 1.21.x still
        # has the directories, one on 26.1+ does not, and both get the right answer.
        subdirs = tuple(
            [world] + [d for d in (f"{world}_nether", f"{world}_the_end")
                      if os.path.isdir(posixpath.join(base, d))]
        )
        return BackupSpec(
            data_dir=base,
            world_subdirs=subdirs,
            pre_hook=_before_packing,
            post_hook=lambda: _rcon("save-on"),
        )

    # --- Config: server.properties (file edit; needs the data directory mounted) ---
    def config_fields(self, server: GameServer) -> list[ConfigField]:
        path = posixpath.join(server.data_dir, "data", "server.properties")
        fields: list[ConfigField] = []
        try:
            with open(path, encoding="utf-8") as f:
                for line in f:
                    line = line.rstrip("\n")
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    k, v = line.split("=", 1)
                    managed = is_env_managed_key(k, _MC_ENV_MANAGED)
                    fields.append(ConfigField(
                        key=k, label=k, group="server.properties", env_managed=managed,
                        value=mask_if_secret(k, v) if managed else v,
                    ))
        except OSError:
            pass  # not mounted or similar → empty
        return fields

    def set_config(self, server: GameServer, updates: dict[str, str]) -> None:
        # Prevent key injection: a value or key with a line break would create additional lines
        # in server.properties, i.e. keys of their own (e.g. enable-rcon/rcon.password).
        # Validate before every write; a `=` in the value is fine (config_fields splits on the
        # first `=`).
        for k, v in updates.items():
            if any(c in str(k) for c in "\r\n=") or "\n" in str(v) or "\r" in str(v):
                raise DriverError(err(
                    "config.minecraft_invalid_entry",
                    "Invalid server.properties entry {key} (line breaks and '=' are not allowed in a key)",
                    key=repr(k),
                ))
        path = posixpath.join(server.data_dir, "data", "server.properties")
        try:
            lines = Path(path).read_text(encoding="utf-8").splitlines()
        except OSError as e:
            # Do NOT return silently: `server_ops.put_config` decides solely by whether
            # `set_config` gets through; a silent `return` becomes 200 + audit entry
            # `config.update` + `restart_required: true` for a change that never happened.
            # READING may come back empty (`config_fields` above), WRITING may not.
            raise DriverError(err(
                "config.minecraft_properties_unreadable",
                "server.properties cannot be read ({path}): {reason}", path=path, reason=str(e),
            )) from e
        seen: set[str] = set()
        out: list[str] = []
        for line in lines:
            if "=" in line and not line.startswith("#"):
                k = line.split("=", 1)[0]
                if k in updates:
                    out.append(f"{k}={updates[k]}")
                    seen.add(k)
                    continue
            out.append(line)
        out.extend(f"{k}={v}" for k, v in updates.items() if k not in seen)
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(out) + "\n")

    def sync_config_from_registry(self, server: GameServer, secret: str) -> list[str]:
        """Write what the registry owns into an EXISTING server.properties. Returns the keys set.

        The image writes `server-port` (from `SERVER_PORT`) and `rcon.password` (from
        `RCON_PASSWORD`) only when it creates server.properties on the first start; with
        `OVERRIDE_SERVER_PROPERTIES=false` it leaves an existing file alone (measured on
        2026.9.2: "server.properties already created and managed manually"). A port or password
        changed in the dashboard after that first start would reach the container env and
        nothing else: the game keeps listening on the old port behind the new published one,
        and RCON refuses the new password. So the edit path writes both into the file before
        the container is recreated. No file yet: nothing to do, the first start writes it.
        """
        from .base import game_port

        path = posixpath.join(server.data_dir, "data", "server.properties")
        if not os.path.isfile(path):
            return []
        updates = {"rcon.port": str(server.rcon_port or 25575), "enable-rcon": "true"}
        if (port := game_port(server)):
            updates["server-port"] = str(port)
        if secret:
            updates["rcon.password"] = secret
        self.set_config(server, updates)
        return sorted(updates)

    def ensure_config(self, server: GameServer, secret: str = "") -> None:
        """The same sync when a server is created and when its container is created.

        A server added again on a KEPT data directory finds the old server.properties, and with
        it the old `rcon.password`. The new container gets the new password in its env, the image
        leaves the existing file alone, and the game answers RCON only to the old one: the
        console says "authentication failed", "Back up now" fails at `save-off`, and a recreate
        rolls back because its boot check never gets through. Writing what the registry owns into
        the file before the container exists closes that gap. No file yet: nothing to do.
        """
        self.sync_config_from_registry(server, secret)

    # --- Mods/plugins: jars under /data/mods (Fabric/Forge) or /data/plugins (Paper) ---
    def list_mods(self, server: GameServer) -> dict:
        base = posixpath.join(server.data_dir, "data")
        result: dict = {"plugins": [], "mods": []}
        for kind in ("plugins", "mods"):
            try:
                result[kind] = sorted(f for f in os.listdir(posixpath.join(base, kind)) if f.endswith(".jar"))
            except OSError:
                pass
        return result

    def stop_timeout(self, server: GameServer) -> int:
        """90 s. The measured value was 0.3 s, but on an EMPTY world.

        On stop Minecraft writes all loaded chunks; the measurement ran against a server without
        players ("Saved 0 block chunks"), and the save time scales with what is loaded. 90 s cost
        nothing as long as the process exits by itself (`mc-server-runner` handles SIGTERM
        cleanly, exit code 0), and they are the reserve for a large, half-loaded world.
        """
        return 90

    # --- Moderation: vanilla RCON commands. Player names strictly [A-Za-z0-9_] (Java Edition) ---
    @staticmethod
    def _mc_name(username: str) -> str:
        return re.sub(r"[^A-Za-z0-9_]", "", username or "")

    def moderation_spec(self, server: GameServer) -> ModerationSpec:
        # Access level = op/deop (admin|none only); whitelist via whitelist.json.
        return ModerationSpec(
            kick=True, ban=True, ban_ip=True, unban=True,
            access_levels=("admin", "none"), whitelist=True,
            reasons={
                "access_levels": "Minecraft only knows operator or regular player — the Java "
                                 "Edition has no levels in between.",
                "kick": "Only works for someone who is online right now.",
            },
        )

    async def kick(self, server: GameServer, username: str, reason: str = "") -> str:
        u, r = self._mc_name(username), sanitize_message(reason)
        return await self.console(server, f"kick {u}" + (f" {r}" if r else ""))

    async def ban(self, server: GameServer, username: str, reason: str = "", ip: bool = False) -> str:
        u, r = self._mc_name(username), sanitize_message(reason)
        base = f"ban-ip {u}" if ip else f"ban {u}"
        return await self.console(server, base + (f" {r}" if r else ""))

    async def unban(self, server: GameServer, username: str) -> str:
        return await self.console(server, f"pardon {self._mc_name(username)}")

    async def set_access(self, server: GameServer, username: str, level: str) -> str:
        u = self._mc_name(username)
        return await self.console(server, f"op {u}" if level == "admin" else f"deop {u}")

    async def whitelist_add(self, server: GameServer, username: str) -> str:
        return await self.console(server, f"whitelist add {self._mc_name(username)}")

    async def whitelist_remove(self, server: GameServer, username: str) -> str:
        return await self.console(server, f"whitelist remove {self._mc_name(username)}")

    def whitelist_list(self, server: GameServer) -> dict:
        path = posixpath.join(server.data_dir, "data", "whitelist.json")
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"available": False, "users": []}
        users = [
            {"username": e.get("name", ""), "accesslevel": "", "banned": False, "steamid": str(e.get("uuid", ""))}
            for e in data
            if isinstance(e, dict)
        ]
        return {"available": True, "users": users}

    # --- Safe console verbs + overview figures ---
    def safe_console_commands(self) -> frozenset[str]:
        return frozenset({"list", "save-all", "say"})

    def overview_stats(self, server: GameServer) -> dict:
        max_players = None
        try:
            for f in self.config_fields(server):
                if f.key == "max-players":
                    max_players = int(f.value)
                    break
        except (ValueError, OSError):
            pass
        try:
            m = self.list_mods(server)
            mods_count = len(m.get("mods", [])) + len(m.get("plugins", []))
        except OSError:
            mods_count = None
        return {"max_players": max_players, "mods_count": mods_count}
