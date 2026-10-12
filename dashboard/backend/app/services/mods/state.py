"""Draft ↔ live: what is configured, what is running, and is it healthy?

The **draft** is the only place where editing happens; it lives in the database and does not
touch the running server. **Live** is what is actually in the `.ini`. Only "apply" (see
`apply.py`) transfers the draft.

The **status light** (`status()`) is deliberately cheap: file access only, no network. It
answers the question one has when looking at the dashboard: "is my mod configuration OK?".
"""
from __future__ import annotations

import json
import logging
import time

from sqlmodel import Session, select

from ...db import engine
from ...models.mod_set import APPLIED, DRAFT, ModSet
from ...models.player import utcnow_naive
from .. import ini_editor as ini
from . import inventory
from .preflight import WARNING, Finding, ModPlan, PreflightResult, active_spawn_regions, check

log = logging.getLogger("uvicorn.error")

# The status light walks the mod directory, so cache it briefly to keep frontend polling from
# scanning the disk all the time. Process-local (single-worker invariant).
STATUS_TTL_SECONDS = 30.0
_status_cache: dict[str, tuple[float, dict]] = {}


# --------------------------------------------------------------------- live (.ini)

def live_plan(server) -> ModPlan:
    """What the server has loaded RIGHT NOW (from its .ini), server-scoped."""
    try:
        data = ini.get_ini_at(ini.ini_path(server))
    except FileNotFoundError:
        # **Not there yet is different from not readable.** PZ only writes the `.ini` on first
        # boot; a blocker here would make a freshly created server unstartable. `detail` stays
        # empty; that is how `preflight.check` tells the two cases apart.
        return ModPlan(readable=False)
    except OSError as e:
        # An empty `ModPlan()` here would be indistinguishable from "this server has no mods":
        # `preflight.check` would find nothing, `status()` would report green and `guard_boot`
        # would return an empty blocker list.
        return ModPlan(readable=False, detail=f"{ini.ini_path(server)}: {e}")
    return ModPlan(
        workshop_items=tuple(_split(data.get("WorkshopItems", ""))),
        mods=tuple(_split(data.get("Mods", ""))),
        maps=tuple(_split(data.get("Map", ""))),
    )


def _split(value: str) -> list[str]:
    return [x.strip() for x in value.split(";") if x.strip()]


# --------------------------------------------------------------------- draft (DB)

def _row_to_plan(row: ModSet) -> ModPlan:
    return ModPlan(
        workshop_items=tuple(json.loads(row.workshop_items or "[]")),
        mods=tuple(json.loads(row.mods or "[]")),
        maps=tuple(json.loads(row.maps or "[]")),
    )


def get_draft(server) -> ModPlan:
    """Read the draft. If none exists, it is created from the live state on first access, so
    one never starts from nothing and "apply" without editing is a no-op."""
    with Session(engine) as s:
        row = _draft_row(s, server)
        if row is None:
            plan = live_plan(server)
            _write_draft(s, server, plan)
            return plan
        return _row_to_plan(row)


def set_draft(server, plan: ModPlan) -> ModPlan:
    with Session(engine) as s:
        _write_draft(s, server, plan)
    _status_cache.pop(server.key, None)
    return plan


def reset_draft(server) -> ModPlan:
    """Discard the draft and set it back to the running state.

    **An unreadable live state is NOT written to the database.** Otherwise a read error on the
    `.ini` would permanently empty the draft, and the next apply would deselect all mods
    although nobody asked for that.
    """
    live = live_plan(server)
    if not live.readable and live.detail:
        from fastapi import HTTPException, status as http

        from ...errors import err
        raise HTTPException(
            http.HTTP_409_CONFLICT,
            detail=err(
                "mods.ini_unreadable_reset_refused",
                "The server configuration (.ini) cannot be read ({error}) — a reset would "
                "empty the draft instead of bringing it to the running state.",
                error=live.detail,
            ),
        )
    return set_draft(server, live)


def _draft_row(s: Session, server) -> ModSet | None:
    return s.exec(
        select(ModSet).where(ModSet.server_id == server.id, ModSet.kind == DRAFT)
    ).first()


def _write_draft(s: Session, server, plan: ModPlan) -> ModSet:
    row = _draft_row(s, server)
    if row is None:
        row = ModSet(server_id=server.id, kind=DRAFT)
    row.workshop_items = json.dumps(list(plan.workshop_items))
    row.mods = json.dumps(list(plan.mods))
    row.maps = json.dumps(list(plan.maps))
    row.updated_at = utcnow_naive()
    s.add(row)
    s.commit()
    s.refresh(row)
    return row


def snapshot_applied(server, plan: ModPlan, spawn_regions: list[str], label: str = "") -> int:
    """Create an immutable record of what is actually running now. Returns its ID."""
    with Session(engine) as s:
        row = ModSet(
            server_id=server.id, kind=APPLIED, label=label,
            workshop_items=json.dumps(list(plan.workshop_items)),
            mods=json.dumps(list(plan.mods)),
            maps=json.dumps(list(plan.maps)),
            spawn_regions=json.dumps(list(spawn_regions)),
        )
        s.add(row)
        s.commit()
        s.refresh(row)
        return row.id


def last_good(server) -> ModPlan | None:
    """The last successfully applied set: the place to return to after a failure."""
    with Session(engine) as s:
        row = s.exec(
            select(ModSet)
            .where(ModSet.server_id == server.id, ModSet.kind == APPLIED)
            .order_by(ModSet.id.desc())
        ).first()
        return _row_to_plan(row) if row else None


# --------------------------------------------------------------------- draft operations
#
# Every edit goes through here and nowhere else. If edits wrote straight to the server's `.ini`,
# a half-finished configuration could go live at the next restart (scheduled, watchdog, idle
# autostop) without anyone having pressed "apply".

def _sorted_maps(plan: ModPlan, inv) -> tuple[str, ...]:
    """Normalise the map list: mod maps first, vanilla last, the base map at the very end.

    The base map MUST be the last entry, otherwise it covers the mod maps.
    """
    from .preflight import BASE_MAP, VANILLA_MAPS

    seen: set[str] = set()
    modded, vanilla, base = [], [], []
    vanilla_lower = {m.lower() for m in VANILLA_MAPS}
    for entry in (e.strip() for e in plan.maps):
        if not entry or entry.lower() in seen:
            continue
        seen.add(entry.lower())
        if entry.lower() == BASE_MAP.lower():
            base.append(entry)
        elif entry.lower() in vanilla_lower:
            vanilla.append(entry)
        else:
            modded.append(entry)
    if not base:
        base = [BASE_MAP]      # without the base map the mod maps' vanilla references do not resolve
    return tuple(modded + vanilla + base)


def _with_maps(plan: ModPlan, maps) -> ModPlan:
    from dataclasses import replace as _replace

    return _replace(plan, maps=tuple(maps))


def add_item(server, wid: str) -> ModPlan:
    """Add an item, its mod IDs and its tile maps to the draft (respecting the load order)."""
    from . import inventory as inv_mod

    wid = wid.strip()
    plan = get_draft(server)
    inv = inv_mod.scan(server)
    item = inv.items.get(wid)

    items = tuple(plan.workshop_items) + ((wid,) if wid not in plan.workshop_items else ())
    mods = list(plan.mods)
    maps = list(plan.maps)
    if item is not None:
        for mid in item.mod_ids:
            if mid not in mods:
                mods.append(mid)
        for name in item.tile_maps:
            if name.lower() not in {m.lower() for m in maps}:
                maps.append(name)
    new = ModPlan(workshop_items=items, mods=tuple(sort_load_order(server, mods)), maps=tuple(maps))
    return set_draft(server, _with_maps(new, _sorted_maps(new, inv)))


def remove_item(server, wid: str) -> ModPlan:
    """Take an item with its mod IDs and maps out of the draft.

    The files stay where they are; they only move into quarantine on apply, and only AFTER the
    preflight has passed.
    """
    from . import inventory as inv_mod

    wid = wid.strip()
    plan = get_draft(server)
    inv = inv_mod.scan(server)
    item = inv.items.get(wid)
    owned_mods = set(item.mod_ids) if item else set()
    owned_maps = {m.lower() for m in (item.map_dirs if item else {})}

    new = ModPlan(
        workshop_items=tuple(x for x in plan.workshop_items if x != wid),
        mods=tuple(m for m in plan.mods if _strip(m) not in owned_mods),
        maps=tuple(m for m in plan.maps if m.strip().lower() not in owned_maps),
    )
    return set_draft(server, _with_maps(new, _sorted_maps(new, inv)))


def set_load_order(server, mods: list[str]) -> ModPlan:
    from dataclasses import replace as _replace

    return set_draft(server, _replace(get_draft(server), mods=tuple(m.strip() for m in mods if m.strip())))


def set_maps(server, maps: list[str]) -> ModPlan:
    from . import inventory as inv_mod

    plan = _with_maps(get_draft(server), [m.strip() for m in maps if m.strip()])
    return set_draft(server, _with_maps(plan, _sorted_maps(plan, inv_mod.scan(server))))


def sort_load_order(server, mods: list[str]) -> list[str]:
    """Move dependencies in front of their dependants (stable depth-first search, cycles are tolerated)."""
    from . import inventory as inv_mod

    inv = inv_mod.scan(server)
    requires: dict[str, set[str]] = {}
    for item in inv.items.values():
        for mid in item.mod_ids:
            requires.setdefault(mid, set()).update(item.requires)

    order: list[str] = []
    seen: set[str] = set()

    def visit(mid: str, trail: frozenset) -> None:
        if mid in seen or mid in trail:
            return
        for dep in sorted(requires.get(mid, ())):
            if dep in mods:
                visit(dep, trail | {mid})
        if mid not in seen:
            seen.add(mid)
            order.append(mid)

    for mid in mods:
        visit(mid, frozenset())
    return order


def _strip(mod_id: str) -> str:
    from ..mod_check import _strip_modid

    return _strip_modid(mod_id)


# --------------------------------------------------------------------- diff & status light

def diff(live: ModPlan, draft: ModPlan) -> dict:
    """What would "apply" change? Map order matters, item order does not."""
    def added(a, b):
        return [x for x in b if x not in set(a)]

    return {
        "items_added": added(live.workshop_items, draft.workshop_items),
        "items_removed": added(draft.workshop_items, live.workshop_items),
        "mods_added": added(live.mods, draft.mods),
        "mods_removed": added(draft.mods, live.mods),
        "maps_added": added(live.maps, draft.maps),
        "maps_removed": added(draft.maps, live.maps),
        "order_changed": list(live.mods) != list(draft.mods)
                         and sorted(live.mods) == sorted(draft.mods),
        "map_order_changed": list(live.maps) != list(draft.maps)
                             and sorted(live.maps) == sorted(draft.maps),
    }


def has_changes(d: dict) -> bool:
    return any(bool(v) for v in d.values())


def summarize(d: dict) -> str:
    """Short form for the history: "+1 mod, −2 maps"."""
    parts = []
    for label, plus, minus in (
        ("mod", "mods_added", "mods_removed"),
        ("map", "maps_added", "maps_removed"),
    ):
        for sign, n in (("+", len(d[plus])), ("−", len(d[minus]))):
            if n:
                parts.append(f"{sign}{n} {label}{'' if n == 1 else 's'}")
    if d["order_changed"] or d["map_order_changed"]:
        parts.append("order changed")
    return ", ".join(parts) or "no change"


def status(server, *, force: bool = False) -> dict:
    """The status light: is the running configuration sound, and does the draft differ from it?

    The **live** state is checked, because that is the question that counts ("is my server
    running on a healthy configuration?"). It also reports whether an unapplied draft exists.
    """
    cached = _status_cache.get(server.key)
    if cached and not force and (time.monotonic() - cached[0]) < STATUS_TTL_SECONDS:
        return cached[1]

    live = live_plan(server)
    inv = inventory.scan(server)
    result = _pz_live_result(live, inv)
    draft = get_draft(server)
    changes = diff(live, draft)

    # The workshop finding from the last comparison counts too. It comes from the CACHE: this
    # function must not touch the network, it sits behind every status call of the UI. If it
    # is missing (never ran), that is reported rather than glossed over as "OK".
    from .. import mod_check

    report = mod_check.cached(server.key)
    steam_errors = list((report or {}).get("issues") or []) if report else []
    steam_errors = [i for i in steam_errors if i.get("level") == "error"]

    if result.blockers:
        level, headline = "blocked", _headline(result.blockers[0].message)
    elif steam_errors:
        level, headline = "blocked", _headline(steam_errors[0]["text"])
    elif result.warnings:
        level, headline = "warn", _headline(result.warnings[0].message)
    elif report is None:
        level, headline = "warn", "Workshop check still pending — everything is fine locally"
    elif not report["summary"].get("steam_ok"):
        level, headline = "warn", "Workshop check incomplete — everything is fine locally"
    else:
        level, headline = "ok", "Mod configuration is fine"

    data = {
        "level": level,
        "headline": headline,
        "blockers": len(result.blockers),
        "warnings": len(result.warnings),
        "steam_errors": len(steam_errors),
        "steam_ok": bool(report) and bool(report["summary"].get("steam_ok")),
        "mods": len(live.clean_mod_ids()),
        "maps": len([m for m in live.maps if m.strip()]),
        "has_pending_changes": has_changes(changes),
        "pending_summary": summarize(changes) if has_changes(changes) else "",
    }
    _status_cache[server.key] = (time.monotonic(), data)
    return data


def invalidate(server_key: str) -> None:
    _status_cache.pop(server_key, None)


def _headline(text: str, limit: int = 120) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def spawn_regions_for(plan: ModPlan, server) -> list[str]:
    return active_spawn_regions(plan, inventory.scan(server))


# --------------------------------------------------------------------- boot gate

def _folder_unreadable(detail: str) -> Finding:
    """The one finding for "the dashboard cannot read this server's mod folder".

    A WARNING, never a blocker (owner's decision): the game server reads its mods itself, and a
    permission problem on the dashboard's side must not keep a server down — not on a start,
    a scheduled restart, an autoheal, a version change or a restore. Applying a draft stays
    fail-closed (`preflight.check`, `foreign_apply`): there the dashboard would write blind.
    """
    return Finding(
        code="MOD_FOLDER_UNREADABLE",
        level=WARNING,
        message="The dashboard cannot read the server's mod folder, so the mod configuration "
                "was not checked. Starts are not blocked by this; fix the folder permissions to "
                "get the check back.",
        detail=detail,
    )


def _pz_live_result(live: ModPlan, inv) -> PreflightResult:
    """`preflight.check` for the LIVE state — with an unreadable mod folder as a warning.

    `check` answers for the apply path too and blocks there (`WORKSHOP_DIR_UNREADABLE`); for the
    running configuration the same state is `MOD_FOLDER_UNREADABLE`, as for every other game.
    """
    result = check(live, inv)
    if any(f.code == "WORKSHOP_DIR_UNREADABLE" for f in result.findings):
        return PreflightResult([_folder_unreadable(inv.detail)])
    return result


async def live_check(server):
    """The preflight result of the LIVE configuration, for every game that has one.

    **The single choke point.** Both the mod status light (`routers/mod_config._health`) and
    the boot gate (`guard_boot`) ask here, so the two cannot answer the same question
    differently.

    Returns `None` when this game has no preflight (then there is nothing to block).
    """
    from ...models.server import GameType

    if server.game_type == GameType.pz:
        return _pz_live_result(live_plan(server), inventory.scan(server))

    if server.game_type == GameType.avorion:
        from . import preflight_avorion

        return await preflight_avorion.check(server)

    from . import foreign

    if server.game_type in foreign.FOLDERS_ARE_MODS or server.game_type in foreign.EXTENSIONS:
        try:
            mods = foreign.scan(server)      # raises on an unreadable folder, on purpose
        except OSError as e:
            return PreflightResult([_folder_unreadable(str(e))])
        active = {m.filename for m in mods if m.enabled}
        return await foreign.check(server, active, mods)

    return None


async def guard_boot(server) -> list:
    """Blockers of the LIVE configuration: the guard before every start/restart.

    The preflight must not hang on "apply" alone: a scheduled restart, a watchdog autoheal, a
    manual action or a version change would otherwise boot the unchecked configuration.

    It covers every game with a preflight, not just Project Zomboid. For file-based games such
    as Factorio an incomplete mod set is the most expensive mistake: no crash visible in the
    log, but clients that can no longer join.

    Deliberately defensive: it must never block an operation through an error of its own. If
    it cannot check, it returns an empty list (a crash INSIDE the guard must not prevent the
    server start). A mod folder the dashboard cannot read is the expected form of "cannot
    check": `live_check` reports it as the warning `MOD_FOLDER_UNREADABLE`, which does not
    block and is logged here.
    """
    try:
        result = await live_check(server)
    except Exception:  # the guard must never cause an outage itself
        log.warning("mods[%s]: boot gate could not check the mod configuration", server.key,
                    exc_info=True)
        return []
    if result is None:
        return []
    for finding in result.warnings:
        if finding.code == "MOD_FOLDER_UNREADABLE":
            log.warning("mods[%s]: mod folder not readable, start not blocked (%s)",
                        server.key, finding.detail)
    return result.blockers


def guard_message(blockers: list) -> str:
    """Plain text for the rejection: never block silently."""
    first = blockers[0].message if blockers else ""
    extra = f" (and {len(blockers) - 1} more)" if len(blockers) > 1 else ""
    return (f"The mod configuration is faulty — a start would damage the server: "
            f"{first}{extra}")
