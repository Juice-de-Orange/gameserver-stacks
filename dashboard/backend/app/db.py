"""Database engine + session helper."""
from collections.abc import Iterator

from sqlalchemy import text
from sqlmodel import Session, SQLModel, create_engine

from .config import settings

engine = create_engine(settings.database_url, pool_pre_ping=True)

# Lightweight migrations (no Alembic, v1). create_all ONLY creates missing tables — new columns
# on existing tables have to be added here, idempotently. Postgres supports
# ADD COLUMN IF NOT EXISTS.
# IMPORTANT: the DB default must match the model default exactly (see models/).
_COLUMN_MIGRATIONS: tuple[str, ...] = (
    # tickets — categories + mod-request fields
    "ALTER TABLE tickets ADD COLUMN IF NOT EXISTS category    VARCHAR   NOT NULL DEFAULT 'general'",
    "ALTER TABLE tickets ADD COLUMN IF NOT EXISTS workshop_id VARCHAR   NOT NULL DEFAULT ''",
    "ALTER TABLE tickets ADD COLUMN IF NOT EXISTS mod_id      VARCHAR   NOT NULL DEFAULT ''",
    "ALTER TABLE tickets ADD COLUMN IF NOT EXISTS applied_at  TIMESTAMP NULL",
    # users — permission flag for game-master actions
    "ALTER TABLE users   ADD COLUMN IF NOT EXISTS can_use_actions BOOLEAN NOT NULL DEFAULT FALSE",
    # users — permission flag for player moderation
    "ALTER TABLE users   ADD COLUMN IF NOT EXISTS can_moderate    BOOLEAN NOT NULL DEFAULT FALSE",
    # users — last login (player info)
    "ALTER TABLE users   ADD COLUMN IF NOT EXISTS last_login      TIMESTAMP NULL",
    # users — last request of any kind; `last_login` alone does not answer "last online"
    "ALTER TABLE users   ADD COLUMN IF NOT EXISTS last_seen       TIMESTAMP NULL",
    # users — e-mail for magic-link sign-in. NULLABLE, not `NOT NULL DEFAULT ''`: Postgres allows
    # any number of NULLs in a UNIQUE index but only ONE empty string — existing accounts would
    # collide when the index is created.
    "ALTER TABLE users   ADD COLUMN IF NOT EXISTS email           VARCHAR NULL",
    "CREATE UNIQUE INDEX IF NOT EXISTS ix_users_email ON users (email)",
    # users — token revocation: every token carries this value, raising it invalidates them all
    "ALTER TABLE users   ADD COLUMN IF NOT EXISTS token_version   INTEGER NOT NULL DEFAULT 0",
    # --- Multi-game: server_id on all per-feature tables ---
    # Kept without a foreign key (plain INTEGER).
    "ALTER TABLE audit_log           ADD COLUMN IF NOT EXISTS server_id INTEGER NULL",
    "ALTER TABLE player_sessions     ADD COLUMN IF NOT EXISTS server_id INTEGER NULL",
    # `player_links.server_id` used to be added here and is dropped again further down — the
    # line was deliberately replaced by the DROP instead of just being removed (see there).
    "ALTER TABLE player_events       ADD COLUMN IF NOT EXISTS server_id INTEGER NULL",
    "ALTER TABLE restart_schedules   ADD COLUMN IF NOT EXISTS server_id INTEGER NULL",
    "ALTER TABLE broadcast_schedules ADD COLUMN IF NOT EXISTS server_id INTEGER NULL",
    "ALTER TABLE metric_samples      ADD COLUMN IF NOT EXISTS server_id INTEGER NULL",
    "ALTER TABLE server_incidents    ADD COLUMN IF NOT EXISTS server_id INTEGER NULL",
    "ALTER TABLE tickets             ADD COLUMN IF NOT EXISTS server_id INTEGER NULL",
    # player_links — cross-game identity (leaderboard)
    "ALTER TABLE player_links        ADD COLUMN IF NOT EXISTS game        VARCHAR NOT NULL DEFAULT 'pz'",
    "ALTER TABLE player_links        ADD COLUMN IF NOT EXISTS external_id VARCHAR NOT NULL DEFAULT ''",
    # broadcast_schedules — one announcement to ALL running servers
    "ALTER TABLE broadcast_schedules ADD COLUMN IF NOT EXISTS target_all BOOLEAN NOT NULL DEFAULT FALSE",
    # users — global RBAC flags (admin-only by default via the seed_role_permissions top-up)
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS can_manage_resources    BOOLEAN NOT NULL DEFAULT FALSE",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS can_manage_versions     BOOLEAN NOT NULL DEFAULT FALSE",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS can_manage_cross_server BOOLEAN NOT NULL DEFAULT FALSE",
    # metric_samples.mem_bytes: INTEGER → BIGINT (container RSS > 2^31 B; idempotent, widening only)
    "ALTER TABLE metric_samples ALTER COLUMN mem_bytes TYPE BIGINT",
    # --- Arma Reforger: extend the native Postgres ENUMs with the new values. ---
    # create_all ONLY creates missing tables, not missing ENUM values on existing types.
    # `ADD VALUE IF NOT EXISTS` is idempotent; on PG12+ it is also allowed INSIDE a transaction
    # as long as the new value is not USED in that same transaction (here it is only added).
    # (init_db() does not run in the SQLite tests — there create_all builds the enums from scratch.)
    "ALTER TYPE gametype ADD VALUE IF NOT EXISTS 'arma_reforger'",
    "ALTER TYPE consolekind ADD VALUE IF NOT EXISTS 'battleye_rcon'",
    # --- Valheim, Avorion, Factorio: same extension, same reason as above. ---
    # `consolekind` needs NO new value: Avorion and Factorio speak Source RCON, Valheim has no
    # console at all (`none` exists for both already).
    "ALTER TYPE gametype ADD VALUE IF NOT EXISTS 'valheim'",
    "ALTER TYPE gametype ADD VALUE IF NOT EXISTS 'avorion'",
    "ALTER TYPE gametype ADD VALUE IF NOT EXISTS 'factorio'",
    # player_events — game account ID and world coordinates from the log line. The ID is the
    # reliable key for account linking (names can change); the coordinates make a map of death
    # locations possible.
    "ALTER TABLE player_events ADD COLUMN IF NOT EXISTS game_id VARCHAR NOT NULL DEFAULT ''",
    "ALTER TABLE player_events ADD COLUMN IF NOT EXISTS x INTEGER NULL",
    "ALTER TABLE player_events ADD COLUMN IF NOT EXISTS y INTEGER NULL",
    "ALTER TABLE player_events ADD COLUMN IF NOT EXISTS z INTEGER NULL",
    # `player_links.server_id` was dead weight: the column was created and backfilled at every
    # start and read by nobody. Account links are scoped by `game` (routers/stats.py), not by
    # server; the field does not exist on the model. A fresh database therefore never had the
    # column while a grown one did — exactly the kind of drift `pytest -m pg` reports.
    # DROP instead of silently removing the ADD line: otherwise the column would stay in grown
    # databases forever and the drift would persist, just unobserved.
    "ALTER TABLE player_links DROP COLUMN IF EXISTS server_id",
    # `magic_tokens` and `invites` were created for a sign-in by link that was never built: no
    # route, job or service ever wrote or read a row, and the models are gone. Dropped for the
    # same reason as the column above — a grown database would otherwise keep two empty tables
    # a fresh one does not have.
    # game_servers — host address the game ports are published on (`services/ports.py`)
    "ALTER TABLE game_servers ADD COLUMN IF NOT EXISTS bind_address VARCHAR NOT NULL DEFAULT '0.0.0.0'",
    "DROP TABLE IF EXISTS magic_tokens",
    "DROP TABLE IF EXISTS invites",
    # --- Indexes for the columns added above ---------------------------------------------------
    #
    # `SQLModel.create_all(checkfirst=True)` creates indexes ONLY when it creates a table. On a
    # grown database the table already exists — `create_all` skips it including its indexes, and
    # an `ADD COLUMN` above only adds the column. `index=True` on the model has no effect for
    # these columns there. `metric_samples` grows by one row per minute and server, and
    # `server_id` is the column every statistics query filters on — a full scan without this.
    #
    # The names are exactly the ones SQLAlchemy assigns itself (`ix_<table>_<column>`) —
    # otherwise a fresh database would carry different names than a grown one, which is the
    # drift `tests/test_migrations.py` is there to find. No `CONCURRENTLY`: it cannot run inside
    # the transaction of `init_db()`, and at these row counts the simple way takes seconds.
    "CREATE INDEX IF NOT EXISTS ix_audit_log_server_id ON audit_log (server_id)",
    "CREATE INDEX IF NOT EXISTS ix_player_sessions_server_id ON player_sessions (server_id)",
    "CREATE INDEX IF NOT EXISTS ix_player_events_server_id ON player_events (server_id)",
    "CREATE INDEX IF NOT EXISTS ix_player_events_game_id ON player_events (game_id)",
    "CREATE INDEX IF NOT EXISTS ix_restart_schedules_server_id ON restart_schedules (server_id)",
    "CREATE INDEX IF NOT EXISTS ix_broadcast_schedules_server_id ON broadcast_schedules (server_id)",
    "CREATE INDEX IF NOT EXISTS ix_metric_samples_server_id ON metric_samples (server_id)",
    "CREATE INDEX IF NOT EXISTS ix_server_incidents_server_id ON server_incidents (server_id)",
    "CREATE INDEX IF NOT EXISTS ix_tickets_server_id ON tickets (server_id)",
    "CREATE INDEX IF NOT EXISTS ix_tickets_category ON tickets (category)",
)


def init_db() -> None:
    """Create the tables (v1 via create_all; Alembic as a follow-up).

    The models are imported so that they are registered on SQLModel.metadata.
    Afterwards the idempotent column migrations run for tables that already exist.
    """
    from . import models  # noqa: F401

    SQLModel.metadata.create_all(engine)

    with engine.begin() as conn:  # begin() commits on success
        for statement in _COLUMN_MIGRATIONS:
            conn.execute(text(statement))


def get_session() -> Iterator[Session]:
    with Session(engine) as session:
        yield session
