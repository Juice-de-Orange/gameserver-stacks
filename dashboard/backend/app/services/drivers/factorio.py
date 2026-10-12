"""FactorioDriver for the factoriotools/factorio image.

**What is special about this game, and what follows from it:**

* **The image tag IS the game version** (`factoriotools/factorio:2.0.77`), as with Project
  Zomboid → `VersionSource.image_tag`. Unlike PZ the tags are exact, and the `stable` and
  `latest` channels each exist as a tag series of their own.
* **Joining is stricter than with PZ:** server and client need *exactly* the same version or
  nobody gets in. A pinned server falls behind as soon as Steam updates the clients, which is
  why `auto_update` keeps Factorio current within its patch line.
* **A downgrade can destroy the world.** A map that has been loaded in a newer version cannot
  reliably be taken back. The world rollback of the version change is not a convenience here
  but the only way back.

**Control:** real Source RCON (27015/tcp, internal network only). The image reads the password
from `config/rconpw`; it has no env variable for it. `ensure_config` writes the file from the
`.env` secret; if `resolve_secret` finds nothing, `_rcon_secret` reads the file the image
generated itself, because that is the truth for the running server.

**Files** (volume `/factorio`, UID 845 in the regular image; the directory permissions on the
host have to match or the server does not start):

    config/server-settings.json   configuration (curated field table below)
    config/rconpw                 RCON password, NEVER in the config editor
    config/server-{admin,ban,white}list.json
    mods/                         mod zips + mod-list.json
    saves/                        the worlds (one .zip each)

**Uncalibrated, and marked as such:** the log patterns below come from the Factorio
documentation, not from a real server log. For Minecraft exactly this shortcut would break the
chat bridge (`[Server thread/INFO]:` is in the log file, `[20:36:01 INFO]:` on stdout, and
stdout is what gets read). Until the patterns have been checked against `docker logs`, an empty
event list is a possible result and no proof of a wrong pattern.
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
    JOIN_PASSWORD, AutoUpdate, BackupSpec, ConfigField, DriverError, DriverUnsupported, GameDriver,
    ModerationSpec,
    mask_if_secret, sanitize_message,
    check_console_response,
    QuiesceWarning, ServerEvent, VersionSource, coerce_value, json_get, json_set,
    write_config_file,
)

if TYPE_CHECKING:
    from ...models.server import GameServer

log = logging.getLogger("uvicorn.error")

# "   0.123 Factorio 2.0.77 (build 82345, linux64, headless)"
_VERSION_RE = re.compile(r"Factorio\s+(\d+\.\d+\.\d+)\s*\(build", re.I)

# Console lines of the server. The timestamp is Factorio's own (not Docker's, which
# `log_parser._TS_RE` strips beforehand) and may be missing; the ANCHOR is `^…[TAG]`.
#
# Without `^`, `search` would run over the whole line and a player could write
# "[JOIN] Victim joined the game" in chat, which would record a real join that ends up in
# session times and the leaderboard.
_F_PREFIX = r"^(?:\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} )?"

# DELIBERATELY no "chat" entry: chat is not a `PlayerEvent` (driver contract in `base.py`); it
# flows through `parse_chat` into the cross-server bus.
#
# And deliberately NO death pattern: Factorio does not write player deaths to the console. A
# guessed pattern would be worse than none; it would create events nobody can verify.
_F_EVENTS: list[tuple[re.Pattern, str]] = [
    (re.compile(_F_PREFIX + r"\[JOIN\]\s+(?P<u>.+?) joined the game"), "join"),
    (re.compile(_F_PREFIX + r"\[LEAVE\]\s+(?P<u>.+?) left the game"), "leave"),
]

# `[CHAT] Alice: text`. The bridge's own injection appears as `[CHAT] <server>: text`; a name in
# angle brackets is excluded (loop protection). It cannot be forged: `sanitize_message` removes
# `<` and `>` from everything the dashboard sends.
_F_CHAT_RE = re.compile(_F_PREFIX + r"\[CHAT\]\s+(?P<p>[^<:][^:]*):\s(?P<m>.+)$")

# "Online players (2):" followed by one line "  Alice (online)" each.
_F_PLAYERS_HEAD = re.compile(r"Online players\s*\((\d+)\)", re.I)
_F_PLAYERS_LINE = re.compile(r"^\s*(?P<u>.+?)\s*\(online\)\s*$", re.I)

# Config keys the editor must NOT write. Secrets are recognised by name in `mask_if_secret`;
# this is for structural values whose change would cut the dashboard off from its own server.
# Factorio has none (the ports are in the container start, not in the file). The constant stays
# so the next addition has a place and does not end up as a special case in the code.
_F_STRUCTURAL: frozenset[str] = frozenset()


def parse_factorio_players(out: str) -> dict:
    """Output of `/players online` → {"count": int, "names": [...]}.

    The header line gives the number, the following lines the names. Both are read and NOT
    reconciled: if the header says 3 and 2 names follow, the number from the header is the more
    honest figure. It comes from the server; the name list may be truncated.
    """
    text = out or ""
    header = _F_PLAYERS_HEAD.search(text)
    names: list[str] = []
    for line in text.splitlines():
        m = _F_PLAYERS_LINE.match(line)
        if m and not _F_PLAYERS_HEAD.search(line):
            names.append(m.group("u").strip())
    if header:
        return {"count": int(header.group(1)), "names": names}
    return {"count": len(names), "names": names}


def _lua_string(text: str) -> str:
    """Defuse text so it cannot break out of `game.print("…")`.

    `sanitize_message` already removes control characters, `"`, `;`, `<` and `>`. That leaves
    the backslash, which is dangerous here: a `\\` at the end would escape the closing quote and
    turn the rest of the line into Lua code.
    """
    return sanitize_message(text).replace("\\", "")

# Mods that belong to the game and are not files under `mods/`. They are still listed in
# `mod-list.json`; a preflight comparing files and list would otherwise report them as missing.
# Source: the factoriotools documentation (`DLC_SPACE_AGE`), which exempts them from mod updates.
_BUILTIN = frozenset({"base", "elevated-rails", "quality", "space-age"})


class FactorioDriver(GameDriver):
    game_type = GameType.factorio
    # The image tag is the version, as with PZ, only with exact tags.
    version_source = VersionSource.image_tag
    # Factorio compares the client version EXACTLY, so a stale pin locks everybody out.
    # 2.0.x follows automatically, 2.0 → 2.1 stays manual (a downgrade afterwards can destroy the world).
    auto_update_limit = AutoUpdate.patch

    # Curated config fields: (key, label, kind, group, env_managed, help).
    # The keys and their meaning come from `wube/factorio-data/server-settings.example.json`,
    # the vendor's authoritative template, not from a hosting wiki.
    _FIELDS: tuple[tuple[str, str, str, str, bool, str], ...] = (
        # --- Server ---
        ("name", "Server name", "str", "Server", False, "The name shown in the game list."),
        ("description", "Description", "str", "Server", False, ""),
        ("tags", "Tags", "list", "Server", False, ""),
        ("max_players", "Max. players", "int", "Server", False, "0 = unlimited; admins can join a full server as well."),
        ("visibility.public", "Listed publicly", "bool", "Server", False,
         "Needs a factorio.com account (user name + token). Without it the server runs but is "
         "never announced — join directly by address then."),
        ("visibility.lan", "Visible on the LAN", "bool", "Server", False, ""),
        ("require_user_verification", "Verified accounts only", "bool", "Server", False,
         "Only lets clients with a valid factorio.com account in."),
        # --- Game ---
        ("allow_commands", "Console commands allowed", "enum", "Game", False,
         "true, false or admins-only. Note: commands disable achievements."),
        ("auto_pause", "Pause without players", "bool", "Game", False, "Saves CPU when nobody is there."),
        ("auto_pause_when_players_connect", "Pause while a player connects", "bool", "Game", False, ""),
        ("only_admins_can_pause_the_game", "Only admins can pause", "bool", "Game", False, ""),
        ("afk_autokick_interval", "AFK auto-kick (min)", "int", "Game", False, "0 = never."),
        ("ignore_player_limit_for_returning_players", "Returning players despite the limit", "bool", "Game", False, ""),
        # --- Saving ---
        ("autosave_interval", "Autosave interval (min)", "int", "Saving", False, ""),
        ("autosave_slots", "Autosave slots", "int", "Saving", False, "Overwritten in rotation."),
        ("autosave_only_on_server", "Autosave on the server only", "bool", "Saving", False, ""),
        # --- Network ---
        ("max_upload_in_kilobytes_per_second", "Upload limit (kB/s)", "int", "Network", False, "0 = unlimited."),
        ("max_upload_slots", "Upload slots", "int", "Network", False, "0 = unlimited."),
        ("max_heartbeats_per_second", "Network tick rate", "int", "Network", False, "6 to 240, default 60."),
        ("minimum_latency_in_ticks", "Minimum latency (ticks)", "int", "Network", False, "One tick is 16 ms."),
        # The four segment sizes and `non_blocking_saving` complete the key set of
        # `wube/factorio-data/server-settings.example.json`.
        ("minimum_segment_size", "Min. segment size", "int", "Network", False,
         "Long network messages are sent across several ticks. Larger segments need more bandwidth "
         "and can cause disconnects on weak connections."),
        ("minimum_segment_size_peer_count", "…from how many players", "int", "Network", False,
         "From this number of players the minimum segment size applies."),
        ("maximum_segment_size", "Max. segment size", "int", "Network", False, ""),
        ("maximum_segment_size_peer_count", "…up to how many players", "int", "Network", False,
         "Up to this number of players the maximum segment size applies."),
        # --- Saving (continued) ---
        ("non_blocking_saving", "Non-blocking saving", "bool", "Saving", False,
         "The server forks to save instead of pausing briefly. The vendor explicitly calls it "
         "“highly experimental, enable only at your own risk of losing your saves” — and it only "
         "works on Linux. Off by default for good reason."),
        # --- Access ---
        ("game_password", "Join password", JOIN_PASSWORD, "Access", False,
         "Empty = anyone can join. Shown in plain text because every player knows it anyway."),
        ("username", "factorio.com user name", "str", "Access", False,
         "Only needed if the server is to be listed publicly."),
        ("token", "factorio.com token", "password", "Access", False,
         "From factorio.com/profile. Also needed for mod downloads from the portal."),
        # `password` from the vendor template is DELIBERATELY not listed. It is the password of
        # the factorio.com ACCOUNT, not of the server; whoever reads it can log in to
        # factorio.com as the account owner. The vendor explicitly names `token` as the
        # replacement ("May be used instead of 'password'"), and a token can be revoked on its
        # own. The editor is therefore exactly one key narrower than
        # `server-settings.example.json`, and that is a decision, not a gap.
        # `tests/test_factorio_config_covers_the_vendor_template.py` pins both.
    )

    def capabilities(self) -> frozenset[str]:
        # `announce` is included because `game.print` really reaches all players, unlike
        # Reforger, which has no way to do that.
        return frozenset({
            "console", "players", "events", "config", "mods", "backup", "backup-config-only",
            "versions", "announce",
        })

    # --- Console / RCON ---------------------------------------------------------------------
    def _rcon_secret(self, server: GameServer) -> str:
        """The RCON password: from the file the server reads, otherwise from the `.env`.

        `config/rconpw` is what the RUNNING server uses. If the `.env` says something else,
        because somebody changed it without recreating the container, the `.env` value would
        only produce errors. **So the file wins over the `.env`**; the reasoning is at
        `base.rcon_secret_from_file`, where the rule lives for Avorion and Factorio together.
        """
        from .base import rcon_secret_from_file

        try:
            from_file = Path(self._config_path(server, "rconpw")).read_text(encoding="utf-8").strip()
        except OSError:
            from_file = ""
        return rcon_secret_from_file(server, from_file)

    async def console(self, server: GameServer, command: str, *, timeout: float = 8.0) -> str:
        from ..rcon import rcon_command

        # `multi=False`, and that is measured, not assumed. The multi-packet path sends an empty
        # sentinel packet after the command and waits for the server to send it back. Factorio
        # does NOT echo it. Every command would run into the read timeout and take 8 seconds
        # instead of about 14 milliseconds, which matters for a poller that queries the player
        # count regularly.
        #
        # The reason for `multi` does not apply here anyway: a reply of 8999 characters arrived
        # complete in a single packet (`/c for i=1,900 do rcon.print(...) end`), because
        # `_read_packet` reads the length from the header. Minecraft needs that path, this game
        # does not.
        return await rcon_command(
            server.rcon_host, server.rcon_port, self._rcon_secret(server), command,
            timeout=timeout, multi=False,
        )

    async def players(self, server: GameServer) -> dict:
        return parse_factorio_players(await self.console(server, "/players online"))

    async def announce(self, server: GameServer, text: str) -> None:
        # `/silent-command` rather than `/command`: the latter writes the executed line itself to
        # the chat, so the announcement would appear twice, once as command and once as output.
        await self.console(server, f'/silent-command game.print("{_lua_string(text)}")')

    async def save(self, server: GameServer) -> None:
        check_console_response(
            await self.console(server, "/server-save", timeout=120.0),
            command="/server-save", game="Factorio",
        )

    async def graceful_shutdown(self, server: GameServer) -> bool:
        """Save first, then quit, both over RCON.

        The image handles SIGTERM cleanly, but the RCON path is deterministic: after
        `/server-save` the world is guaranteed to be on disk before anything is shut down.
        Errors are swallowed as with Minecraft; `lifecycle.stop_server` has a `docker stop`
        running in parallel.
        """
        try:
            await self.console(server, "/server-save", timeout=120.0)
        except Exception:
            pass  # deliberately broad: `/quit` has to be sent regardless
        try:
            await self.console(server, "/quit", timeout=30.0)
        except Exception:
            # A failed `/quit` is the NORMAL case here, not the error. `/quit` ends the server, so
            # its reply never arrives; and the `docker stop` running in parallel sends SIGTERM,
            # which this image answers by saving the map and exiting 0 ("Received SIGTERM,
            # shutting down", measured on 2.0.77), often before `/quit` is even sent. Either way
            # the clean end is the container going down by itself, so that is what decides.
            # `False` only when it is still running: then the console really did not
            # cooperate and the grace period may end in a hard kill (`lifecycle` logs that).
            return await self._goes_down(server)
        return True

    async def _goes_down(self, server: GameServer, *, within: int = 20) -> bool:
        """Has the container stopped running, or does it within about `within` seconds?

        An unreadable state counts as still running: the claim "shut down cleanly" needs proof.
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

    def connect_info(self, server: GameServer):
        """Factorio has no URL scheme for joining, so there is no URI."""
        from .base import ConnectInfo, game_port, game_proto, public_host

        data, reason = self._config_status(server)
        # A read error is UNKNOWN, not "no password". If an unreadable file were treated as an
        # empty dict, the join page would say "no password needed" for a server that has one,
        # and nothing would say that the file could not be read.
        has_password = None if reason else bool(str(json_get(data, "game_password") or "").strip())
        note = "In the game: “Multiplayer” → “Connect to address” → enter the address."
        if reason:
            note += f" CAUTION: {reason} — whether a join password is set is unknown."
        return ConnectInfo(
            host=public_host(server), port=game_port(server), protocol=game_proto(server),
            uri="",
            password_required=has_password,
            hint=note,
            browser_name=str(json_get(data, "name") or "") or server.display_name or server.key,
        )

    # --- Events & chat -----------------------------------------------------------------------
    def _lines(self, log_text: str):
        """Split off the Docker timestamp; without one "now" applies (as with MC/VS)."""
        import datetime as dt

        from ..log_parser import _TS_RE, _parse_ts

        for line in log_text.splitlines():
            line = line.strip()
            if not line:
                continue
            m = _TS_RE.match(line)
            if m:
                yield line, m.group("rest"), _parse_ts(m.group("ts"))
            else:
                yield line, line, dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)

    def parse_events(self, server: GameServer, log_text: str) -> list[ServerEvent]:
        out: list[ServerEvent] = []
        for raw, content, at in self._lines(log_text):
            for pattern, kind in _F_EVENTS:
                m = pattern.search(content)
                if m:
                    out.append(ServerEvent(kind=kind, username=m.group("u").strip(), at=at, raw=raw))
                    break
        return out

    def parse_chat(self, server: GameServer, log_text: str):
        import datetime as dt

        out: list[tuple[str, str, dt.datetime]] = []
        for _raw, content, at in self._lines(log_text):
            m = _F_CHAT_RE.search(content)
            if m:
                out.append((m.group("p").strip(), m.group("m").strip(), at))
        return out

    # --- Container ---------------------------------------------------------------------------
    def container_env(self, server: GameServer, secret: str) -> dict[str, str]:
        """The image is controlled through env, but the RCON password through a FILE.

        `secret` is therefore unused here; `ensure_config` writes it to `config/rconpw`.
        factoriotools has no `RCON_PASSWORD`, and an invented variable would do nothing while
        looking like a safeguard.
        """
        from .base import game_port

        extra = json.loads(server.extra or "{}")
        env = {
            "PORT": str(game_port(server) or 34197),
            "RCON_PORT": str(server.rcon_port or 27015),
            # Without this the server loads the last autosave after a restore instead of the
            # restored world. `SAVE_NAME` would be the alternative, but it requires the name in
            # the registry and on disk to always match.
            "LOAD_LATEST_SAVE": "true",
            "GENERATE_NEW_SAVE": "false",
            # Mod updates ONLY on request: an automatic run at start would change the mod set
            # without a preflight having seen it, and Factorio lets nobody in whose mod set
            # does not match exactly.
            "UPDATE_MODS_ON_START": "false",
        }
        for k, v in (extra.get("env") or {}).items():
            env[str(k)] = str(v)
        return env

    def container_user(self, server: GameServer) -> str:
        """The dashboard API's own `uid:gid` (`APP_UID`), but only for the `-rootless` image.

        The regular image runs as 845 and does a `chown -R` over the whole volume on every
        start; afterwards the API can no longer write `server-settings.json` and the config
        editor answers "Permission denied". Fixing permissions on the host lasts exactly until
        the next container start.

        Hence the `-rootless` variant of the image. It is made for this case and skips the root
        steps of the entrypoint; the regular image fails under a foreign uid at a `sed -i` in
        its own file system.

        **That is why the answer depends on the TAG.** With a plain tag such as `2.0.77` (which
        `POST /api/servers` accepts unchecked), a container created with the API's uid as `User` on the
        regular image would not come up, and nothing would say why.

        For the regular image `User` therefore stays empty and it runs as its own 845, as
        intended. The known price: the config editor cannot write there. A container that starts
        with a read-only config is better than one that does not start at all.
        """
        if not (server.image_tag or "").endswith("-rootless"):
            log.warning(
                "factorio[%s]: image tag %r is not the -rootless variant — the container runs "
                "as its own uid (845), and the config editor cannot write its files. For the full "
                "feature set change the tag to '<version>-rootless'.",
                server.key, server.image_tag,
            )
            return ""
        # The process's own ids, not a fixed 1000: the API image is built with `APP_UID`.
        return f"{os.getuid()}:{os.getgid()}"

    def container_binds(self, server: GameServer) -> list[str]:
        return [f"{server.data_dir}/data:/factorio"]

    def stop_timeout(self, server: GameServer) -> int:
        """120 s. On exit Factorio writes the complete map into a zip.

        For a grown factory that takes considerably longer than the 0.3 s Minecraft needed on an
        empty world, and a hard kill in the middle of writing leaves half a file that the next
        start will not load.
        """
        return 120

    # --- File paths --------------------------------------------------------------------------
    def _base(self, server: GameServer) -> str:
        return posixpath.join(server.data_dir, "data")

    def _config_path(self, server: GameServer, name: str) -> str:
        return posixpath.join(self._base(server), "config", name)

    def _config(self, server: GameServer) -> dict:
        """Read `server-settings.json`; an empty dict if it is missing or broken.

        READING may come back empty (no mount, fresh server); WRITING may not, see
        `_config_or_error`.

        **Callers that need to tell "missing" from "unreadable" use `_config_status`**;
        `connect_info` does, because a read error would otherwise turn into "no password
        needed".
        """
        return self._config_status(server)[0]

    def _config_status(self, server: GameServer) -> tuple[dict, str]:
        """`(data, reason)`; `reason` is empty as long as everything is fine.

        A MISSING file is not a reason: that is the fresh server, and an empty dict is the right
        answer for it. Every other error (permissions, broken JSON) IS one.
        """
        path = self._config_path(server, "server-settings.json")
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}, ""
        except OSError as e:
            return {}, f"server-settings.json cannot be read ({e})"
        except ValueError as e:
            return {}, f"server-settings.json is not valid JSON ({e})"
        if not isinstance(data, dict):
            return {}, "server-settings.json does not contain a JSON object"
        return data, ""

    def _config_or_error(self, server: GameServer) -> dict:
        try:
            data = json.loads(Path(self._config_path(server, "server-settings.json")).read_text(encoding="utf-8"))
        except OSError as e:
            raise DriverError(err(
                "config.factorio_settings_unreadable",
                "server-settings.json cannot be read: {reason}", reason=str(e),
            )) from e
        except ValueError as e:
            raise DriverError(err(
                "config.factorio_settings_invalid_json",
                "server-settings.json is not valid JSON: {reason}", reason=str(e),
            )) from e
        if not isinstance(data, dict):
            raise DriverError(err(
                "config.factorio_settings_not_an_object",
                "server-settings.json does not contain a JSON object.",
            ))
        return data

    # --- Backup ------------------------------------------------------------------------------
    def backup_spec(self, server: GameServer) -> BackupSpec:
        import asyncio

        def _running() -> bool:
            from ..docker_client import docker

            try:
                return bool(asyncio.run(docker.state(server.container_name)).get("running"))
            except Exception:
                return True   # when in doubt it is running and the quiesce error is real

        def _before_packing() -> None:
            """`/server-save`; a failure does NOT void the archive.

            The difference from Minecraft is how the world is built: there it is a directory the
            server keeps writing into during the tar run, and without `save-off` the archive is
            torn. Here every world is a self-contained zip file; running autosaves create new
            files instead of changing existing ones. A failed `/server-save` therefore costs at
            most the minutes since the last autosave. The archive stays usable, which is exactly
            what `QuiesceWarning` is for.
            """
            try:
                check_console_response(
                    asyncio.run(self.console(server, "/server-save", timeout=120.0)),
                    command="/server-save", game="Factorio",
                )
            except Exception as e:
                if not _running():
                    log.info("factorio[%s]: /server-save skipped — the container is not running", server.key)
                    return
                log.warning("factorio[%s]: /server-save failed: %s", server.key, e)
                raise QuiesceWarning(
                    f"The Factorio save command failed: {e}. The archive contains the "
                    f"autosaves up to this point and is usable."
                ) from e

        return BackupSpec(
            data_dir=self._base(server),
            # `config` and `mods` are listed HERE as well. `backup._pack` packs
            # `world_subdirs` only; `config_subdirs` is used solely by
            # `server_ops._resolve_parts` on RESTORE and has to be a subset (see the
            # `BackupSpec` docstring). Without them no archive would contain `mods/`,
            # `config/server-settings.json`, `config/rconpw` or the moderation lists, and a
            # Factorio world cannot be loaded without exactly the same mod set: the way back
            # after a version jump would be incomplete, and "configuration only" could only
            # answer 400. `test_backupspec_config_is_a_subset.py` pins the rule for all
            # drivers.
            world_subdirs=("saves", "config", "mods"),
            # Mods count as configuration here, not as world: a Factorio world cannot be loaded
            # without exactly the same mod set. Being able to restore them separately is
            # therefore the more important part of the "config only" button.
            config_subdirs=("config", "mods"),
            pre_hook=_before_packing,
        )

    # --- Config editor ------------------------------------------------------------------------
    def config_fields(self, server: GameServer) -> list[ConfigField]:
        data = self._config(server)
        fields: list[ConfigField] = []
        for key, label, kind, group, managed, help_text in self._FIELDS:
            raw = json_get(data, key)
            if kind == JOIN_PASSWORD:
                # Visible and in clear text; see the reasoning at `JOIN_PASSWORD` in base.py.
                fields.append(ConfigField(
                    key=key, label=label, kind=kind, value="" if raw is None else str(raw),
                    group=group, env_managed=managed, help=help_text,
                ))
                continue
            if kind == "password":
                # Write-only: the value never leaves the server, not even masked with its length.
                # Only WHETHER one is set is shown (in the help text).
                is_set = bool(str(raw or "").strip())
                fields.append(ConfigField(
                    key=key, label=label, kind="password", value="", group=group,
                    env_managed=managed,
                    help=(help_text + (" — currently set." if is_set else " — currently empty.")).strip(),
                ))
                continue
            if kind == "list":
                value = json.dumps(raw if isinstance(raw, list) else [], ensure_ascii=False)
            elif kind == "bool":
                value = "true" if raw is True else "false"
            else:
                value = "" if raw is None else str(raw)
            fields.append(ConfigField(
                key=key, label=label, kind=kind, value=mask_if_secret(key, value), group=group,
                env_managed=managed, choices=("true", "false", "admins-only") if key == "allow_commands" else (),
                help=help_text,
            ))
        return fields

    def set_config(self, server: GameServer, updates: dict[str, str]) -> None:
        data = self._config_or_error(server)
        kinds = {k: (kind, managed) for k, _l, kind, _g, managed, _h in self._FIELDS}
        changed = False
        for key, raw in updates.items():
            entry = kinds.get(key)
            if entry is None:
                continue          # unknown key; do not guess what was meant
            kind, managed = entry
            if managed or key in _F_STRUCTURAL:
                continue          # read-only
            if kind == JOIN_PASSWORD:
                # Empty REALLY means empty here: the user sees the current value and must be able
                # to clear it on purpose. Unlike `password`, where empty means "unchanged".
                json_set(data, key, str(raw or ""))
            elif kind == "password":
                # Empty means "unchanged", not "delete"; otherwise every save of the page would
                # wipe the password, because the field always arrives empty there.
                if not str(raw or "").strip():
                    continue
                json_set(data, key, str(raw))
            else:
                value = coerce_value(kind, raw)
                if kind == "int" and not isinstance(value, int):
                    # Reject loudly instead of skipping silently. `coerce_value("int", "")` does
                    # not raise but returns the raw value; skipping it would let `put_config`
                    # answer 200 with an audit entry, and the editor would report "saved" and
                    # adopt a value that was never written.
                    raise DriverError(err(
                        "config.driver_integer_expected",
                        "{key}: expected a whole number — {value} is not one. Nothing was written.",
                        key=key, value=repr(raw),
                    ))
                json_set(data, key, value)
            changed = True
        if not changed:
            return
        path = self._config_path(server, "server-settings.json")
        try:
            # With a backup copy and an atomic swap: a half-written `server-settings.json` is
            # unreadable JSON, and Factorio does not start with that at all.
            # 0600 like the `rconpw` next to it. This file holds the factorio.com token (listed
            # as a secret in `_FIELDS`) and `game_password`; with the umask default of 0644
            # every local account on a shared host could read both. The mode is set on EVERY
            # write, not only on creation; an existing file would otherwise keep its old
            # permissions forever.
            write_config_file(path, json.dumps(data, indent=2, ensure_ascii=False) + "\n",
                                  mode=0o600)
        except OSError as e:
            # Do NOT return silently: `server_ops.put_config` decides solely by this whether it
            # reports 200 + audit entry, for a change that never happened.
            raise DriverError(err(
                "config.factorio_settings_not_writable",
                "server-settings.json cannot be written ({path}): {reason}", path=path, reason=str(e),
            )) from e

    def ensure_config(self, server: GameServer, secret: str = "") -> None:
        """On creation: create missing files with usable values; never overwrite.

        Called from `routers/servers.create_server`, like `ReforgerDriver.ensure_config`. The
        difference from Reforger is `rconpw`: without this file the image generates a RANDOM
        password on the first start, and the dashboard would never reach the console.
        """
        cfg_dir = posixpath.join(self._base(server), "config")
        try:
            os.makedirs(cfg_dir, exist_ok=True)
            os.makedirs(posixpath.join(self._base(server), "mods"), exist_ok=True)
            os.makedirs(posixpath.join(self._base(server), "saves"), exist_ok=True)
        except OSError as e:
            raise DriverError(err(
                "config.factorio_data_dir_not_creatable",
                "The Factorio data directory cannot be created ({path}): {reason}",
                path=cfg_dir, reason=str(e),
            )) from e

        if secret:
            path = self._config_path(server, "rconpw")
            if not Path(path).exists():
                try:
                    Path(path).write_text(secret + "\n", encoding="utf-8")
                    # 0600 is enough because `container_user` runs the server under the same
                    # uid as the dashboard API (1000). With the `-rootless` variant the owner
                    # is the same, and group access would be more open than necessary.
                    os.chmod(path, 0o600)
                except OSError as e:
                    raise DriverError(err(
                        "config.factorio_rconpw_not_writable",
                        "rconpw cannot be written ({path}): {reason}", path=path, reason=str(e),
                    )) from e

        settings_file = self._config_path(server, "server-settings.json")
        if not Path(settings_file).exists():
            reason = {
                "name": server.display_name or server.key,
                "description": "",
                "tags": [],
                "max_players": 0,
                # Without a factorio.com account `public: true` would be a lie: the server
                # would never be announced but would look listed in the editor.
                "visibility": {"public": False, "lan": True},
                "username": "",
                "token": "",
                "game_password": "",
                "require_user_verification": False,
                "allow_commands": "admins-only",
                "autosave_interval": 10,
                "autosave_slots": 5,
                "auto_pause": True,
                "only_admins_can_pause_the_game": True,
            }
            try:
                write_config_file(
                    settings_file, json.dumps(reason, indent=2, ensure_ascii=False) + "\n",
                    mode=0o600,     # the factorio.com token and game_password are in here
                )
            except OSError as e:
                raise DriverError(err(
                    "config.factorio_settings_not_writable",
                    "server-settings.json cannot be written ({path}): {reason}",
                    path=settings_file, reason=str(e),
                )) from e

    # --- Mods --------------------------------------------------------------------------------
    def list_mods(self, server: GameServer) -> dict:
        """Installed mods from `mods/` plus the on/off state from `mod-list.json`.

        The built-in mods (the core and the Space Age mods) appear in `mod-list.json` but do not
        exist as files. They are reported in `builtin`, so a preflight does not flag them as
        "file missing".
        """
        base = posixpath.join(self._base(server), "mods")
        files: list[str] = []
        try:
            files = sorted(f for f in os.listdir(base) if f.endswith(".zip"))
        except OSError:
            pass
        active: dict[str, bool] = {}
        try:
            listing = json.loads(Path(posixpath.join(base, "mod-list.json")).read_text(encoding="utf-8"))
            for entry in (listing.get("mods") or []):
                if isinstance(entry, dict) and entry.get("name"):
                    active[str(entry["name"])] = bool(entry.get("enabled", True))
        except (OSError, ValueError, AttributeError):
            pass
        return {"files": files, "enabled": active, "builtin": sorted(_BUILTIN & set(active))}

    # --- Moderation ---------------------------------------------------------------------------
    def moderation_spec(self, server: GameServer) -> ModerationSpec:
        return ModerationSpec(
            kick=True, ban=True, ban_ip=False, unban=True,
            access_levels=("admin", "none"), whitelist=True,
            reasons={
                "ban_ip": "Factorio bans by account name, not by IP — there is no IP ban on the server side.",
                "kick": "Only works for someone who is online right now.",
                "access_levels": "Factorio only knows admin or regular player.",
            },
        )

    @staticmethod
    def _name(username: str) -> str:
        """Defuse a player name. Factorio allows many characters; whitespace and anything that
        could start a second statement still has to go."""
        return re.sub(r"[^\w.\-]", "", username or "")

    async def kick(self, server: GameServer, username: str, reason: str = "") -> str:
        u, r = self._name(username), sanitize_message(reason)
        return await self.console(server, f"/kick {u}" + (f" {r}" if r else ""))

    async def ban(self, server: GameServer, username: str, reason: str = "", ip: bool = False) -> str:
        if ip:
            raise DriverUnsupported(err("moderation.factorio_no_ip_ban", "Factorio has no IP ban."))
        u, r = self._name(username), sanitize_message(reason)
        return await self.console(server, f"/ban {u}" + (f" {r}" if r else ""))

    async def unban(self, server: GameServer, username: str) -> str:
        return await self.console(server, f"/unban {self._name(username)}")

    async def set_access(self, server: GameServer, username: str, level: str) -> str:
        u = self._name(username)
        return await self.console(server, f"/promote {u}" if level == "admin" else f"/demote {u}")

    async def whitelist_add(self, server: GameServer, username: str) -> str:
        return await self.console(server, f"/whitelist add {self._name(username)}")

    async def whitelist_remove(self, server: GameServer, username: str) -> str:
        return await self.console(server, f"/whitelist remove {self._name(username)}")

    def whitelist_list(self, server: GameServer) -> dict:
        """`server-whitelist.json` is a flat array of account names."""
        try:
            data = json.loads(Path(self._config_path(server, "server-whitelist.json")).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"available": False, "users": []}
        if not isinstance(data, list):
            return {"available": False, "users": []}
        return {
            "available": True,
            "users": [{"username": str(n), "accesslevel": "", "banned": False, "steamid": ""} for n in data],
        }

    # --- Details -----------------------------------------------------------------------------
    def elevated_config_keys(self, server: GameServer) -> frozenset[str]:
        """`allow_commands` opens the console including Lua to every player; that is not a
        setting.

        With `true` everybody connected may issue commands, `admins-only` restricts it to
        admins. Whoever flips this hands out game master power, which must not be covered by
        `can_edit_sandbox`, the permission to maintain game world settings.
        """
        return frozenset({"allow_commands"})

    def safe_console_commands(self) -> frozenset[str]:
        # Deliberately WITHOUT `/server-save`: it blocks the server while writing, and in safe
        # mode nobody should be able to stall the running game.
        #
        # `/whitelist` and `/banlist` are listed only with their READ subcommand. Bare, they are
        # administrative verbs (`whitelist_add` sends `/whitelist add <name>`, banning sends
        # `/banlist add <name>`) that belong to the `can_moderate` permission via
        # `routers/moderation.py`. Without the subcommand, `/whitelist clear` would be reachable
        # for anyone with `can_console_safe`.
        return frozenset({
            "/players", "/admins", "/time", "/seed", "/version",
            "/banlist get", "/whitelist get",
        })

    # --- Worlds / saves -----------------------------------------------------------------------

    _REMOVED = "saves.removed"

    def _saves_dir(self, server: GameServer) -> str:
        return posixpath.join(self._base(server), "saves")

    def _env(self, server: GameServer) -> dict[str, str]:
        """The stored container env. For the two keys that determine the world, the same
        defaults apply as in `container_env`; otherwise "not set" would mean something different
        from what it means when the container is created."""
        env = {"LOAD_LATEST_SAVE": "true", "SAVE_NAME": "_autosave1"}
        env.update(self.registry_env(server))
        return env

    def _set_env(self, server: GameServer, updates: dict[str, str]) -> None:
        env = self.registry_env(server)
        env.update({str(k): str(v) for k, v in updates.items()})
        self.write_registry_env(server, env)

    def world_saves(self, server: GameServer) -> list[dict]:
        """The `.zip` saves under `saves/`.

        **Which one is loaded is decided by the container env, not by the directory**:
        `LOAD_LATEST_SAVE=true` (the image default) takes the NEWEST, otherwise `SAVE_NAME`
        applies. Both are part of the answer, because "active" would otherwise be a mere claim.
        """
        import datetime as dt

        folder = self._saves_dir(server)
        env = self._env(server)
        newest_wins = str(env.get("LOAD_LATEST_SAVE", "true")).strip().lower() not in (
            "false", "0", "no", "off",
        )
        wanted = str(env.get("SAVE_NAME", "_autosave1")).strip()

        try:
            names = sorted(f for f in os.listdir(folder) if f.endswith(".zip"))
        except FileNotFoundError:
            return []
        except OSError as e:
            # Unreadable is not empty; otherwise "nothing here" would also be the answer when
            # we simply could not look inside.
            raise DriverError(err(
                "world.factorio_saves_unreadable",
                "The saves folder cannot be read ({path}): {reason}", path=folder, reason=str(e),
            )) from e

        saves: list[dict] = []
        for file in names:
            path = posixpath.join(folder, file)
            try:
                size = os.path.getsize(path)
                changed = dt.datetime.fromtimestamp(os.path.getmtime(path), dt.timezone.utc)
            except OSError:
                continue
            saves.append({
                "name": file[:-4],          # Factorio knows the save without `.zip`
                "size": size,
                "modified": changed.isoformat(),
                "files": (file,),
                "active": False,
            })
        if not saves:
            return []
        if newest_wins:
            newest = max(saves, key=lambda e: e["modified"])
            newest["active"] = True
            newest["reason"] = "newest save (LOAD_LATEST_SAVE is on)"
        else:
            for e in saves:
                if e["name"] == wanted:
                    e["active"] = True
                    e["reason"] = "SAVE_NAME"
        return saves

    def select_world(self, server: GameServer, name: str) -> str:
        matching = [e for e in self.world_saves(server) if e["name"] == name]
        if not matching:
            raise DriverError(err(
                "world.factorio_save_not_found", "“{name}” is not in the saves folder.", name=name,
            ))
        self._set_env(server, {"SAVE_NAME": name, "LOAD_LATEST_SAVE": "false"})
        return (
            f"“{name}” is set as the save (and “load the newest” is switched off, otherwise the "
            f"image default would have won). **This only takes effect when the container is "
            f"recreated** — a restart does not apply the container variables and would look as "
            f"if it had."
        )

    def remove_world(self, server: GameServer, name: str) -> dict:
        saves = {e["name"]: e for e in self.world_saves(server)}
        entry = saves.get(name)
        if entry is None:
            raise DriverError(err(
                "world.factorio_save_not_found", "“{name}” is not in the saves folder.", name=name,
            ))
        if entry["active"]:
            raise DriverError(err(
                "world.factorio_active_save_not_removable",
                "“{name}” is the save the server loads at its next start. Select another one first "
                "— otherwise it starts into a world that no longer exists.",
                name=name,
            ))
        from .base import set_save_aside

        return set_save_aside(
            self._saves_dir(server), posixpath.join(self._base(server), self._REMOVED),
            (f"{name}.zip",),
        )

    def overview_stats(self, server: GameServer) -> dict:
        data = self._config(server)
        raw = json_get(data, "max_players")
        try:
            max_players = int(raw) if raw is not None else None
        except (TypeError, ValueError):
            max_players = None
        try:
            mods = self.list_mods(server)
            # Built-in mods are not counted: they are not an installation anyone could undo,
            # and they would raise the number by the same three for every server.
            mods_count = len(mods.get("files") or [])
        except OSError:
            mods_count = None
        return {"max_players": max_players, "mods_count": mods_count}

    async def detect_version(self, server: GameServer) -> str:
        return await self._version_from_log(server, _VERSION_RE)
