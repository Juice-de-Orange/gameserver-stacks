"""Catalog of selectable game versions — feeds the version dropdown, refreshed daily.

`version_manager` can CHANGE versions; this module adds the **supply side**: which versions
exist, since when, and what changed. A daily scheduler job refreshes the catalog, after which
new versions show up in the dropdown. **Nothing is ever installed automatically** — the job
only writes the list.

Sources (all without key/login; once a day, so rate limits are not a concern):

| Game          | List                                    | Changes                           |
|---------------|-----------------------------------------|-----------------------------------|
| PZ            | Docker Hub tags of the server image     | Steam announcements (app 108600)  |
| Minecraft     | PaperMC v3 (TYPE=PAPER) or Mojang       | Mojang patch notes                |
| Vintage Story | official stable index                   | — (no machine-readable source)    |
| Factorio      | Docker Hub tags of the server image     | —                                 |
| Avorion       | Docker Hub tags of the server image     | —                                 |

Important for image-based games (PZ, Factorio, Avorion): the list comes from the **built
images**, not from Steam. A version published on Steam becomes selectable only once the
community image for it exists — anything else would be an offer that fails with "no such image"
on change.

Best effort like `mod_check.py`: if a source fails, the last known state stays (rows are NOT
deleted) and the caller sees `source_ok=False`.
"""
from __future__ import annotations

import datetime as dt
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace

import httpx
from sqlmodel import Session, select

from ..db import engine
from ..models.player import utcnow_naive
from ..models.server import GameServer, GameType
from ..models.version_catalog import VersionCatalogEntry
from .container_spec import image_ref
from .version_manager import parse_ver

log = logging.getLogger("uvicorn.error")

# --- Source URLs --------------------------------------------------------------------------
PZ_IMAGE_REPO = "danixu86/project-zomboid-dedicated-server"
PZ_TAGS_URL = f"https://hub.docker.com/v2/repositories/{PZ_IMAGE_REPO}/tags?page_size=100&ordering=last_updated"
PZ_NEWS_URL = (
    "https://api.steampowered.com/ISteamNews/GetNewsForApp/v2/"
    "?appid=108600&count=100&maxlength=400&feeds=steam_community_announcements"
)
PAPER_URL = "https://fill.papermc.io/v3/projects/paper"
MOJANG_MANIFEST_URL = "https://piston-meta.mojang.com/mc/game/version_manifest_v2.json"
MOJANG_PATCHNOTES_URL = "https://launchercontent.mojang.com/v2/javaPatchNotes.json"
VS_STABLE_URL = "https://api.vintagestory.at/stable.json"
FACTORIO_IMAGE_REPO = "factoriotools/factorio"
AVORION_IMAGE_REPO = "rfvgyhn/avorion"
FACTORIO_TAGS_URL = (
    f"https://hub.docker.com/v2/repositories/{FACTORIO_IMAGE_REPO}/tags"
    "?page_size=100&ordering=last_updated"
)
AVORION_TAGS_URL = (
    f"https://hub.docker.com/v2/repositories/{AVORION_IMAGE_REPO}/tags"
    "?page_size=100&ordering=last_updated"
)

_UA = "stacks-console/1.0 (+https://github.com/Juice-de-Orange/gameserver-stacks)"
# Cap per (game, variant): Mojang lists 900+ versions — a dropdown with 900 entries is unusable,
# and realistically nobody wants to go back further than the last few dozen versions.
# The cap is deliberately PER CHANNEL: with a global cap Mojang's many snapshots would push the
# stable releases (the ones people look for) out of the list.
MAX_PER_CHANNEL = {"stable": 40, "unstable": 20, "snapshot": 10, "legacy": 10}
MAX_ENTRIES = sum(MAX_PER_CHANNEL.values())
# Minimum interval for refilling an empty catalog — without it every page load would query all
# upstream endpoints again while a source is permanently down.
LAZY_REFRESH_COOLDOWN = dt.timedelta(minutes=30)
# "NEW" window: how long after first being seen a version counts as new in the dropdown.
NEW_WINDOW = dt.timedelta(days=7)
# When a list counts as STALE. The refresh runs daily; two missed runs are no longer a blip
# but a dead source.
SOURCE_STALE_AFTER = dt.timedelta(days=2, hours=12)

CHANNEL_ORDER = ("stable", "unstable", "snapshot", "legacy")
CHANNEL_LABEL = {
    "stable": "Stable",
    "unstable": "Unstable",
    "snapshot": "Preview",
    "legacy": "Legacy (Build 41)",
}


@dataclass(frozen=True)
class CatalogEntry:
    """One selectable version. `ref` is what a version change writes to the registry."""

    version: str
    ref: str
    channel: str
    released_at: dt.datetime | None = None
    changelog_title: str = ""
    changelog_url: str = ""
    changelog_summary: str = ""


# --- small helpers (pure, testable) ------------------------------------------------------
def _iso(value) -> dt.datetime | None:
    """ISO 8601 string → naive UTC (column convention, cf. log_parser._parse_ts)."""
    if not value:
        return None
    txt = str(value).strip()
    if txt.endswith("Z"):
        txt = txt[:-1] + "+00:00"
    try:
        parsed = dt.datetime.fromisoformat(txt)
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        return parsed.astimezone(dt.timezone.utc).replace(tzinfo=None)
    return parsed


def _unix(value) -> dt.datetime | None:
    try:
        return dt.datetime.fromtimestamp(int(value), dt.timezone.utc).replace(tzinfo=None)
    except (TypeError, ValueError, OSError):
        return None


_BBCODE_RE = re.compile(r"\[/?[^\]\n]{1,24}\]")


def _plain(text: str, limit: int = 300) -> str:
    """Reduce Steam BBCode/whitespace to a short plain-text excerpt."""
    clean = _BBCODE_RE.sub("", text or "")
    clean = re.sub(r"\s+", " ", clean).strip()
    return clean[:limit].rstrip() + ("…" if len(clean) > limit else "")


# Pre-release markers in the version name: "26.2-rc-2", "1.21.9-pre2", "26.3-snapshot-6".
# ("-release"/"-unstable" of the PZ tags are NOT pre-releases — the channel separates those.)
_PRE_RE = re.compile(r"[-_](?:rc|pre|snapshot|beta|alpha)", re.I)


def base_version(version: str) -> tuple:
    """Numeric core WITHOUT pre-release suffix: "26.2-rc-2" → (26, 2)."""
    return parse_ver(re.split(r"[-_]", version or "", maxsplit=1)[0])


def sort_key(version: str) -> tuple:
    """(core, final-before-pre-release, pre-release number).

    Comparing plain number sequences is NOT enough: `parse_ver("26.2-rc-2")` is (26,2,2) and thus
    greater than (26,2) for "26.2" — the release candidate would sit above the final version in
    the dropdown. So the core is ranked separately and the pre-release number only fine-sorts
    WITHIN the core.
    """
    base = base_version(version)
    if not _PRE_RE.search(version or ""):
        return (base, 1, ())
    return (base, 0, parse_ver(version)[len(base):])


def sort_entries(entries: list[CatalogEntry]) -> list[CatalogEntry]:
    """Newest first; channel rank breaks ties (stable before unstable before snapshot)."""
    def key(e: CatalogEntry):
        rank = CHANNEL_ORDER.index(e.channel) if e.channel in CHANNEL_ORDER else len(CHANNEL_ORDER)
        return (sort_key(e.version), -rank)

    return sorted(entries, key=key, reverse=True)


def limit_entries(entries: list[CatalogEntry]) -> list[CatalogEntry]:
    """Keep the newest N per channel (order is preserved).

    A global cap would cut off vanilla Minecraft's stable releases behind the many snapshots —
    exactly the versions people look for in the dropdown.
    """
    kept: dict[str, int] = {}
    out: list[CatalogEntry] = []
    for e in entries:
        quota = MAX_PER_CHANNEL.get(e.channel, 10)
        if kept.get(e.channel, 0) >= quota:
            continue
        kept[e.channel] = kept.get(e.channel, 0) + 1
        out.append(e)
    return out


# --- Parsers per source (network-free = the test surface) --------------------------------
# Tag convention of the image: "<version>-release" (public/stable) or "<version>-unstable".
# `latest`, `latest-release`, `latest-unstable` deliberately do NOT match — a moving target
# contradicts the repo rule "versions pinned, no implicit latest".
_PZ_TAG_RE = re.compile(r"^(?P<ver>\d+(?:\.\d+)+)-(?P<ch>release|unstable)$")
# Mojang's weekly snapshots ("26w14a") — a namespace of their own, not comparable as versions.
_CALENDAR_SNAPSHOT_RE = re.compile(r"^\d{2}w\d{1,2}[a-z]$", re.I)


def parse_pz_tags(payload: dict) -> list[CatalogEntry]:
    out: list[CatalogEntry] = []
    for row in (payload or {}).get("results") or []:
        name = str(row.get("name") or "").strip()
        m = _PZ_TAG_RE.match(name)
        if not m:
            continue
        version = m.group("ver")
        if m.group("ch") == "unstable":
            channel = "unstable"
        else:
            # Build 41 is the frozen legacy line (Steam branch `legacy41`) — its own channel, so
            # it does not sit next to the current stable versions in the dropdown.
            channel = "legacy" if parse_ver(version)[:1] < (42,) else "stable"
        out.append(CatalogEntry(
            version=version, ref=name, channel=channel, released_at=_iso(row.get("last_updated"))
        ))
    return out


# Version numbers in announcement titles: "Build 42.20.0 Stable Released",
# "42.16.2 UNSTABLE Hotfix Released", "Stable(41.78.19) + UNSTABLE(42.16.3) Hotfixes Released".
_VER_IN_TEXT_RE = re.compile(r"(\d{2,3}\.\d{1,2}(?:\.\d{1,2})?)")


def parse_pz_changelogs(payload: dict) -> dict[str, dict]:
    """{version: {title,url,summary,date}} from the official Steam announcements.

    One post can cover SEVERAL versions (combined hotfixes) → every match in the title is mapped.
    The feed is newest-first, so `setdefault` lets the most recent post per version win.
    """
    out: dict[str, dict] = {}
    for item in ((payload or {}).get("appnews") or {}).get("newsitems") or []:
        title = str(item.get("title") or "").strip()
        versions = _VER_IN_TEXT_RE.findall(title)
        if not versions:
            continue
        info = {
            "title": title,
            "url": str(item.get("url") or ""),
            "summary": _plain(str(item.get("contents") or "")),
            "date": _unix(item.get("date")),
        }
        for version in versions:
            out.setdefault(version, info)
    return out


def parse_paper_versions(payload: dict) -> list[CatalogEntry]:
    """PaperMC v3 returns `versions` as {major version: [full versions…]} → flatten.

    (The old v2 API answers 410 Gone and is no longer an option.)
    """
    out: list[CatalogEntry] = []
    for versions in ((payload or {}).get("versions") or {}).values():
        for raw in versions or []:
            version = str(raw).strip()
            if not version:
                continue
            pre = re.search(r"-(?:rc|pre|snapshot)", version, re.I)
            out.append(CatalogEntry(
                version=version, ref=version, channel="snapshot" if pre else "stable"
            ))
    return out


def parse_mojang_versions(payload: dict) -> list[CatalogEntry]:
    out: list[CatalogEntry] = []
    for row in (payload or {}).get("versions") or []:
        version = str(row.get("id") or "").strip()
        kind = str(row.get("type") or "")
        if not version or kind not in ("release", "snapshot"):
            continue
        if _CALENDAR_SNAPSHOT_RE.match(version):
            # Weekly snapshots ("26w14a") live in their own namespace and cannot be sorted
            # sensibly against "26.3-snapshot-6" — they would top the dropdown although they
            # are older. Anyone who really needs them uses the expert field.
            continue
        out.append(CatalogEntry(
            version=version, ref=version,
            channel="stable" if kind == "release" else "snapshot",
            released_at=_iso(row.get("releaseTime")),
        ))
    return out


def parse_mojang_patchnotes(payload: dict) -> dict[str, dict]:
    """Official Java patch notes. There is NO derivable minecraft.net URL (the entries only
    carry a hash path) → title and short text are returned, deliberately without a link."""
    out: dict[str, dict] = {}
    for entry in (payload or {}).get("entries") or []:
        version = str(entry.get("version") or "").strip()
        if not version:
            continue
        out.setdefault(version, {
            "title": str(entry.get("title") or ""),
            "url": "",
            "summary": _plain(str(entry.get("shortText") or "")),
            "date": _iso(entry.get("date")),
        })
    return out


def parse_vs_versions(payload: dict) -> list[CatalogEntry]:
    """Vintage Story stable index: {"1.22.6": {"linuxserver": {…}, …}, …}.

    Only versions WITH a `linuxserver` build are selectable — exactly those the runner image
    downloads (`images/vintagestory/entrypoint.sh`, same index, same key).
    """
    out: list[CatalogEntry] = []
    for version, assets in (payload or {}).items():
        if not isinstance(assets, dict) or "linuxserver" not in assets:
            continue
        out.append(CatalogEntry(version=str(version), ref=str(version), channel="stable"))
    return out


def apply_notes(entries: list[CatalogEntry], notes: dict[str, dict]) -> list[CatalogEntry]:
    """Attach change notes by exact version number (no fuzzy matching: better no note than
    the note of a different version)."""
    out: list[CatalogEntry] = []
    for e in entries:
        note = notes.get(e.version)
        if not note:
            out.append(e)
            continue
        out.append(replace(
            e,
            changelog_title=note.get("title") or "",
            changelog_url=note.get("url") or "",
            changelog_summary=note.get("summary") or "",
            released_at=e.released_at or note.get("date"),
        ))
    return out


# --- Network access (thin wrappers — monkeypatched in tests) -----------------------------
async def _get_json(url: str):
    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as c:
            r = await c.get(url, headers={"User-Agent": _UA})
            r.raise_for_status()
            return r.json()
    except Exception as e:
        log.warning("version_catalog: %s cannot be fetched (%s)", url, e)
        return None


async def fetch_pz(variant: str = "") -> list[CatalogEntry]:
    entries: list[CatalogEntry] = []
    url = PZ_TAGS_URL
    for _ in range(3):  # cap against endless pagination; 300 tags are far more than MAX_ENTRIES
        payload = await _get_json(url)
        if not payload:
            break
        entries += parse_pz_tags(payload)
        url = str(payload.get("next") or "")
        if not url:
            break
    if not entries:
        return []
    return apply_notes(entries, parse_pz_changelogs(await _get_json(PZ_NEWS_URL) or {}))


async def fetch_minecraft(variant: str = "") -> list[CatalogEntry]:
    entries: list[CatalogEntry] = []
    if (variant or "").strip().upper() == "PAPER":
        # "Source down" and "source says: no versions" are two different things here.
        # `_get_json` returns `None` on ANY error. Falling back to Mojang's list when PaperMC is
        # briefly unreachable would store VANILLA versions under the variant PAPER: `refresh_for`
        # would see a non-empty list, report `ok=True` and refresh `fetched_at`, disarming the
        # staleness check — and the dropdown would offer versions that do not exist for Paper.
        payload = await _get_json(PAPER_URL)
        if payload is None:
            log.error("version_catalog: PaperMC unreachable — NOT falling back to the "
                      "vanilla list (it would be useless for a Paper server and would "
                      "count as freshly checked)")
            return []
        entries = parse_paper_versions(payload)
    if not entries and (variant or "").strip().upper() != "PAPER":
        # Not a Paper server (VANILLA/FABRIC/FORGE).
        entries = parse_mojang_versions(await _get_json(MOJANG_MANIFEST_URL) or {})
    if not entries:
        return []
    return apply_notes(entries, parse_mojang_patchnotes(await _get_json(MOJANG_PATCHNOTES_URL) or {}))


async def fetch_vintagestory(variant: str = "") -> list[CatalogEntry]:
    return parse_vs_versions(await _get_json(VS_STABLE_URL) or {})


# Factorio tags: `2.0.77` is a selectable version, `2.0`/`2`/`latest`/`stable` are moving
# pointers and `-rootless` is another build flavour of the same game. Only the exact
# three-part form goes into the list — a moving target in the dropdown would be a pin that
# pins nothing.
_FACTORIO_TAG_RE = re.compile(r"^(?P<ver>\d+\.\d+\.\d+)$")
_FACTORIO_STABLE_RE = re.compile(r"^stable-(?P<ver>\d+\.\d+\.\d+)$")


def parse_factorio_tags(
    names: list[str], times: dict[str, str] | None = None, variant: str = ""
) -> list[CatalogEntry]:
    """Docker Hub tag names → catalog entries; the channel comes from the `stable-…` tags of the
    SAME repo.

    Why no second source: `factorio.com/api/latest-releases` would know the channel more
    directly but would be a second point of failure for information the payload already
    contains — factoriotools pushes a `stable-<version>` tag for every stable build.

    Known limit: if no `stable-…` tag is found at all (pagination, changed naming scheme),
    everything counts as "stable" rather than everything as "unstable". A list in which every
    version is marked as a pre-release would be the worse error — it would sit in the dropdown
    under a channel that has to be expanded first.
    """
    stable = {m.group("ver") for n in names if (m := _FACTORIO_STABLE_RE.match(n.strip()))}
    times = times or {}
    # The build flavour belongs to the `ref`, not the `version`: `2.0.77` and `2.0.77-rootless`
    # are the same GAME VERSION in two image flavours. Only the one the server can actually run
    # is selectable — switching flavour would change the user the container runs as, i.e. a
    # permissions problem instead of a version change.
    suffix = f"-{variant}" if variant else ""
    present = {n.strip() for n in names}
    out: list[CatalogEntry] = []
    for name in names:
        m = _FACTORIO_TAG_RE.match(name.strip())
        if not m:
            continue
        version = m.group("ver")
        ref = version + suffix
        if suffix and ref not in present:
            continue      # this version does not exist in the flavour in use
        channel = "stable" if (not stable or version in stable) else "unstable"
        out.append(CatalogEntry(
            version=version, ref=ref, channel=channel, released_at=_iso(times.get(ref, times.get(name)))
        ))
    return out


async def _hub_tags(start_url: str) -> tuple[list[str], dict[str, str]]:
    """All tag names of a Docker Hub repo (up to 3 pages) plus their `last_updated`.

    The cap is the same as in `fetch_pz`: 300 tags are far more than `MAX_ENTRIES`, and without
    it a daily run could hang on endless pagination. `fetch_pz` keeps its own loop because its
    parsing is interleaved with the page sequence.
    """
    names: list[str] = []
    times: dict[str, str] = {}
    url = start_url
    for _ in range(3):
        payload = await _get_json(url)
        if not payload:
            break
        for row in payload.get("results") or []:
            name = str(row.get("name") or "").strip()
            if not name:
                continue
            names.append(name)
            times[name] = str(row.get("last_updated") or "")
        url = str(payload.get("next") or "")
        if not url:
            break
    return names, times


async def fetch_factorio(variant: str = "") -> list[CatalogEntry]:
    """Selectable is what was actually built as an image — as with PZ.

    Deliberately no change notes: Factorio's changelog is free text in the game's
    `changelog.txt`, not a retrievable list per version. Better no note than a guessed one.
    """
    names, times = await _hub_tags(FACTORIO_TAGS_URL)
    return parse_factorio_tags(names, times, variant)


# Avorion tags: `2.5.13.44140` (four parts — the last is the build number) and the same
# version with `-beta`. `latest`/`stable` are moving pointers and are dropped.
_AVORION_TAG_RE = re.compile(r"^(?P<ver>\d+\.\d+\.\d+\.\d+)(?P<beta>-beta)?$")


def parse_avorion_tags(names: list[str], times: dict[str, str] | None = None) -> list[CatalogEntry]:
    """Docker Hub tag names → catalog entries; `-beta` is the pre-release channel.

    Here the channel is unambiguous in the tag name — unlike Factorio, no second tag pattern is
    needed.
    """
    times = times or {}
    out: list[CatalogEntry] = []
    for name in names:
        m = _AVORION_TAG_RE.match(name.strip())
        if not m:
            continue
        out.append(CatalogEntry(
            version=m.group("ver") + (m.group("beta") or ""),
            ref=name.strip(),
            channel="unstable" if m.group("beta") else "stable",
            released_at=_iso(times.get(name)),
        ))
    return out


async def fetch_avorion(variant: str = "") -> list[CatalogEntry]:
    """Selectable is what was built as an image.

    The image is updated infrequently — after an Avorion update it can take a while until a new
    version shows up here. That is not a catalog error; it shows what can actually be pulled.
    """
    names, times = await _hub_tags(AVORION_TAGS_URL)
    return parse_avorion_tags(names, times)


FETCHERS: dict[GameType, Callable[[str], Awaitable[list[CatalogEntry]]]] = {
    GameType.pz: fetch_pz,
    GameType.minecraft: fetch_minecraft,
    GameType.vintagestory: fetch_vintagestory,
    GameType.factorio: fetch_factorio,
    GameType.avorion: fetch_avorion,
}


# --- Registry integration -----------------------------------------------------------------
def variant_for(server: GameServer) -> str:
    """What this server's version list is additionally split by.

    Two cases, both statements about the game — not about the version source:
    * **Minecraft**: not every version is built for Paper; the list depends on the server type.
    * **Factorio** has two image flavours per version. The dashboard runs the `-rootless` one
      (see `FactorioDriver.container_user`), and only what exists in THAT flavour may be
      selectable — otherwise a version change would also change the user the container runs as.
    """
    if server.game_type == GameType.minecraft:
        return (server.server_type or "").strip().upper()
    if server.game_type == GameType.factorio:
        tag = (server.image_tag or "").strip()
        return "rootless" if tag.endswith("-rootless") else ""
    return ""


def current_ref(server: GameServer) -> str:
    """What is currently running — from the image tag or the VERSION env, depending on
    `version_source`.

    Delegates to `drivers.running_version` so the rule exists exactly once
    (`tests/test_the_version_has_one_source.py` guards that).
    """
    from .drivers import running_version

    return running_version(server)


async def current_ref_detected(server: GameServer) -> str:
    """Like `current_ref`, but falls back to the version REPORTED by the server.

    Vintage Story deliberately runs without `VERSION` set — `current_ref` is empty there, so
    without this fallback no catalog entry would be marked as running and `update_available`
    would always be `None`. The detected value is only read, never written: pinning it in the
    registry would set `VERSION` on the next rebuild and could trigger a world migration (see
    the comment in the VS driver).
    """
    ref = current_ref(server)
    if ref:
        return ref
    try:
        from .drivers import get_driver

        return await get_driver(server).detect_version(server)
    except Exception:
        return ""


def _game_key(game_type) -> str:
    return game_type.value if isinstance(game_type, GameType) else str(game_type)


def _store(game_type: str, variant: str, entries: list[CatalogEntry]) -> dict:
    """Replace catalog rows. `first_seen_at` survives (it drives the "NEW" marker)."""
    now = utcnow_naive()
    new = 0
    with Session(engine) as s:
        existing = {
            r.ref: r
            for r in s.exec(
                select(VersionCatalogEntry).where(
                    VersionCatalogEntry.game_type == game_type,
                    VersionCatalogEntry.variant == variant,
                )
            ).all()
        }
        seen: set[str] = set()
        for e in entries:
            seen.add(e.ref)
            row = existing.get(e.ref)
            if row is None:
                row = VersionCatalogEntry(
                    game_type=game_type, variant=variant, ref=e.ref, first_seen_at=now
                )
                new += 1
            row.version = e.version
            row.channel = e.channel
            row.released_at = e.released_at
            # Empty notes do not overwrite existing ones. The change notes come from a SECOND,
            # independent source whose failure `_get_json` swallows silently. If the tag list is
            # reachable and the news endpoint is not, every entry arrives without a note —
            # writing those empty strings would wipe the stored titles, links and excerpts for
            # good (this table is their only store) while `refresh_for` reports `ok=True`.
            #
            # The cost: an entry that really has no note any more keeps the old one. That is
            # the cheaper error — a stale note can be read and dismissed, a deleted one is gone.
            if e.changelog_title:
                row.changelog_title = e.changelog_title
            if e.changelog_url:
                row.changelog_url = e.changelog_url
            if e.changelog_summary:
                row.changelog_summary = e.changelog_summary
            row.fetched_at = now
            s.add(row)
        # Remove withdrawn tags/versions — the table is a cache, not an archive.
        for ref, row in existing.items():
            if ref not in seen:
                s.delete(row)
        s.commit()
    return {"total": len(entries), "new": new}


async def refresh_for(game_type, variant: str = "") -> dict:
    """Refresh one catalog. Returns `ok=False` if the source yielded nothing — the previous
    state is then deliberately kept (better old than empty)."""
    key = _game_key(game_type)
    try:
        resolved = game_type if isinstance(game_type, GameType) else GameType(key)
    except ValueError:
        resolved = None  # unknown game type → treated like "no source" instead of raising here
    fetch = FETCHERS.get(resolved) if resolved is not None else None
    if fetch is None:
        return {"game_type": key, "variant": variant, "ok": False, "total": 0, "new": 0,
                "detail": "there is no version source for this game"}
    entries = limit_entries(sort_entries(await fetch(variant)))
    if not entries:
        return {"game_type": key, "variant": variant, "ok": False, "total": 0, "new": 0,
                "detail": "source unreachable or empty"}
    result = _store(key, variant, entries)
    return {"game_type": key, "variant": variant, "ok": True, **result}


def _registered_targets() -> list[tuple[GameType, str]]:
    """(game type, variant) of all enabled servers of ACTIVE games with a version source — only
    what is needed. An inactive game queries no upstream (`services/game_settings.py`)."""
    from . import game_settings
    from . import servers as servers_registry

    active = game_settings.active_games()
    targets: list[tuple[GameType, str]] = []
    for srv in servers_registry.list_enabled():
        if srv.game_type in FETCHERS and srv.game_type in active:
            pair = (srv.game_type, variant_for(srv))
            if pair not in targets:
                targets.append(pair)
    return targets


async def refresh_all() -> list[dict]:
    """Each target on its own — a broken one does not stop the others.

    `refresh_for` catches nothing around its `await fetch(variant)`, and `_get_json` only
    swallows NETWORK errors; an unexpected payload FORMAT raises in the parser. With the `try`
    outside the loop that would end the round, and every target AFTER the broken one would not
    be refreshed. So the `try` sits INSIDE the loop, as in the other per-server jobs
    (`metrics.sample_once`, `watchdog.check_once`, `stats_poller.poll_once`, …).
    """
    results: list[dict] = []
    for gt, variant in _registered_targets():
        key = gt.value if isinstance(gt, GameType) else str(gt)
        try:
            results.append(await refresh_for(gt, variant))
        except Exception as e:
            log.exception("version_catalog[%s/%s]: sync failed — the other games "
                          "carry on", key, variant or "-")
            results.append({"game_type": key, "variant": variant, "ok": False,
                               "total": 0, "new": 0, "detail": f"source unreadable: {e}"})
    return results


async def scheduled_refresh() -> None:
    """Scheduler job (daily): refresh the catalog. Errors stay inside the job (like mod_check)."""
    try:
        for r in await refresh_all():
            log.info(
                "version_catalog[%s/%s]: %s versions, %s new (ok=%s)",
                r["game_type"], r["variant"] or "-", r["total"], r["new"], r["ok"],
            )
    except Exception:
        log.exception("version_catalog: daily check failed")


# Last refill attempt per (game, variant) — process-local, like the other caches.
_lazy_attempts: dict[tuple[str, str], dt.datetime] = {}


def _lazy_refresh_due(game_type: str, variant: str) -> bool:
    """May an empty catalog be refilled right now?

    Without this throttle EVERY load of the version page would query all upstream endpoints
    again (20 s timeout each) while a source is permanently down, and block the page.
    """
    now = utcnow_naive()
    last = _lazy_attempts.get((game_type, variant))
    if last is not None and now - last < LAZY_REFRESH_COOLDOWN:
        return False
    _lazy_attempts[(game_type, variant)] = now
    return True


def _rows_for(game_type: str, variant: str) -> list[VersionCatalogEntry]:
    with Session(engine) as s:
        return list(s.exec(
            select(VersionCatalogEntry).where(
                VersionCatalogEntry.game_type == game_type,
                VersionCatalogEntry.variant == variant,
            )
        ).all())


# Tags that name a channel rather than a build: upstream moves them on, and the next recreate
# (or pull) runs something else. A digest never moves; any other tag (`sha-d042142`, `1.4.0`)
# moves only when its publisher re-pushes it.
_MOVING_TAGS = frozenset({"", "latest", "edge", "stable", "main", "master", "nightly", "dev", "beta"})


def image_tag_floats(tag: str) -> bool:
    return (tag or "").strip().lower() in _MOVING_TAGS


def _no_list_reason(server: GameServer) -> str:
    """Why this game has no selection list — in plain text, not as a placeholder.

    The reason depends on the version source, not on the game name: for every game whose image
    fetches the game from Steam on each start (Arma Reforger, Valheim) the pinned tag is only
    the shell.
    """
    from .drivers import VersionSource, version_source

    if version_source(server) is VersionSource.runtime:
        return (
            "This game has no selectable version: the server image fetches the game from Steam "
            "on every start. So the top shows what is really running right now — a restart "
            "updates it. The image below is only the shell that starts the server; the mark "
            "next to it says whether that shell can change without anyone changing it here."
        )
    return "There is no version list for this game — please enter the version manually."


async def entries_for(server: GameServer) -> dict:
    """Catalog for a server's version page (the dropdown contract with the frontend).

    Refills itself on the very first call so the dropdown does not stay empty until the next
    daily run (same pattern as `mod_check.get_report`).
    """
    key = _game_key(server.game_type)
    variant = variant_for(server)
    if server.game_type not in FETCHERS:
        # No version list does not mean "nothing to say": `current` is still filled via
        # `current_ref_detected()`, because the driver may be able to read the running version
        # (e.g. Arma Reforger from the start banner).
        return {
            "supported": False,
            "entries": [],
            "current": await current_ref_detected(server),
            "image_ref": image_ref(server.image_repo, server.image_tag) if server.image_repo else "",
            "image_pinned": server.image_tag.startswith("sha256:"),
            "image_floating": image_tag_floats(server.image_tag),
            "update_available": None,
            "checked_at": None,
            "note": _no_list_reason(server),
        }

    rows = _rows_for(key, variant)
    source_ok = True
    if not rows and _lazy_refresh_due(key, variant):
        source_ok = bool((await refresh_for(server.game_type, variant)).get("ok"))
        rows = _rows_for(key, variant)

    # An old list is not a checked list. The daily run can fail silently (`_get_json` swallows
    # every exception, `refresh_for` deliberately keeps the old rows, `scheduled_refresh` only
    # logs), and the version page shows its warning only for `source_ok === false`.
    #
    # `fetched_at` is the honest measure — `refresh_for` writes it only on a successful fetch.
    # If the last one is older than two daily runs, the response says so.
    last_fetch = max((r.fetched_at for r in rows), default=None)
    if last_fetch is not None and utcnow_naive() - last_fetch > SOURCE_STALE_AFTER:
        source_ok = False
    elif not rows:
        # An empty catalog is never "fine". When the catalog is EMPTY and the refill throttle
        # is active, neither the refill branch above nor the staleness check applies; without
        # this branch the response would be `{"entries": [], "source_ok": True}` for every page
        # load during the 30-minute throttle — "did not look because I may not right now" would
        # read as "checked, there is nothing".
        source_ok = False

    current = await current_ref_detected(server)
    # Compare against the CORE of the running version: a "26.2-rc-2" is not an update to "26.2".
    current_ver = base_version(current)
    now = utcnow_naive()
    entries: list[dict] = []
    for r in rows:
        newer = bool(current_ver) and base_version(r.version) > current_ver
        entries.append({
            "version": r.version,
            "ref": r.ref,
            "channel": r.channel,
            "channel_label": CHANNEL_LABEL.get(r.channel, r.channel),
            "released_at": r.released_at.isoformat() if r.released_at else None,
            "changelog_title": r.changelog_title,
            "changelog_url": r.changelog_url,
            "changelog_summary": r.changelog_summary,
            "current": r.ref == current,
            "newer": newer,
            "is_new": r.first_seen_at >= now - NEW_WINDOW,
        })
    entries.sort(key=lambda e: (sort_key(e["version"]), e["ref"]), reverse=True)

    # "Update available" deliberately means the STABLE channel only — a newer unstable build is
    # not an update one should suggest to anyone.
    upgrade = next(
        (e for e in entries if e["newer"] and e["channel"] == "stable"), None
    )
    return {
        "supported": True,
        "entries": entries,
        "current": current,
        "current_detected": bool(current) and not current_ref(server),
        # "We do not know what is running" is not "running the latest". `current_ref_detected`
        # returns `""` on any error; `current_ver` is then `()`, `newer` is `False` for EVERY
        # row and `update_available` is `None` — just as for a server that really is up to
        # date, with `source_ok` still True. This flag makes the difference visible.
        "running_version_known": bool(current_ver),
        "update_available": upgrade,
        "source_ok": source_ok,
        "checked_at": max((r.fetched_at for r in rows), default=None).isoformat() if rows else None,
        "note": "",
    }
