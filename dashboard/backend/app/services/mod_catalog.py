"""Curated Arma Reforger mod preset catalog.

Reforger modding is error-prone: GUIDs, versions, LOAD ORDER and dependencies. Instead of having
the user assemble individual mods in the right order, they are bundled into **dependency-complete,
correctly ordered presets** — one click writes the whole bundle (mods in load order, the matching
`scenarioId` and any settings the mode requires) into `game.mods`/`game.scenarioId` of the
Reforger config.json.

The structure deliberately mirrors `action_catalog.py` (frozen dataclasses,
`CATALOGS: dict[GameType, …]`, `catalog_for`/`by_id_for`/`serialize_for`).

All mod GUIDs and scenario IDs were checked against the live workshop
(`reforger.armaplatform.com/workshop/<GUID>`); the vanilla scenarios (Game Master and Conflict,
Everon/Arland each) come from the official scenario list. Pinning a mod version later keeps the
GUID — only `version` would be set.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass

from ..models.server import GameType


@dataclass(frozen=True)
class ReforgerMod:
    modId: str            # 16-hex workshop GUID (required)
    name: str = ""
    version: str = ""     # empty = latest; otherwise the pinned mod version


@dataclass(frozen=True)
class ReforgerModPreset:
    id: str
    label: str
    description: str
    kind: str                                   # "mode" | "content"
    group: str                                  # UI grouping, e.g. "Game mode" | "Realism"
    mods: tuple[ReforgerMod, ...]               # load order with dependencies FIRST
    scenarioId: str = ""
    sets_scenario: bool = False                 # True → applying also sets game.scenarioId
    config_patch: tuple[tuple[str, object], ...] = ()  # settings the mode requires (dotted path → value)


# Freedom Fighters (WCS) — the default mode: guerrilla, capture locations, persistent campaign,
# 2–8 player co-op. Everon is Reforger's flagship map, hence the default scenario.
_FF = ReforgerMod("CAFEBEEFF0CACC1A", "Freedom Fighters (WCS)")

REFORGER_PRESETS: tuple[ReforgerModPreset, ...] = (
    ReforgerModPreset(
        id="freedom_fighters",
        label="Freedom Fighters — Everon",
        description="Default mode: NPCs all over the map, capture/neutralise locations, "
        "persistent campaign (10–60 h), 2–8 player co-op. Map: Everon.",
        kind="mode",
        group="Game mode",
        mods=(_FF,),
        scenarioId="{64B2F8D8059EE270}Missions/FreedomFighters/Everon.conf",
        sets_scenario=True,
        # FF needs cross-faction VON, otherwise faction radios do not transmit.
        config_patch=(("game.gameProperties.VONCanTransmitCrossFaction", True),),
    ),
    ReforgerModPreset(
        id="freedom_fighters_arland",
        label="Freedom Fighters — Arland",
        description="Freedom Fighters on the smaller map Arland (a more compact campaign).",
        kind="mode",
        group="Game mode",
        mods=(_FF,),
        scenarioId="{E2EC49F13FBAC56F}Missions/FreedomFighters/Arland.conf",
        sets_scenario=True,
        config_patch=(("game.gameProperties.VONCanTransmitCrossFaction", True),),
    ),
    ReforgerModPreset(
        id="freedom_fighters_kolguyev",
        label="Freedom Fighters — Kolguyev",
        description="Freedom Fighters on Kolguyev (the third, larger FF map).",
        kind="mode",
        group="Game mode",
        mods=(_FF,),
        scenarioId="{3AD5C3E6BB400639}Missions/FreedomFighters/Kolguyev.conf",
        sets_scenario=True,
        config_patch=(("game.gameProperties.VONCanTransmitCrossFaction", True),),
    ),
    ReforgerModPreset(
        id="freedom_fighters_plus",
        label="Freedom Fighters+ — Weapons & Vehicles",
        description="Add-on content for Freedom Fighters (apply on top of an active FF mode): many weapons "
        "(SMGs/LMGs/launchers/bolt guns/shotguns/rifles/FAL), vehicles (M1 Abrams, T-72, BMP-1, M113, Mi-24V, "
        "Mi-8, motorbike) + ACE + weather/ambience + characters. The resolved FF Plus dependency tree "
        "(~40 mods, large download on first start). Does NOT change the scenario or the save — it sits on "
        "top of the running FF mode.",
        kind="content",
        group="Realism",
        # FF Plus dependencies (without the FF base mod — the mode preset supplies it).
        # Frameworks/cores first, then content. Car Radio4All (621D3771875C1D3D) is left out on
        # purpose: partly broken and purely cosmetic.
        mods=(
            # Frameworks / Cores
            ReforgerMod("5D6EA74A94173EDF", "Enfusion Database Framework"),
            ReforgerMod("5D6EBC81EB1842EF", "Enfusion Persistence Framework"),
            ReforgerMod("5E389BB9F58B79A6", "SpaceCore"),
            ReforgerMod("62CCD69DD17E4F2F", "AKI_Core"),
            ReforgerMod("60C4CE4888FF4621", "ACE Core"),
            ReforgerMod("606C369BAC3F6CC3", "ACE Finger"),
            ReforgerMod("611CB1D409001EB0", "ACE Magazine Repack"),
            ReforgerMod("66D6340C1D565C98", "Chungus Core"),
            ReforgerMod("64C912EF952E1075", "ToH ReCharacters Core"),
            ReforgerMod("629B2BA37EFFD577", "WCS_Armaments"),
            # Weapons
            ReforgerMod("60E6F54E174C53C5", "Chungus SMGs"),
            ReforgerMod("61344BDC155A5A28", "Chungus LMGs"),
            ReforgerMod("6190F1B505C08562", "Chungus Launchers"),
            ReforgerMod("61BD6595183FCEBD", "Chungus Bolt Guns"),
            ReforgerMod("620E584B1D2C96A4", "Chungus Shotguns"),
            ReforgerMod("62A711001B8FDEEA", "Chungus Rifles"),
            ReforgerMod("623E0EA4ED5F44D5", "FAL"),
            # Vehicles
            ReforgerMod("5D1880C4AD410C14", "M1 Abrams"),
            ReforgerMod("5E0AB16BEB16D6A4", "T-72 Main Battle Tank"),
            ReforgerMod("5E524E4FEECCA92B", "BMP-1 IFV"),
            ReforgerMod("5E5C154FEE1094BB", "M113 APC"),
            ReforgerMod("61B84088181A8FA4", "Motorbike"),
            ReforgerMod("628933A0D3A0D700", "WCS_Mi-24V"),
            ReforgerMod("631D09484433EB0A", "FIA Mi-8"),
            ReforgerMod("617AC5E57EF1D9E3", "Zagoria 89 Vehicles"),
            ReforgerMod("6528F296E88F783F", "RTG Launches"),
            # Characters (ToH ReCharacters)
            ReforgerMod("596330D9AF34AF38", "ToH ReCharacters Man"),
            ReforgerMod("65FB4A6CFFC4079C", "ToH ReCharacters European"),
            ReforgerMod("65FB50F7FA33DECA", "ToH ReCharacters African"),
            ReforgerMod("65FB5184A09FE8A1", "ToH ReCharacters Asian"),
            ReforgerMod("65FB51CDA61F1071", "ToH ReCharacters Greek"),
            ReforgerMod("65FB5234B4A8E20B", "ToH ReCharacters Livonian"),
            ReforgerMod("65FB5265B4F72F4B", "ToH ReCharacters Russian"),
            ReforgerMod("65FB52DAABB24B05", "ToH ReCharacters Tanoan"),
            ReforgerMod("65FB762EB503CCF9", "ToH ReCharacters Persian"),
            # QoL / environment
            ReforgerMod("612851D73DF01668", "WCS_FlipPush"),
            ReforgerMod("612F512CD4CB21D5", "WCS_Earplugs"),
            ReforgerMod("64ED6553B8AF6B62", "Atmospheric Weather Mod"),
            ReforgerMod("6528C95796EBEDE0", "Environmental Ambience Mod"),
            ReforgerMod("65134A128837CAA7", "Cold War Colour"),
            # (The FF base mod is deliberately NOT listed: the FF mode preset supplies it; this is add-on content only.)
        ),
    ),
    ReforgerModPreset(
        id="overthrow_eden",
        label="Overthrow — Eden",
        description="Antistasi successor: open world, liberate towns, persistent campaign state "
        "(Enfusion persistence). An alternative to the default mode.",
        kind="mode",
        group="Game mode",
        mods=(
            # Dependencies FIRST: Enfusion Database Framework → Enfusion Persistence Framework → Overthrow.
            ReforgerMod("5D6EA74A94173EDF", "Enfusion Database Framework"),
            ReforgerMod("5D6EBC81EB1842EF", "Enfusion Persistence Framework"),
            ReforgerMod("59B657D731E2A11D", "Overthrow"),
        ),
        scenarioId="{3DAD390C31623F04}Missions/24_OVT_Eden.conf",
        sets_scenario=True,
    ),
    # --- Vanilla modes (no mods needed) ---
    ReforgerModPreset(
        id="game_master_everon",
        label="Game Master — Everon",
        description="Zeus-style sandbox: spawn units/objects live as an admin, control AI and "
        "weather. A player has to be listed under “Admins / Game Masters” to open the GM menu.",
        kind="mode",
        group="Game mode",
        mods=(),
        scenarioId="{59AD59368755F41A}Missions/21_GM_Eden.conf",
        sets_scenario=True,
    ),
    ReforgerModPreset(
        id="game_master_arland",
        label="Game Master — Arland",
        description="Game Master (Zeus) on the smaller map Arland.",
        kind="mode",
        group="Game mode",
        mods=(),
        scenarioId="{2BBBE828037C6F4B}Missions/22_GM_Arland.conf",
        sets_scenario=True,
    ),
    ReforgerModPreset(
        id="conflict_everon",
        label="Conflict — Everon",
        description="Official PvP/PvE campaign: capture bases and locations, faction war. Vanilla, no mods.",
        kind="mode",
        group="Game mode",
        mods=(),
        scenarioId="{ECC61978EDCC2B5A}Missions/23_Campaign.conf",
        sets_scenario=True,
    ),
    ReforgerModPreset(
        id="conflict_arland",
        label="Conflict — Arland",
        description="Conflict campaign on Arland (more compact). Vanilla, no mods.",
        kind="mode",
        group="Game mode",
        mods=(),
        scenarioId="{C41618FD18E9D714}Missions/23_Campaign_Arland.conf",
        sets_scenario=True,
    ),
    ReforgerModPreset(
        id="realism_addon",
        label="Realism pack (RHS + ACE + GM Enhanced)",
        description="Optional add-on: content pack RHS – Status Quo + realism framework ACE "
        "+ Game Master Enhanced. Larger download; verify the GUIDs before enabling it.",
        kind="content",
        group="Realism",
        mods=(
            ReforgerMod("595F2BF2F44836FB", "RHS – Status Quo"),
            ReforgerMod("60C4E0B49618CC62", "ACE"),
            ReforgerMod("5964E0B3BB7410CE", "Game Master Enhanced"),
        ),
    ),
)


# ------------------------------------------------------------------ Registry (as in action_catalog)
CATALOGS: dict[GameType, tuple[ReforgerModPreset, ...]] = {
    GameType.arma_reforger: REFORGER_PRESETS,
}

# Default preset per game (seeds a fresh config.json, see ReforgerDriver.initial_config).
DEFAULT_PRESET_ID: dict[GameType, str] = {
    GameType.arma_reforger: "freedom_fighters",
}


def catalog_for(game_type: GameType) -> tuple[ReforgerModPreset, ...]:
    return CATALOGS.get(game_type, ())


def by_id_for(game_type: GameType, preset_id: str) -> ReforgerModPreset | None:
    return {p.id: p for p in catalog_for(game_type)}.get(preset_id)


def default_preset_for(game_type: GameType) -> ReforgerModPreset | None:
    pid = DEFAULT_PRESET_ID.get(game_type)
    return by_id_for(game_type, pid) if pid else None


def serialize_for(game_type: GameType) -> list[dict]:
    # The JSON round trip normalises the dataclass tuples (mods/config_patch) to plain lists, so
    # the contract with the frontend (mods: RMod[]) is unambiguous regardless of the encoder.
    return [json.loads(json.dumps(asdict(p))) for p in catalog_for(game_type)]
