"""Mod preflight for Avorion: workshop IDs instead of files.

Avorion is the only supported game server that **downloads its mods itself**. The galaxy
folder only holds `modconfig.lua` with a list of workshop IDs; what becomes of it is decided
by Steam at the next start. So there is nothing to scan and nothing to move here; the list is
the only thing that can be checked.

**What can go wrong, and which of it can be detected beforehand:**

| Error | detectable beforehand? |
|---|---|
| The file exists but cannot be read | yes, and that is a blocker, not an empty result |
| No `mods = { … }` block (syntax error) | yes |
| An ID no longer exists / is banned | yes, via the Steam Web API |
| An ID belongs to a different game | yes (`consumer_app_id`) |
| `allowed` does not match `mods` | yes |
| Two mods are incompatible at runtime | **no**; only the boot proof helps against that |

The Steam query shares its path with the PZ workshop check (`services/mod_check.py`):
`fetch_steam` and `check_availability` are game-agnostic, only the app ID differs. Do not
add a second copy.

**"Not verifiable" is a warning, neither an acquittal nor a blocker**, the same
classification as for Project Zomboid (`mod_check.WORKSHOP_ITEM_UNVERIFIABLE`). The reason for
both: a Steam outage is not a finding AGAINST a mod, but it must not pass as green either. It
therefore shows up visibly in the list without stopping the apply.
"""
from __future__ import annotations

import logging
import os
import re

from ..mod_check import REMOVED, UNVERIFIABLE, fetch_steam, check_availability
from .preflight import BLOCKER, WARNING, Finding, PreflightResult

log = logging.getLogger("uvicorn.error")

AVORION_APP_ID = 445220

# `mods = { {workshopid = "123"}, }`: the block that matters.
_MODS_BLOCK_RE = re.compile(r"\bmods\s*=\s*\{", re.S)
# Only the START of the block; `_block_span` finds its end by counting braces.
#
# **Why no regex for the whole block:** `allowed\s*=\s*\{(.*?)\}` ends non-greedily at the
# FIRST `}`, and in the file that `build_modconfig` itself generates that is the closing brace
# of the first `{workshopid = "…"}`, not the block's. The consequences would be false
# `AV_ID_DUPLICATE` and `AV_NOT_ALLOWED` findings and, worse, an ID that is only in `allowed`
# (client mods) would end up in `mods`, be checked against Steam and could abort the apply
# with a 409 although the server does not load it at all.
_ALLOWED_START_RE = re.compile(r"\ballowed\s*=\s*\{")
_ID_RE = re.compile(r"workshopid\s*=\s*[\"'](?P<id>\d+)[\"']")


def _read(server) -> tuple[str | None, str]:
    """`(content, error)`. `None` means "file does not exist", which is allowed here.

    The difference matters: a missing `modconfig.lua` is a vanilla server. An EXISTING one
    that cannot be read is a blocker; otherwise the same empty mod set would result as for a
    server without mods, and the apply would go through.
    """
    from ..drivers.avorion import AvorionDriver

    path = os.path.join(AvorionDriver()._galaxy(server), "modconfig.lua")
    if not os.path.exists(path):
        return None, ""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read(), ""
    except OSError as e:
        return "", f"{path}: {e}"


def _block_span(text: str) -> tuple[int, int] | None:
    """Where does the `allowed = { … }` block start and end? Braces are COUNTED.

    A non-greedy regex cannot do this: the block itself contains `{…}` entries, and the first
    closing brace belongs to the first entry. Lua comments and strings are not evaluated; this
    file only has numbers in quotes, and a brace inside them would be syntactically
    meaningless anyway.

    Returns `None` when there is no block or it is unterminated. Unterminated deliberately
    means "not present": half a file should be noticed (the preflight then reports missing
    `allowed` entries), not silently read halfway.
    """
    m = _ALLOWED_START_RE.search(text)
    if not m:
        return None
    depth = 1
    for i in range(m.end(), len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return (m.start(), i + 1)
    return None


def ids_and_allowed(text: str) -> tuple[list[str], list[str], list[str]]:
    """`(mods, allowed, duplicates)` from the Lua text.

    Deliberately a regex and not a Lua parser: all that is needed are numbers from two flat
    lists. A parser would be a dependency for a one-line job; the same trade-off as
    `_yaml_first` in `foreign.py`.
    """
    span = _block_span(text or "")
    allowed = _ID_RE.findall((text or "")[span[0]:span[1]]) if span else []
    # Take the `allowed` matches out of the full text, otherwise they would count as mods.
    rest = text or ""
    if span:
        rest = rest[:span[0]] + rest[span[1]:]
    all_ids = _ID_RE.findall(rest)
    seen: set[str] = set()
    mods: list[str] = []
    duplicates: list[str] = []
    for mid in all_ids:
        if mid in seen:
            if mid not in duplicates:
                duplicates.append(mid)
            continue
        seen.add(mid)
        mods.append(mid)
    return mods, allowed, duplicates


async def check(server) -> PreflightResult:
    """Check the configured mod set. Network access only when there are IDs to check."""
    findings: list[Finding] = []
    text, error = _read(server)

    if error:
        return PreflightResult([Finding(
            code="AV_MODCONFIG_UNREADABLE", level=BLOCKER, subject="modconfig.lua",
            message="modconfig.lua exists but cannot be read — what the server loads from it "
                    "is therefore unknown.",
            detail=error,
        )])
    if text is None:
        return PreflightResult([])          # server runs without mods, nothing to check

    if not _MODS_BLOCK_RE.search(text):
        return PreflightResult([Finding(
            code="AV_MODCONFIG_WITHOUT_MODS", level=BLOCKER, subject="modconfig.lua",
            message="modconfig.lua has no `mods = { … }` block. Avorion would ignore the file "
                    "and start without mods — clients with mods could no longer join.",
        )])

    mods, allowed, duplicates = ids_and_allowed(text)

    for mid in duplicates:
        findings.append(Finding(
            code="AV_ID_DUPLICATE", level=WARNING, subject=mid,
            message=f"The workshop ID {mid} appears more than once in modconfig.lua.",
        ))

    if allowed:
        missing = [m for m in mods if m not in allowed]
        if missing:
            findings.append(Finding(
                code="AV_NOT_ALLOWED", level=WARNING, subject=", ".join(missing),
                message=f"{len(missing)} mod(s) are in `mods` but not in `allowed`. "
                        f"When `allowed` has entries, it counts as the complete list.",
                detail=", ".join(missing),
            ))

    if not mods:
        return PreflightResult(findings)

    steam = await fetch_steam(mods)
    state = await check_availability(mods, steam)
    for mid in mods:
        details = steam.get(mid) or {}
        title = str(details.get("title") or f"Workshop item {mid}")
        if state.get(mid) == REMOVED:
            findings.append(Finding(
                code="AV_ITEM_REMOVED", level=BLOCKER, subject=mid,
                message=f"{title}: no longer available on the workshop. The server can no "
                        f"longer load the mod, and clients get stuck while joining.",
            ))
        elif state.get(mid) == UNVERIFIABLE:
            findings.append(Finding(
                code="AV_ITEM_UNVERIFIABLE", level=WARNING, subject=mid,
                message=f"{title}: availability could not be checked (Steam unreachable or "
                        f"unclear answer). That is NOT a finding against the mod.",
            ))
        app = details.get("app_id")
        if app and int(app) != AVORION_APP_ID:
            findings.append(Finding(
                code="AV_ITEM_WRONG_GAME", level=BLOCKER, subject=mid,
                message=f"{title}: does not belong to Avorion (app {app}).",
            ))
    return PreflightResult(findings)
