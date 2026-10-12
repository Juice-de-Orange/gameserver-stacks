"""FastAPI app of the Stacks Console. All routes live under /api (Caddy proxies /api and /ws)."""
import asyncio
import datetime as dt
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from sqlalchemy import text

from . import log_filters
from .auth import perm
from .config import jwt_secret_problem, secret_key_problem, settings
from .db import engine, init_db
from .routers import (
    actions,
    audit,
    auth,
    broadcasts,
    config,
    console,
    cross_server,
    firewall,
    join,
    logs,
    mod_config,
    moderation,
    reachability,
    resources,
    roles,
    schedules,
    server_ops,
    servers,
    settings_games,
    settings_network,
    stats,
    tickets,
    users,
    versions,
)
from .scheduler import scheduler
from .services import presence
from .services import broadcasts as broadcast_svc
from .services import jobs, restarts
from .services.cleanup import prune_history
from .services.backup import scheduled_backup
from .services.cross_server.bridge import fanout_once as bridge_fanout
from .services.cross_server.bridge import ingest_once as bridge_ingest
from .services.auto_update import auto_update_once
from .services.idle_autostop import JOB_INTERVAL_MIN as IDLE_JOB_INTERVAL_MIN
from .services.idle_autostop import check_once as idle_check
from .services.log_parser import poll_log_events
from .services.audit import prune_audit
from .services.cross_server.bus import prune_chat
from .services.firewall import close_stale as firewall_close_stale
from .services.metrics import prune_metrics, sample_once
from .services.mod_check import scheduled_refresh as mod_check_refresh
from .services.mods.apply import scheduled_prune as quarantine_prune
from .services.operations import cancel_stale
from .services.seed import seed_admin, seed_role_permissions
from .services.seed_servers import seed_servers
from .services import game_settings
from .services.stats_poller import poll_once
from .services.version_catalog import scheduled_refresh as version_catalog_refresh
from .services.watchdog import check_once as watchdog_check

log = logging.getLogger("uvicorn.error")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # FIRST: mask credentials in the logs. WebSockets carry their ticket as a query parameter
    # and uvicorn logs the full query — lines written before this call would already be out
    # unmasked.
    log_filters.install()
    # Fail closed, unconditionally: never sign with the public default secret or a short one,
    # otherwise anybody can forge admin tokens offline. This must not depend on any other
    # setting — a check tied to the seed password would stop applying once that is removed
    # from the .env after the first start.
    if (problem := jwt_secret_problem(settings.jwt_secret)):
        raise RuntimeError(problem)
    # The same for the key that encrypts the game-server passwords entered in the UI: without
    # it they could neither be stored nor read (`services/secrets.py`).
    if (problem := secret_key_problem(settings.secret_key)):
        raise RuntimeError(problem)
    init_db()
    # Plain-text console passwords an older version stored in `game_servers.rcon_password` go
    # into encrypted storage. Idempotent; NOT best-effort: a failure here means the key does
    # not work, and the start must say so rather than run with passwords it cannot read.
    from .services import secrets as server_secrets
    server_secrets.migrate_literals(engine)
    seed_admin()
    seed_role_permissions()
    # Registry singletons (resource budget, cross-server settings). BEST-EFFORT: an error here
    # must never block the start.
    try:
        seed_servers()
    except Exception:
        log.exception("seed_servers() failed — the app starts anyway (seed the registry later).")
    # Which games this installation runs. NOT best-effort: `GSS_GAMES` with a name that is no
    # game type stops the start here, with the variable named, instead of running nothing.
    game_settings.seed()
    # The public address saved under Settings → Network (read for every fleet poll, so cached).
    from .services import install_settings
    install_settings.load()
    perm.load_matrix()  # load the role matrix into the in-memory cache
    # Close operations that did not survive a backend restart — otherwise they would say
    # "running" forever and the UI would wait for a result.
    await cancel_stale()
    # The same for steamcmd helper containers of a download the previous process did not finish.
    from .services.steam_download import remove_stale_helpers
    await remove_stale_helpers()
    # Stats poller (presence via RCON).
    scheduler.add_job(poll_once, "interval", seconds=60, id="stats_poll", max_instances=1, replace_existing=True)
    # "Last seen" of the DASHBOARD users (not the players — the poller above does those). The
    # request path only notes a dict entry; this job writes them out in one batch. A write inside
    # `get_current_user` would dirty the request's ORM instance, block the event loop on the
    # WebSocket path and take a second pool connection per request. See `services/presence.py`.
    scheduler.add_job(_presence_flush, "interval", seconds=60,
                      id="presence", max_instances=1, replace_existing=True)
    # Log parser for player events (deaths/kills) — best effort; the patterns may need calibrating.
    #
    # **30 s on purpose:** this job PRODUCES the events, `stats_poll` (60 s) consumes them. At
    # a slower rate than its consumer the poller usually wins the race for a join, creates the
    # session with the poll time instead of the log time and without the game account id, and
    # nothing corrects it later (the fix-up hangs on the LEAVE event).
    scheduler.add_job(poll_log_events, "interval", seconds=30, id="log_events", max_instances=1, replace_existing=True)
    # Weekly backup (Sun 04:00) with retention — for EVERY enabled server. `max_instances=1`
    # because the job fans out over several servers and each run packs a tar.gz: two overlapping
    # rounds would read the same directories at once.
    scheduler.add_job(
        jobs.logged("weekly_backup", scheduled_backup), "cron", day_of_week="sun", hour=4,
        id="weekly_backup", max_instances=1, replace_existing=True,
    )
    # Metric samples (history charts, 60 s) and their daily prune (04:10).
    scheduler.add_job(sample_once, "interval", seconds=60, id="metrics_sample", max_instances=1, replace_existing=True)
    scheduler.add_job(jobs.logged("metrics_prune", prune_metrics), "cron", hour=4, minute=10,
                      id="metrics_prune", replace_existing=True)
    # Watchdog: restart a hung game process (RCON dead while the container runs).
    scheduler.add_job(watchdog_check, "interval", seconds=60, id="watchdog", max_instances=1, replace_existing=True)
    # Refresh the mod report every 6 h so that health and update hints stay current.
    #
    # **The first run is shortly after the start, not six hours later.** An interval job without
    # `next_run_time` starts a full interval later, and the only store of the result is a
    # process-local cache that is gone after every restart. Until the first run
    # `mod_check.cached()` returns `None` and the status bar shows yellow ("workshop check
    # pending").
    scheduler.add_job(
        mod_check_refresh, "interval", hours=6, id="mod_check", max_instances=1,
        replace_existing=True,
        next_run_time=dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=90),
    )
    # Refresh the version catalogue daily (04:20, after weekly_backup 04:00 and metrics_prune
    # 04:10): new game versions show up in the version picker by themselves. Nothing is
    # INSTALLED — the job only writes the list of choices.
    scheduler.add_job(
        jobs.logged("version_catalog", version_catalog_refresh), "cron", hour=4, minute=20,
        id="version_catalog", max_instances=1, replace_existing=True,
    )
    # Automatic version change (04:45, after version_catalog 04:20 and BEFORE the nightly
    # scheduled restart): ONLY hotfixes of the same minor version and only if demonstrably
    # nobody is playing. Off unless `AUTO_UPDATE_ENABLED` is set.
    # Why it exists and where its limit is: `services/auto_update.py`.
    scheduler.add_job(
        # **Logged, but deliberately NOT in the catch-up list below.** The job swaps an image
        # including stop and boot proof; catching it up in the afternoon because the backend
        # restarted at night would be the opposite of "at night, when nobody plays". A missed
        # night costs nothing — the next one fetches the same hotfix.
        jobs.logged("auto_update", auto_update_once), "cron", hour=4, minute=45,
        id="auto_update", max_instances=1, replace_existing=True,
    )
    # Clean up quarantines (daily 04:30): a mod operation sets several GB aside — without a
    # retention period the disk fills up. Analogous to the backup retention.
    scheduler.add_job(jobs.logged("quarantine_prune", quarantine_prune), "cron", hour=4, minute=30,
                      id="quarantine_prune", replace_existing=True)
    # Prune the audit log (daily 04:40, after quarantine_prune 04:30).
    scheduler.add_job(jobs.logged("audit_prune", prune_audit), "cron", hour=4, minute=40,
                      id="audit_prune", replace_existing=True)
    # Prune the chat bridge (daily 04:50, after audit_prune 04:40): a high-frequency table with
    # every chat line of every server plus three indexes.
    scheduler.add_job(jobs.logged("chat_prune", prune_chat), "cron", hour=4, minute=50,
                      id="chat_prune", replace_existing=True)
    # Prune the history tables `server_incidents`, `player_events` and `operations` (daily
    # 05:10, after chat_prune 04:50 and the 05:00 scheduled restart).
    scheduler.add_job(jobs.logged("history_prune", _prune_history_async), "cron",
                      hour=5, minute=10, id="history_prune", replace_existing=True)

    # Idle auto-stop: stop servers after a long time without players (long timer, default about
    # 20 days). The interval comes from `idle_autostop.JOB_INTERVAL_MIN` rather than a number
    # here — the UI needs it to say "at the earliest in …".
    scheduler.add_job(
        idle_check, "interval", minutes=IDLE_JOB_INTERVAL_MIN,
        id="idle_autostop", max_instances=1, replace_existing=True,
    )
    # Cross-server chat bridge: ingest ~25 s / fan-out ~12 s. BOTH are no-ops while
    # `chat_bridge_enabled` is False (the model default); the switch lives in the database.
    scheduler.add_job(bridge_ingest, "interval", seconds=25, id="bridge_ingest", max_instances=1, replace_existing=True)
    scheduler.add_job(bridge_fanout, "interval", seconds=12, id="bridge_fanout", max_instances=1, replace_existing=True)
    # Firewall helper (compose profile `firewall`): close the ports of servers that were switched
    # off, removed or moved. Opening waits for an admin's apply; closing does not. A no-op
    # without GSS_FIREWALL_TOKEN. See `services/firewall.py`.
    scheduler.add_job(firewall_close_stale, "interval", seconds=60, id="firewall_close",
                      max_instances=1, replace_existing=True)
    scheduler.start()

    # **Catch up on what was due while the backend was restarting.**
    #
    # The jobs live in memory and are registered anew here; `add_job` counts from NOW. For the
    # minute in which the process was down no job existed, so `misfire_grace_time` cannot catch
    # anything: a restart on Sunday at 04:00 would drop the weekly run silently.
    #
    # The maximum gap is the interval plus a generous allowance: a run that came a few hours
    # late only because of the restart is not a miss.
    for job_id, fn, max_gap_h in (
        ("weekly_backup", scheduled_backup, 7 * 24 + 12),
        ("metrics_prune", prune_metrics, 24 + 12),
        ("version_catalog", version_catalog_refresh, 24 + 12),
        ("quarantine_prune", quarantine_prune, 24 + 12),
        ("audit_prune", prune_audit, 24 + 12),
        ("chat_prune", prune_chat, 24 + 12),
        ("history_prune", _prune_history_async, 24 + 12),
    ):
        jobs.schedule_catchup(scheduler, job_id, fn, max_gap_h=max_gap_h)

    restarts.reload_jobs()       # register the scheduled restarts from the database
    broadcast_svc.reload_jobs()  # register the scheduled broadcasts from the database
    try:
        yield
    finally:
        # **`try/finally`, not just statements after the `yield`.** If an exception tears down
        # the lifespan context, nothing after a bare `yield` runs — neither the scheduler stop
        # nor the write-out below.
        scheduler.shutdown(wait=False)
        # Lose nothing on shutdown: without this line every restart would cost up to a minute of
        # "last seen". `flush` swallows and logs its own errors.
        await asyncio.to_thread(presence.flush, engine)


async def _presence_flush() -> None:
    """Pure SQL — belongs in the thread pool, not in the loop (like `_prune_history_async`)."""
    await asyncio.to_thread(presence.flush, engine)


async def _prune_history_async() -> None:
    """The clean-up run is pure SQL — it belongs in the thread pool, not in the loop."""
    await asyncio.to_thread(prune_history)


app = FastAPI(title="Stacks Console API", version="0.1.0", lifespan=lifespan)

for module in (
    auth, users, roles, servers, console, logs, config, tickets,
    join, stats, audit, resources, versions,
    cross_server, server_ops, mod_config, settings_games, settings_network, reachability, firewall,
):
    app.include_router(module.router)

# Server-scoped restarts/broadcasts, game-master actions and moderation.
#
# There are deliberately NO global counterparts (`/api/schedules`, `/api/broadcasts`,
# `/api/actions`, `/api/moderation`, `/api/backups`): a global route has no scope or ownership
# check, so one game's schedules and broadcasts could be read and overwritten from another, and
# a ban would always go to one hard-wired server. `tests/test_smoke.py` asserts their absence.
app.include_router(schedules.scoped_router)
app.include_router(broadcasts.scoped_router)
app.include_router(actions.scoped_router)
app.include_router(moderation.scoped_router)

app.include_router(cross_server.ws_router)  # /ws/cross-chat (without the /api prefix, like /ws/console)


@app.get("/api/health")
def health() -> dict:
    """Liveness plus database check."""
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        db_ok = True
    except Exception:
        db_ok = False
    return {"status": "ok", "db": db_ok}
