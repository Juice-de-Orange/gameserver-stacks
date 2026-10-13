"""RCON liveness watchdog (multi-game fan-out).

Detects a *hanging* game process: the container is running but RCON has not answered for
several checks → targeted restart. Deliberately NOT responsible for:
- container crashes → Docker handles those itself (`restart: unless-stopped`).
- a deliberately stopped server → stays stopped (the watchdog never starts one on its own).

This avoids both "admin stops → watchdog starts" conflicts and double restarts. State is
process-local (single worker) and kept **per `server_key`** so several servers do not affect
each other. Scheduled/manual restarts call `suppress(server_key, minutes)` so the boot is not
mistaken for a hang.

`check_once()` is ONE scheduler job (`max_instances=1`) that iterates sequentially over all
enabled servers per round. Sequential (no `gather`) is a deliberate choice for a handful of
servers (simplicity); with many dead servers a round could exceed 60 s → a round is then
skipped (`max_instances=1`), which only slows detection down and crashes nothing.
"""
from __future__ import annotations

import logging
import re
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING

from fastapi import HTTPException
from sqlmodel import Session

from ..config import settings
from ..db import engine
from ..models import ServerIncident
from ..services import servers as servers_registry
from ..models.player import utcnow_naive
from ..services.docker_client import docker, start_time as docker_start_time
from ..services.mods import state as mod_state
from ..services.rcon import RconError

if TYPE_CHECKING:
    from ..models.server import GameServer
    from ..services.drivers import GameDriver

log = logging.getLogger("uvicorn.error")

# How long a boot may take before the watchdog may act.
#
# It has to match what the rest of the backend allows a boot: `version_manager.verify_boot`
# works with `timeout = 900.0` (a PZ world migration/restart takes minutes for a grown world).
# A shorter window is guaranteed to be too short: the job ticks every 60 s and acts after
# `watchdog_fail_threshold` (3) dead answers, i.e. three minutes after the silence ends, while
# the server is still booting.
#
# The number lives here because this is where the question is decided; the scheduled restart
# and the watchdog's own autoheal both use it. Changing it changes it for all of them.
BOOT_WINDOW_MIN = 15.0

# SteamCMD's progress line, as the images pass it through to the container log:
# `Update state (0x61) downloading, progress: 57.32 (1253443568 / 2186650630)`, and the same with
# `(0x81) verifying update`, `(0x5) verifying install`, `(0x101) committing`. Recorded from the
# Valheim image 1.4.0 on a first start; SteamCMD prints one every two seconds while it works.
_STEAMCMD_PROGRESS = re.compile(r"Update state \(0x[0-9a-fA-F]+\) [a-z ]+, progress: \d")
# Enough for 15 minutes of SteamCMD output (two lines every two seconds with the image's colour
# reset lines) plus what the game logs after it. A chattier log only makes the check fall back
# to the old rule (count the failure), never to a silenced watchdog.
_INSTALL_LOG_TAIL = 5000


@dataclass
class _WdState:
    fail_count: int = 0
    # Monotonic time of the last auto-restart; `None` = none yet. Not `0.0`: the monotonic clock
    # counts from the host's boot, so for the first `watchdog_cooldown_min` after a host boot
    # `now - 0.0` looked like a cooldown, and a hanging server was not restarted.
    last_restart: float | None = None
    suppress_until: float = 0.0   # monotonic — paused until then (boot window)
    in_incident: bool = False
    # Boot loop detection: an `unless-stopped` container that keeps crashing is restarted by
    # Docker forever. The restart counter is the only reliable signal for that.
    last_restart_count: int | None = None
    in_boot_loop: bool = False
    # WHICH container does the counter above belong to? A create-then-swap (version change,
    # rebuild) creates a new container under the same name whose `RestartCount` starts at 0
    # again. Without this field `last_restart_count` would keep the deleted container's value
    # and `restarts > last_restart_count` would never be true again — boot loop detection would
    # be dead after every version change, the very operation most likely to trigger one.
    last_container_id: str = ""
    # A running world operation rejects the auto-restart again every round. It is reported
    # ONCE — `in_incident` cannot be used for that, it already belongs to the `rcon_hang`.
    last_blocked: bool = False
    no_probe_reported: bool = False
    installing_reported: bool = False


_states: dict[str, _WdState] = {}  # process-local, one entry per server_key (single worker)


def _state(key: str) -> _WdState:
    return _states.setdefault(key, _WdState())


def suppress(server_key: str, minutes: float) -> None:
    """Pause the watchdog for THIS server (called by scheduled/manual restarts)."""
    st = _state(server_key)
    st.suppress_until = max(st.suppress_until, time.monotonic() + minutes * 60)


@contextmanager
def silence(server_key: str, minutes: float):
    """Mute the watchdog for the DURATION of this block — and take it back afterwards.

    `suppress()` alone sets a deadline in the future and leaves it there. That is right for a
    boot (it takes as long as it takes) but wrong for a section with a known end, such as the
    mandatory backup of the scheduled restart: a `suppress()` before it would keep the watchdog
    muted even if the backup fails and no restart follows. The context manager resolves that:
    silent while packing, armed again as soon as it is over.

    **Only this block's OWN silence is taken back.** If another operation muted for longer in
    the meantime, the value belongs to it and stays. (`lifecycle._revert_silence` follows the
    same rule.)
    """
    st = _state(server_key)
    before = st.suppress_until
    suppress(server_key, minutes)
    ours = st.suppress_until
    try:
        yield
    finally:
        if st.suppress_until == ours:
            st.suppress_until = before


async def _steamcmd_working(server: GameServer) -> bool:
    """Has SteamCMD logged progress in this container within the last boot window?

    Best effort: an unreadable log is "no", so the failure counts as it did before.
    """
    since = int(time.time() - BOOT_WINDOW_MIN * 60)
    try:
        text = await docker.logs(server.container_name, tail=_INSTALL_LOG_TAIL, since=since)
    except Exception:
        return False
    return bool(_STEAMCMD_PROGRESS.search(text or ""))


def _record(kind: str, detail: str, server_id: int | None) -> None:
    try:
        with Session(engine) as session:
            session.add(ServerIncident(kind=kind, detail=detail, server_id=server_id))
            session.commit()
    except Exception:
        log.warning("watchdog: incident could not be saved", exc_info=False)


async def check_once() -> None:
    """ONE scheduler job: one watchdog round over all enabled servers."""
    if not settings.watchdog_enabled:
        return
    for server, drv in servers_registry.list_enabled_with_driver():
        try:
            await _check_server(server, drv)
        except Exception:
            log.warning("watchdog[%s]: round failed", server.key, exc_info=False)


async def _check_server(server: GameServer, drv: GameDriver) -> None:
    st = _state(server.key)
    now = time.monotonic()
    if now < st.suppress_until:
        # This is a "nothing wrong here" branch too, so it must reset the counter like its two
        # neighbours (container not running, RCON answers). Failures can pile up before the
        # silence starts (e.g. PZ blocks RCON during a world save); with `fail_count = 2`
        # carried over, the first round after the silence — still mid-boot — would reach 3.
        # The cooldown does not catch that: `last_restart` is set only by the watchdog's OWN
        # restart.
        st.fail_count = 0
        return
    try:
        state = await docker.state(server.container_name)
    except Exception:
        # Only THIS server is skipped (the loop in `check_once` catches per server anyway) —
        # but the skip must leave a trace: if the socket proxy is down, the watchdog is
        # ineffective, and that must not happen silently.
        log.warning(
            "watchdog[%s]: container state cannot be read — round skipped", server.key,
        )
        return

    # Does the remembered restart counter still belong to this container? A create-then-swap
    # replaces it under the same name and the new one starts at 0 (see `last_container_id`).
    container_id = str(state.get("id") or "")
    if container_id and container_id != st.last_container_id:
        if st.last_container_id:
            log.info("watchdog[%s]: new container — boot-loop counter starts over",
                     server.key)
        st.last_container_id = container_id
        st.last_restart_count = None
        st.in_boot_loop = False

    if not state.get("running"):
        # Not running → Docker self-healing or a deliberate stop. Do not intervene …
        st.fail_count = 0
        # … BUT a container that keeps restarting under `unless-stopped` would otherwise spin
        # silently. That is exactly what a broken mod configuration causes, so it gets an
        # incident.
        restarts = int(state.get("restarts") or 0)
        if st.last_restart_count is None:
            st.last_restart_count = restarts
        elif restarts > st.last_restart_count:
            st.last_restart_count = restarts
            if not st.in_boot_loop:
                st.in_boot_loop = True
                detail = f"Container keeps restarting (restart counter {restarts})."
                blockers = await mod_state.guard_boot(server)
                if blockers:
                    detail += " " + mod_state.guard_message(blockers)
                _record("boot_crash_loop", detail, server.id)
                log.error("watchdog[%s]: boot loop detected — %s", server.key, detail)
        return
    st.in_boot_loop = False
    st.last_restart_count = int(state.get("restarts") or 0)

    # Can this game answer at all?
    #
    # `drv.players()` is the watchdog's ONLY liveness probe, and for Valheim it depends on the
    # Steam query: `A2sError` derives from `RconError`, so a failure would count as "game
    # process hangs". With `SERVER_PUBLIC=false` Valheim answers no A2S query at all — the
    # server would look PERMANENTLY sick although it runs fine: autoheal after three rounds,
    # cooldown, and again.
    #
    # This is a question of its OWN, not that of the boot check. `players_usable_as_boot_proof`
    # must not be used here: it is `False` for PZ — not because the query does not work but
    # because `verify_boot` uses the stronger RCON handshake — and would switch the watchdog off
    # for PZ. `players_is_queryable` asks what matters here: do I get an answer at all? Where
    # `players()` structurally cannot tell anything, failures are not counted.
    if not drv.players_is_queryable(server):
        if st.fail_count or not st.no_probe_reported:
            st.no_probe_reported = True
            log.info("watchdog[%s]: no usable liveness probe (the player query is switched off "
                     "for this server) — hangs are not checked",
                     server.key)
        st.fail_count = 0
        return

    # Container is running → check RCON liveness (the driver encapsulates host/port/secret per game).
    try:
        await drv.players(server)
        st.last_blocked = False
        if st.in_incident:
            _record("recovered", "RCON reachable again.", server.id)
            st.in_incident = False
        st.fail_count = 0
        return
    except RconError:
        # A server that is still booting is not hanging. `suppress()` only protects starts the
        # dashboard itself performed. A start done by Docker (`unless-stopped` after a host
        # reboot or crash) and every freshly started backend (`_states` is process-local) have
        # `suppress_until = 0` — without this check the watchdog would restart a server that is
        # loading its world after three minutes, and after a host reboot that hits ALL servers
        # at once. The container's `started_at` answers the question (the idle auto-stop reads
        # the same field for its own grace period).
        #
        # `None` means **unknown** — and the safe direction here is to GRANT the window: acting
        # too late costs minutes, a restart in the middle of loading a large world throws the
        # load away and starts it over.
        started = docker_start_time(state.get("started_at"))
        if started is None or (utcnow_naive() - started).total_seconds() < BOOT_WINDOW_MIN * 60:
            if st.fail_count == 0:
                log.info("watchdog[%s]: RCON does not answer yet, the container has only been running "
                         "briefly — boot window (%g min) in progress", server.key, BOOT_WINDOW_MIN)
            return
        # The game cannot answer before it is installed. Images that download the game with
        # SteamCMD at start (Valheim on every start) begin the boot only after the download, and
        # a first download can outlast the window above: a slow line, or one "Missing
        # configuration" retry, and the watchdog restarted Valheim at 99.95 % verification —
        # which starts the download over. So the window counts from SteamCMD's last progress
        # line, not from the container start: a "nothing wrong here" branch like the others.
        if await _steamcmd_working(server):
            if not st.installing_reported:
                st.installing_reported = True
                log.info("watchdog[%s]: SteamCMD is still downloading or verifying the game — "
                         "not a hang; the boot window (%g min) counts from its last progress line",
                         server.key, BOOT_WINDOW_MIN)
            st.fail_count = 0
            return
        st.installing_reported = False
        st.fail_count += 1

    if st.fail_count < settings.watchdog_fail_threshold:
        return
    if st.last_restart is not None and now - st.last_restart < settings.watchdog_cooldown_min * 60:
        return  # cooldown — avoid restart loops

    # Self-healing must not activate a broken mod configuration either: the server would run
    # into a boot loop no restart can get it out of.
    blockers = await mod_state.guard_boot(server)
    if blockers:
        if not st.in_incident:
            st.in_incident = True
            _record("restart_blocked", mod_state.guard_message(blockers), server.id)
            log.error("watchdog[%s]: auto-restart rejected — %s",
                      server.key, mod_state.guard_message(blockers))
        return

    st.in_incident = True
    _record(
        "rcon_hang",
        f"RCON dead {st.fail_count}× in a row with the container running — auto-restart.",
        server.id,
    )
    try:
        # Via `lifecycle`, so the auto-restart also tries the game's own shutdown first. With a
        # real hang RCON is dead → the attempt fails fast and the signal path with the full
        # grace period remains; if the server hangs only partially, the world still gets
        # written.
        from . import lifecycle  # local import: lifecycle lazily imports watchdog back

        await lifecycle.restart_server(server)
        _record("auto_restart", "Game server hung — restarted automatically (watchdog).", server.id)
        log.warning("watchdog[%s]: game server hung — restarted automatically", server.key)
        st.last_blocked = False
    except HTTPException as e:
        # Rejected is not failed. `lifecycle.restart_server` answers 409 while a world-writing
        # operation is running (mod apply, version change, restore). That is the lock doing its
        # job, not an "auto-restart failed" incident — and the lines after this `try` (cooldown,
        # counter reset, silence) must not run for an intervention that did not happen, or a
        # really hanging game process would stay hung for a whole cooldown longer.
        if e.status_code != 409:
            raise
        if not st.last_blocked:
            st.last_blocked = True
            _record("restart_blocked", f"Auto-restart rejected: {lifecycle.detail_text(e.detail)}", server.id)
        log.info("watchdog[%s]: auto-restart rejected (world operation running) — no intervention",
                 server.key)
        return   # NO cooldown, NO silence, counter stays: nothing was done
    except Exception as e:
        _record("restart_failed", f"Auto-restart failed: {e}", server.id)
        log.warning("watchdog[%s]: auto-restart failed", server.key, exc_info=False)
    st.last_restart = time.monotonic()
    # Via `suppress()`, not by assignment: `lifecycle.restart_server` has just muted in its own
    # `finally`, and a raw assignment would throw that away. `suppress()` uses `max()` so that
    # someone else's longer silence is not shortened.
    suppress(server.key, BOOT_WINDOW_MIN)
    st.fail_count = 0
