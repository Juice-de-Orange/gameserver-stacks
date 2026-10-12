"""Curated RCON action catalogue (game master): spawning + events + weather + players.

Instead of free text: validated, audited presets. Every entry has a template with
{placeholders} that are filled ONLY from validated parameters (no raw strings).

CAUTION (B42 `unstable`): command names/signatures can drift. Commands with `note`/`danger`
often require an in-game admin position that RCON does not have, so test them once live
through the free-text console before relying on them.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass

from ..models.server import GameType


@dataclass(frozen=True)
class ParamSpec:
    name: str
    kind: str               # player | item | int | float | str | coords | toggle
    label: str = ""
    required: bool = True
    default: str = ""
    placeholder: str = ""
    choices: tuple[str, ...] = ()


@dataclass(frozen=True)
class ActionSpec:
    id: str
    label: str
    group: str              # display group, e.g. Spawning | Events | Weather | Player
    template: str
    params: tuple[ParamSpec, ...] = ()
    needs_online_player: bool = False
    danger: bool = False
    note: str = ""


PZ_CATALOG: tuple[ActionSpec, ...] = (
    # --- Spawning ---
    ActionSpec(
        "additem", "Give item", "Spawning", 'additem "{player}" {item} {count}',
        params=(
            ParamSpec("player", "player", "Player"),
            ParamSpec("item", "item", "Item-ID", placeholder="Base.Axe"),
            ParamSpec("count", "int", "Count", required=False, default="1"),
        ),
        needs_online_player=True,
    ),
    ActionSpec(
        "addvehicle", "Spawn vehicle", "Spawning", 'addvehicle "{vehicle}" "{player}"',
        params=(
            ParamSpec("vehicle", "token", "Vehicle ID", placeholder="Base.VanAmbulance"),
            ParamSpec("player", "player", "at the player"),
        ),
        needs_online_player=True,
        note="Spawns at the player's location.",
    ),
    ActionSpec(
        "addxp", "Grant XP", "Spawning", 'addxp "{player}" {perk}={amount}',
        params=(
            ParamSpec("player", "player", "Player"),
            ParamSpec("perk", "token", "Skill", placeholder="Strength"),
            ParamSpec("amount", "int", "XP amount", default="100"),
        ),
        needs_online_player=True,
    ),
    # --- Events ---
    ActionSpec(
        "createhorde", "Spawn zombie horde", "Events", "createhorde {count}",
        params=(ParamSpec("count", "int", "Number of zombies", default="10"),),
        danger=True,
        note="Usually needs an in-game admin position — test it live over RCON.",
    ),
    ActionSpec("chopper", "Helicopter event", "Events", "chopper",
               note="Triggers the helicopter event (attracts zombies)."),
    ActionSpec("gunshot", "Gunshot sound", "Events", "gunshot",
               note="Attracts zombies."),
    ActionSpec("alarm", "House alarm", "Events", "alarm",
               note="Alarm at the admin's location — may have no effect over RCON."),
    # --- Weather ---
    ActionSpec("startrain", "Start rain", "Weather", "startrain"),
    ActionSpec("stoprain", "Stop rain", "Weather", "stoprain"),
    ActionSpec("startstorm", "Start storm", "Weather", "startstorm"),
    ActionSpec("stopweather", "Stop weather", "Weather", "stopweather"),
    ActionSpec(
        "thunder", "Thunder", "Weather", 'thunder "{player}"',
        params=(ParamSpec("player", "player", "above player"),),
        needs_online_player=True, note="The argument form varies in B42 — test it live.",
    ),
    ActionSpec(
        "lightning", "Lightning", "Weather", 'lightning "{player}"',
        params=(ParamSpec("player", "player", "above player"),),
        needs_online_player=True, note="The argument form varies in B42 — test it live.",
    ),
    # --- Players ---
    ActionSpec(
        "teleport", "Teleport to player", "Players", 'teleport "{player}" "{target}"',
        params=(
            ParamSpec("player", "player", "Player"),
            ParamSpec("target", "player", "to player"),
        ),
        needs_online_player=True,
    ),
    ActionSpec(
        "teleportto", "To coordinates", "Players", "teleportto {coords}",
        params=(ParamSpec("coords", "coords", "x,y,z", placeholder="12345,6789,0"),),
        danger=True,
        note="Acts on the executing player — may have no effect over RCON.",
    ),
    ActionSpec(
        "godmod", "God mode", "Players", 'godmod "{player}" -{toggle}',
        params=(
            ParamSpec("player", "player", "Player"),
            ParamSpec("toggle", "toggle", "on/off", default="true"),
        ),
        needs_online_player=True,
    ),
    ActionSpec(
        "invisible", "Invisible", "Players", 'invisible "{player}" -{toggle}',
        params=(
            ParamSpec("player", "player", "Player"),
            ParamSpec("toggle", "toggle", "on/off", default="true"),
        ),
        needs_online_player=True,
    ),
    ActionSpec(
        "noclip", "Noclip (walls)", "Players", 'noclip "{player}" -{toggle}',
        params=(
            ParamSpec("player", "player", "Player"),
            ParamSpec("toggle", "toggle", "on/off", default="true"),
        ),
        needs_online_player=True,
    ),
    ActionSpec(
        "servermsg", "Server message", "Players", 'servermsg "{text}"',
        params=(ParamSpec("text", "str", "Message", placeholder="Attention everyone…"),),
    ),
)

# ------------------------------------------------------------------ Minecraft
# MC commands WITHOUT a leading slash (sent via RCON). Player placeholders are NOT quoted (MC
# targets are bare names; validation ensures the name is one reported online by
# `driver.players`). Additional param kinds: mc_item / mc_entity (namespaced-id regex) and
# enum (value ∈ choices).
MC_CATALOG: tuple[ActionSpec, ...] = (
    # --- Give & summon ---
    ActionSpec(
        "mc_give", "Give item", "Give & summon", "give {player} {item} {count}",
        params=(
            ParamSpec("player", "player", "Player"),
            ParamSpec("item", "mc_item", "Item-ID", placeholder="minecraft:diamond"),
            ParamSpec("count", "int", "Count", required=False, default="1"),
        ),
        needs_online_player=True,
    ),
    ActionSpec(
        "mc_summon", "Summon creature", "Give & summon",
        "execute at {player} run summon {entity}",
        params=(
            ParamSpec("player", "player", "at player"),
            ParamSpec("entity", "mc_entity", "Entity-ID", placeholder="minecraft:zombie"),
        ),
        needs_online_player=True, note="Appears at the player's position.",
    ),
    # --- World & time ---
    ActionSpec(
        "mc_weather", "Set weather", "World & time", "weather {state} {duration}",
        params=(
            ParamSpec("state", "enum", "Weather", choices=("clear", "rain", "thunder")),
            ParamSpec("duration", "int", "Duration (s)", required=False, default="600"),
        ),
    ),
    ActionSpec(
        "mc_time", "Set time", "World & time", "time set {value}",
        params=(ParamSpec("value", "enum", "Time of day", choices=("day", "noon", "night", "midnight")),),
    ),
    ActionSpec(
        "mc_difficulty", "Difficulty", "World & time", "difficulty {level}",
        params=(ParamSpec("level", "enum", "Level", choices=("peaceful", "easy", "normal", "hard")),),
        danger=True, note="Applies to the whole server.",
    ),
    ActionSpec(
        "mc_keepinv", "Keep inventory on death", "World & time", "gamerule keepInventory {value}",
        params=(ParamSpec("value", "enum", "On/Off", choices=("true", "false")),), danger=True,
    ),
    ActionSpec(
        "mc_daylight", "Day/night cycle", "World & time", "gamerule doDaylightCycle {value}",
        params=(ParamSpec("value", "enum", "On/Off", choices=("true", "false")),),
    ),
    # --- Players ---
    ActionSpec(
        "mc_gamemode", "Game mode", "Players", "gamemode {mode} {player}",
        params=(
            ParamSpec("mode", "enum", "Mode", choices=("survival", "creative", "adventure", "spectator")),
            ParamSpec("player", "player", "Player"),
        ),
        needs_online_player=True,
    ),
    ActionSpec(
        "mc_tp", "Teleport to player", "Players", "tp {player} {target}",
        params=(
            ParamSpec("player", "player", "Player"),
            ParamSpec("target", "player", "to player"),
        ),
        needs_online_player=True,
    ),
    ActionSpec(
        "mc_effect", "Give effect", "Players",
        "effect give {player} minecraft:{effect} {duration} {amplifier}",
        params=(
            ParamSpec("player", "player", "Player"),
            ParamSpec("effect", "enum", "Effect", choices=(
                "speed", "regeneration", "strength", "resistance",
                "night_vision", "jump_boost", "haste", "fire_resistance",
            )),
            ParamSpec("duration", "int", "Duration (s)", required=False, default="30"),
            ParamSpec("amplifier", "int", "Strength (0 = I)", required=False, default="0"),
        ),
        needs_online_player=True,
    ),
    ActionSpec(
        "mc_clear_effects", "Remove effects", "Players", "effect clear {player}",
        params=(ParamSpec("player", "player", "Player"),), needs_online_player=True,
    ),
    ActionSpec(
        "mc_xp", "Give experience", "Players", "xp add {player} {amount} {kind}",
        params=(
            ParamSpec("player", "player", "Player"),
            ParamSpec("amount", "int", "Amount", default="10"),
            ParamSpec("kind", "enum", "Kind", choices=("levels", "points")),
        ),
        needs_online_player=True,
    ),
    # --- Server ---
    ActionSpec(
        "mc_say", "Server message", "Server", "say {text}",
        params=(ParamSpec("text", "str", "Message", placeholder="Attention everyone…"),),
    ),
)


# ------------------------------------------------------------------ Vintage Story
# VS commands WITHOUT a slash (via the "Vintage RCON" mod). Deliberately small: `announce` is
# verified (the driver uses it for broadcasts); further verbs depend on the mod and carry a
# note (like the uncertain PZ entries).
VS_CATALOG: tuple[ActionSpec, ...] = (
    ActionSpec(
        "vs_announce", "Server message", "Server", "announce {text}",
        params=(ParamSpec("text", "str", "Message", placeholder="Attention everyone…"),),
    ),
    ActionSpec(
        "vs_time", "Set time", "World & time", "time set {value}",
        params=(ParamSpec("value", "enum", "Time of day", choices=("day", "night", "noon")),),
        note="The VS command set depends on the mod — check it once through the console if needed.",
    ),
)


# ------------------------------------------------------------------ Factorio
# **Every entry here was measured against a running server** (RCON, Factorio 2.0.77), not copied
# from a wiki. The responses are noted on the entries where they explain something. This
# matters: for Avorion the same measurement showed that seven of the twelve commands listed by
# the hosting wikis do not exist on the real server.
FACTORIO_CATALOG: tuple[ActionSpec, ...] = (
    # --- Players ---
    ActionSpec(
        "fac_players", "Who is online", "Players", "/players online",
    ),
    ActionSpec(
        "fac_admins", "List admins", "Players", "/admins",
    ),
    ActionSpec(
        "fac_promote", "Promote to admin", "Players", "/promote {player}",
        params=(ParamSpec("player", "player", "Player"),),
        note="Factorio answers /promote and /demote with EMPTY text — even on success. "
             "“List admins” tells whether it worked.",
    ),
    ActionSpec(
        "fac_demote", "Revoke admin rights", "Players", "/demote {player}",
        params=(ParamSpec("player", "player", "Player"),),
        note="Also answers with empty text — check with “List admins”.",
    ),
    ActionSpec(
        "fac_mute", "Mute", "Players", "/mute {player}",
        params=(ParamSpec("player", "player", "Player"),),
        needs_online_player=True,
        note="Measured: an unknown name is answered with “Player … doesn't exist.”",
    ),
    ActionSpec(
        "fac_unmute", "Unmute", "Players", "/unmute {player}",
        params=(ParamSpec("player", "player", "Player"),),
    ),
    ActionSpec("fac_mutes", "List muted players", "Players", "/mutes"),
    ActionSpec(
        "fac_purge", "Purge chat history", "Players", "/purge {player}",
        params=(ParamSpec("player", "player", "Player"),),
        note="Removes this player's previous chat messages.",
    ),
    ActionSpec("fac_bans", "Show ban list", "Players", "/bans"),
    ActionSpec("fac_whitelist", "Show whitelist", "Players", "/whitelist get"),
    # --- Server ---
    ActionSpec(
        "fac_shout", "Server message", "Server", "/shout {text}",
        params=(ParamSpec("text", "str", "Message", placeholder="Attention everyone…"),),
        note="Answers with empty text — the message appears in the game chat, not in the console.",
    ),
    ActionSpec(
        "fac_save", "Save now", "Server", "/server-save",
        note="Answers “Saving the map”. The server pauses briefly while it saves.",
    ),
    ActionSpec(
        "fac_config_get", "Look up setting", "Server", "/config get {option}",
        params=(ParamSpec("option", "enum", "Setting", choices=(
            # The twelve options the running server actually answered. `game-password` and
            # `non-blocking-saving` are deliberately NOT included: they came back as
            # "Unknown config" although they appear in server-settings.json.
            "afk-auto-kick", "allow-commands", "autosave-interval", "autosave-only-on-server",
            "ignore-player-limit-for-returning-players", "max-players", "max-upload-speed",
            "only-admins-can-pause", "password", "require-user-verification",
            "visibility-lan", "visibility-public",
        )),),
    ),
    # --- Information ---
    ActionSpec("fac_version", "Game version", "Information", "/version"),
    ActionSpec("fac_seed", "Map seed", "Information", "/seed"),
    ActionSpec("fac_time", "World time", "Information", "/time"),
    ActionSpec(
        "fac_evolution", "Biter evolution", "Information", "/evolution",
        note="Shows the evolution factor and where it comes from (time / pollution / nests).",
    ),
    # --- Lua ---
    # `/silent-command` instead of `/c`: the latter echoes the executed line into the chat, so
    # the output would appear twice. Both were measured and work.
    ActionSpec(
        "fac_time_of_day", "Set time of day", "World",
        "/silent-command game.surfaces[1].daytime = {value}",
        params=(ParamSpec("value", "enum", "Time", choices=("0", "0.25", "0.5", "0.75")),),
        danger=True,
        note="Lua command. On a server this depends on the setting “allow commands” "
             "(/config get allow-commands) — if it is “false”, nothing happens.",
    ),
    ActionSpec(
        "fac_heal", "Heal player", "World",
        '/silent-command game.get_player("{player}").character.health = 250',
        params=(ParamSpec("player", "player", "Player"),),
        needs_online_player=True,
        danger=True,
        note="Lua command; raises an error if the player has no character (e.g. in the map editor).",
    ),
)


# ------------------------------------------------------------------ Avorion
# **The source is `/help` on a running server** (Avorion 2.5.13, RCON); the output is kept as
# `tests/fixtures/avorion_commands.txt`.
#
# **Two plausible-looking sources were refuted by it:**
# * The HOSTING WIKIS list twelve commands; seven of them do not exist: `/tp`, `/maxplayers`,
#   `/password`, `/pvp`, `/sector`, `/regeneratesector`, `/clearsector`. (`/teleport` exists,
#   `/tp` does not.)
# * The `admin.xml` that the server ITSELF writes into the galaxy folder lists `factionstats`,
#   which the server answers with "Unknown command". Conversely it lacks `/admin`, `/stop` and
#   `/workers`. It is the permission management, not the command set.
#
# **Known limit:** `/help` proves name and intent, not the argument form. Entries with
# parameters therefore carry either a measured form or a `note`.
AVORION_CATALOG: tuple[ActionSpec, ...] = (
    # --- Players ---
    ActionSpec("av_players", "Who is online", "Players", "/players"),
    ActionSpec(
        "av_playerinfo", "Player information", "Players", "/playerinfo {player}",
        params=(ParamSpec("player", "player", "Player"),),
        note="`/help` says “Print players and their details”. With no player on the server the "
             "answer came back empty — so the argument form is not conclusively proven.",
    ),
    # --- Server ---
    ActionSpec(
        "av_say", "Server message", "Server", "/say {text}",
        params=(ParamSpec("text", "str", "Message", placeholder="Attention everyone…"),),
    ),
    ActionSpec(
        "av_save", "Save now", "Server", "/save",
        note="The server halts while it writes — which is why /save is not among the "
             "safe-mode verbs.",
    ),
    ActionSpec(
        "av_status", "Server status", "Server", "/status",
        note="Player count, loaded sectors, server load and update times.",
    ),
    ActionSpec(
        "av_pause", "Pause server", "Server", "/pause",
        danger=True,
        note="Only works if `pausable` is switched on in the configuration — and on a "
             "public server only with at most one player online.",
    ),
    # --- Information ---
    ActionSpec("av_version", "Game version", "Information", "/version"),
    ActionSpec(
        "av_seed", "Server seed", "Information", "/seed",
        note="Measured: returns `0` — that is NOT the galaxy seed from server.ini "
             "(which holds e.g. `ExampleSeed`).",
    ),
    ActionSpec(
        "av_groups", "Permission groups", "Information", "/groups -l",
        note="Without an option `/groups` answers “Invalid usage” and lists its switches — "
             "`-l` is the one that shows the groups.",
    ),
    ActionSpec(
        "av_whitelist_status", "Whitelist status", "Information", "/whitelist -s",
        note="Tells whether the whitelist is active at all. It only takes effect if "
             "`accessListMode` is set to “Whitelist”.",
    ),
)


# ------------------------------------------------------------------ Registry
CATALOGS: dict[GameType, tuple[ActionSpec, ...]] = {
    GameType.pz: PZ_CATALOG,
    GameType.minecraft: MC_CATALOG,
    GameType.vintagestory: VS_CATALOG,
    GameType.factorio: FACTORIO_CATALOG,
    GameType.avorion: AVORION_CATALOG,
}


def catalog_for(game_type: GameType) -> tuple[ActionSpec, ...]:
    return CATALOGS.get(game_type, ())


def by_id_for(game_type: GameType, action_id: str) -> ActionSpec | None:
    return {a.id: a for a in catalog_for(game_type)}.get(action_id)


def serialize_for(game_type: GameType) -> list[dict]:
    return [asdict(a) for a in catalog_for(game_type)]

