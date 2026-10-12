"""Import smoke: loads the complete app graph (verifies: no import cycles)
and checks that the routes are registered. Does NOT trigger lifespan/DB."""


def test_app_imports_and_routes():
    from app.main import app
    from tests.route_walk import flat_routes

    paths = {r.path for r in flat_routes(app)}
    for expected in (
        "/api/health",
        # scoped server API (the legacy `/api/server/*` aliases were removed)
        "/api/servers",
        "/api/servers/{key}/overview",
        "/api/servers/{key}/history",
        "/api/servers/{key}/incidents",
        "/api/me/stats",
        "/api/servers/{key}/join-info",
        "/api/auth/login",
        # Mod management: server-scoped and draft-based. The former global `/api/mods/*` router
        # was removed without replacement — it wrote into the production server's .ini on every
        # click and so went live past the preflight.
        "/api/servers/{key}/mods/status",
        "/api/servers/{key}/mods/draft",
        "/api/servers/{key}/mods/preflight",
        "/api/servers/{key}/mods/apply",
        "/api/servers/{key}/mods/history",
        "/api/servers/{key}/mods/items",
        # server-scoped operating endpoints (backups/config/logs/restarts/broadcasts per server)
        "/api/servers/{key}/backups",
        "/api/servers/{key}/config",
        "/api/servers/{key}/logs",
        "/api/servers/{key}/schedules",
        "/api/servers/{key}/broadcasts",
        # server-scoped actions (per-game catalogue)
        "/api/servers/{key}/actions/catalog",
        "/api/servers/{key}/actions/run",
        # server-scoped moderation + mod list
        "/api/servers/{key}/moderation/spec",
        "/api/servers/{key}/moderation/kick",
        "/api/servers/{key}/moderation/whitelist",
        "/api/servers/{key}/mods",
        # Curated PZ .ini editor — scoped as well. As the global `/api/config` it pointed at
        # pz-1 only.
        "/api/servers/{key}/config/ini",
        "/api/servers/{key}/config/raw",
        "/api/servers/{key}/config/sandbox",
        "/api/servers/{key}/config/sandbox-presets",
    ):
        assert expected in paths, f"route missing: {expected}"

    # --- What must NOT be there any more -----------------------------------------------------
    # This is the more valuable part of this test: a removed route comes back easily (a merge, a
    # restored import) and nobody notices. Every entry here has a reason that would make it
    # dangerous again.
    for gone, why in (
        ("/api/server/overview", "legacy alias to the default PZ server"),
        ("/api/server/history", "legacy alias to the default PZ server"),
        ("/api/server/incidents", "legacy alias to the default PZ server"),
        ("/api/server/status", "legacy alias to the default PZ server"),
        # Carried address, port and server name as literals in the source and so only knew PZ.
        ("/api/join-info", "IP/port/name as literals, PZ only"),
        # Hard-wired to the PZ container; logs go through `/api/servers/{key}/logs`.
        ("/api/logs", "hard-wired to the PZ container"),
        ("/api/logs/download", "hard-wired to the PZ container"),
        # --- the last global feature routers ---
        # Without a scope AND without an ownership check: this way the broadcasts of Minecraft
        # and Vintage Story could be read and overwritten.
        ("/api/broadcasts", "no scope/ownership check — other games' entries editable"),
        ("/api/broadcasts/{bid}", "no scope/ownership check"),
        # The same gap; the global DELETE also hit MC/VS restart schedules.
        ("/api/schedules", "no scope/ownership check"),
        ("/api/schedules/{sched_id}", "no scope/ownership check"),
        # Fixed to pz-1: a ban always went to Project Zomboid, whichever server was selected.
        ("/api/moderation/kick", "prod_rcon — always hit pz-1"),
        ("/api/moderation/ban", "prod_rcon — always hit pz-1"),
        ("/api/moderation/unban", "prod_rcon — always hit pz-1"),
        ("/api/moderation/whitelist", "read the pz-1 SQLite directly"),
        ("/api/actions/run", "prod_rcon — always hit pz-1"),
        ("/api/actions/catalog", "only the PZ catalogue"),
        # Replaced by `/api/servers/{key}/backups*` in server_ops.py.
        ("/api/backups", "global, not tied to a server"),
        ("/api/backups/{name}/restore", "global, not tied to a server"),
        # The curated .ini editor, now scoped (see above).
        ("/api/config", "ini.get_ini()/prod_rcon — fixed to pz-1"),
        ("/api/config/raw", "fixed to pz-1"),
        ("/api/config/sandbox", "fixed to pz-1"),
        ("/api/config/sandbox-presets", "fixed to pz-1"),
        # The test instance was dropped without replacement, and with it the last feature router
        # without scoping. It is listed like every other removed route, so that bringing it back
        # is not just flagged as an "unscoped route" but named.
        ("/api/test/status", "test instance removed without replacement"),
        ("/api/test/clone", "test instance removed without replacement"),
        ("/api/test/promote", "test instance removed without replacement"),
    ):
        assert gone not in paths, f"removed route is back: {gone} — {why}"

    # And in general: no unscoped feature route any more. What MAY stay global is either not
    # bound to a server (auth, users, roles, audit, tickets) or deliberately spans ACROSS the
    # servers. Everything else belongs under `/api/servers/{key}/…`.
    #
    # The comparison works on SEGMENT level, not via `startswith` on characters: otherwise a
    # future `/api/servers-legacy/…` would slip through under `/api/servers`.
    allowed_global = {
        "health", "auth", "users", "roles", "audit", "tickets", "me", "links", "stats", "servers",
        # `/api/cross/*` is global on purpose: presence, leaderboard and the chat bridge belong
        # to no single server.
        "cross",
        # Host-wide resource view (`/api/budget`); the per-server parts are scoped.
        "budget",
        # Installation-wide settings (`/api/settings/games`: which games this installation runs).
        "settings",
    }
    # The exemption list is EMPTY, and that is the intended state: every new feature route is
    # scoped without exception.
    unexpected = sorted(
        p for p in paths
        if p.startswith("/api/") and p.split("/")[2] not in allowed_global
    )
    assert not unexpected, f"unscoped feature routes appeared: {unexpected}"
