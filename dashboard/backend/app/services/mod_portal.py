"""Factorio's mod portal — search, find updates, install.

Factorio has an open portal with a documented API, and an outdated mod matters more for this
game than elsewhere: the client compares the mod set exactly, and anyone on a different version
cannot join at all.

Behaviour of the real API that this module relies on (measured, not taken from the docs):

1. **There is NO full-text search.** `?q=` and `?search=` are silently ignored — they return the
   same full list as a request without parameters, which merely looks like a search result.
   Searching therefore happens here: the complete list is fetched (about 13 MB) and filtered
   locally.
2. **`sort=downloads_count` does not sort.** It returns without error and yields nonsense
   (`sort=updated_at` works). Sorting is therefore local as well.
3. **`namelist=a,b,c` is exact and cheap** — the right way to check for updates: only the
   installed mods, one request, no 13 MB download.

**No cache, on purpose.** Even the slimmed-down full list is about 6.5 MB; keeping it would
burden the single uvicorn worker with a mod catalog that is rarely needed. Searching is an
explicit action on a button press, not a typing aid — the UI must not search on every keystroke.
"""
from __future__ import annotations

import logging
import re

import httpx

log = logging.getLogger("uvicorn.error")

BASE_URL = "https://mods.factorio.com/api/mods"
DOWNLOAD_BASE_URL = "https://mods.factorio.com"

# The full list (about 13 MB) normally arrives in a few seconds; generous margin for a bad day.
LIST_TIMEOUT_S = 60.0
DETAIL_TIMEOUT_S = 20.0


class PortalError(RuntimeError):
    """The portal was unreachable or returned something unexpected.

    Deliberately an exception and not an empty result: "no hits" and "could not look" are two
    very different answers to a mod search, and the second must not look like the first.
    """


def _version_tuple(raw: str) -> tuple[int, ...]:
    """`"1.2.10"` → `(1, 2, 10)`. Anything unreadable becomes `()` and loses every comparison."""
    parts = re.findall(r"\d+", str(raw or ""))
    return tuple(int(t) for t in parts) if parts else ()


def is_newer(candidate: str, installed: str) -> bool:
    """Whether `candidate` is a higher version than `installed`.

    Anything incomparable (empty, broken) yields **False** — better to report no update than one
    based on an unreadable version string. A falsely reported update makes someone change their
    Factorio mod set, after which no client still on the old version can join.
    """
    a, b = _version_tuple(candidate), _version_tuple(installed)
    if not a or not b:
        return False
    return a > b


def _matches_game_version(mod_series: str, game: str) -> bool:
    """`factorio_version` is the release SERIES the mod was built for (e.g. `2.0`), not a version.

    If either value is missing the entry counts as matching — otherwise a hit would be hidden
    because of missing information, i.e. an unknown presented as a result.
    """
    a, b = _version_tuple(mod_series), _version_tuple(game)
    if not a or not b:
        return True
    return a[:2] == b[:2]


def _latest_release(entry: dict) -> dict:
    """The newest release of a portal entry — **the portal delivers it in TWO shapes.**

    * The list without `namelist` (and `/api/mods` in general) attaches a `latest_release` field.
    * The query WITH `namelist=` returns `releases` instead — the complete list, without
      `latest_release`. Same route, same object, different key.

    Reading only `latest_release` would break the update check, which queries via `namelist`:
    the available version would always be empty and the check would never report an update
    while looking like "everything up to date". Test doubles must mirror the shape of the query
    they stand in for.

    The highest version is taken, not the last row: the portal guarantees no order, and a
    sorting error there would become a wrong update here.
    """
    if entry.get("latest_release"):
        return entry["latest_release"]
    releases = entry.get("releases") or []
    if not releases:
        return {}
    return max(releases, key=lambda r: _version_tuple(str(r.get("version") or "")))


def _slim(entry: dict) -> dict:
    release = _latest_release(entry)
    info = release.get("info_json") or {}
    return {
        "name": str(entry.get("name") or ""),
        "title": str(entry.get("title") or ""),
        "owner": str(entry.get("owner") or ""),
        "summary": str(entry.get("summary") or "")[:300],
        "downloads_count": int(entry.get("downloads_count") or 0),
        "category": str(entry.get("category") or ""),
        "version": str(release.get("version") or ""),
        "file_name": str(release.get("file_name") or ""),
        "download_url": str(release.get("download_url") or ""),
        "sha1": str(release.get("sha1") or ""),
        "factorio_version": str(info.get("factorio_version") or ""),
    }


async def search(term: str, *, game_version: str = "", limit: int = 30) -> list[dict]:
    """Search mods — filtered locally because the portal cannot do it.

    Searches name, title and summary; sorted by downloads (descending), the only hint of
    usefulness the list offers.
    """
    term = (term or "").strip().lower()
    if len(term) < 2:
        raise ValueError("Please search for at least two characters — otherwise half the catalogue comes back.")
    try:
        async with httpx.AsyncClient(timeout=LIST_TIMEOUT_S) as c:
            r = await c.get(BASE_URL, params={"page_size": "max"})
            r.raise_for_status()
            data = r.json()
    except Exception as e:
        raise PortalError(f"The mod portal could not be reached: {e}") from e

    results: list[dict] = []
    for entry in data.get("results") or []:
        text = " ".join(str(entry.get(k) or "") for k in ("name", "title", "summary")).lower()
        if term not in text:
            continue
        slim = _slim(entry)
        if not _matches_game_version(slim["factorio_version"], game_version):
            continue
        results.append(slim)
    results.sort(key=lambda m: m["downloads_count"], reverse=True)
    return results[:limit]


async def details(name: str) -> dict:
    """Full information about one mod, including all releases and dependencies."""
    try:
        async with httpx.AsyncClient(timeout=DETAIL_TIMEOUT_S) as c:
            r = await c.get(f"{BASE_URL}/{name}/full")
            if r.status_code == 404:
                raise PortalError(f"“{name}” does not exist in the mod portal.")
            r.raise_for_status()
            return r.json()
    except PortalError:
        raise
    except Exception as e:
        raise PortalError(f"The mod portal could not be reached: {e}") from e


async def updates(installed: dict[str, str], *, game_version: str = "") -> list[dict]:
    """Which installed mods have a newer version?

    Queries only the installed names (`namelist=`) — one request, no full-list download. Mods
    the portal does not know (home-made, renamed) are returned as such instead of silently
    disappearing.
    """
    names = sorted(n for n in installed if n)
    if not names:
        return []
    try:
        async with httpx.AsyncClient(timeout=DETAIL_TIMEOUT_S) as c:
            r = await c.get(BASE_URL, params={"page_size": "max", "namelist": ",".join(names)})
            r.raise_for_status()
            data = r.json()
    except Exception as e:
        raise PortalError(f"The mod portal could not be reached: {e}") from e

    known = {str(m.get("name")): _slim(m) for m in (data.get("results") or [])}
    out: list[dict] = []
    for name in names:
        current = installed[name]
        portal = known.get(name)
        if portal is None:
            out.append({
                "name": name, "installed": current, "available": "", "update": False,
                "unknown": True,
                "note": "The portal does not know this mod — home-made or renamed. "
                           "Whether it is up to date cannot be told from here.",
            })
            continue
        newer = is_newer(portal["version"], current)
        matches = _matches_game_version(portal["factorio_version"], game_version)
        out.append({
            "name": name,
            "title": portal["title"],
            "installed": current,
            "available": portal["version"],
            "update": newer and matches,
            "unknown": False,
            "file_name": portal["file_name"],
            "download_url": portal["download_url"],
            "sha1": portal["sha1"],
            "factorio_version": portal["factorio_version"],
            "note": (
                "" if matches else
                f"The new version is built for Factorio {portal['factorio_version']}, this "
                f"server runs {game_version or '?'} — it is therefore not offered."
            ),
        })
    return out


def download_url(path: str, *, username: str, token: str) -> str:
    """The complete download URL — **it does not work without credentials**.

    The portal redirects a request without `username`/`token` to the login page; the result
    would be an HTML page named `.zip`. The caller guards against that by never getting here
    (see `credentials_missing`).
    """
    return f"{DOWNLOAD_BASE_URL}{path}?username={username}&token={token}"


def credentials_missing(username: str, token: str) -> str:
    """Empty string = everything present. Otherwise the sentence explaining what is missing and why.

    No silent failure: the portal does not serve downloads without an account. A button that
    does not say so would put an HTML file named `.zip` into the mod folder — and the server
    would no longer start.
    """
    if username.strip() and token.strip():
        return ""
    return (
        "Downloads from the mod portal need a factorio.com account: user name and token are "
        "set in the configuration under “Access” (the token is shown on factorio.com/profile). "
        "Searching and showing updates works without one."
    )
