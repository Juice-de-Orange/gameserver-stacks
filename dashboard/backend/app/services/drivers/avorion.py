"""AvorionDriver for the rfvgyhn/avorion image.

**Three quirks that shape the driver:**

1. **The server rewrites its `server.ini` on every start.** A change made while the server is
   running is lost on the next start, with no error and no trace. `set_config` therefore
   requires a container that is verifiably stopped and refuses otherwise. That is the only way a
   setting sticks.
2. **The image has no env variables.** The entrypoint is fixed
   (`./bin/AvorionServer --galaxy-name avorion_galaxy`); everything else comes from files in the
   galaxy directory. `container_env` is consequently empty; an invented variable would look like
   a setting and do nothing.
3. **The server is PID 1 and handles SIGTERM itself** (saves, closes connections).
   The driver therefore has no `graceful_shutdown` of its own: the signal path is sufficient,
   as with Reforger, and unlike PZ, where PID 1 is a bash without a handler.

**What this driver deliberately does NOT do:**

* **No events and no chat ingest.** Avorion writes joins and chat to `serverlog <date>.txt`, but
  the exact format of those lines is undocumented and was not available when the driver was
  built. A guessed pattern has two outcomes, both worse than none: it finds nothing (and looks
  as if detection worked) or it finds the wrong thing (and records joins that never happened,
  which end up in session times and the leaderboard). `capabilities()` therefore does not list
  `events`. Session times still exist: the stats poller queries `players()` over RCON.
  To be added once a real server log with player joins is available.

Announcements work through `/say` (see `announce`).

**Files** (the bind points directly at the galaxy directory, `1000:1000` in the container):

    <data_dir>/galaxy/server.ini        configuration, INI with sections
    <data_dir>/galaxy/modconfig.lua     workshop mods
    <data_dir>/galaxy/sectors/, players/, …   the galaxy
    <data_dir>/galaxy/serverlog *.txt   server logs

The directory is called `galaxy` and not `data` as for Minecraft and Factorio: here the volume
IS the galaxy directory, not a data directory that contains it. The name in the archive should
say what is inside.
"""
from __future__ import annotations

import configparser
import io
import json
import logging
import os
from pathlib import Path
import posixpath
import re
import secrets
from typing import TYPE_CHECKING

from ...errors import err, note
from ...models.server import GameType
from .base import (
    JOIN_PASSWORD, AutoUpdate, BackupSpec, ConfigField, DriverError, GameDriver, ModerationSpec,
    check_console_response,
    QuiesceWarning, ServerEvent, VersionSource, coerce_value, mask_if_secret, sanitize_message,
    write_config_file,
)

if TYPE_CHECKING:
    from ...models.server import GameServer

log = logging.getLogger("uvicorn.error")

# Start banner: "Server startup complete. Avorion 2.5.13 r44140" or "Avorion version: 2.5.13".
# Not calibrated on a real log. A miss is harmless here: it shows no version rather than a
# wrong one, which is why this is acceptable while a guessed event pattern is not.
_VERSION_RE = re.compile(r"Avorion[^\d]{0,20}(\d+\.\d+\.\d+)", re.I)

# The galaxy directory inside the container. Fixed, because the image's entrypoint fixes it.
_GALAXY_IN_CONTAINER = "/home/steam/.avorion/galaxies/avorion_galaxy"

# `/players` output: one line per player. The exact format is undocumented, so
# `parse_avorion_players` reads tolerantly, but does NOT guess; see there.
_A_INDEX_RE = re.compile(r"^\s*(?:\d+\s*[.:)\-]?\s+)?(?P<u>\S.*?)\s*$")

# "Nobody online" in the forms servers usually reply with. Without this an empty server could
# not be told apart from output that was not understood, and reporting both as "unknown" would
# turn the normal case into a fault.
_A_EMPTY_RE = re.compile(
    r"^\s*(?:there\s+are\s+)?(?:no|0|none)\b[^.]*\bplayers?\b|^\s*players?\s*(?:online)?\s*[:(]\s*0\b",
    re.I,
)

# Error messages of the server. Word boundaries are required: without them `error` also matches
# player names such as "Terrorbyte", "Errol" and "Perry_Error", and the player count would be
# permanently unknown for that server.
_A_ERROR_RE = re.compile(
    r"\b(?:unknown\s+command|error|not\s+(?:a\s+|an\s+)?admin|permission|usage)\b", re.I,
)

# Does a line look like a SENTENCE rather than a player name? The output format has not been
# measured, so nothing is guessed: a line that ends in punctuation, contains a colon or slash,
# or consists of many words is not a name line.
_A_SENTENCE_RE = re.compile(r"[.!?]\s*$|[:/]")

# Workshop ids from `modconfig.lua`: `{workshopid = "1819452708"}`.
_A_MOD_RE = re.compile(r"workshopid\s*=\s*[\"'](?P<id>\d+)[\"']")


def parse_avorion_players(out: str) -> dict:
    """Output of `/players` → {"count": int, "names": [...]}.

    **Raises on output that does not look like a player list**, and that is the most important
    line in this file. `players()` feeds `idle_autostop`, `watchdog` and `auto_update`; all three
    treat an exception as "unknown, do not touch". A silently reported 0, by contrast, means
    "nobody there" to them, and a server with somebody playing on it would be stopped or updated.

    The output format is undocumented and could not be measured when this driver was built.
    That is exactly why unclear output fails loudly here instead of quietly becoming zero.
    """
    text = (out or "").strip()
    if not text:
        raise ValueError("Avorion: empty reply to /players — player count unknown")

    lines = [z for z in text.splitlines() if z.strip()]
    # Evaluate a header line of the form "Players online: 2" / "2 players online" if present.
    header = re.search(r"(?:players?\s*(?:online)?\s*[:(]\s*(\d+)|^(\d+)\s+players?\b)", lines[0], re.I)

    # Error detection only runs when there is NO header line with a number. Otherwise it would
    # run over the player names as well, and a "Terrorbyte" would make the player count
    # permanently unknown. The number in the header is the most reliable figure this game gives.
    if not header and _A_ERROR_RE.search(text):
        raise ValueError(f"Avorion: /players was not answered ({lines[0][:80]!r})")

    # "Nobody online" is an answer, not a riddle. Without this it would fall through
    # `_A_INDEX_RE` and become a player named "No players online.": a PlayerSession that never
    # closes, an overview showing 1 player, and a server that never idle-stops or auto-updates.
    if not header and len(lines) == 1 and _A_EMPTY_RE.search(lines[0]):
        return {"count": 0, "names": []}

    names: list[str] = []
    for line in lines[1:] if header else lines:
        if re.match(r"^\s*[-=_]{3,}\s*$", line):      # separator line
            continue
        if _A_EMPTY_RE.search(line):                    # "No players online." below a header
            continue
        m = _A_INDEX_RE.match(line)
        value = m.group("u").strip() if m and m.group("u") else ""
        if not value:
            continue
        if _A_SENTENCE_RE.search(value) or len(value.split()) > 4:
            # The most important refusal in this function. `_A_INDEX_RE` matches ANY non-empty
            # line; without this check "Command not found." or "Server is still starting up"
            # would become a player of that name. The format has not been measured, so what
            # does not look like a name is not turned into one.
            raise ValueError(
                f"Avorion: /players returned a line that is not a player name ({value[:80]!r}) "
                f"— player count unknown"
            )
        names.append(value)

    if header:
        return {"count": int(header.group(1) or header.group(2)), "names": names}
    if not names:
        # No header AND no line: exactly the ambiguous case; "nobody online" looks identical
        # to "command not understood" here. Ambiguous means unknown.
        raise ValueError("Avorion: /players returned nothing that can be evaluated")
    return {"count": len(names), "names": names}


def parse_modconfig(text: str) -> list[str]:
    """ALL workshop ids in `modconfig.lua`, in file order and without duplicates.

    **Careful: this includes the `allowed` block.** For the server's mods use
    `mods.preflight_avorion.ids_and_allowed`, which separates the two lists (`list_mods` does);
    this function returns `mods ∪ allowed`, and applying that as the mod list would turn the
    access list into real mods. It remains for the case that really every id mentioned is needed
    (e.g. "which workshop items does this file know at all").
    """
    seen: set[str] = set()
    out: list[str] = []
    for m in _A_MOD_RE.finditer(text or ""):
        mid = m.group("id")
        if mid not in seen:
            seen.add(mid)
            out.append(mid)
    return out


class AvorionDriver(GameDriver):
    game_type = GameType.avorion
    # The image ships the game (SteamCMD runs at BUILD time, not at start); the tag is the
    # version, as with PZ and Factorio.
    version_source = VersionSource.image_tag
    # Within the minor version; a jump beyond it can migrate the galaxy.
    auto_update_limit = AutoUpdate.patch

    # Curated fields: (Section.key, label, kind, group, env_managed, help).
    # The keys follow `server.ini` with its sections. Avorion writes them in exactly this
    # spelling, and `configparser` is made case-sensitive via `optionxform`.
    _FIELDS: tuple[tuple[str, str, str, str, bool, str], ...] = (
        # --- Server ---
        ("Administration.name", "Server name", "str", "Server", False, "The name shown in the server browser."),
        ("Administration.description", "Description", "str", "Server", False, ""),
        ("Administration.maxPlayers", "Max. players", "int", "Server", False, "Plus one admin slot."),
        ("Administration.password", "Join password", JOIN_PASSWORD, "Server", False,
         "Empty = anyone can join. Shown in plain text because every player knows it anyway."),
        ("Administration.accessListMode", "Access list", "enum", "Server", False,
         "Blacklist = everyone except banned players; Whitelist = listed players only."),
        ("Administration.pausable", "Pausable", "bool", "Server", False, ""),
        ("Networking.isListed", "Listed publicly", "bool", "Server", False, ""),
        # --- Game world ---
        ("Game.motd", "Welcome message", "str", "Game world", False, ""),
        ("Game.Difficulty", "Difficulty", "int", "Game world", False, "-3 (beginner) to 3 (insane)."),
        ("Game.PlayerToPlayerDamage", "PvP", "bool", "Game world", False, ""),
        ("Game.CollisionDamage", "Collision damage", "str", "Game world", False, "0 to 1."),
        ("Game.BoardingAllowed", "Boarding allowed", "bool", "Game world", False, ""),
        ("Game.PermaDestruction", "Ships destroyed permanently", "bool", "Game world", False, ""),
        ("Game.HardcoreEnabled", "Hardcore", "bool", "Game world", False, "Permanent death."),
        ("Game.InfiniteResources", "Infinite resources", "bool", "Game world", False, ""),
        ("Game.LogoutInvincibility", "Invincible after logout", "bool", "Game world", False, ""),
        ("Game.LogoutInvincibilityDelay", "Delay (s)", "int", "Game world", False, ""),
        # --- Limits ---
        ("Game.MaximumPlayerShips", "Max. ships per player", "int", "Limits", False, "-1 = unlimited."),
        ("Game.MaximumPlayerStations", "Max. stations per player", "int", "Limits", False, "-1 = unlimited."),
        ("Game.MaximumBlocksPerCraft", "Max. blocks per ship", "int", "Limits", False, "-1 = unlimited."),
        ("Game.PlayerInventorySlots", "Inventory slots", "int", "Limits", False, ""),
        # --- Performance ---
        ("System.saveInterval", "Save interval (s)", "int", "Performance", False, ""),
        ("System.workerThreads", "Worker threads", "int", "Performance", False,
         "Do not set this above the number of cores of the host."),
        ("System.generatorThreads", "Generator threads", "int", "Performance", False, ""),
        ("System.aliveSectorsPerPlayer", "Active sectors per player", "int", "Performance", False,
         "The biggest lever on RAM: every active sector is held in memory."),
        ("System.sectorUpdateTimeLimit", "Sector keep-alive time (s)", "int", "Performance", False, ""),
        # --- The game's own backup ---
        ("System.backups", "The game's own hourly backup", "bool", "Performance", False,
         "The game's own, in addition to the dashboard backup. Takes up space in the galaxy folder."),
        # ------------------------------------------------------------------------------------
        # The fields below follow `server.ini - readme.txt`, the file Avorion itself writes to
        # the galaxy directory and in which EVERY key is explained.
        # What is deliberately LEFT OUT is listed with reasons in
        # `tests/test_avorion_config_covers_the_vendor_docs.py` rather than as a comment here,
        # so that a new key in the next Avorion version gets noticed instead of slipping through.
        # ------------------------------------------------------------------------------------

        # --- Game world ---
        ("Game.Storyline", "Guided story", "bool", "Game world",  False,
         "Only the GUIDANCE through the story. Artifacts and progress stay in any case."),
        ("Game.Barrier", "Barrier to the centre", "bool", "Game world", False,
         "The barrier on the way to the centre of the galaxy."),
        ("Game.BehemothEvents", "Behemoth events", "bool", "Game world", False,
         "Events from the Behemoth DLC."),
        ("Game.DockingRestrictions", "Docking restrictions", "bool", "Game world", False,
         "Some objects (e.g. gates) cannot be docked, and docked turrets need slots on the mother ship."),
        ("Game.ShipyardBoundBuilding", "Building only at a shipyard", "bool", "Game world", False,
         "Certain blocks can only be built in sectors with a shipyard."),
        ("Game.RepairingAlwaysAllowed", "Repairing always allowed", "bool", "Game world", False,
         "In combat too."),
        ("Game.FullBuildingUnlocked", "Building knowledge unlocked", "bool", "Game world", False,
         "Every new player can build everything from the start."),
        ("Game.InstantShipyardBuilding", "Shipyard builds instantly", "bool", "Game world", False, ""),
        ("Game.BuildModeFreeTurrets", "Free turrets in build mode", "bool", "Game world", False,
         "Build mode copies turrets from the inventory instead of using them up."),
        ("Game.BuildModeExtraBlockTypes", "Extra blocks in build mode", "bool", "Game world", False,
         "Wreckage and rift stone become buildable."),
        ("Game.CreativeModeCommandCenter", "Creative-mode command centre", "bool", "Game world", False, ""),
        ("Game.UnlimitedProcessingPower", "Unlimited processing power", "bool", "Game world", False,
         "Building knowledge no longer limits processing power — ships can become arbitrarily large."),
        ("Game.UnlimitedShipSize", "Unlimited ship size", "bool", "Game world", False, ""),
        ("Game.PreciseAIAim", "AI aims precisely", "bool", "Game world", False,
         "The AI no longer misses on purpose."),
        ("Game.BlockDestructionThreshold", "Blocks break off (HP %)", "str", "Game world", False,
         "The remaining HP at which blocks on player ships start to break."),
        ("Game.BlockOverlapExploit", "Allow block overlap", "bool", "Game world", False,
         "CAUTION: the vendor explicitly recommends “false”. Switched on, it allows exploit builds "
         "with infinite block overlap; it only still exists for worlds from before version 2.0."),
        ("Game.sameStartSector", "Everyone in the same start sector", "bool", "Game world", False,
         "The vendor advises against it once the server is larger or public."),

        # --- Balance ---
        ("Game.DamageMultiplier", "Damage factor (NPC)", "str", "Balance", False,
         "Multiplied onto the damage of NPC ships."),
        ("Game.StartingResources", "Starting resources", "int", "Balance", False,
         "-4 to 0 (corresponds to difficulties -3 to 0). -4 is the quick start."),
        ("Game.InitialRelations", "Initial relations", "int", "Balance", False,
         "-3 (best) to 3 (worst)."),
        ("Game.RelationGainFactor", "Relation gain ×", "str", "Balance", False,
         "1.5 means 50 % more relation gain."),
        ("Game.RelationLossFactor", "Relation loss ×", "str", "Balance", False, ""),
        ("Game.ResourceAsteroidFactor", "Resource asteroids ×", "str", "Balance", False, ""),
        ("Game.ResourceWreckageFactor", "Resource wreckage ×", "str", "Balance", False, ""),
        ("Game.EventsFactor", "Events ×", "str", "Balance", False, ""),
        ("Game.RiftMassFactor", "Mass in rifts ×", "str", "Balance", False, ""),
        ("Game.RiftDamageFactor", "Xsotan damage in rifts ×", "str", "Balance", False,
         "Multiplied with the damage factor in addition."),
        ("Game.MapFactions", "Factions on the map", "int", "Balance", False,
         "Below 1 or above 1700 the behaviour is undefined according to the vendor."),
        ("Game.Rifts", "Rifts on the map", "int", "Balance", False,
         "Below 0 or above 500 the behaviour is undefined according to the vendor."),
        ("Game.xsotanInvasionSectors", "Xsotan invasion sectors", "int", "Balance", False,
         "In how many sectors the invasion takes place at the same time after the end boss."),
        ("Game.BigWreckageDespawnTime", "Large wreckage despawns (s)", "int", "Balance", False,
         "From 16 blocks. Only affects wreckage from destruction, not the generator's."),
        ("Game.SmallWreckageDespawnTime", "Small wreckage despawns (s)", "int", "Balance", False,
         "Up to 15 blocks."),

        # --- Limits ---
        ("Game.MaximumStationsPerSector", "Max. stations per sector", "int", "Limits", False,
         "-1 = no limit. Only applies when players found a station; the generator may exceed it."),
        ("Game.MaximumVolumePerShip", "Max. volume per ship", "int", "Limits", False, "-1 = no limit."),
        ("Game.MaximumVolumePerStation", "Max. volume per station", "int", "Limits", False, "-1 = no limit."),
        ("Game.MaximumBlocksPerTurret", "Max. blocks per turret", "int", "Limits", False, "-1 = no limit."),
        ("Game.MaximumFightersPerSectorAndPlayer", "Max. fighters per sector and player", "int",
         "Limits", False, "-1 = no limit."),
        ("Game.MaximumAllianceShips", "Max. alliance ships", "int", "Limits", False, "-1 = no limit."),
        ("Game.MaximumAllianceStations", "Max. alliance stations", "int", "Limits", False, "-1 = no limit."),
        ("Game.MaximumAllianceShipsPerMember", "Max. alliance ships per member", "int", "Limits",
         False, "If both limits are set, the smaller one applies."),
        ("Game.MaximumAllianceStationsPerMember", "Max. alliance stations per member", "int",
         "Limits", False, "If both limits are set, the smaller one applies."),
        ("Game.AllianceInventorySlots", "Inventory slots (alliance)", "int", "Limits", False, ""),
        ("Game.MinimumCraftSize", "Minimum craft size", "str", "Limits", False,
         "0 = none. Height, length and width must not be below it; the vendor advises against more "
         "than 1.0, which is the size of the founding block."),
        ("Game.MinimumBlockSize", "Minimum block size", "str", "Limits", False,
         "Between 0.00001 and 0.01. The default is 0.01, which is recommended for multiplayer."),
        ("Game.MaxShipVelocity", "Maximum velocity (m/s)", "int", "Limits", False,
         "0 = no limit."),

        # --- Performance ---
        ("System.MaxTimeStep", "Max. tick length (s)", "str", "Performance", False,
         "A tick that takes longer is cut short so that objects do not tunnel through walls. The "
         "vendor advises against changing it."),
        ("System.emptySectorUpdateInterval", "Update interval of empty sectors (s)", "str", "Performance", False, ""),
        ("System.scriptBackgroundThreads", "Script threads", "int", "Performance", False, ""),
        ("System.weakUpdate", "Simplified simulation", "bool", "Performance", False,
         "Sectors without players are simulated in a simplified way. Saves a lot of performance."),
        ("System.sendSectorDelay", "Sector change delay (s)", "int", "Performance", False, ""),
        ("System.placeInShipOnDeathDelay", "Without a ship after destruction (s)", "int", "Performance", False, ""),
        ("System.respawnAloneDelay", "Respawn alone (s)", "int", "Performance", False, ""),
        ("System.respawnMultiplayerDelay", "Respawn with others present (s)", "int", "Performance", False,
         "Applies when another player is in the sector."),
        ("System.backupsPath", "Path of the hourly backup", "str", "Performance", False,
         "Empty = the default folder. Affects ONLY the server's hourly backup, not the archives of "
         "this dashboard."),
        ("System.statsLogging", "Log statistics", "bool", "Performance", False,
         "Writes a CSV next to the server log."),
        ("System.sendCrashReports", "Send crash reports", "bool", "Performance", False,
         "Explicitly recommended by the vendor."),
        ("System.hangDetection", "Report hangs", "bool", "Performance", False,
         "Reports when the server does not respond for 30 s. Explicitly recommended."),
        ("System.profiling", "Performance profiling", "bool", "Performance", False,
         "Costs some performance and around 100 MB of memory. For troubleshooting only."),
        ("System.autoSavePerformanceData", "Save performance data automatically", "bool", "Performance",
         False, "Requires performance profiling to be switched on."),
        ("System.performanceDataAutoSaveFiles", "Performance files to keep", "int", "Performance", False, ""),
        ("System.timeBetweenPerformanceDataAutoSaves", "Interval of performance data (s)", "int",
         "Performance", False, ""),
        ("System.fileClustering", "Cluster files", "bool", "Performance", False,
         "Combines many small files on shutdown. Makes start and stop take longer but reduces the "
         "number of files in the save."),
        ("System.clusterFileSize", "Size of clustered files (bytes)", "int", "Performance", False, ""),
        ("System.clusteringThreads", "Clustering threads", "int", "Performance", False,
         "The server needs [file size] × [threads] of free memory."),

        # --- Network ---
        ("Networking.isMultiplayer", "Multiplayer", "bool", "Network", False,
         "OFF means: only a single administrator gets in. In effect this is an access switch for "
         "the whole server."),
        ("Networking.useSteam", "Steam networking", "bool", "Network", False,
         "Explicitly recommended for public servers."),
        ("Networking.forceSteam", "Force Steam", "bool", "Network", False,
         "No fallback to TCP — if anything goes wrong while connecting to Steam, the server DIES at start."),
        ("Networking.vacSecure", "VAC check", "bool", "Network", False,
         "Only possible with Steam networking."),
        ("Networking.broadcastInterval", "Broadcast interval (s)", "int", "Network", False,
         "Interval between two complete network updates."),
        ("Networking.sendStatsToAdmins", "Statistics to admins", "bool", "Network", False,
         "Can cause a lot of traffic."),
        ("Networking.networkingThreads", "Networking threads", "int", "Network", False, "At least 2."),
        ("Networking.maxReceivableMessageSize", "Max. message size (bytes)", "int", "Network",
         False, "Larger messages are dropped SILENTLY."),
        ("Networking.safeBytesPerClientPerTick", "Unsuspicious bytes per tick", "int", "Network", False,
         "Above this the traffic is examined for suspicious patterns. 100,000 to 100,000,000."),
        ("Networking.suspiciousTickHistorySize", "Ticks observed", "int", "Network", False, "2 to 120."),
        ("Networking.maxSuspiciousTicks", "Suspicious ticks until kick", "int", "Network", False, ""),

        # --- Scripts (security) ---
        ("Game.ExplicitCallables", "Marked script functions only", "bool", "Game world", False,
         "OFF opens, in the vendor's words, “doors for a lot of cheats and hacks”: a client may "
         "then call any script function, not only the explicitly released ones. A security switch, "
         "not a matter of game feel."),

        # --- Control channel (read-only) ---
        # RCON is how the dashboard talks to the server. Making it editable would give the
        # config editor a switch that turns the console off.
        ("Networking.rconPassword", "RCON password", "str", "Control channel", True, ""),
        ("Networking.rconPort", "RCON-Port", "int", "Control channel", True, ""),
        ("Networking.port", "Game port", "int", "Control channel", True, ""),
    )

    def capabilities(self) -> frozenset[str]:
        # `announce` is included: `/say` has been measured on a running server.
        # `events` is missing, because the format of the join lines cannot be observed until
        # somebody has actually joined. See `parse_events`.
        return frozenset({
            "console", "players", "config", "mods", "backup", "versions", "announce",
        })

    # --- Console / RCON ---------------------------------------------------------------------
    def _rcon_secret(self, server: GameServer) -> str:
        """From the running server's `server.ini`, otherwise from the `.env`.

        Avorion REWRITES its `server.ini` at start and the file lives INSIDE the backed-up galaxy
        directory: every restore brings back the password of that time. The shared rule and its
        reasoning are at `base.rcon_secret_from_file`.
        """
        from .base import rcon_secret_from_file

        from_file = str(self._ini(server).get("Networking", {}).get("rconPassword", "")).strip()
        return rcon_secret_from_file(server, from_file)

    async def console(self, server: GameServer, command: str, *, timeout: float = 8.0) -> str:
        from ..rcon import rcon_command

        return await rcon_command(
            server.rcon_host, server.rcon_port, self._rcon_secret(server), command,
            timeout=timeout, multi=True,
        )

    async def players(self, server: GameServer) -> dict:
        """Player count via `/players`; an unclear result is raised as `RconError`.

        `parse_avorion_players` raises `ValueError`, which no consumer expects: `routers/server`
        catches `(RconError, NotImplementedError)`, `watchdog._check_server` and `stats_poller`
        only `RconError`. A `ValueError` would reach the status bar as a 500, and the watchdog
        would never record an incident for this server. The conversion belongs here because the
        parser is right on its own terms: output that is not understood IS an error.
        """
        from ..rcon import RconError

        try:
            return parse_avorion_players(await self.console(server, "/players"))
        except ValueError as e:
            raise RconError(str(e)) from e

    async def announce(self, server: GameServer, text: str) -> None:
        """Announcement to all players via `/say`.

        Measured on a running server (Avorion 2.5.13), `/help` lists verbatim

            /say: Send chat message to all players on the server

        The command replies with empty text; the message appears in the game chat, not in the
        console. So the reply is NOT checked here: there is nothing to check.

        The announcements tab, the warning before scheduled restarts and the cross-server bridge
        depend on this method.
        """
        await self.console(server, f"/say {sanitize_message(text)}")

    async def save(self, server: GameServer) -> None:
        # The reply is CHECKED, not merely awaited: a rejected command comes back as ordinary
        # text, and the scheduled restart shuts down right afterwards.
        check_console_response(
            await self.console(server, "/save", timeout=120.0), command="/save", game="Avorion",
        )

    # No `graceful_shutdown` of its own, which is the right answer here, not a gap.
    # AvorionServer is PID 1 of the container and handles SIGTERM itself: it saves the galaxy and
    # closes the connections. An RCON command before that would not be safer, only a second path
    # that can go wrong. Inheriting the base default also tells `lifecycle` to skip the console
    # attempt and its head start; it gives the signal `stop_timeout` to do its work.

    def connect_info(self, server: GameServer):
        from .base import ConnectInfo, game_port, game_proto, public_host

        ini, reason = self._ini_status(server)
        # A read error is UNKNOWN, not "no password"; see `_ini_status`.
        has_password = (None if reason
                        else bool(str(ini.get("Administration", {}).get("password", "")).strip()))
        note = "In the game: “Multiplayer” → “Connect directly” → enter the address."
        if reason:
            note += f" CAUTION: {reason} — whether a join password is set is unknown."
        return ConnectInfo(
            host=public_host(server), port=game_port(server), protocol=game_proto(server),
            uri="",
            password_required=has_password,
            hint=note,
            browser_name=str(ini.get("Administration", {}).get("name", "")) or server.display_name or server.key,
        )

    def parse_events(self, server: GameServer, log_text: str) -> list[ServerEvent]:
        """Empty on purpose; see the module docstring.

        The format of the join lines in `serverlog <date>.txt` is undocumented. A guessed pattern
        would either find nothing (and feign function) or record joins that never happened.
        A running server has been queried (`/help`, `/status`, server log), but without a single
        real join there is no line a pattern could be calibrated on.

        Open point, to do once somebody has played: open `serverlog <date>.txt` in the galaxy
        directory, pick out the join and leave lines, store them as a fixture and calibrate the
        pattern against it, the way `tests/fixtures/pz_user_log.txt` does for Project Zomboid.
        """
        return []

    # --- Container ---------------------------------------------------------------------------
    def container_env(self, server: GameServer, secret: str) -> dict[str, str]:
        """Empty except for extras passed through; the image has no variables of its own.

        `secret` stays unused: the RCON password is in the `server.ini` that `ensure_config`
        writes. An invented variable would look like a setting and do nothing.
        """
        extra = json.loads(server.extra or "{}")
        return {str(k): str(v) for k, v in (extra.get("env") or {}).items()}

    def container_binds(self, server: GameServer) -> list[str]:
        return [f"{self._galaxy(server)}:{_GALAXY_IN_CONTAINER}"]

    def stop_timeout(self, server: GameServer) -> int:
        """180 s. On stop Avorion writes the whole galaxy.

        How much that is depends on how far the players have travelled; every visited sector is
        a file. A hard kill in the middle of it is the one way to really lose something here,
        and 180 s cost nothing as long as the process exits by itself.
        """
        return 180

    # --- File paths --------------------------------------------------------------------------
    def _galaxy(self, server: GameServer) -> str:
        return posixpath.join(server.data_dir, "galaxy")

    def _ini_path(self, server: GameServer) -> str:
        return posixpath.join(self._galaxy(server), "server.ini")

    def _reader(self) -> configparser.ConfigParser:
        p = configparser.ConfigParser(interpolation=None)
        # Avorion writes `maxPlayers`, not `maxplayers`. Without this every change would arrive
        # in the file as a second, lower-case key while the server keeps reading the old one.
        p.optionxform = str
        return p

    def _ini(self, server: GameServer) -> dict[str, dict[str, str]]:
        """`server.ini` as a nested dict; empty if it is missing or broken.

        **Callers that need to tell "missing" from "unreadable" use `_ini_status`**; `connect_info`
        does, because a read error would otherwise turn into "no join password".
        """
        return self._ini_status(server)[0]

    def _ini_status(self, server: GameServer) -> tuple[dict[str, dict[str, str]], str]:
        """`(data, reason)`; `reason` is empty as long as everything is fine.

        A MISSING file is not a reason (fresh server, `ensure_config` creates it); every other
        error is one.
        """
        try:
            text = Path(self._ini_path(server)).read_text(encoding="utf-8")
        except FileNotFoundError:
            return {}, ""
        except OSError as e:
            return {}, f"server.ini cannot be read ({e})"
        p = self._reader()
        try:
            p.read_string(text)
        except configparser.Error as e:
            return {}, f"server.ini cannot be parsed ({e})"
        return {section: dict(p.items(section)) for section in p.sections()}, ""

    # --- Backup ------------------------------------------------------------------------------
    def backup_spec(self, server: GameServer) -> BackupSpec:
        import asyncio

        def _running() -> bool:
            from ..docker_client import docker

            try:
                return bool(asyncio.run(docker.state(server.container_name)).get("running"))
            except Exception:
                return True

        def _before_packing() -> None:
            """`/save`; a failure costs the save interval, not the archive.

            Avorion writes the galaxy as many individual sector files and saves by itself every
            `saveInterval` seconds (default 600). A failed `/save` therefore means the archive
            is up to ten minutes old. That deserves a warning but is no reason to create no
            backup at all.
            """
            try:
                check_console_response(
                    asyncio.run(self.console(server, "/save", timeout=120.0)),
                    command="/save", game="Avorion",
                )
            except Exception as e:
                if not _running():
                    log.info("avorion[%s]: /save skipped — the container is not running", server.key)
                    return
                log.warning("avorion[%s]: /save failed: %s", server.key, e)
                raise QuiesceWarning(
                    f"The Avorion save command failed: {e}. The archive contains the state of "
                    f"the last automatic save."
                ) from e

        return BackupSpec(
            data_dir=server.data_dir,
            world_subdirs=("galaxy",),
            # Empty: Avorion's `server.ini` lives INSIDE the galaxy directory. Nothing here can
            # be restored separately from the world, so the "configuration only" button
            # disappears instead of doing something wrong.
            config_subdirs=(),
            pre_hook=_before_packing,
        )

    # --- Config editor ------------------------------------------------------------------------
    def config_fields(self, server: GameServer) -> list[ConfigField]:
        ini = self._ini(server)
        fields: list[ConfigField] = []
        for key, label, kind, group, managed, help_text in self._FIELDS:
            section, name = key.split(".", 1)
            raw = ini.get(section, {}).get(name)
            if kind == JOIN_PASSWORD:
                fields.append(ConfigField(
                    key=key, label=label, kind=kind, value="" if raw is None else str(raw),
                    group=group, env_managed=managed, help=help_text,
                ))
                continue
            if kind == "password":
                is_set = bool(str(raw or "").strip())
                fields.append(ConfigField(
                    key=key, label=label, kind="password", value="", group=group, env_managed=managed,
                    help=(help_text + (" — currently set." if is_set else " — currently empty.")).strip(),
                ))
                continue
            value = "" if raw is None else str(raw)
            fields.append(ConfigField(
                key=key, label=label, kind=kind, value=mask_if_secret(name, value), group=group,
                env_managed=managed,
                choices=("Blacklist", "Whitelist") if name == "accessListMode" else (),
                help=help_text,
            ))
        return fields

    def set_config(self, server: GameServer, updates: dict[str, str]) -> None:
        """Writes `server.ini`, **only while the container is verifiably stopped**.

        Avorion rewrites the file at start from its saved state. A change on a running server is
        therefore not "takes effect after a restart" but "is gone after the restart", and in
        between the editor shows it as if it were set.

        The check lives HERE and not as a hint in the UI: a rule one has to read is not a rule.
        When in doubt (Docker unreachable) the server counts as running and nothing is written.
        """
        import asyncio

        from ..docker_client import docker

        try:
            state = asyncio.run(docker.state(server.container_name))
        except Exception as e:
            raise DriverError(err(
                "config.avorion_state_unknown",
                "Avorion configuration not written: the state of the container cannot be determined "
                "right now ({reason}). On a running server the change would be lost at the next start.",
                reason=str(e),
            )) from e
        if state.get("running") or state.get("restarting"):
            raise DriverError(err(
                "config.avorion_must_be_stopped",
                "Avorion has to be stopped to change its configuration: the server rewrites its "
                "server.ini on every start, so the change would be gone without a trace at the next "
                "start.",
            ))

        try:
            text = Path(self._ini_path(server)).read_text(encoding="utf-8")
        except OSError as e:
            raise DriverError(err(
                "config.avorion_ini_unreadable", "server.ini cannot be read ({path}): {reason}",
                path=self._ini_path(server), reason=str(e),
            )) from e
        p = self._reader()
        try:
            p.read_string(text)
        except configparser.Error as e:
            raise DriverError(err(
                "config.avorion_ini_invalid", "server.ini is not a valid INI file: {reason}", reason=str(e),
            )) from e

        kinds = {k: (kind, managed) for k, _l, kind, _g, managed, _h in self._FIELDS}
        changed = False
        for key, raw in updates.items():
            entry = kinds.get(key)
            if entry is None:
                continue
            kind, managed = entry
            if managed:
                continue
            section, name = key.split(".", 1)
            if kind == JOIN_PASSWORD:
                # Empty really means empty here; the user sees the value and must be able to
                # switch it off.
                value = str(raw or "")
            elif kind == "password":
                if not str(raw or "").strip():
                    continue          # empty = unchanged, do not delete
                value = str(raw)
            elif kind == "bool":
                value = "true" if coerce_value("bool", raw) else "false"
            elif kind == "int":
                number = coerce_value("int", raw)
                if not isinstance(number, int):
                    # Reject loudly instead of skipping silently. `coerce_value("int", "")` does
                    # not raise but returns the raw value, and `put_config` only checks
                    # `env_managed`; skipping would yield 200 with an audit entry, the editor
                    # would report "saved" and adopt a value that was never written.
                    raise DriverError(err(
                        "config.driver_integer_expected",
                        "{key}: expected a whole number — {value} is not one. Nothing was written.",
                        key=key, value=repr(raw),
                    ))
                value = str(number)
            else:
                value = str(raw)
            if any(c in value for c in "\r\n"):
                raise DriverError(err(
                    "config.avorion_line_break",
                    "Invalid value for {key}: line breaks would create new keys", key=repr(key),
                ))
            if not p.has_section(section):
                p.add_section(section)
            p.set(section, name, value)
            changed = True
        if not changed:
            return
        buffer = io.StringIO()
        p.write(buffer)
        try:
            # With a backup copy and an atomic swap: half a `server.ini` makes Avorion write its
            # own default on the next start, including an EMPTY `rconPassword`; the dashboard
            # would lose the console and could not restore it (`set_config` requires a stopped
            # container).
            write_config_file(self._ini_path(server), buffer.getvalue(), mode=0o600)
        except OSError as e:
            raise DriverError(err(
                "config.avorion_ini_not_writable", "server.ini cannot be written ({path}): {reason}",
                path=self._ini_path(server), reason=str(e),
            )) from e

    def ensure_config(self, server: GameServer, secret: str = "") -> None:
        """Create the galaxy directory and a `server.ini` with RCON; never overwrite.

        Without `rconPassword` in the file Avorion does NOT start the RCON interface at all
        (empty password = disabled). The dashboard would have no console, no player list and no
        save command, and none of that would look like a configuration error.
        """
        galaxy = self._galaxy(server)
        try:
            os.makedirs(galaxy, exist_ok=True)
        except OSError as e:
            raise DriverError(err(
                "config.avorion_galaxy_dir_not_creatable",
                "The Avorion galaxy folder cannot be created ({path}): {reason}",
                path=galaxy, reason=str(e),
            )) from e
        if Path(self._ini_path(server)).exists():
            return
        p = self._reader()
        p["Administration"] = {
            "name": server.display_name or server.key,
            "description": "",
            "password": "",
            "maxPlayers": "10",
            "accessListMode": "Blacklist",
        }
        from .base import game_port

        # An empty `rconPassword` disables RCON, exactly the failure this function exists to
        # prevent. `resolve_secret`, however, silently returns `""` when neither `rcon_password`
        # nor `rcon_password_env` was set at creation. Unlike Factorio, Avorion cannot help
        # itself then: the image generates no password, it simply starts without a control
        # channel. So the dashboard generates one; `_rcon_secret` reads it back from the same
        # file the server reads.
        rcon_pw = (secret or "").strip() or secrets.token_urlsafe(24)
        p["Networking"] = {
            "port": str(game_port(server) or 27000),
            "isListed": "false",
            "rconPassword": rcon_pw,
            "rconPort": str(server.rcon_port or 27015),
        }
        p["System"] = {"saveInterval": "600", "backups": "false"}
        buffer = io.StringIO()
        p.write(buffer)
        try:
            write_config_file(self._ini_path(server), buffer.getvalue(), mode=0o600)
        except OSError as e:
            raise DriverError(err(
                "config.avorion_ini_not_writable", "server.ini cannot be written ({path}): {reason}",
                path=self._ini_path(server), reason=str(e),
            )) from e

    # --- Mods --------------------------------------------------------------------------------
    def list_mods(self, server: GameServer) -> dict:
        """Workshop ids from `modconfig.lua`.

        Avorion downloads workshop mods itself; the galaxy directory only holds the list, not
        the content. What is reported here is therefore the INTENDED state; whether the server
        really loaded them only its log can tell.
        """
        try:
            text = Path(posixpath.join(self._galaxy(server), "modconfig.lua")).read_text(encoding="utf-8")
        except OSError:
            return {"workshop_ids": [], "allowed": [], "source": "modconfig.lua", "present": False}
        # `allowed` is a list of its OWN, not part of the mods. `parse_modconfig` runs over the
        # WHOLE text and would return `mods ∪ allowed`; if the frontend sent that back on apply,
        # the server would load mods that were only ALLOWED before and the access list would be
        # gone. The rollback would not catch it: additional mods do not prevent the boot, so
        # `verify_boot` reports success.
        from ..mods.preflight_avorion import ids_and_allowed

        mods, allowed, _ = ids_and_allowed(text)
        return {"workshop_ids": mods, "allowed": allowed,
                "source": "modconfig.lua", "present": True}

    # --- Moderation ---------------------------------------------------------------------------
    def moderation_spec(self, server: GameServer) -> ModerationSpec:
        return ModerationSpec(
            kick=True, ban=True, ban_ip=True, unban=True,
            access_levels=(), whitelist=True,
            reasons={
                "kick": "Only works for someone who is online right now.",
                "access_levels": "Avorion manages admin rights through the Steam IDs in "
                                 "server.ini, not through a command — the server has to be stopped "
                                 "for that anyway.",
                # The list is also READABLE (`whitelist_list`).
                "whitelist": "Only takes effect when accessListMode is set to “Whitelist”.",
            },
        )

    @staticmethod
    def _name(username: str) -> str:
        """Avorion player names may contain spaces, but no line breaks.

        Unlike Factorio the name cannot be reduced to `\\w` here without cutting names in half.
        So exactly what could start a second statement is removed.
        """
        return re.sub(r"[\x00-\x1f\x7f;\"]", "", username or "").strip()

    async def kick(self, server: GameServer, username: str, reason: str = "") -> str:
        u, r = self._name(username), sanitize_message(reason)
        return await self.console(server, f"/kick {u}" + (f" {r}" if r else ""))

    async def ban(self, server: GameServer, username: str, reason: str = "", ip: bool = False) -> str:
        u = self._name(username)
        return await self.console(server, f"/banip {u}" if ip else f"/ban {u}")

    async def unban(self, server: GameServer, username: str) -> str:
        return await self.console(server, f"/unban {self._name(username)}")

    def _access_list(self, server: GameServer, file: str) -> list[str]:
        """Read one of the access lists from the galaxy directory.

        **Only a MISSING file is empty.** Any other read error raises, the same rule as for
        Valheim and for the same reason: "I cannot look" must not turn into "nobody is listed".
        For a whitelist that is the most dangerous confusion of all.
        """
        path = posixpath.join(self._galaxy(server), file)
        try:
            text = Path(path).read_text(encoding="utf-8")
        except FileNotFoundError:
            return []
        except OSError as e:
            raise DriverError(err(
                "moderation.list_file_unreadable", "{file} cannot be read ({path}): {reason}",
                file=file, path=path, reason=str(e),
            )) from e
        return [z.strip() for z in text.splitlines() if z.strip()]

    def whitelist_list(self, server: GameServer) -> dict:
        """The whitelist, and whether it has any EFFECT.

        The list comes from the FILE, not from the console: Avorion's own documentation
        (`server.ini - readme.txt` in the galaxy directory) says about `accessListMode` verbatim
        "Black/Whitelist files can be found in the galaxy folder". That is the source the server
        uses, and it is readable even while the server is stopped.

        The hint is not decoration: `accessListMode` defaults to `Blacklist`. A carefully
        maintained whitelist then does **nothing at all**. Without that sentence the page looks
        like a closed server while everybody gets in.
        """
        entries = self._access_list(server, "whitelist.txt")
        banned = set(self._access_list(server, "blacklist.txt"))
        mode = str(self._ini(server).get("Administration", {}).get("accessListMode", "")).strip()
        active = mode.lower() == "whitelist"
        return {
            "available": True,
            "note": (
                ""
                if active
                else note(
                    "moderation.avorion_whitelist_inactive",
                    "“accessListMode” is set to “{mode}” — the whitelist currently has NO effect. "
                    "It can be switched in the configuration, which for Avorion can only be "
                    "changed while the server is stopped.",
                    mode=mode or "Blacklist",
                )
            ),
            "users": [
                {"username": e, "accesslevel": "", "banned": e in banned, "steamid": e}
                for e in entries
            ],
        }

    async def whitelist_add(self, server: GameServer, username: str) -> str:
        return await self.console(server, f"/whitelist add {self._name(username)}")

    async def whitelist_remove(self, server: GameServer, username: str) -> str:
        return await self.console(server, f"/whitelist remove {self._name(username)}")

    # --- Details -----------------------------------------------------------------------------
    def elevated_config_keys(self, server: GameServer) -> frozenset[str]:
        """`accessListMode` arms or disarms the whitelist; that is moderation power.

        This one value decides whether the list maintained by the moderators does anything at
        all. Behind `can_edit_sandbox` alone, a config maintainer without any moderation
        permission could switch off the server's access protection with one field.
        """
        return frozenset({
            "Administration.accessListMode",
            # These are not matters of taste either but switches that determine access to or
            # the security of the server.
            #
            # `isMultiplayer=false` lets only ONE administrator in; whoever flips it locks out
            # all players while the server looks perfectly healthy.
            #
            # `ExplicitCallables=false` opens, according to the vendor, "doors for a lot of
            # cheats and hacks": a client may then call any script function, not only the
            # explicitly exposed ones.
            "Networking.isMultiplayer",
            "Game.ExplicitCallables",
        })

    def safe_console_commands(self) -> frozenset[str]:
        # `/save` is deliberately NOT included: it halts the server while writing.
        #
        # `/whitelist` and `/blacklist` are out as well. They are administrative verbs
        # (`whitelist_add` sends `/whitelist add <name>`) that belong to the `can_moderate`
        # permission via `routers/moderation.py`. Unlike Factorio no read subcommand is listed:
        # this game's console verbs could not be measured when the driver was built, and a
        # guessed read form in a permission list is worse than none. The lists are visible on
        # the moderation page.
        return frozenset({"/players", "/status", "/version"})

    def overview_stats(self, server: GameServer) -> dict:
        ini = self._ini(server)
        try:
            max_players = int(str(ini.get("Administration", {}).get("maxPlayers", "")).strip())
        except (TypeError, ValueError):
            max_players = None
        mods = self.list_mods(server)
        return {
            "max_players": max_players,
            "mods_count": len(mods["workshop_ids"]) if mods["present"] else None,
        }

    async def detect_version(self, server: GameServer) -> str:
        return await self._version_from_log(server, _VERSION_RE)
