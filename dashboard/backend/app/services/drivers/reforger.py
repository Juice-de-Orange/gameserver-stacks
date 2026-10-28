"""ReforgerDriver for the Arma Reforger dedicated server.

Reforger differs from PZ/MC/VS in three ways that shape this module:

1. **Config = ONE JSON file** (`config.json`). The dashboard OWNS it ("mounted config"): the
   acemod image reads it through `ARMA_CONFIG` **unchanged** (bypassing env templating). This
   mirrors the VS model (the dashboard owns `serverconfig.json`) and avoids PZ's ".ini versus
   env" conflict. Mods are listed as `game.mods = [{modId,name,version}]`; the SERVER downloads
   them itself (no steamcmd workshop step as with PZ). The game mode is `game.scenarioId`.
2. **RCON = Bohemia's UDP BattlEye protocol** (NOT Source RCON) → `console()` goes through
   `services/bercon.py`, not `rcon_command`. Commands are prefixed with `#`. Kick/ban use the
   VOLATILE `#players` id (not the name) → name→id lookup, the target has to be online.
3. **No server broadcast** over RCON (no `say`/`servermsg` verb) → `announce()`/`save()` are
   deliberate no-ops (like VS `save`), and the announcements UI is greyed out for Reforger.

File access (config.json/mods/saves) requires the server's data directory to be mounted into the
API container. All file paths are best effort (no mount → empty, no crash). The player and ban
list parsers are **not yet calibrated** against real RCON output.

Container paths (acemod, ghcr.io/acemod/arma-reforger): config `/reforger/Configs`, saves/`.db`
`/home/profile`, workshop mods `/reforger/workshop`, the game itself (about 10 GB, fetched by
SteamCMD at every start) `/reforger`, SteamCMD `/steamcmd`. All of them are binds into the data
directory (`container_binds`), so a recreate keeps the installed game.

**Runs as root.** The image has no PUID/PGID and its launcher (`/app/launch.py`) runs SteamCMD
and the server as root, so everything it writes under the data directory belongs to root.
Moving saves between mode slots and deleting the data therefore go through short-lived root
helper containers (`routers/server_ops._run_save_swap`, `provisioning.delete_tree_as_root`).
"""
from __future__ import annotations

import json
import os
import posixpath
import re
from typing import TYPE_CHECKING, ClassVar

from ...errors import err
from ...models.server import GameType
from .base import (
    JOIN_PASSWORD, BackupSpec, ConfigField, DriverError, GameDriver, ModerationSpec, ServerEvent,
    VersionSource, coerce_value, json_get, json_set, sanitize_message,
)

if TYPE_CHECKING:
    from ...models.server import GameServer

# --- `#players` output: (id, name) pairs. The columns drift between versions and still need
# calibrating. Heuristic: the line starts with a numeric id, then the name up to the next column
# separator (2+ spaces).
_RF_PLAYER_RE = re.compile(r"^\s*(?P<id>\d+)\s+(?P<name>\S.*?)\s{2,}", re.M)
# --- `#ban list`: (index, rest of line). The format needs calibrating as well.
_RF_BAN_RE = re.compile(r"^\s*(?P<id>\d+)\s+(?P<rest>\S.*)$", re.M)

# --- Log events (Docker container log): join/leave, best effort. HAS to be calibrated against
# real Reforger logs (like the MC/VS parsers). Kept conservative → when in doubt NO (false) hit.
_RF_EVENTS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\bPlayer\b.*?\bname[=:\s\"']+(?P<u>[\w.\- ]{1,32}?)[\"']?\s.*\b(?:connected|joined)\b", re.I), "join"),
    (re.compile(r"\bPlayer\b.*?\bname[=:\s\"']+(?P<u>[\w.\- ]{1,32}?)[\"']?\s.*\b(?:disconnected|left)\b", re.I), "leave"),
]


# Host folders below the data directory for the installed game and SteamCMD (`container_binds`).
_INSTALL = "install"
_STEAMCMD_IN_IMAGE = "/steamcmd"


def parse_reforger_players(out: str) -> list[tuple[str, str]]:
    """(id, name) pairs from the `#players` RCON output. Best effort, not yet calibrated."""
    return [(m.group("id"), m.group("name").strip()) for m in _RF_PLAYER_RE.finditer(out or "")]


def _registry_port(server: GameServer, name: str, default: int) -> int:
    """A port from `game_ports` (`{"game": "2001/udp"}`), the default if it is missing."""
    try:
        raw = str(json.loads(server.game_ports or "{}").get(name, "")).split("/")[0]
    except (ValueError, AttributeError):
        raw = ""
    return int(raw) if raw.isdigit() else default


def public_address(server: GameServer) -> str:
    """`publicAddress` for config.json: the public address if it is an IPv4 address, else `""`.

    The server registers this address with the Bohemia backend, and the field takes an IPv4
    address: a host name would be registered as it is. Empty lets the backend take the address
    the server connects from — right on a host with one public address; the repository's
    README says to set it where the server would otherwise register the container's address.
    """
    import ipaddress

    from .base import public_host

    try:
        ip = ipaddress.ip_address(public_host(server))
    except ValueError:
        return ""
    return str(ip) if ip.version == 4 else ""


def _check_rcon_password(secret: str) -> None:
    """Reforger's schema: rcon.password ≤32 characters, no spaces; otherwise the server rejects
    the config at boot (crash loop). Fail early and clearly instead of writing a broken file."""
    if len(secret) > 32 or " " in secret:
        raise DriverError(err(
            "config.reforger_rcon_password_invalid",
            "The RCON password (REFORGER_RCON_PW) must be at most 32 characters without spaces "
            "(Reforger schema); it currently has {length} characters.",
            length=len(secret),
        ))


def _dedup_ordered(mods: list[dict]) -> list[dict]:
    """Normalise the mod array: deduplicate by `modId`, KEEP THE INPUT ORDER (load order matters
    in Reforger: dependencies have to come before the mods that depend on them)."""
    seen: set[str] = set()
    out: list[dict] = []
    for m in mods or []:
        mid = str((m or {}).get("modId") or "").strip()
        if not mid or mid in seen:
            continue
        seen.add(mid)
        out.append({"modId": mid, "name": str(m.get("name", "")), "version": str(m.get("version", ""))})
    return out


class ReforgerDriver(GameDriver):
    game_type = GameType.arma_reforger
    # The acemod image fetches the game from Steam on every start; nothing can be chosen.
    version_source = VersionSource.runtime
    # Proof of boot = BattlEye RCON answers `#players`. "Running for three rounds" is no proof
    # here: the container runs while SteamCMD still downloads or validates the game, so a
    # recreate reported "completed" long before anyone could join.
    boot_proof_via_players = True

    # Curated, editable config fields: (dotted path, label, kind, group, env_managed, help).
    # kind ∈ {str,int,bool,list}. env_managed=True → read-only + masked (secrets). list → chip editor.
    _FIELDS: tuple[tuple[str, str, str, str, bool, str], ...] = (
        # --- Server ---
        ("game.name", "Server name", "str", "Server", False, ""),
        ("game.maxPlayers", "Max. players", "int", "Server", False, ""),
        ("game.scenarioId", "Scenario / mode", "str", "Server", False, "Usually set through the mod presets."),
        ("game.visible", "Listed publicly", "bool", "Server", False, ""),
        ("game.modsRequiredByDefault", "Mods required", "bool", "Server", False, "Clients have to load all server mods."),
        ("maxFPS", "Server max. FPS", "int", "Server", False, "Empty/0 = unlimited; 60–120 saves CPU."),
        # --- Crossplay (mind the mod interaction) ---
        ("game.crossPlatform", "Crossplay (all platforms)", "bool", "Crossplay", False,
         "true also allows PlayStation → by Sony's rule that disables ALL mods. Leave it OFF for a "
         "modded server."),
        ("game.supportedPlatforms", "Allowed platforms", "list", "Crossplay", False,
         "PLATFORM_PC, PLATFORM_XBL, PLATFORM_PSN. PC always has to be included. PSN in the list ⇒ "
         "mods off."),
        # --- Gameplay ---
        ("game.gameProperties.battlEye", "BattlEye", "bool", "Gameplay", False, "Mandatory for console crossplay."),
        ("game.gameProperties.disableThirdPerson", "Third person off", "bool", "Gameplay", False, ""),
        ("game.gameProperties.VONCanTransmitCrossFaction", "VON across factions", "bool", "Gameplay", False,
         "Needed for Freedom Fighters (otherwise faction radios do not transmit)."),
        ("game.gameProperties.VONDisableUI", "VON UI off", "bool", "Gameplay", False, ""),
        ("game.gameProperties.VONDisableDirectSpeechUI", "Direct speech UI off", "bool", "Gameplay", False, ""),
        ("game.gameProperties.serverMaxViewDistance", "Server view distance", "int", "Gameplay", False, "e.g. 1600–2500."),
        ("game.gameProperties.networkViewDistance", "Network view distance", "int", "Gameplay", False, ""),
        ("game.gameProperties.serverMinGrassDistance", "Minimum grass distance", "int", "Gameplay", False, "0 = off; 50–100 saves performance."),
        ("game.gameProperties.fastValidation", "Fast validation", "bool", "Gameplay", False, ""),
        # --- Saving / persistence ---
        ("game.gameProperties.persistence.autoSaveInterval", "Autosave interval (s)", "int", "Saving", False, "e.g. 300."),
        ("game.gameProperties.persistence.saveRetention", "Saves to keep", "int", "Saving", False, ""),
        ("game.gameProperties.persistence.loadSessionSave", "Load the last save", "bool", "Saving", False, ""),
        ("game.gameProperties.persistence.keepSessionSave", "Keep the save", "bool", "Saving", False, ""),
        # --- AI & operation ---
        ("operating.aiLimit", "AI limit", "int", "AI & operation", False, "-1 = no limit."),
        ("operating.disableAI", "AI off", "bool", "AI & operation", False, ""),
        ("operating.slotReservationTimeout", "Slot reservation (s)", "int", "AI & operation", False, ""),
        ("operating.lobbyPlayerSynchronise", "Lobby-Sync", "bool", "AI & operation", False, ""),
        ("operating.joinQueue.maxSize", "Max. queue size", "int", "AI & operation", False, ""),
        # --- Admins / game masters ---
        ("game.admins", "Admins / game masters", "list", "Admins", False,
         "Bohemia identity ID (Reforger → main menu → profile) or SteamID. These players are "
         "admins and can open the GM menu (spawn/control) in a Game Master scenario."),
        # --- Secrets ---
        # Join/admin password: editable as real password fields (admin: write-only, empty = unchanged).
        ("game.password", "Join password", JOIN_PASSWORD, "Secrets", False,
         "Password for joining (empty = open). Shown in plain text because every player knows it anyway."),
        ("game.passwordAdmin", "Admin password (#login)", "password", "Secrets", False,
         "For “#login <password>” in the game → admin. Type a new one to change it; leave empty = "
         "unchanged."),
        # The RCON password stays read-only (it comes from the .env).
        ("rcon.password", "RCON password", "str", "Secrets", True, ""),
    )

    def capabilities(self) -> frozenset[str]:
        # `versions` is included not because there is a list to choose from but because there is
        # something to report: which game version is running (from the start banner) and which
        # image the server is pinned to. The page is information here, not a dropdown.
        # NO "announce": Reforger has no broadcast verb → the announcements UI stays greyed out.
        return frozenset({"console", "players", "events", "config", "mods", "backup", "versions"})

    # --- config.json (mounted): load/save ---
    def _config_path(self, server: GameServer) -> str:
        return posixpath.join(server.data_dir, "config", "config.json")

    def _load(self, server: GameServer) -> dict:
        try:
            with open(self._config_path(server), encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save(self, server: GameServer, data: dict) -> None:
        with open(self._config_path(server), "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)

    def _load_or_raise(self, server: GameServer) -> dict:
        """Like _load, but raises when there is no usable config.json, so the mod/scenario
        writers NEVER create an incomplete, non-bootable file.

        Distinguishes "does not exist" from "is broken". `_load` swallows both into `{}`;
        reporting "not initialised yet" for an existing but unreadable file would be a wrong
        diagnosis that sends the operator off to recreate the server instead of to the stray
        comma in the file. The case is real: the acemod container runs as root and may write the
        file back with mode 0600, while the dashboard runs as uid 1000.
        """
        path = self._config_path(server)
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except FileNotFoundError as e:
            raise DriverError(err(
                "config.reforger_not_initialised",
                "The Reforger config is not initialised yet. Create the server again (auto-seed) or "
                "seed config.json by hand, then try again.",
            )) from e
        except OSError as e:
            raise DriverError(err(
                "config.reforger_unreadable",
                "The Reforger config cannot be read ({path}): {reason}", path=path, reason=str(e),
            )) from e
        except ValueError as e:
            raise DriverError(err(
                "config.reforger_invalid_json",
                "The Reforger config is not valid JSON ({path}): {reason}", path=path, reason=str(e),
            )) from e
        if not isinstance(data, dict) or not data:
            raise DriverError(err(
                "config.reforger_not_an_object",
                "The Reforger config does not contain a JSON object ({path}) — it is not overwritten.",
                path=path,
            ))
        return data

    def ensure_config(self, server: GameServer, secret: str) -> None:
        """Called on server creation: create the directories (config/profile/workshop/install)
        and config.json from `initial_config` if missing. Best effort; the caller catches errors."""
        for sub in ("config", "profile", "workshop", _INSTALL):
            os.makedirs(posixpath.join(server.data_dir, sub), exist_ok=True)
        path = self._config_path(server)
        if not os.path.exists(path):
            with open(path, "w", encoding="utf-8") as f:
                json.dump(self.initial_config(server, secret), f, indent=2)
            try:
                # The acemod container (own UID) has to be able to READ the file. 0644 is
                # acceptable on a single-tenant host; harden via PUID/chown if needed.
                os.chmod(path, 0o644)
            except OSError:
                pass

    # --- Console / RCON (Bohemia's UDP BattlEye via services/bercon.py, NOT rcon_command) ---
    async def console(self, server: GameServer, command: str, *, timeout: float = 8.0) -> str:
        from ..bercon import bercon_command
        from ..servers import resolve_secret

        secret = resolve_secret(server)
        return await bercon_command(server.rcon_host, server.rcon_port, secret, command, timeout=timeout)

    async def players(self, server: GameServer) -> dict:
        pairs = parse_reforger_players(await self.console(server, "#players"))
        return {"count": len(pairs), "names": [n for _, n in pairs]}

    async def _resolve_player_id(self, server: GameServer, username: str) -> str | None:
        """Resolve a name to the volatile `#players` id (kick/ban work ONLY with the id)."""
        target = (username or "").strip()
        for pid, name in parse_reforger_players(await self.console(server, "#players")):
            if name == target:
                return pid
        return None

    async def _resolve_ban_id(self, server: GameServer, username: str) -> str | None:
        """Ban list index for a name/GUID from `#ban list` (best effort, not yet calibrated)."""
        target = (username or "").strip()
        if not target:
            return None
        for m in _RF_BAN_RE.finditer(await self.console(server, "#ban list")):
            if target in m.group("rest"):
                return m.group("id")
        return None

    # --- Honest limits: no broadcast verb → announce/save are deliberate no-ops ---
    async def announce(self, server: GameServer, text: str) -> None:
        # Reforger RCON has NO `say`/`servermsg`; a broadcast is simply not possible.
        # Deliberate no-op (like VS `save`): do NOT inherit the PZ `servermsg` default. The
        # cross-server announce additionally skips Reforger through the "announce" capability
        # gate (features.py/bridge.py).
        return None

    async def save(self, server: GameServer) -> None:
        # Enfusion saves automatically (autosave + clean shutdown on signal); there is no RCON
        # save verb. Like VS a deliberate no-op instead of a raw `save` that does nothing.
        return None

    # Start banner line as written by a running server:
    #   INIT : Creating game instance(ArmaReforgerScripted), version 1.7.0.54 built 2026-06-14 …
    _VERSION_RE: ClassVar = re.compile(r"Creating game instance\([^)]*\),\s*version\s+([0-9][\w.]*)")
    # A cache dict of its own per driver class; otherwise VS and Reforger would share a namespace.
    _version_cache: ClassVar[dict[tuple[str, str], str]] = {}

    async def detect_version(self, server: GameServer) -> str:
        """Read the running game version from the start banner.

        Reforger has **no selectable game version**: the acemod image fetches the game via
        SteamCMD on every start. So the *running* version is the only meaningful information.

        As with Vintage Story the line is in the start banner, i.e. far from the end of the log;
        a small `tail` does not find it after a longer uptime. Cache handling:
        `_version_from_log`.
        """
        return await self._version_from_log(server, self._VERSION_RE)

    def connect_info(self, server: GameServer):
        """Arma Reforger runs on the Enfusion engine: **no** `steam://connect`.

        Joining only works through the in-game server browser. So the server name is the useful
        piece of information here, not the address.
        """
        from .base import ConnectInfo, game_port, game_proto, public_host

        cfg = self._load(server)
        game = cfg.get("game") if isinstance(cfg.get("game"), dict) else {}
        return ConnectInfo(
            host=public_host(server), port=game_port(server), protocol=game_proto(server),
            uri="",
            password_required=bool(game.get("password")),
            hint="In the game: open the server browser and search for the server name (Enfusion "
                 "has no direct join by address).",
            browser_name=str(game.get("name") or server.display_name or server.key),
        )

    # No `graceful_shutdown` of its own: the `docker stop` IS the clean stop. The container
    # handles the signal itself (`StopSignal=SIGINT` on `python3 /launch.py`; measured: "Save
    # completed" and exit code 0 in under a second, see `stop_timeout`). An override that only
    # returned `False` made `lifecycle` wait its head start and log "falling back to the signal
    # path" on every stop, although that path was the right one. `#shutdown` over RCON is not
    # used: what it answers and whether it saves first has not been measured.

    # --- Log/event parsing (best effort; calibrate against real Reforger container logs) ---
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
            for pattern, kind in _RF_EVENTS:
                em = pattern.search(content)
                if em:
                    out.append(ServerEvent(kind=kind, username=em.group("u").strip(), at=at, raw=line))
                    break
        return out

    # --- Container specification (acemod: ARMA_CONFIG reads the mounted config.json verbatim) ---
    def container_env(self, server: GameServer, secret: str) -> dict[str, str]:
        extra = json.loads(server.extra or "{}")
        # ARMA_CONFIG => acemod takes OUR file unchanged (no env override). RCON_PASSWORD is set
        # defensively (it enables the RCON port in acemod; a no-op with the ARMA_CONFIG bypass).
        env: dict[str, str] = {"ARMA_CONFIG": "config.json", "RCON_PASSWORD": secret}
        for k, v in (extra.get("env") or {}).items():
            env[str(k)] = str(v)
        return env

    def container_binds(self, server: GameServer) -> list[str]:
        """acemod paths: config verbatim, profile = saves (Enfusion `.db`), workshop = mod
        downloads, plus the installed game and SteamCMD.

        Without the last two the game (about 10 GB) lives in the container layer, and every
        recreate downloads it again (measured: about 16 minutes of downtime); every container
        also leaves an anonymous `/steamcmd` volume behind. Docker mounts `/reforger` before the
        binds below it.

        Both are bound only once they exist, so a container created before this change keeps
        working as it did until it is created anew from the edit page:
        * `install/` is created by `ensure_config`. A recreate of an older server whose folder
          has none would download the whole game inside the boot check's window.
        * `steamcmd/` has to hold the image's own SteamCMD first: a bind hides what the image
          has at that path (only a named volume would be filled from it). Provisioning copies it
          there (`image_seed_dirs`).
        """
        binds = []
        if os.path.isdir(self._install_dir(server)):
            binds.append(f"{self._install_dir(server)}:/reforger")
        binds += [
            f"{server.data_dir}/config:/reforger/Configs",
            f"{server.data_dir}/profile:/home/profile",
            f"{server.data_dir}/workshop:/reforger/workshop",
        ]
        if os.path.isfile(posixpath.join(self._steamcmd_dir(server), "steamcmd.sh")):
            binds.append(f"{self._steamcmd_dir(server)}:{_STEAMCMD_IN_IMAGE}")
        return binds

    def image_seed_dirs(self, server: GameServer) -> list[tuple[str, str]]:
        """(folder in the image, host folder): what provisioning copies out of the image once,
        before the container is created, so that the bind does not hide it."""
        return [(_STEAMCMD_IN_IMAGE, self._steamcmd_dir(server))]

    def _install_dir(self, server: GameServer) -> str:
        return posixpath.join(server.data_dir, _INSTALL)

    def _steamcmd_dir(self, server: GameServer) -> str:
        return posixpath.join(server.data_dir, "steamcmd")

    # --- Backup: back up ALL of profile/ (the Enfusion `.db` is below it, possibly profile/save/) ---
    def backup_spec(self, server: GameServer) -> BackupSpec:
        # No RCON save hook → a fully consistent backup needs stop-then-copy (the backup router's
        # job; the restore endpoint already stops the server).
        #
        # `profile/` only contains the LIVE save. The saves of the other game modes live next to
        # it in `save-slots/`; a mode switch moves the active save exactly there. Without this
        # entry every mode not currently played would be missing from the backup.
        return BackupSpec(data_dir=server.data_dir, world_subdirs=("profile", "save-slots"))

    # --- Config editor: curated nested fields from config.json ---
    def config_fields(self, server: GameServer) -> list[ConfigField]:
        data = self._load(server)
        if not data:
            return []  # not seeded → empty editor with a "no config" hint (frontend)
        fields: list[ConfigField] = []
        for path, label, kind, group, env_managed, help_ in self._FIELDS:
            val = json_get(data, path)
            if env_managed:
                shown = "••••••" if val not in (None, "") else ""  # never deliver a secret in clear text
            elif kind == JOIN_PASSWORD:
                # Visible: every player knows the join password anyway. Its admin twin
                # `game.passwordAdmin` stays write-only; it is a control channel (`#login`).
                shown = "" if val is None else str(val)
            elif kind == "password":
                shown = ""  # write-only: never delivered; an empty field in the editor = "unchanged"
            elif kind == "list":
                shown = json.dumps(val if isinstance(val, list) else [])
            elif kind == "bool":
                shown = str(bool(val)).lower() if val is not None else "false"
            else:  # int / str: missing → empty (field stays editable; empty is not written)
                shown = "" if val is None else str(val)
            fields.append(
                ConfigField(key=path, label=label, kind=kind, value=shown, group=group, env_managed=env_managed, help=help_)
            )
        return fields

    def elevated_config_keys(self, server: GameServer) -> frozenset[str]:
        """`game.admins` and `game.passwordAdmin` hand out game master rights, not settings.

        Whoever is listed in `game.admins` can open the GM menu in a Game Master scenario and
        spawn and control; whoever sets `game.passwordAdmin` can make themselves server admin via
        `#login`. Neither belongs behind `can_edit_sandbox`, the permission to maintain the game
        world settings.
        """
        return frozenset({"game.admins", "game.passwordAdmin"})

    def set_config(self, server: GameServer, updates: dict[str, str]) -> None:
        kinds = {f[0]: f[2] for f in self._FIELDS}
        managed = {f[0] for f in self._FIELDS if f[4]}
        # `_load_or_raise` instead of `_load`: not seeded still means "write nothing" (no partial
        # config.json), but the caller has to be told. A silent `return` becomes 200 + audit
        # entry in `server_ops.put_config` for a change that never happened.
        data = self._load_or_raise(server)
        changed = False
        for path, raw in updates.items():
            if path in managed or path not in kinds:
                continue  # env-managed (secret) or unknown path → ignore
            kind = kinds[path]
            if kind == JOIN_PASSWORD:
                # Empty really means empty here (open server); the user sees the value.
                json_set(data, path, str(raw or ""))
                changed = True
                continue
            if kind == "password":
                if str(raw).strip() == "":
                    continue  # empty = unchanged (do NOT overwrite the existing password)
                json_set(data, path, str(raw))
                changed = True
                continue
            val = coerce_value(kind, raw)
            if kind == "int" and not isinstance(val, int):
                continue  # empty/invalid int → do not write (no junk in numeric fields)
            json_set(data, path, val)
            changed = True
        if changed:
            self._save(server, data)

    # --- Mods: read/write game.mods in config.json (+ scenario/mode) ---
    def list_mods(self, server: GameServer) -> dict:
        game = self._load(server).get("game") or {}
        return {"mods": game.get("mods", []), "scenarioId": game.get("scenarioId", "")}

    def set_mods(self, server: GameServer, mods: list[dict]) -> None:
        data = self._load_or_raise(server)
        data.setdefault("game", {})["mods"] = _dedup_ordered(mods)
        self._save(server, data)

    def set_scenario(self, server: GameServer, scenario_id: str) -> None:
        data = self._load_or_raise(server)
        data.setdefault("game", {})["scenarioId"] = scenario_id
        self._save(server, data)

    def patch_config(self, server: GameServer, pairs) -> None:
        """Set trusted, nested config overrides (preset `config_patch`, e.g.
        VONCanTransmitCrossFaction for Freedom Fighters). The values are already typed."""
        data = self._load_or_raise(server)
        for path, val in dict(pairs).items():
            json_set(data, path, val)
        self._save(server, data)

    # --- Saves separated by mode (every mode has its own save) -----------------------------------
    # Reforger writes the save (`.db` + `.save`) to `<data_dir>/profile/profile/` (fixed
    # `-profile`), as root (the container runs as root). The dashboard (uid 1000) may ONLY read →
    # moving saves between mode slots is done by a short-lived ROOT helper container (see
    # routers/server_ops). Slots: `<data_dir>/save-slots/<mode-id>/`. The dashboard reads the
    # metadata (size/age) directly (read-only).
    def active_mode(self, server: GameServer) -> str | None:
        """The currently loaded mode preset (save slot key), by the scenarioId in config.json."""
        from ..mod_catalog import catalog_for

        sid = (self._load(server).get("game") or {}).get("scenarioId", "")
        if not sid:
            return None
        for p in catalog_for(server.game_type):
            if p.sets_scenario and p.scenarioId and p.scenarioId == sid:
                return p.id
        return None

    def save_slots(self, server: GameServer) -> dict:
        """Per mode: is there a save? (size/last modified). Read-only; the API may read."""
        base = posixpath.join(server.data_dir, "save-slots")
        active = self.active_mode(server)

        def _meta(dbdir: str) -> dict | None:
            try:
                if not os.path.isdir(dbdir):
                    return None
                size = sum(
                    os.path.getsize(os.path.join(dp, f))
                    for dp, _, fs in os.walk(dbdir)
                    for f in fs
                )
                return {"size": size, "modified": os.path.getmtime(dbdir)}
            except OSError:
                return None

        slots: dict[str, dict] = {}
        try:
            for mode in os.listdir(base):
                m = _meta(posixpath.join(base, mode, ".db"))
                if m:
                    slots[mode] = m
        except OSError:
            pass
        # Active mode: the live save is still in the active area (not in the slot).
        if active:
            m = _meta(posixpath.join(server.data_dir, "profile", "profile", ".db"))
            cur = {**slots.get(active, {}), **(m or {})}
            cur["active"] = True
            slots[active] = cur
        return {"active_mode": active, "slots": slots}

    def save_swap_script(self) -> str:
        """sh script for the ROOT helper (mount `<data_dir>` → /data; modes via $FROM/$TO).

        Moves the active save into the $FROM slot and loads the $TO slot (copy, so the
        checkpoint stays in place).

        **Nothing is deleted that has not been saved first.** The `rm -rf "$A/.db" "$A/.save"`
        must never run for a live save without a known source mode: `$FROM` is empty whenever
        `active_mode()` returns `None`, which happens for instance when `game.scenarioId` matches
        no catalog preset, a freely editable field in the config editor. In that case the script
        aborts.

        Two more points:
        * The save branch checks for `.db` OR `.save`; a save consisting of `.save` only is
          moved as well.
        * The target slot is replaced only AFTER the new state has been moved into an
          intermediate directory. If the `mv` fails, the old archived copy is still there.
        """
        return (
            "set -e; A=/data/profile/profile; S=/data/save-slots; mkdir -p \"$S\"; "
            # Is there a live save? Then it MUST be saved before anything is removed.
            "if [ -e \"$A/.db\" ] || [ -e \"$A/.save\" ]; then "
            "  if [ -z \"$FROM\" ]; then "
            "    echo 'SWAP_ABORT: source mode unknown — the live save would have no slot it "
            "belongs in. Nothing was changed.' >&2; exit 3; "
            "  fi; "
            "  T=\"$S/.incoming-$FROM\"; rm -rf \"$T\"; mkdir -p \"$T\"; "
            "  [ -e \"$A/.db\" ] && mv \"$A/.db\" \"$T/.db\"; "
            "  [ -e \"$A/.save\" ] && mv \"$A/.save\" \"$T/.save\"; "
            # Only now that the new content is safe, replace the old slot.
            "  rm -rf \"$S/$FROM\"; mv \"$T\" \"$S/$FROM\"; "
            "fi; "
            "rm -rf \"$A/.db\" \"$A/.save\"; mkdir -p \"$A\"; "
            # Symmetric to the save branch above: EVERY file is saved individually there, so
            # each has to be loaded individually here. A slot with only `.save` (which saving
            # does produce, see `test_swap_sichert_auch_einen_save_ohne_db`) must be loaded
            # too; otherwise the mode would silently start from zero.
            "if [ -n \"$TO\" ]; then "
            "  [ -e \"$S/$TO/.db\" ] && cp -a \"$S/$TO/.db\" \"$A/.db\"; "
            "  [ -e \"$S/$TO/.save\" ] && cp -a \"$S/$TO/.save\" \"$A/.save\"; "
            "fi; "
            "echo SWAP_DONE"
        )

    def has_live_save(self, server: GameServer) -> bool:
        """Is there a save in the active area? Read-only; the API may read.

        Lets the router refuse a mode switch BEFORE it stops the server instead of relying on
        the abort in the helper container, which remains as a second safeguard.
        """
        active = posixpath.join(server.data_dir, "profile", "profile")
        return any(os.path.exists(posixpath.join(active, n)) for n in (".db", ".save"))

    def initial_config(self, server: GameServer, secret: str) -> dict:
        """Template config.json for a fresh server (mounted config: it has to exist BEFORE the
        first boot; written by `ensure_config` on creation). Sets ports/RCON (on 0.0.0.0 so the
        dashboard reaches RCON over the internal network), PC-only crossplay (mods stay active)
        and the default mode (Freedom Fighters) from the mod catalog."""
        from ..mod_catalog import default_preset_for

        _check_rcon_password(secret)

        cfg: dict = {
            "bindAddress": "0.0.0.0",
            "bindPort": _registry_port(server, "game", 2001),
            "publicAddress": public_address(server),
            "publicPort": _registry_port(server, "game", 2001),
            "a2s": {"address": "0.0.0.0", "port": _registry_port(server, "a2s", 17777)},
            # RCON on 0.0.0.0, otherwise the dashboard container cannot reach the port over the internal network.
            "rcon": {"address": "0.0.0.0", "port": server.rcon_port or 19999, "password": secret, "permission": "admin"},
            "game": {
                "name": server.display_name or server.key,
                "password": "",
                "passwordAdmin": "",
                "admins": [],
                "scenarioId": "",
                "maxPlayers": 32,
                "visible": True,
                # PC only: crossPlatform:false + supportedPlatforms:[PC] → mods are guaranteed to
                # stay on (crossPlatform:true would allow PSN and, per Sony's rules, disable ALL mods).
                "crossPlatform": False,
                "supportedPlatforms": ["PLATFORM_PC"],
                "gameProperties": {"battlEye": True, "disableThirdPerson": False},
                "mods": [],
            },
        }
        preset = default_preset_for(server.game_type)
        if preset is not None:
            cfg["game"]["mods"] = [{"modId": m.modId, "name": m.name, "version": m.version} for m in preset.mods]
            if preset.sets_scenario and preset.scenarioId:
                cfg["game"]["scenarioId"] = preset.scenarioId
            for path, val in preset.config_patch:
                json_set(cfg, path, val)
        return cfg

    def sync_config_from_registry(self, server: GameServer, secret: str) -> list[str]:
        """Write what the registry owns into an EXISTING config.json. Returns the keys set.

        `ARMA_CONFIG` makes the image use this file unchanged, so a port, address or console
        password changed in the dashboard after creation reaches the server ONLY through it:
        the container env has no say. Without this the server would keep listening on the old
        port behind the newly published one, and announce it to the Bohemia backend. The edit
        path calls it before anything is saved; the change takes effect at the next start. No
        file yet: nothing to do, `ensure_config` writes it.
        """
        if not os.path.isfile(self._config_path(server)):
            return []
        cfg = self._load_or_raise(server)
        game = _registry_port(server, "game", 2001)
        cfg["bindPort"] = game
        cfg["publicPort"] = game
        cfg["publicAddress"] = public_address(server)
        a2s = cfg.get("a2s") if isinstance(cfg.get("a2s"), dict) else {"address": "0.0.0.0"}
        a2s["port"] = _registry_port(server, "a2s", 17777)
        cfg["a2s"] = a2s
        keys = ["a2s.port", "bindPort", "publicAddress", "publicPort"]
        rcon = cfg.get("rcon") if isinstance(cfg.get("rcon"), dict) else None
        if rcon is not None:
            rcon["port"] = server.rcon_port or rcon.get("port") or 19999
            keys.append("rcon.port")
            if secret:
                _check_rcon_password(secret)
                rcon["password"] = secret
                keys.append("rcon.password")
        self._save(server, cfg)
        return sorted(keys)

    def stop_timeout(self, server: GameServer) -> int:
        """90 s. Reforger has no `graceful_shutdown` of its own.

        PZ and Vintage Story are told through the console to save and exit; Reforger has no such
        verb and depends **solely** on this grace period. Measured: from
        `OnGameEnd`/"Save completed" to process exit in under a second, exit code 0, so the
        signal path works. The reserve is for the Enfusion persistence, which writes to
        `$profile:/.db/…` on exit: if that ever takes longer, a torn save is the most expensive
        outcome imaginable.
        """
        return 90

    # --- Moderation: kick/ban/unban over RCON (by volatile #players id; target must be online) ---
    def moderation_spec(self, server: GameServer) -> ModerationSpec:
        # A hint for the SUPPORTED actions too: Reforger knows players only by a volatile
        # `#players` id that does not exist for offline players. Without the hint the UI would
        # show a button that ends in an error without explanation.
        # Each reason is ONE literal (no shared prefix glued on): the frontend translates a
        # fixed text by its exact wording.
        return ModerationSpec(
            kick=True, ban=True, unban=True,
            reasons={
                "kick": "Only works for someone who is on the server right now: Arma Reforger "
                        "addresses players by a short-lived number from the player list, which "
                        "does not exist for absent players. Reforger shows no reason on a kick.",
                "ban": "Only works for someone who is on the server right now: Arma Reforger "
                       "addresses players by a short-lived number from the player list, which "
                       "does not exist for absent players. A ban is always permanent.",
                "ban_ip": "Arma Reforger has no IP ban — what gets banned is the game account.",
                "access_levels": "Admin rights are handled through the server configuration in "
                                 "Reforger: Configuration → Admins, with the player's identity ID.",
                "whitelist": "Arma Reforger has no whitelist. Who gets in is controlled through "
                             "the server password and the platform setting.",
            },
        )

    async def kick(self, server: GameServer, username: str, reason: str = "") -> str:
        pid = await self._resolve_player_id(server, username)
        if pid is None:
            # Raises (instead of returning a string) → router → HTTP 400 → a real error toast, not fake success.
            raise DriverError(err(
                "moderation.reforger_kick_needs_online",
                "Player '{username}' is not connected — a kick needs a player who is online.",
                username=username,
            ))
        # Reforger's `#kick` takes no reason → `reason` is ignored.
        return await self.console(server, f"#kick {pid}")

    async def ban(self, server: GameServer, username: str, reason: str = "", ip: bool = False) -> str:
        pid = await self._resolve_player_id(server, username)
        if pid is None:
            raise DriverError(err(
                "moderation.reforger_ban_needs_online",
                "Player '{username}' is not connected — a ban needs a player who is online.",
                username=username,
            ))
        r = sanitize_message(reason)
        # `#ban create <id> <duration> <reason>`; the duration is always 0 = permanent.
        # **The unit is unverified**: the sources contradict each other (minutes vs. seconds),
        # and since this always sends 0 it has never been tested. Whoever adds temporary bans
        # has to measure it on a real server first; a factor-60 error would go unnoticed.
        # Reforger has no IP ban.
        cmd = f"#ban create {pid} 0" + (f" {r}" if r else "")
        return await self.console(server, cmd)

    async def unban(self, server: GameServer, username: str) -> str:
        ban_id = await self._resolve_ban_id(server, username)
        if ban_id is None:
            raise DriverError(err(
                "moderation.reforger_ban_entry_not_found",
                "No ban entry found for '{username}' (via #ban list).", username=username,
            ))
        return await self.console(server, f"#ban remove {ban_id}")

    # --- Safe console verbs + overview figures ---
    def safe_console_commands(self) -> frozenset[str]:
        # Read-only Reforger verbs (prefix `#`); destructive ones (#kick/#ban/#restart/#shutdown) need the full console.
        return frozenset({"#players", "#roles"})

    def overview_stats(self, server: GameServer) -> dict:
        game = self._load(server).get("game") or {}
        max_players = game.get("maxPlayers")
        try:
            max_players = int(max_players) if max_players is not None else None
        except (TypeError, ValueError):
            max_players = None
        mods = game.get("mods")
        return {"max_players": max_players, "mods_count": len(mods) if isinstance(mods, list) else None}
