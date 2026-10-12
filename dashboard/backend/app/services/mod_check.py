"""Mod health: compares the installed mods against the Steam Workshop.

Answers the questions **only the network** can answer: does the mod still exist? Is it banned?
Is there an update? Which build is it tagged for?

Everything local (which mod IDs an item provides, which maps, which cells) comes from
`services/mods/inventory.py`, which is **fail-closed** and scoped to one server. This module must
not keep a filesystem view of its own: one that silently reports "nothing found" on a read error
yields a preflight that shows green although it could not see anything.

Best effort: without network the report degrades cleanly (`steam_ok=False`) instead of raising.
The result lives in a process-local cache per server (single-worker invariant), refreshed by the
6-hour scheduler job.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import time

import httpx

STEAM_URL = "https://api.steampowered.com/ISteamRemoteStorage/GetPublishedFileDetails/v1/"
PZ_APP_ID = 108600

# Result of the availability check per item. The three-way split is the point: "unverifiable"
# is a state of its own and must pass neither as "fine" nor as "removed".
AVAILABLE = "available"
REMOVED = "removed"
UNVERIFIABLE = "unverifiable"

log = logging.getLogger("uvicorn.error")


def workshop_url(wid: str) -> str:
    return f"https://steamcommunity.com/sharedfiles/filedetails/?id={wid}"


def _strip_modid(entry: str) -> str:
    """Strip leading backslashes — the .ini allows `\\ModID` as a spelling."""
    return entry.lstrip("\\").strip()


# ---------------------------------------------------------------- Steam Web API
async def fetch_steam(ids: list[str]) -> dict[str, dict]:
    """Workshop metadata (no API key needed). Empty dict on any error — the caller flags that
    as `steam_ok=False` instead of presenting it as "all fine"."""
    if not ids:
        return {}
    data = {"itemcount": str(len(ids))}
    for i, wid in enumerate(ids):
        data[f"publishedfileids[{i}]"] = str(wid)
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.post(STEAM_URL, data=data)
            r.raise_for_status()
            details = r.json()["response"]["publishedfiledetails"]
    except Exception as e:
        log.info("mod_check: Steam comparison not possible (%s)", e)
        return {}
    out: dict[str, dict] = {}
    for d in details:
        wid = str(d.get("publishedfileid"))
        out[wid] = {
            "title": d.get("title") or "",
            "time_updated": d.get("time_updated"),
            "banned": bool(d.get("banned")),
            "result": d.get("result"),
            "app_id": d.get("consumer_app_id"),
            "tags": [t.get("tag") for t in (d.get("tags") or []) if isinstance(t, dict)],
        }
    return out


async def probe_workshop_page(wid: str) -> str:
    """Cross-check against the workshop PAGE: is the item alive, gone, or unknown?

    **Why this second source exists.** `GetPublishedFileDetails` is the legacy endpoint and its
    `result` alone is no proof of availability: a live, downloadable item with a working page can
    return `result=9`, and so does an item that was actually removed (whose page is an error
    page). Treating `result != 1` as "removed" therefore produces false alarms — and a false
    alarm here gets an intact mod removed from a live configuration.

    **Matching on the page text does not work either.** The sentence "This item has been removed
    from the community because it violates Steam Community & Content Guidelines" is present on
    EVERY workshop page, inside a `<div class="bannedNotification" style="display: none">`, so a
    text search always finds it. The page TYPE is what differs: a live item page has
    `<title>Steam Workshop::…`, a removed one `Steam Community :: Error`.

    When in doubt `UNVERIFIABLE` — a network error is no verdict on the mod.
    """
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as c:
            r = await c.get(workshop_url(wid))
            r.raise_for_status()
            html = r.text
    except Exception as e:
        log.info("mod_check: workshop page %s could not be fetched (%s)", wid, e)
        return UNVERIFIABLE
    m = re.search(r"<title>(.*?)</title>", html, re.S | re.I)
    title = (m.group(1) if m else "").strip()
    if title.startswith("Steam Workshop::"):
        return AVAILABLE
    if "Error" in title:
        return REMOVED
    return UNVERIFIABLE


#: How many workshop pages are fetched concurrently. Each is several hundred KB of HTML, and
#: there is ONE uvicorn worker.
CROSS_CHECKS_CONCURRENT = 4


async def check_availability(ids: list[str], steam: dict[str, dict]) -> dict[str, str]:
    """Availability per item — with a cross-check exactly where the Web API is doubtful.

    The page is fetched only for suspicious items, not for every one: `result=1` is undisputed,
    so fetching every page on every report would be load without insight.

    That alone is no upper bound, though. In the very situation the cross-check exists for — the
    Steam Web API misbehaves and returns `result != 1` for MANY items — `suspicious` is the
    whole mod list. An uncapped `gather` would then open one `httpx.AsyncClient` per item at once
    and hold every several-hundred-KB HTML response in the memory of the ONE worker. The
    semaphore below bounds that.
    """
    out: dict[str, str] = {}
    suspicious: list[str] = []
    for wid in ids:
        s = steam.get(wid)
        if s is None:
            out[wid] = UNVERIFIABLE          # no Steam data — no verdict
        elif s.get("banned"):
            out[wid] = REMOVED            # Steam states `banned` explicitly; reliable
        elif s.get("result") == 1:
            out[wid] = AVAILABLE
        else:
            suspicious.append(wid)        # `result != 1` alone proves nothing → cross-check
    if suspicious:
        cap = asyncio.Semaphore(CROSS_CHECKS_CONCURRENT)

        async def _capped(wid: str):
            async with cap:
                return await probe_workshop_page(wid)

        if len(suspicious) > CROSS_CHECKS_CONCURRENT:
            log.info("mod_check: %d items need the cross-check against the workshop page — "
                     "at most %d at a time", len(suspicious), CROSS_CHECKS_CONCURRENT)
        results = await asyncio.gather(
            *(_capped(w) for w in suspicious), return_exceptions=True
        )
        for wid, e in zip(suspicious, results, strict=True):
            out[wid] = e if isinstance(e, str) else UNVERIFIABLE
    return out


# ---------------------------------------------------------------- local files
def _parse_mod_info(path: str) -> dict:
    """Read `mod.info`: provided mod IDs, `require=` dependencies, display name."""
    info = {"ids": [], "requires": [], "name": ""}
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.read().splitlines()
    except OSError:
        return info
    for line in lines:
        s = line.strip()
        if "=" not in s or s.startswith("#"):
            continue
        key, value = s.split("=", 1)
        key, value = key.strip().lower(), value.strip()
        if key == "id" and value:
            info["ids"].append(value)
        elif key == "name" and value and not info["name"]:
            info["name"] = value
        elif key == "require" and value:
            info["requires"].extend(x.strip() for x in value.split(",") if x.strip())
    return info


def read_acf(server) -> tuple[dict[str, int], bool]:
    """{workshop_id: timeupdated} from `appworkshop_108600.acf`, plus **whether the file was
    readable**.

    The flag matters: if every `OSError` (wrong permissions, vanished mount) became an empty
    dict, `local_ts` would be `None` for EVERY item and the update detection
    (`... and local_ts and int(s["time_updated"]) > int(local_ts)`) could never fire. The report
    would still say `ok` and stay green for a server whose installed state is unknown.

    The report's two sibling sources carry the same kind of flag (`plan.readable`,
    `inv.readable`) and report `MOD_LIST_UNREADABLE` / `WORKSHOP_DIR_UNREADABLE`.
    """
    path = os.path.join(server.data_dir, "workshop-mods", f"appworkshop_{PZ_APP_ID}.acf")
    result: dict[str, int] = {}
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            text = f.read()
    except FileNotFoundError:
        # A workshop collection that was never installed has no file. That is a RESULT (nothing
        # to compare), not a read error.
        return result, True
    except OSError as e:
        log.warning("mod_check[%s]: appworkshop file cannot be read (%s)", server.key, e)
        return result, False
    # Item blocks without deeper nesting: "<id>" { … "timeupdated" "<ts>" … }
    for m in re.finditer(r'"(\d+)"\s*\{([^{}]*)\}', text):
        tm = re.search(r'"timeupdated"\s*"(\d+)"', m.group(2))
        if tm:
            wid = m.group(1)
            result[wid] = max(result.get(wid, 0), int(tm.group(1)))
    return result, True


# ---------------------------------------------------------------- Report
async def run_check(server) -> dict:
    """Health report for ONE server. Local view from the inventory, the rest from the workshop."""
    from .mods import inventory, state

    plan = state.live_plan(server)
    inv = await asyncio.to_thread(inventory.scan, server)
    workshop_items = list(plan.workshop_items)

    items: list[dict] = []
    issues: list[dict] = []
    errors = warnings = 0

    def add(level: str, code: str, text: str, wid: str = "") -> None:
        nonlocal errors, warnings
        issues.append({"level": level, "code": code, "text": text, "workshop_id": wid})
        if level == "error":
            errors += 1
        else:
            warnings += 1

    # Fail closed before anything else: an unreadable .ini yields an EMPTY item list, and an
    # empty list must not pass as `steam_ok=True` with `errors=0` — "nothing to complain about"
    # for a server whose mods are unknown.
    if not plan.readable:
        add("error", "MOD_LIST_UNREADABLE",
            f"The mod list cannot be read — NOTHING was checked. {plan.detail}".strip())
    if not inv.readable:
        add("error", "WORKSHOP_DIR_UNREADABLE",
            "The workshop directory cannot be read — the local comparison is missing.")

    steam = await fetch_steam(workshop_items) if plan.readable else {}
    available = await check_availability(workshop_items, steam) if plan.readable else {}
    acf, acf_readable = await asyncio.to_thread(read_acf, server)
    if not acf_readable:
        add("error", "WORKSHOP_STATE_UNREADABLE",
            "The local installation state (appworkshop file) cannot be read — it can NOT be "
            "determined whether mods are outdated.")

    for wid in workshop_items:
        s = steam.get(wid)
        item = inv.items.get(wid)
        local_ts = acf.get(wid)
        status: list[str] = []
        title = (s or {}).get("title") or f"Workshop {wid}"
        availability = available.get(wid, UNVERIFIABLE)

        if availability == REMOVED:
            status.append("removed_or_banned")
            add("error", "WORKSHOP_ITEM_REMOVED",
                f"{title}: no longer available on the workshop — joining clients cannot load "
                f"it and get stuck while joining", wid)
        elif availability == UNVERIFIABLE:
            status.append("steam_unknown")
            # NOT a finding-free state: "could not be checked" is a statement about the check,
            # not about the mod — it must not pass as green.
            add("warn", "WORKSHOP_ITEM_UNVERIFIABLE",
                f"{title}: availability could not be checked (Steam unreachable or unclear "
                f"answer). That is NOT a finding against the mod.", wid)

        if s:
            if s.get("app_id") and int(s["app_id"]) != PZ_APP_ID:
                status.append("not_pz")
                add("error", "WORKSHOP_ITEM_WRONG_APP",
                    f"{title}: does not belong to Project Zomboid (app {s['app_id']})", wid)
            tags = s.get("tags") or []
            if tags and "Build 42" not in tags and any(t in tags for t in ("Build 40", "Build 41")):
                status.append("wrong_build")
                add("warn", "WORKSHOP_ITEM_WRONG_BUILD",
                    f"{title}: only tagged for {', '.join(tags)} (the server runs Build 42)", wid)

        if item is None:
            status.append("not_downloaded")
            add("warn", "WORKSHOP_ITEM_NOT_DOWNLOADED", f"{title}: not present locally", wid)
        elif s and s.get("time_updated") and local_ts and int(s["time_updated"]) > int(local_ts):
            status.append("update_available")
            add("warn", "WORKSHOP_ITEM_UPDATE", f"{title}: update available on the workshop", wid)

        items.append({
            "workshop_id": wid,
            "title": title,
            "url": workshop_url(wid),
            "status": status,
            "availability": availability,
            "mod_ids": list(item.mod_ids) if item else [],
            "builds": sorted(item.builds) if item else [],
            "tile_maps": list(item.tile_maps) if item else [],
            "spawn_regions": list(item.spawn_regions) if item else [],
            "installed": local_ts,
            "steam_updated": (s or {}).get("time_updated"),
        })

    # `steam_ok` describes the CHECK, not the mods: it is fine only if the list was readable AND
    # every item has a verdict. (`bool(steam) or not workshop_items` would be fail-open at both
    # ends.)
    return {
        "items": items,
        "issues": issues,
        "summary": {
            "ok": errors == 0,
            "errors": errors,
            "warnings": warnings,
            "mod_count": len(workshop_items),
            "steam_ok": plan.readable and all(
                available.get(w) != UNVERIFIABLE for w in workshop_items
            ),
            "removed": [i["workshop_id"] for i in items if i["availability"] == REMOVED],
            "list_readable": plan.readable,
            "inventory_readable": inv.readable,
            "state_readable": acf_readable,
        },
    }


# Process-local cache per server key (like the other caches: single-worker invariant).
# Value: (report, monotonic time of retrieval).
_cache: dict[str, tuple[dict, float]] = {}

#: How long a report counts as current.
#:
#: The report carries no timestamp of its own and, via `cached()`, decides the mod indicator in
#: the status bar, in `routers/server` and in the join info. Without an expiry it would show the
#: verdict on the OLD mod list for up to six hours after a mod apply — the very change it is
#: meant to judge. The expiry is shorter than the 6-hour job so that a stale report never
#: outlives the next page load.
REPORT_VALID_S = 30 * 60


async def get_report(server, force: bool = False) -> dict:
    entry = _cache.get(server.key)
    if force or entry is None or (time.monotonic() - entry[1]) > REPORT_VALID_S:
        _cache[server.key] = (await run_check(server), time.monotonic())
    return _cache[server.key][0]


def discard(server_key: str) -> None:
    """Discard this server's report as outdated.

    Called by every operation that changes the mod state. Without it the indicator would show
    the previous verdict right after a mod apply — exactly when someone is looking.
    """
    _cache.pop(server_key, None)


def cached(server_key: str) -> dict | None:
    entry = _cache.get(server_key)
    if entry is None:
        return None
    if (time.monotonic() - entry[1]) > REPORT_VALID_S:
        # Expired: better "pending" than a false assurance.
        _cache.pop(server_key, None)
        return None
    return entry[0]


async def scheduled_refresh() -> None:
    """Scheduler job: refresh the report per PZ server (errors stay inside the job)."""
    from ..models.server import GameType
    from . import servers as servers_registry

    from . import game_settings, world_lock

    try:
        # An inactive game runs no workshop check (`services/game_settings.py`).
        if not game_settings.is_active(GameType.pz):
            return
        for srv in servers_registry.list_enabled():
            if srv.game_type != GameType.pz:
                continue
            # No report from the middle of a mod operation. `run_check` reads the very folders
            # `mods/apply` is moving around (`shutil.move` into quarantine, `.ini` rewritten);
            # the result would be an intermediate state cached as current. `backup.scheduled_backup`
            # asks the same question for the same folders.
            if (holder := world_lock.held_by(srv.key)):
                log.info("mod_check[%s]: skipped — %s is running", srv.key,
                         world_lock.OPERATION_LABELS.get(holder, holder))
                continue
            await get_report(srv, force=True)
    except Exception:
        log.exception("mod_check: refresh failed")
