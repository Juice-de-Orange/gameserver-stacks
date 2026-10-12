"""ValheimDriver for the ghcr.io/community-valheim-tools/valheim-server image.

**The odd one out: this server has no console.** No RCON, no stdin, no command interface. What
the dashboard learns about it comes from two sources, the Steam query (`services/a2s.py`) and the
container log. What it can tell the server goes through files and the container.

Consequences for the whole driver:

* `capabilities()` lists **no `console`** and **no `announce`**. The frontend greys the console
  out with a real reason instead of offering an input box in which nothing happens.
* `players()` queries A2S on the query port (game port + 1). If the query fails it RAISES;
  `idle_autostop`, `watchdog` and `auto_update` read that as "unknown, do not touch".
* `set_config` writes the **container env**, not a file. Hence `ConfigReload.recreate`: a
  restart would not apply the change and would look as if it had.
* Moderation works through `adminlist.txt` / `bannedlist.txt` / `permittedlist.txt`. There is no
  kick; that would need the console that does not exist.

**The version cannot be pinned, and that is a decision.** The image's `valheim-updater` runs
`update()` in the first iteration of its main loop, i.e. on EVERY container start, regardless of
`UPDATE_CRON`. The server is therefore always current and cannot fall behind the clients. The
price is on the other side: an update can break BepInEx mods at any time without anyone having
decided it. `VersionSource.runtime`: the version catalog greys the list out with a reason.

**Files** (volume `/config`):

    worlds_local/<WORLD_NAME>/            the world (Valheim 1.0+: _main.<n>.fwl2, .db2, chunks)
    worlds_local/<WORLD_NAME>.db + .fwl   an older world (the two belong together!)
    adminlist.txt, bannedlist.txt, permittedlist.txt
    backups/                              the image's own backups; SWITCHED OFF here, see `container_env`
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import os
from pathlib import Path
import posixpath
import re
from typing import TYPE_CHECKING, ClassVar

from ...errors import err
from ...models.server import GameType
from .base import (
    JOIN_PASSWORD, BackupSpec, ConfigField, ConfigReload, DriverError, DriverUnsupported, GameDriver,
    ModerationSpec,
    QuiesceWarning, ServerEvent, VersionSource, coerce_value, join_password_field,
)

if TYPE_CHECKING:
    from ...models.server import GameServer

log = logging.getLogger("uvicorn.error")

# Events from the container log. These lines are documented and used by countless bridge
# scripts; they still need calibrating against a real log.
#
# `Got character ZDOID from <Name> : <id>` appears on join AND on death. The difference is the
# ZDOID: `0:0` means died. Without that distinction every death would record a second join and
# the session times would be nonsense.
_V_ZDOID_RE = re.compile(r"Got character ZDOID from (?P<u>.+?)\s*:\s*(?P<zdo>-?\d+:-?\d+)")
_V_CONNECT_RE = re.compile(r"Got connection SteamID (?P<id>\d+)")
_V_DISCONNECT_RE = re.compile(r"Closing socket|RPC_Disconnect")

# The world is named after `WORLD_NAME`; without the value every backup definition would be a guess.
_DEFAULT_WORLD = "Dedicated"

# The log line after which Valheim answers the Steam query.
_OPENED_STEAM_SERVER = "Opened Steam server"


def _query_not_open_yet() -> Exception:
    """What to say before that line: an `A2sError` (so every consumer still reads "no answer")
    that carries a coded `detail`, so the reachability panel can show it translated."""
    from ..a2s import A2sError

    detail = err(
        "server.valheim_query_not_open_yet",
        "Valheim has not opened its Steam query yet. It does so once the world is ready (log line "
        "“Opened Steam server”); for a new world that is about a minute after “Game server "
        "connected”. Check again in a minute.",
    )
    error = A2sError(detail["message"])
    error.detail = detail
    return error

# `08/23/2026 20:20:31: Valheim version: l-0.221.12 (network version 36)`, as written by a
# running server. The `l-` is the platform prefix (linux) and not part of the version.
_VERSION_RE = re.compile(r"Valheim version:\s*[a-z]?-?(\d+\.\d+\.\d+)", re.I)


def parse_zdoid(line: str) -> tuple[str, bool] | None:
    """`(player name, is_death)` from a ZDOID line, or None.

    The distinction rests solely on the ZDOID `0:0`. It is the only one Valheim gives in the
    log, and without it every death would be an additional join.
    """
    m = _V_ZDOID_RE.search(line or "")
    if not m:
        return None
    return m.group("u").strip(), m.group("zdo") == "0:0"


class ValheimDriver(GameDriver):
    game_type = GameType.valheim
    # The image downloads the game via SteamCMD on EVERY start; nothing can be chosen.
    version_source = VersionSource.runtime
    # The configuration lives in the container env, which can only be set at creation.
    config_reload = ConfigReload.recreate
    # Proof of boot depends on the Steam query, not on "the container is running". The image
    # runs SteamCMD first at start; the container is long up while the game does not exist yet.
    # Without this switch the rollback after a broken mod change would practically never fire.
    boot_proof_via_players = True

    # Valheim updates itself INSIDE the running container. The image's `valheim-updater` runs in
    # the main loop and only restarts the game process after an update; the container stays the
    # same, `started_at` does not change, and the version cache in `base._version_from_log`
    # would never be invalidated. Because `version_source = runtime`, `detect_version` is the
    # ONLY source here: the versions page would show the number from container start forever.
    #
    # Ten minutes balance "read the full log" (expensive) against "the display freezes".
    version_cache_ttl: ClassVar[dt.timedelta | None] = dt.timedelta(minutes=10)

    # Curated fields. The key IS the name of the image's env variable; what is listed here ends
    # up in the container unchanged.
    _FIELDS: tuple[tuple[str, str, str, str, str], ...] = (
        ("SERVER_NAME", "Server name", "str", "Server", "The name shown in the Steam server browser."),
        ("WORLD_NAME", "World name", "str", "Server",
         "Determines the world under worlds_local: the folder <name>/ (every world since Valheim "
         "1.0) or, for an older world, the files <name>.db and .fwl. Changing it means: a "
         "DIFFERENT world — the old one stays on disk but is no longer loaded."),
        ("SERVER_PASS", "Join password", JOIN_PASSWORD, "Server",
         "At least 5 characters, otherwise the server does NOT START AT ALL. Shown in plain text "
         "because every player knows it anyway. Empty = open server."),
        ("SERVER_PUBLIC", "Listed publicly", "bool", "Server",
         "CAUTION, this is more than an entry in the server browser: when it is “off”, Valheim no "
         "longer answers Steam queries either — and those are the ONLY source this dashboard gets "
         "the player count and the proof of boot from. With “off” the server does listen on the "
         "query port but does not answer. A join password protects the server even when it is "
         "listed."),
        ("CROSSPLAY", "Crossplay", "bool", "Server",
         "Also allows non-Steam clients (Xbox/PC Game Pass)."),
        ("BEPINEX", "Load BepInEx", "bool", "Mods",
         "Mod loader. Without it files under BepInEx/plugins are not loaded."),
        ("SERVER_PORT", "Game port (UDP)", "int", "Server",
         "Valheim ALWAYS occupies two ports: this one and the next higher one for the Steam query. "
         "Changing it also means adjusting the port mapping of the container — otherwise the "
         "server can no longer be reached from outside."),

        # --- World modifiers (vendor guide "A Guide to Dedicated Servers") --------------------
        # They are NOT env variables of the image but start arguments. `SERVER_ARGS` passes them
        # through; `_build_args` builds the `-modifier`/`-setkey` chain from them.
        ("modifier.combat", "Combat", "enum", "World", ""),
        ("modifier.deathpenalty", "Death penalty", "enum", "World", ""),
        ("modifier.resources", "Resources", "enum", "World", ""),
        ("modifier.raids", "Raids", "enum", "World", ""),
        ("modifier.portals", "Portals", "enum", "World", ""),
        ("setkey.nobuildcost", "Building without materials", "bool", "World",
         "Creative building: build pieces cost nothing."),
        ("setkey.playerevents", "Events per player", "bool", "World",
         "Raids depend on the progress of the individual player instead of the world."),
        ("setkey.passivemobs", "Passive enemies", "bool", "World", "Enemies do not attack."),
        ("setkey.nomap", "No map", "bool", "World", "Map and minimap are switched off."),

        # --- Operation: the IMAGE's timers, not the dashboard's -------------------------------
        ("arg.saveinterval", "Save interval (sec.)", "int", "Operation",
         "How often the world is written. The vendor default is 1800 (30 min). Shorter means less "
         "loss in a crash and more disk load."),
        ("BACKUPS", "The image's own backups", "bool", "Operation",
         "Off by default of the DRIVER (`container_env`), not on as the image default would be. "
         "Reason: otherwise the image backs up hourly to `config/backups` — past the size limit "
         "`BACKUP_MAX_GB_PER_SERVER`, into the same volume. Switching it on gives a second, "
         "independent backup; it can only be restored on the host, though, not through this "
         "interface."),
        ("BACKUPS_CRON", "Schedule of the image backup", "str", "Operation",
         "Cron expression; the image default would be `5 * * * *` (hourly). Only takes effect when "
         "“The image's own backups” next to it is switched on."),
        ("BACKUPS_MAX_AGE", "Keep image backups (days)", "int", "Operation", ""),
        ("UPDATE_CRON", "Update check", "str", "Operation",
         "Cron expression; the image default `*/15 * * * *` applies here, the driver deliberately "
         "does NOT reset it. The image fetches Valheim updates itself — which is why there is no "
         "selectable version for this game (`version_source = runtime`). Empty switches the check "
         "off; the server then stays on the state it had at its last start while the clients move "
         "on."),
        ("UPDATE_IF_IDLE", "Update only without players", "bool", "Operation",
         "Postpones an update while somebody is playing. Strongly recommended."),
        ("RESTART_CRON", "The image's own restart", "str", "Operation",
         "Empty by default of the DRIVER, i.e. off. The image default would be `10 5 * * *` — a "
         "restart at 05:10 UTC that nobody planned and that the restart schedule of this dashboard "
         "does not know. Whoever enters something here has two clocks: the second start may then "
         "fall into the boot window of the first."),
        ("RESTART_IF_IDLE", "Restart only without players", "bool", "Operation", ""),
        ("PUID", "User ID in the container", "int", "Operation",
         "Set automatically to the dashboard API's own user id (APP_UID) so that the backup can "
         "read the files — with the image default 0 they belong to root and the backup fails on "
         "`config/backups`. Only change it if you know why."),
        ("PGID", "Group ID in the container", "int", "Operation", "See PUID."),
        ("TZ", "Time zone", "str", "Operation", "e.g. Europe/Berlin — affects the timestamps in the log."),
    )

    # The permitted values of the five world modifiers. Source: the vendor guide ("A Guide to
    # Dedicated Servers", section `-modifier`). Valheim accepts exactly these words and
    # otherwise starts with an error message in the log that nobody reads.
    _MODIFIER: ClassVar[dict[str, tuple[str, ...]]] = {
        "combat": ("veryeasy", "easy", "hard", "veryhard"),
        "deathpenalty": ("casual", "veryeasy", "easy", "hard", "hardcore"),
        "resources": ("muchless", "less", "more", "muchmore", "most"),
        "raids": ("none", "muchless", "less", "more", "muchmore"),
        "portals": ("casual", "hard", "veryhard"),
    }
    _SETKEYS: ClassVar[tuple[str, ...]] = ("nobuildcost", "playerevents", "passivemobs", "nomap")

    def capabilities(self) -> frozenset[str]:
        # Without `console` (there is none) and without `announce` (it would need one).
        # `backup-config-only` is included: the archive contains the moderation lists and the
        # BepInEx plugins, so there is a part that can be restored without the world.
        return frozenset({
            "players", "events", "config", "mods", "backup", "backup-config-only", "versions",
        })

    # --- Console: there is none, and that is stated -------------------------------------------
    async def console(self, server: GameServer, command: str, *, timeout: float = 8.0) -> str:
        """There is none.

        The method is abstract and has to exist, but it must not pretend anything. Returning an
        empty string would mean "command executed, no output", and the console in the dashboard
        would look functional while every command went nowhere.
        """
        raise DriverUnsupported(err(
            "console.valheim_none",
            "Valheim has no remote console: no RCON, no stdin. Moderation works through the list "
            "files, and there are no announcements.",
        ))

    async def announce(self, server: GameServer, text: str) -> None:
        # Deliberate no-op as with Reforger: do NOT inherit the PZ default `servermsg`, which
        # would run into a `NotImplementedError` from `console` here.
        return None

    async def save(self, server: GameServer) -> None:
        # Valheim saves by itself (every 20 minutes and on shutdown). There is no way to trigger
        # it; a call without effect would be worse than an honest no-op.
        return None

    # No `graceful_shutdown` of its own: the image traps SIGTERM and lets the server write its
    # world (there is no console to ask anyway). That is why it recommends `--stop-timeout 120`;
    # `stop_timeout` below provides exactly that, and `lifecycle` skips the console attempt.

    # --- Steam query: the only information this server gives ----------------------------------
    def _query_port(self, server: GameServer) -> int:
        """The Steam query port. From the registry, otherwise game port + 1.

        Valheim occupies `SERVER_PORT` and `SERVER_PORT+1`; the query uses the second. The
        fallback is not a guess but the game's documented behaviour. If `game_ports` has an
        `a2s` entry of its own (as with Reforger), that wins.
        """
        from .base import game_port

        try:
            spec = str(json.loads(server.game_ports or "{}").get("a2s") or "")
        except ValueError:
            spec = ""
        if spec:
            try:
                return int(spec.split("/")[0])
            except (ValueError, IndexError):
                pass
        return (game_port(server) or 2456) + 1

    async def loader_running(self, server: GameServer) -> bool | None:
        """Is the CONTAINER running with `BEPINEX=true`? `None` = cannot be determined.

        Unlike `list_mods()['loader_enabled']`, which reads the registry's `extra.env`, i.e. the
        INTENDED state. The container only gets its env at creation (`config_reload =
        recreate`); a `set_config` changes nothing on the running container. The mod preflight
        has to decide on the running side, or it cannot see the most misleading state of this
        game ("everything looks right and nothing takes effect").

        `None` means unknown; the caller reports it as a warning, not as clearance.
        """
        from ..docker_client import docker

        try:
            state = await docker.state(server.container_name)
        except Exception as e:      # noqa: BLE001 — proxy unreachable is "unknown", not "off"
            log.info("valheim[%s]: container env cannot be read (%s)", server.key, e)
            return None
        if not state.get("exists"):
            return None
        env = state.get("env")
        if not isinstance(env, dict) or "BEPINEX" not in env:
            return None
        return str(env["BEPINEX"]).strip().lower() in ("true", "1", "yes", "on")

    def _is_public(self, server: GameServer) -> bool:
        """Does this server answer Steam queries? The image default is `true`."""
        raw = str(self._env(server).get("SERVER_PUBLIC", "true")).strip().lower()
        return raw in ("true", "1", "yes", "on", "")

    def players_usable_as_boot_proof(self, server: GameServer) -> bool:
        """Only while the server answers Steam queries, i.e. with `SERVER_PUBLIC=true`.

        Measured three times: with `SERVER_PUBLIC=false` Valheim no longer answers A2S queries.
        `players()` depends on exactly that and then raises permanently. As proof of boot that
        would mean: `verify_boot` waits its full 900 s in vain and reports `False`, and
        `foreign_apply` rolls back a successful mod change on a healthy server, with a second
        900 s window afterwards and the final message that the server did not come up.

        The switch deliberately stays editable, so the boot check has to cope with it instead of
        the field being locked.
        """
        return self._is_public(server)

    def players_is_queryable(self, server: GameServer) -> bool:
        """The same condition as for proof of boot, here for the watchdog's liveness probe.

        `players()` depends on the Steam query, and `SERVER_PUBLIC=false` switches it off. The
        watchdog would count the failure as "game process hangs" (`A2sError` inherits from
        `RconError`) and restart a healthy server every 20 minutes.
        """
        return self._is_public(server)

    async def players(self, server: GameServer) -> dict:
        """Player count and names from the Steam query.

        The COUNT comes from A2S_INFO and is the reliable figure. The NAMES come from A2S_PLAYER,
        and Valheim does not always fill the name field, so a failure there is swallowed and
        only the list stays empty. A failure of the COUNT is passed on: `idle_autostop` and
        `auto_update` depend on it.
        """
        from ..a2s import A2sError
        from ..a2s import info as a2s_info
        from ..a2s import players as a2s_players

        port = self._query_port(server)
        try:
            # 3 s, below the 5 s of the reachability check that waits on this call: on a local
            # Docker network the answer takes milliseconds, and the explanation below can only
            # reach the panel when this call gives up first.
            info = await a2s_info(server.rcon_host or server.container_name, port, timeout=3.0)
        except A2sError as e:
            if await self._query_not_open_yet(server):
                raise _query_not_open_yet() from e
            raise
        names: list[str] = []
        try:
            names = [str(p.get("name") or "").strip()
                     for p in await a2s_players(server.rcon_host or server.container_name, port)]
            names = [n for n in names if n]
        except Exception as e:
            log.info("valheim[%s]: name list not available (%s) — the count still stands", server.key, e)
        return {"count": int(info.get("players") or 0), "names": names}

    async def _query_not_open_yet(self, server: GameServer) -> bool:
        """Is the container up, but has the game not opened its Steam query since it started?

        On the first start of a new world Valheim logs "Game server connected" (the line its
        image documents as "ready") well before it answers the Steam query: it generates the
        world first and logs "Opened Steam server" only then. Measured on image 1.4.0, game
        1.0.17: 76 s between the two lines, and the first A2S answer came in the same second as
        the second one (a restart of an existing world logs both within a second). A check in
        that window got a bare "no answer" that looked like a broken server. Best effort:
        anything unreadable is "no", and the original error stands.
        """
        import datetime as _dt

        from ..docker_client import docker

        try:
            state = await docker.state(server.container_name)
            if not state.get("running"):
                return False
            started = _dt.datetime.fromisoformat(str(state.get("started_at") or "")[:19])
            since = int(started.replace(tzinfo=_dt.timezone.utc).timestamp())
            text = await docker.logs(server.container_name, tail=5000, since=since)
        except Exception:
            return False
        return _OPENED_STEAM_SERVER not in text

    async def detect_version(self, server: GameServer) -> str:
        """The running game version, from the log, NOT from the Steam query.

        The obvious source is the wrong one: for Valheim A2S_INFO returns the string `1.0.0.0`
        as `version`, a constant that has nothing to do with the game version. Showing it would
        show the same wrong number forever, and nobody would notice because it looks plausible.

        The log has it right:
            `08/23/2026 20:20:31: Valheim version: l-0.221.12 (network version 36)`
        """
        return await self._version_from_log(server, _VERSION_RE)

    def connect_info(self, server: GameServer):
        from .base import ConnectInfo, game_port, game_proto, public_host

        from .. import secrets

        env = self._env(server)
        return ConnectInfo(
            host=public_host(server), port=game_port(server), protocol=game_proto(server),
            uri="",
            password_required=bool(secrets.resolve(server, secrets.JOIN).strip()),
            hint="In the game: “Join Game” → “Add Server” → enter the address with the port.",
            browser_name=str(env.get("SERVER_NAME", "")) or server.display_name or server.key,
        )

    # --- Events -------------------------------------------------------------------------------
    def parse_events(self, server: GameServer, log_text: str) -> list[ServerEvent]:
        """Join and death from `Got character ZDOID`; no leave events.

        A disconnect carries NO name: Valheim only logs the connection, not the character. A
        leave event without a name would be worthless for session accounting and misleading in
        the feed, so none is produced. Sessions are closed by the stats poller, which compares
        the A2S player list.
        """
        import datetime as dt

        from ..log_parser import _TS_RE, _parse_ts

        out: list[ServerEvent] = []
        # Whoever has just died does NOT join again; they respawn.
        #
        # Valheim writes "Got character ZDOID from <Name> : <id>" not only on connect but
        # whenever a character is assigned to the player, i.e. also on re-entry after every
        # death. The death itself is separated by `0:0`; the respawn right after it has to be
        # skipped too, or every death would be followed by a join that never happened.
        #
        # Known limit: the rule works within ONE block of text read. If death and respawn fall
        # into two fetches, the phantom join still occurs. The clean way would be `Got
        # connection SteamID` as the only join source (`_V_CONNECT_RE` exists), but that line
        # carries only the SteamID, no name, and a nameless join event is worthless for session
        # accounting. Until that has been measured on a running server (open point), the
        # respawn rule is the better approximation.
        died: set[str] = set()
        for line in log_text.splitlines():
            line = line.strip()
            if not line:
                continue
            m = _TS_RE.match(line)
            if m:
                at, content = _parse_ts(m.group("ts")), m.group("rest")
            else:
                at, content = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None), line
            if _V_DISCONNECT_RE.search(content):
                # Whoever disconnects really starts afresh at the next ZDOID. The name is not
                # in this line, so the whole set is cleared; for a server with ten slots that
                # is accurate enough.
                died.clear()
                continue
            parsed = parse_zdoid(content)
            if not parsed:
                continue
            name, is_death = parsed
            if is_death:
                died.add(name)
                out.append(ServerEvent(kind="death", username=name, at=at, raw=line))
            elif name in died:
                died.discard(name)     # respawn: no event, but the marker is reset
            else:
                out.append(ServerEvent(kind="join", username=name, at=at, raw=line))
        return out

    # --- Container ----------------------------------------------------------------------------
    def container_env(self, server: GameServer, secret: str) -> dict[str, str]:
        """The env IS this server's configuration.

        `secret` stays unused: there is no RCON password, so none to set. The join password is a
        config field (`SERVER_PASS`) and lives in its encrypted slot (`services/secrets.py`);
        `extra.env` keeps only an EMPTY `SERVER_PASS`, the statement "no password".
        """
        from .. import secrets
        from .base import game_port

        env: dict[str, str] = {
            "SERVER_PORT": str(game_port(server) or 2456),
            # Otherwise the image backs up HOURLY alongside the dashboard, with its own
            # retention, bypassing `BACKUP_MAX_GB_PER_SERVER` and into the same volume.
            "BACKUPS": "false",
            # The image default is a restart at 05:10 UTC that nobody planned and the
            # dashboard's restart schedule does not know about. The schedule here is the only one.
            "RESTART_CRON": "",
            # The same uid as the dashboard API. By default the image runs as root (PUID=0),
            # and all files under `/config` then belong to root: the backup fails with
            # "Permission denied" on `config/backups`, and the mod directory would be next.
            #
            # Deliberately via `PUID`/`PGID` and not `container_user` as for Factorio: the image
            # needs its root steps at start (supervisor, SteamCMD setup) and runs ONLY the game
            # process under this id. That is the way the image intends; `--user` would bypass it.
            # The process's own ids, not a fixed 1000: the API image is built with `APP_UID`.
            "PUID": str(os.getuid()),
            "PGID": str(os.getgid()),
            # Without this line the server no longer starts after a recreate. Valheim now
            # creates a world as a DIRECTORY (`worlds_local/<Name>/` with `_main.1.db2`,
            # `.fwl2`, `*.chunk`), while the image only knows the old file format. Its
            # `ensure_permissions` runs `chmod "$WORLDS_FILE_PERMISSIONS" "${worlds_dir}/"*` and
            # so puts the FILE mode (default `0666 & ~umask` = 0644) on the world DIRECTORY.
            # Without the x bit the server cannot get at its own world: IOException "Permission
            # denied" in `World.GetCreateWorld`, then endless "The WorldGenerator instance was
            # null". The process runs but NEVER listens on the game or query port; `docker ps`
            # shows it healthy, and on the HOST the query port even looks open, because that is
            # the docker-proxy. Measure INSIDE the container.
            #
            # A manual `chmod` does not last: `ensure_permissions` runs after EVERY server start
            # (`ensure_permissions_after_start &`) and on every update check, i.e. every
            # 15 minutes. A running server would lose write access to its world mid-game.
            "WORLDS_FILE_PERMISSIONS": "0755",
        }
        # Empty values belong IN the env. The image uses `SERVER_PASS=${SERVER_PASS-secret}`
        # (one hyphen, not `:-`): the default only applies when the variable is NOT set at all;
        # a set but empty value stays empty.
        #
        # Filtering empty values out here would break exactly that: clearing the join password
        # in the dashboard would not give the open server the UI promises but the image
        # password "secret", and nobody would see it, because the field would look correctly
        # empty afterwards.
        env.update({k: str(v) for k, v in self._env(server).items()})
        if (join := secrets.resolve(server, secrets.JOIN)):
            env["SERVER_PASS"] = join
        env.setdefault("SERVER_NAME", server.display_name or server.key)
        env.setdefault("WORLD_NAME", _DEFAULT_WORLD)
        # Deliberately "on" by default: without the listing the server does not answer Steam
        # queries, and the dashboard would have neither player count nor proof of boot, without
        # anything looking like an error. Whoever wants to hide the server switches it off in
        # the config editor and reads there what that costs.
        env.setdefault("SERVER_PUBLIC", "true")
        return env

    def container_binds(self, server: GameServer) -> list[str]:
        # `/opt/valheim` holds the game files downloaded by SteamCMD. Without the second bind
        # every container start downloads the whole game again.
        return [
            f"{server.data_dir}/config:/config",
            f"{server.data_dir}/server:/opt/valheim",
        ]

    def stop_timeout(self, server: GameServer) -> int:
        """120 s, exactly what the image recommends as `--stop-timeout`.

        Valheim writes its world on shutdown. A hard kill in the middle of it is the one way to
        really lose progress here.
        """
        return 120

    # --- Configuration: registry `extra.env` ---------------------------------------------------
    def _env(self, server: GameServer) -> dict[str, str]:
        try:
            extra = json.loads(server.extra or "{}")
        except ValueError:
            return {}
        values = extra.get("env")
        return {str(k): str(v) for k, v in values.items()} if isinstance(values, dict) else {}

    # --- World modifiers: they live in SERVER_ARGS, not in env variables of their own ---------
    #
    # The image knows `SERVER_NAME`, `WORLD_NAME`, `SERVER_PASS` and about twenty more
    # variables, but NONE for the world modifiers. The vendor sets them as start arguments
    # (`-modifier combat veryhard`, `-setkey nomap`, `-saveinterval 1200`), and `SERVER_ARGS` is
    # the one variable the image passes to the server unchanged.
    #
    # These fields therefore have pseudo keys (`modifier.combat`, `setkey.nomap`,
    # `arg.saveinterval`): the editor sees ordinary fields, `set_config` translates them into
    # the argument chain, and `container_env` passes them on with the rest. Offering
    # `SERVER_ARGS` as a free-text field instead would require the operator to remember the
    # exact spelling, and a typo there starts the server with an error message in the log that
    # nobody reads.

    @staticmethod
    def _split_args(raw: str) -> tuple[dict[str, str], list[str]]:
        """Split `SERVER_ARGS` into the known values and the untouched rest.

        **The rest is preserved.** Custom arguments are not lost because the dashboard writes a
        modifier.
        """
        known: dict[str, str] = {}
        rest: list[str] = []
        parts = (raw or "").split()
        i = 0
        while i < len(parts):
            t = parts[i]
            if t == "-modifier" and i + 2 < len(parts):
                known[f"modifier.{parts[i + 1].lower()}"] = parts[i + 2].lower()
                i += 3
            elif t == "-setkey" and i + 1 < len(parts):
                known[f"setkey.{parts[i + 1].lower()}"] = "true"
                i += 2
            elif t == "-saveinterval" and i + 1 < len(parts):
                known["arg.saveinterval"] = parts[i + 1]
                i += 2
            else:
                rest.append(t)
                i += 1
        return known, rest

    @classmethod
    def _build_args(cls, known: dict[str, str], rest: list[str]) -> str:
        parts = list(rest)
        for axis in cls._MODIFIER:
            value = (known.get(f"modifier.{axis}") or "").strip().lower()
            if value:
                parts += ["-modifier", axis, value]
        for key in cls._SETKEYS:
            if str(known.get(f"setkey.{key}", "")).strip().lower() in ("true", "1", "on", "yes"):
                parts += ["-setkey", key]
        interval = str(known.get("arg.saveinterval", "")).strip()
        if interval:
            parts += ["-saveinterval", interval]
        return " ".join(parts)

    def config_fields(self, server: GameServer) -> list[ConfigField]:
        env = self._env(server)
        from_args, _ = self._split_args(str(env.get("SERVER_ARGS", "")))
        fields: list[ConfigField] = []
        for key, label, kind, group, help_text in self._FIELDS:
            if key.startswith(("modifier.", "setkey.", "arg.")):
                axis = key.split(".", 1)[1]
                choices = tuple(self._MODIFIER.get(axis, ()))
                fields.append(ConfigField(
                    key=key, label=label, kind=kind,
                    value=from_args.get(key, "false" if kind == "bool" else ""),
                    group=group,
                    # Empty means "default" for a modifier; that has to be stated, otherwise an
                    # empty field reads like "not set, so broken".
                    choices=("",) + choices if choices else (),
                    help=help_text or ("Empty = the game's default setting." if choices else ""),
                ))
                continue
            raw = env.get(key)
            if kind == JOIN_PASSWORD:
                fields.append(join_password_field(server, key=key, label=label, group=group, help=help_text))
                continue
            if kind == "password":
                is_set = bool(str(raw or "").strip())
                fields.append(ConfigField(
                    key=key, label=label, kind="password", value="", group=group,
                    help=(help_text + (" — currently set." if is_set else " — currently empty.")).strip(),
                ))
                continue
            fields.append(ConfigField(
                key=key, label=label, kind=kind, value="" if raw is None else str(raw),
                group=group, help=help_text,
            ))
        return fields

    def set_config(self, server: GameServer, updates: dict[str, str]) -> None:
        """Writes `extra.env` in the registry, the only place Valheim's configuration lives.

        **Why the driver writes to the database here.** For all other games the configuration
        is a file the driver can touch. Valheim has none: the image is controlled solely through
        env variables, and those come from `container_env`, i.e. from the registry row. The
        driver stays stateless (it holds nothing); it just writes to the place this game has.

        This only takes effect when the container is RECREATED (`ConfigReload.recreate`); a
        restart would not apply the change and would look as if it had.
        """
        kinds = {k: kind for k, _l, kind, _g, _h in self._FIELDS}
        env = self._env(server)
        changed = False
        join_password: str | None = None
        # The argument pseudo keys are collected and written to `SERVER_ARGS` together at the
        # end; individually every change would rebuild the chain.
        from_args, arg_rest = self._split_args(str(env.get("SERVER_ARGS", "")))
        args_changed = False
        for key, raw in updates.items():
            kind = kinds.get(key)
            if kind is None:
                continue
            if key.startswith(("modifier.", "setkey.", "arg.")):
                value = str(raw or "").strip()
                if kind == "bool":
                    value = "true" if coerce_value("bool", raw) else ""
                if kind == "enum" and value:
                    axis = key.split(".", 1)[1]
                    allowed = self._MODIFIER.get(axis, ())
                    if value.lower() not in allowed:
                        # With an unknown value Valheim starts with an error message in the log
                        # while the container looks healthy. Catching it here is the only time
                        # anyone sees it.
                        raise DriverError(err(
                            "config.valheim_invalid_modifier",
                            "“{value}” is not a valid value for {axis}. Allowed: {allowed} (or empty "
                            "for the default setting).",
                            value=value, axis=axis, allowed=", ".join(allowed),
                        ))
                    value = value.lower()
                if kind == "int" and value and not value.isdigit():
                    raise DriverError(err(
                        "config.valheim_seconds_expected",
                        "{key} expects a number of seconds, not {value}.", key=key, value=repr(value),
                    ))
                if value:
                    from_args[key] = value
                else:
                    from_args.pop(key, None)
                args_changed = True
                continue
            if kind == JOIN_PASSWORD:
                value = str(raw or "")
                # Empty is a valid statement here ("open server"), not "unchanged".
                if value and len(value) < 5:
                    # The image does NOT start at all with a shorter password. A server that
                    # does not come up after saving is the more expensive surprise.
                    raise DriverError(err(
                        "config.valheim_password_too_short",
                        "Valheim requires a join password of at least 5 characters.",
                    ))
                if any(c in value for c in "\r\n\x00"):
                    raise DriverError(err(
                        "config.valheim_control_chars",
                        "Invalid value for {key}: control characters are not allowed in an env variable",
                        key=repr(key),
                    ))
                # Into the encrypted slot, never into `extra.env`; only an empty value stays
                # there, as the statement "no password" (`container_env`).
                join_password = value
                if value:
                    env.pop(key, None)
                else:
                    env[key] = ""
                changed = True
                continue
            elif kind == "bool":
                value = "true" if coerce_value("bool", raw) else "false"
            else:
                value = str(raw)
            if any(c in value for c in "\r\n\x00"):
                raise DriverError(err(
                    "config.valheim_control_chars",
                    "Invalid value for {key}: control characters are not allowed in an env variable",
                    key=repr(key),
                ))
            env[key] = value
            changed = True
        if args_changed:
            new = self._build_args(from_args, arg_rest)
            if new:
                env["SERVER_ARGS"] = new
            else:
                env.pop("SERVER_ARGS", None)
            changed = True
        if not changed:
            return
        if join_password is not None:
            from .. import secrets

            secrets.store_for(server, join_password, secrets.JOIN)
        # The JSON rewrite lives in `base.write_registry_env`, shared with Factorio's world
        # switch.
        self.write_registry_env(server, env)

    # --- World & backup -----------------------------------------------------------------------
    def _world_name(self, server: GameServer) -> str:
        return str(self._env(server).get("WORLD_NAME") or _DEFAULT_WORLD)

    def ensure_config(self, server: GameServer, secret: str = "") -> None:
        """On creation: create the directories and the three list files; never overwrite.

        The image creates the list files itself only on the first start. Without this the
        moderation page could not add anyone before the server had run once, because
        `_edit_list` would write into a directory that does not exist yet.

        **Empty files are the right default here, but they do not all say the same thing:**
        * an empty `adminlist.txt` means "nobody is admin",
        * an empty `bannedlist.txt` means "nobody is banned",
        * an empty `permittedlist.txt` does **NOT** mean "nobody may enter" but "the whitelist is
          off, everybody may". That is Valheim's quirk, and it is the right default here: a
          freshly created server should not be locked by accident.
        """
        base = posixpath.join(server.data_dir, "config")
        try:
            os.makedirs(base, exist_ok=True)
            os.makedirs(posixpath.join(base, "worlds_local"), exist_ok=True)
        except OSError as e:
            raise DriverError(err(
                "config.valheim_data_dir_not_creatable",
                "The Valheim data directory cannot be created ({path}): {reason}",
                path=base, reason=str(e),
            )) from e

        for file in ("adminlist.txt", "bannedlist.txt", "permittedlist.txt"):
            path = Path(posixpath.join(base, file))
            if path.exists():
                continue
            try:
                path.write_text("", encoding="utf-8")
            except OSError as e:
                raise DriverError(err(
                    "config.valheim_list_not_creatable", "{file} cannot be created ({path}): {reason}",
                    file=file, path=str(path), reason=str(e),
                )) from e

    def backup_spec(self, server: GameServer) -> BackupSpec:
        """The world **and** what nobody could otherwise bring back.

        Next to `worlds_local` the same directory holds the three list files (`adminlist.txt`,
        `bannedlist.txt`, `permittedlist.txt`) and, once BepInEx is on, `bepinex/` with the
        plugins. They have to be in the archive: a restore without them would bring the world
        back and then let **everybody in**. The `permittedlist` is the whitelist, and for Valheim
        an empty file is not an empty whitelist but none at all. The admin list would be gone as
        well. This weighs more for Valheim than for any other game, because there is no console:
        banning and permitting work exclusively through these files.

        `backups/` is explicitly NOT included: that is where the image puts its own backups and
        where the dashboard's archives live. Packing it would nest every backup in the next.

        The whole `worlds_local` directory is packed, not just the current world. A world is a
        directory of many files (Valheim 1.0 and later) or an inseparable `.db` + `.fwl` pair,
        and next to them are the earlier states the game itself rotates (`_main.<n>.*`, `.old`),
        exactly what one wants when the main file is broken.
        Packing a directory is also robust against a changed `WORLD_NAME`.
        """
        def _before_packing() -> None:
            """There is nothing to trigger, and that is stated rather than concealed.

            Valheim has no console, hence no `save`. The game rotates `.db.old` as a complete
            state when saving; an archive taken during a write can catch the main file half
            written, and the `.old` next to it is then the intact one. This warning does NOT
            void the backup (`QuiesceWarning`); it only says what one is relying on.
            """
            raise QuiesceWarning(
                "Valheim has no save command from outside: the archive is a copy taken while the "
                "server is running. The .old files next to it are the consistent state.",
                # Structural, not failed, so no incident is created from it. The hook checks no
                # state and can never succeed; recorded as a fault it would put a red line into
                # the server view on EVERY backup.
                structural=True,
            )

        return BackupSpec(
            data_dir=posixpath.join(server.data_dir, "config"),
            # Every archive of this server is taken while it is stopped: the weekly and the
            # manual backup stop it (`backup.backup_with_stop`), the scheduled restart packs in
            # its own stop (`backup.restart_with_backup_while_stopped`), and recreate, restore
            # and mod changes stop first anyway (`lifecycle.stop_and_snapshot`). A configuration
            # write takes no archive: it only reaches the container through a recreate, which
            # backs up after its stop. The hook below can never save; it stays as the honest
            # answer for any path that would pack the running server.
            stop_for_consistency=True,
            # `bepinex` is missing while BEPINEX is off; `_pack` silently skips what is missing.
            world_subdirs=(
                "worlds_local",
                "bepinex",
                "adminlist.txt",
                "bannedlist.txt",
                "permittedlist.txt",
            ),
            # Valheim's GAME configuration lives in the container env and cannot be in the
            # archive at all. What is listed here is everything else that is not world content:
            # moderation lists and mods. Exactly what one wants to restore individually after a
            # world loss, without touching the world.
            config_subdirs=("bepinex", "adminlist.txt", "bannedlist.txt", "permittedlist.txt"),
            pre_hook=_before_packing,
        )

    # --- Mods: BepInEx files -------------------------------------------------------------------
    def _mods_dir(self, server: GameServer) -> str:
        return posixpath.join(server.data_dir, "config", "bepinex", "plugins")

    def list_mods(self, server: GameServer) -> dict:
        """Plugin files under `bepinex/plugins`.

        Whether BepInEx is loaded at all is decided by the env variable `BEPINEX`; files alone
        do nothing. So the switch is part of the answer: a list full of plugins on a server
        with `BEPINEX=false` would otherwise be the most misleading display of all.
        """
        active = str(self._env(server).get("BEPINEX", "")).strip().lower() in ("true", "1", "yes", "on")
        try:
            files = sorted(f for f in os.listdir(self._mods_dir(server)) if f.endswith(".dll"))
        except OSError:
            files = []
        return {"plugins": files, "loader_enabled": active}

    # --- Moderation through files --------------------------------------------------------------
    def _list(self, server: GameServer, name: str) -> list[str]:
        """Read one of the list files.

        **Only a MISSING file is empty.** Any other read error (permissions, IO) raises;
        otherwise "I cannot look" turns into "nobody is listed", which is the most dangerous
        answer of all for the `permittedlist`: it reaches the UI as "whitelist active, zero
        entries" while the server in fact lets everybody in. On write it would be even more
        expensive: `_edit_list` would overwrite the existing entries with an empty list.
        """
        path = Path(posixpath.join(server.data_dir, "config", name))
        try:
            text = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return []
        except OSError as e:
            raise DriverError(err(
                "moderation.list_file_unreadable", "{file} cannot be read ({path}): {reason}",
                file=name, path=str(path), reason=str(e),
            )) from e
        return [z.strip() for z in text.splitlines() if z.strip() and not z.strip().startswith("//")]

    def _write_list(self, server: GameServer, name: str, entries: list[str]) -> None:
        path = posixpath.join(server.data_dir, "config", name)
        try:
            os.makedirs(posixpath.dirname(path), exist_ok=True)
            Path(path).write_text("\n".join(entries) + ("\n" if entries else ""), encoding="utf-8")
        except OSError as e:
            raise DriverError(err(
                "moderation.list_file_not_writable", "{file} cannot be written ({path}): {reason}",
                file=name, path=path, reason=str(e),
            )) from e

    @staticmethod
    def _id(username: str) -> str:
        """Valheim lists contain platform ids, not names, and that is CHECKED here.

        The format is `Steam_76561198…` or the bare SteamID64 (17 digits). A filter on non-word
        characters is not enough: a player name consists of word characters only, so "Bjorn"
        would end up in `bannedlist.txt` unchanged. Valheim matches these files against platform
        ids only; the entry would be a dead string, the audit entry would exist anyway, and the
        banned player would get back in on the next join.
        """
        raw = (username or "").strip()
        m = re.fullmatch(r"(?:Steam_)?(\d{7,20})", raw, re.I)
        if not m:
            raise DriverError(err(
                "moderation.valheim_not_a_platform_id",
                "{value} is not a platform ID. Valheim matches its lists exclusively against "
                "Steam_76561198… or the bare SteamID64 — a player name would have no effect but, as "
                "an entry, could not be told apart from one that works.",
                value=repr(raw),
            ))
        # Always write with the prefix: both forms are allowed, but a file with mixed forms can
        # no longer be compared reliably.
        return f"Steam_{m.group(1)}"

    def moderation_spec(self, server: GameServer) -> ModerationSpec:
        return ModerationSpec(
            kick=False, ban=True, ban_ip=False, unban=True,
            access_levels=("admin", "none"), whitelist=True,
            reasons={
                "kick": "Valheim cannot kick from outside — that would need a console, and the "
                        "dedicated server has none. A ban still works.",
                "ban": "What gets listed is the platform ID (Steam_76561198… or the bare "
                       "SteamID64), not the player name — a name would have no effect.",
                "ban_ip": "Valheim keeps no IP list.",
                "whitelist": "The permittedlist only takes effect when it is not empty: an empty "
                             "file lets everybody in.",
                "access_levels": "Admin means: an entry in the adminlist. There are no levels in between.",
            },
        )

    async def ban(self, server: GameServer, username: str, reason: str = "", ip: bool = False) -> str:
        if ip:
            raise DriverUnsupported(err("moderation.valheim_no_ip_list", "Valheim keeps no IP list"))
        return self._edit_list(server, "bannedlist.txt", username, add=True)

    async def unban(self, server: GameServer, username: str) -> str:
        return self._edit_list(server, "bannedlist.txt", username, add=False)

    async def set_access(self, server: GameServer, username: str, level: str) -> str:
        return self._edit_list(server, "adminlist.txt", username, add=(level == "admin"))

    async def whitelist_add(self, server: GameServer, username: str) -> str:
        return self._edit_list(server, "permittedlist.txt", username, add=True)

    async def whitelist_remove(self, server: GameServer, username: str) -> str:
        return self._edit_list(server, "permittedlist.txt", username, add=False)

    def whitelist_list(self, server: GameServer) -> dict:
        """The whitelist, and whether it has any EFFECT.

        An empty `permittedlist.txt` lets EVERYBODY in, not nobody. Without `note` that state
        would look like the most closed one in the UI ("whitelist active, zero entries") while
        the server is wide open.
        """
        entries = self._list(server, "permittedlist.txt")
        banned = set(self._list(server, "bannedlist.txt"))
        admins = set(self._list(server, "adminlist.txt"))
        return {
            "available": True,
            "note": (
                "" if entries else
                "permittedlist.txt is empty — that opens the server to EVERYONE who has the "
                "address and (if set) the join password. The whitelist only takes effect from the "
                "first entry on."
            ),
            "users": [
                {"username": e, "accesslevel": "admin" if e in admins else "",
                 "banned": e in banned, "steamid": e}
                for e in entries
            ],
        }

    def _edit_list(self, server: GameServer, file: str, username: str, *, add: bool) -> str:
        """Add or remove an entry in one of the list files.

        **When this takes effect has not been measured.** Whether Valheim re-reads the lists
        while running or only at start is unverified. The reply therefore states the
        conservative case, "at the latest after the next restart".
        """
        value = self._id(username)
        entries = self._list(server, file)
        if add:
            if value in entries:
                return f"{value} is already in {file}."
            entries.append(value)
        else:
            if value not in entries:
                return f"{value} is not in {file}."
            # Removing the LAST whitelist entry would open the server to everybody.
            #
            # The permittedlist only has an effect when it is not empty: an empty file lets
            # everybody in. Writing a 0-byte file here would announce a TIGHTENING ("removed
            # from permittedlist.txt") while doing the opposite. The lists are part of every
            # backup (`backup_spec`), but a backup is yesterday's way back, not a substitute
            # for a button that does what it says.
            #
            # Hence fail-closed. Whoever really wants to switch the access protection off does
            # so deliberately on the host, not with the button called "remove a player".
            if file == "permittedlist.txt" and len(entries) == 1:
                raise DriverError(err(
                    "moderation.valheim_last_whitelist_entry",
                    "{value} is the last entry of the whitelist. Emptying it would open the server "
                    "to EVERYONE — an empty permittedlist.txt lets everybody in instead of nobody. "
                    "To lift the access protection, delete the file on the host; to keep it, add "
                    "the next permitted player first.",
                    value=value,
                ))
            entries = [e for e in entries if e != value]
        self._write_list(server, file, entries)
        return (
            f"{value} {'added to' if add else 'removed from'} {file} — "
            f"takes effect at the latest after the next restart of the server."
        )

    # --- Worlds / saves -----------------------------------------------------------------------

    def _worlds_dir(self, server: GameServer) -> str:
        return posixpath.join(server.data_dir, "config", "worlds_local")

    def world_saves(self, server: GameServer) -> list[dict]:
        """The worlds under `worlds_local/`, in both formats the game writes.

        * **Directory format** (Valheim 1.0 and later, every new world): `<Name>/` holding
          `_main.<n>.fwl2` (seed and metadata), `_main.<n>.db2` (the world, written at the first
          save) and chunk files. The directory is ONE unit; a directory without any `.fwl2` is
          no world. A brand-new world has only its `.fwl2` until the first save (measured on
          image 1.4.0, game 1.0.17), and it is still the world the server runs.
        * **Pair format** (older worlds): `<Name>.db` (the world) and `<Name>.fwl` (its
          metadata). They belong together inseparably, so the search is for `.fwl` with the `.db`
          added, not the other way round: a `.db` without `.fwl` is not a loadable world.

        A name present in both formats (an old world the game converted) is one entry with all
        its files, so selecting or removing it acts on everything that carries the name.

        The `.old` states next to the pair and the game's `_backup_auto` copies are DELIBERATELY
        not entries of their own. They are the backup state of the same world, not a second
        save; as a separate row they would invite loading "the old world" and overwriting the
        current one.
        """
        import datetime as dt

        folder = self._worlds_dir(server)
        active = str(self._env(server).get("WORLD_NAME", _DEFAULT_WORLD)).strip()
        try:
            entries = os.listdir(folder)
        except FileNotFoundError:
            return []
        except OSError as e:
            raise DriverError(err(
                "world.valheim_worlds_unreadable",
                "The worlds folder cannot be read ({path}): {reason}", path=folder, reason=str(e),
            )) from e

        found: dict[str, list[str]] = {}
        for entry in sorted(entries):
            if "_backup_auto" in entry:
                continue          # the game's automatic backups, not a world of their own
            path = posixpath.join(folder, entry)
            if os.path.isdir(path) and not os.path.islink(path):
                try:
                    inside = os.listdir(path)
                except OSError:
                    continue
                if any(f.endswith(".fwl2") for f in inside):
                    found.setdefault(entry, []).append(entry)
                continue
            if not entry.endswith(".fwl"):
                continue
            name = entry[:-4]
            if os.path.exists(posixpath.join(folder, f"{name}.db")):
                found.setdefault(name, []).extend([f"{name}.db", f"{name}.fwl"])
            # metadata only, no world

        saves: list[dict] = []
        for name in sorted(found):
            files = tuple(found[name])
            size = 0
            newest = 0.0
            for unit in files:
                path = posixpath.join(folder, unit)
                paths = [path]
                if os.path.isdir(path):
                    paths = [posixpath.join(path, f) for f in os.listdir(path)]
                for one in paths:
                    try:
                        size += os.path.getsize(one)
                        newest = max(newest, os.path.getmtime(one))
                    except OSError:
                        continue
            saves.append({
                "name": name,
                "size": size,
                "modified": dt.datetime.fromtimestamp(newest, dt.timezone.utc).isoformat(),
                "files": files,
                "active": name == active,
                "reason": "WORLD_NAME" if name == active else "",
            })
        return saves

    def select_world(self, server: GameServer, name: str) -> str:
        if not [e for e in self.world_saves(server) if e["name"] == name]:
            raise DriverError(err(
                "world.valheim_world_not_found", "“{name}” is not in the worlds folder.", name=name,
            ))
        env = self.registry_env(server)
        env["WORLD_NAME"] = name
        self.write_registry_env(server, env)
        return (
            f"“{name}” is set as the world. **This only takes effect when the container is "
            f"recreated** — for Valheim the whole configuration lives in the container env, and "
            f"a restart does not apply it. Until then the old world keeps running while the new "
            f"one is already shown here."
        )

    def remove_world(self, server: GameServer, name: str) -> dict:
        saves = {e["name"]: e for e in self.world_saves(server)}
        entry = saves.get(name)
        if entry is None:
            raise DriverError(err(
                "world.valheim_world_not_found", "“{name}” is not in the worlds folder.", name=name,
            ))
        if entry["active"]:
            raise DriverError(err(
                "world.valheim_active_world_not_removable",
                "“{name}” is the configured world. Select another one first — otherwise the server "
                "creates a NEW, empty world under this name at its next start, and that looks like "
                "data loss.",
                name=name,
            ))
        from .base import set_save_aside

        return set_save_aside(
            self._worlds_dir(server),
            posixpath.join(server.data_dir, "config", "worlds_local.removed"),
            entry["files"],
        )

    def overview_stats(self, server: GameServer) -> dict:
        mods = self.list_mods(server)
        return {
            "max_players": 10,      # Valheim is hard-capped at ten; there is no setting
            "mods_count": len(mods["plugins"]) if mods["loader_enabled"] else None,
        }
