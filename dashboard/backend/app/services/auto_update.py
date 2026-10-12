"""Automatic version change, **only within the running minor version**.

**Why it exists.** A server pinned to an old patch release becomes unjoinable as soon as Steam
updates the clients to a newer one. PZ does not report such a mismatch: the client hangs while
joining, and the server looks perfectly healthy (RCON answers, A2S answers, the world ticks).

**Why still not "always the latest".** The boot proof in `version_manager` catches "server does
not start"; it does **not** catch "world migrated" or "workshop mods broken". Both are the
normal case when jumping to a new minor version, and the automatic rollback does not trigger
there because from its point of view everything went fine. This job therefore only applies
hotfixes WITHIN the running minor version (42.20.0 → 42.20.3), the range the vendor declares
save-compatible. A 42.20 → 42.21 change stays manual.

**Fail-closed at the one place that counts:** if it cannot be established with certainty that
nobody is playing, no update happens. An unreachable RCON means "unknown", not "empty", the
same pattern as in `idle_autostop` and for the same reason.
"""
from __future__ import annotations

import logging

from sqlmodel import Session

from ..config import settings
from ..db import engine
from ..models import ServerIncident
from ..models.server import GameServer
from . import version_catalog, version_manager
from . import servers as servers_registry
from .drivers import AutoUpdate, VersionSource, get_driver, version_source, versioned_by_image_tag

log = logging.getLogger("uvicorn.error")


def same_minor_version(a: str, b: str) -> bool:
    """Do two versions share major AND minor version? ("42.20.0" and "42.20.3" → yes.)

    Deliberately via `version_catalog.base_version`, so pre-release suffixes (`-rc`, `-pre`) are
    dropped here just as in the dropdown; otherwise "42.20.3-rc-1" would count as its own line.
    """
    ka, kb = version_catalog.base_version(a), version_catalog.base_version(b)
    return bool(ka) and bool(kb) and ka[:2] == kb[:2]


def hotfix_candidate(entries: list[dict]) -> dict | None:
    """The highest entry that is newer, has the same channel and the same minor version.

    `entries` is the list from `version_catalog.entries_for(...)["entries"]`; each entry
    carries `current`, `newer` and `channel`. The running version comes from the entry with
    `current=True`, not from the image tag, which is not a version at all for a digest pin.
    """
    current = next((e for e in entries if e.get("current")), None)
    if not current:
        return None                      # no comparison without a known current version
    matching = [
        e for e in entries
        if e.get("newer")
        and e.get("channel") == current.get("channel")
        and same_minor_version(current.get("version", ""), e.get("version", ""))
    ]
    if not matching:
        return None
    return max(matching, key=lambda e: version_catalog.sort_key(e.get("version", "")))


async def is_empty(server: GameServer) -> bool | None:
    """`True` = nobody online · `False` = someone is playing · `None` = **unknown**.

    `None` is the important return value: a hanging or unreachable RCON must not pass as
    "empty", otherwise this job stops a server under active players.
    """
    try:
        response = await get_driver(server).players(server)
    except Exception as e:
        log.info("auto_update: player count of %s cannot be determined (%s)", server.key, e)
        return None

    # **`count`, not `names`**, and the difference is exactly the case this job must not get
    # wrong. The driver contract returns both, but `count` is the reliable figure:
    # `parse_factorio_players` notes that the name list can be truncated, and
    # `parse_avorion_players` returns count=2 with an EMPTY name list for a bare header line
    # ("2 players online"). Read via `names`, that would mean "server is empty", and the job
    # would stop, back up, swap the image and restart under two active players. All other
    # consumers (`idle_autostop`, `metrics`, `routers/join`) use `count` as well.
    number = response.get("count")
    if number is None:
        # No `count` in the response means UNKNOWN, not zero. Falling back to `names` would be
        # the same mistake as above on a smaller scale.
        log.info("auto_update: %s returns no player count — treated as unknown", server.key)
        return None
    try:
        return int(number) == 0
    except (TypeError, ValueError):
        log.info("auto_update: %s returns an unreadable player count (%r) — treated as unknown",
                 server.key, number)
        return None


async def _safely_stopped(server: GameServer) -> bool:
    """`True` only if the container is provably not running.

    Any uncertainty (proxy unreachable, unclear answer) yields `False`. The caller then
    continues with the normal checks, and `is_empty` fails closed.
    """
    from .docker_client import docker

    try:
        state = await docker.state(server.container_name)
    except Exception as e:
        log.info("auto_update: state of %s cannot be determined (%s) — checking as usual",
                 server.key, e)
        return False
    return not (state.get("running") or state.get("restarting"))


def _incident(server_id: int | None, detail: str) -> None:
    try:
        with Session(engine) as s:
            s.add(ServerIncident(kind="auto_update", detail=detail, server_id=server_id))
            s.commit()
    except Exception:       # pragma: no cover - bookkeeping must not take down the job
        log.exception("auto_update: incident could not be written")


async def check_server(server: GameServer) -> dict | None:
    """One server: is there a hotfix, and may it be applied now?

    Returns the applied candidate, otherwise `None`. Does not raise: the job runs over several
    servers, and a broken one must not take the others down.
    """
    try:
        # The driver decides, not the game type: `patch` means "hotfixes of the same minor
        # version may run at night", `none` means "never automatically". The job iterates over
        # the whole registry, so without this check it would apply to EVERY enabled server.
        if get_driver(server).auto_update_limit is AutoUpdate.none:
            return None

        # **A stopped server is not checked at all, and SILENTLY so.**
        #
        # Updating it would mean starting it at night: `change_version` stops, backs up, swaps
        # and STARTS. That is never wanted for an on-demand server. Without this check the
        # behaviour is still correct (`is_empty` returns "unknown" because RCON is dead, and
        # nothing is touched), but it leaves an incident "hotfix available but not applied:
        # player count unknown" EVERY NIGHT, and noise repeated daily eventually hides what
        # matters.
        #
        # **When in doubt, check rather than skip:** if the state cannot be determined (Docker
        # proxy gone), take the normal path, where `is_empty` decides, which is the more
        # cautious side.
        if await _safely_stopped(server):
            log.debug("auto_update: %s is stopped — not checked", server.key)
            return None

        # **The job never pins a server FOR THE FIRST TIME.**
        #
        # An env-versioned server may deliberately run without a set version: an empty
        # `game_version` must NOT act as a version change (it might migrate the world), and
        # `stats_poller` respects that by writing `game_version` back only for Reforger.
        #
        # Without this condition the job would not: it gets the running version from the log
        # via `detect_version()`, finds a candidate and calls `change_version(game_version=…)`.
        # `_direction("", "1.21.7")` is "unknown", so the downgrade lock does not apply, and
        # `build_create_payload` creates the new container with `VERSION` set. A deliberately
        # unpinned server would become a pinned one overnight, with a possible world migration
        # that the boot proof does not see.
        #
        # A server with a set `game_version` is not affected: there a hotfix is a change
        # between two known versions.
        if version_source(server) is VersionSource.env and not (server.game_version or "").strip():
            log.info(
                "auto_update: %s deliberately runs without a pinned version — not touched", server.key,
            )
            return None
        data = await version_catalog.entries_for(server)
        if not data.get("supported"):
            return None

        # **A dead version source is not "nothing new".** If the daily sync has been failing,
        # `hotfix_candidate` would compare against the stale catalogue, find nothing and return
        # SILENTLY, recreating invisibly the very situation this job exists to prevent (a
        # hotfix exists and nobody notices). This is an incident and not just a log line,
        # because otherwise the operator only notices once a client update blocks joining.
        if data.get("source_ok") is False:
            log.warning("auto_update: %s has no current version list — not checked",
                        server.key)
            _incident(server.id,
                     "Version list is stale (the daily sync is failing) — "
                     "hotfixes were not checked")
            return None

        # **And an unknown RUNNING version is not "nothing new" either.**
        # `current_ref_detected` returns `""` on any error; `hotfix_candidate` then compares
        # against nothing and never finds a match. That is the same silent shutdown as with the
        # stale list, on the other side of the comparison: the list is fresh and `source_ok`
        # stays True.
        if data.get("running_version_known") is False:
            log.warning("auto_update: %s — the running version cannot be determined, "
                        "hotfixes were not checked", server.key)
            _incident(server.id,
                     "Running version cannot be determined — hotfixes were not checked")
            return None

        candidate = hotfix_candidate(data.get("entries") or [])
        if not candidate:
            return None

        empty = await is_empty(server)
        if empty is not True:
            reason = "players online" if empty is False else "player count unknown"
            log.info("auto_update: %s on %s postponed (%s)", candidate["ref"], server.key, reason)
            _incident(server.id,
                     f"{candidate['ref']} available but not applied: {reason}")
            return None

        log.info("auto_update: %s → %s on %s", data.get("current"), candidate["ref"], server.key)
        # **The change must use the right argument.** `change_version` expects `image_tag` OR
        # `game_version` depending on the version source. Passing `image_tag` for an
        # env-versioned game leaves `new_ver` equal to `old_ver`, and `change_version` raises
        # "no version change given", every night anew without ever updating anything.
        if versioned_by_image_tag(server):
            await version_manager.change_version(server.key, image_tag=candidate["ref"])
        else:
            await version_manager.change_version(server.key, game_version=candidate["ref"])
        _incident(server.id, f"automatically updated to {candidate['ref']}")
        return candidate
    except Exception as e:
        log.warning("auto_update: %s skipped (%s)", server.key, e)
        _incident(server.id, f"automatic version change failed: {e}")
        return None


async def auto_update_once() -> None:
    """Scheduler job. Off as long as `AUTO_UPDATE_ENABLED` is not set."""
    if not settings.auto_update_enabled:
        return
    try:
        server_list = servers_registry.list_enabled()
    except Exception:       # pragma: no cover - a broken registry is other guards' business
        log.exception("auto_update: server list cannot be read")
        return
    for server in server_list:
        await check_server(server)
