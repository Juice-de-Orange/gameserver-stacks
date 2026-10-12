"""Actually run `init_db()` — against a throw-away Postgres, not against SQLite.

`tests/test_migrations.py` reads the statements as text. That catches typos and drift, but not
everything: `ALTER TYPE … ADD VALUE` and `ALTER COLUMN … TYPE BIGINT` do not exist in SQLite, so
they can never run there. Here they run.

Also checked is IDEMPOTENCE: `init_db()` runs on EVERY application start. A statement that
raises the second time takes the dashboard down on the next restart — and only then, not when it
is introduced.

The test skips itself when no Postgres answers; `pytest.ini` excludes it from the default run
anyway (marker `pg`).

Start a throw-away Postgres and point the test at it:

    docker run -d --rm --name console-migtest -e POSTGRES_PASSWORD=x -p 127.0.0.1:55433:5432 postgres:16-alpine
    cd dashboard/backend
    PG_TEST_URL=postgresql+psycopg://postgres:x@127.0.0.1:55433/postgres .venv-test/bin/python -m pytest -m pg
(PowerShell: $env:PG_TEST_URL="…"; .venv-test\\Scripts\\python -m pytest -m pg)

⚠ The fixture below runs `DROP SCHEMA public CASCADE`. It therefore **refuses** to empty a
schema that holds tables of another project, and the default port is 55433 rather than the
more common 55432 — a test must never be able to wipe somebody else's database by accident.
"""
import os

import pytest
from sqlalchemy import create_engine, inspect, text

pytestmark = pytest.mark.pg

# 55433, not the more common 55432 (see the module docstring).
PG_URL = os.environ.get("PG_TEST_URL", "postgresql+psycopg://postgres:x@127.0.0.1:55433/postgres")


@pytest.fixture(scope="module")
def pg_engine():
    try:
        eng = create_engine(PG_URL, pool_pre_ping=True)
        with eng.connect() as c:
            c.execute(text("SELECT 1"))
    except Exception as e:  # noqa: BLE001 — any kind of connection error means "not there"
        pytest.skip(f"No Postgres at {PG_URL} ({type(e).__name__}) — test skipped.")
    # **Before emptying: does this schema belong to us at all?**
    #
    # What follows is `DROP SCHEMA public CASCADE` — it deletes EVERYTHING.
    #
    # So only two cases are allowed: an EMPTY schema (fresh throw-away Postgres) or one that
    # holds nothing but our own tables (an earlier run of this same test).
    import app.models  # noqa: F401 — registers all tables, as `init_db` does
    from sqlmodel import SQLModel as _SQLModel

    ours = set(_SQLModel.metadata.tables)
    present = set(inspect(eng).get_table_names(schema="public"))
    foreign = sorted(present - ours)
    if foreign:
        pytest.fail(
            f"ABORT: {PG_URL} holds tables that do not belong to this project "
            f"({', '.join(foreign[:8])}{' …' if len(foreign) > 8 else ''}). This test would "
            f"EMPTY the schema. Use a throw-away Postgres of your own."
        )

    # Fresh schema per run, so an earlier run cannot falsify the result.
    with eng.begin() as c:
        c.execute(text("DROP SCHEMA public CASCADE"))
        c.execute(text("CREATE SCHEMA public"))
    yield eng
    eng.dispose()  # otherwise psycopg reports open connections as ResourceWarning at the end


def _init_db_against(engine, monkeypatch):
    """Run `init_db()` with a swapped engine — the same function as at app start."""
    from app import db as db_mod

    monkeypatch.setattr(db_mod, "engine", engine)
    db_mod.init_db()


def test_init_db_runs_and_is_idempotent(pg_engine, monkeypatch):
    """Twice in a row — exactly what happens on every container restart."""
    _init_db_against(pg_engine, monkeypatch)
    _init_db_against(pg_engine, monkeypatch)  # must not raise

    names = set(inspect(pg_engine).get_table_names())
    assert "game_servers" in names and "users" in names and "player_events" in names


def test_the_extended_enums_know_their_new_values(pg_engine, monkeypatch):
    """`ALTER TYPE … ADD VALUE` cannot be expressed on SQLite and is otherwise unchecked.

    If it fails, no Reforger, Valheim, Avorion or Factorio server can be created — with a
    database error, not with a message.
    """
    _init_db_against(pg_engine, monkeypatch)
    with pg_engine.connect() as c:
        for type_name, value in (
            ("gametype", "arma_reforger"),
            ("consolekind", "battleye_rcon"),
            ("gametype", "valheim"),
            ("gametype", "avorion"),
            ("gametype", "factorio"),
        ):
            labels = c.execute(
                text("SELECT enumlabel FROM pg_enum e JOIN pg_type t ON t.oid = e.enumtypid "
                     "WHERE t.typname = :t"),
                {"t": type_name},
            ).scalars().all()
            assert value in labels, f"'{value}' missing from Postgres enum '{type_name}' (has: {labels})"


def test_mem_bytes_is_really_bigint(pg_engine, monkeypatch):
    """A container RSS above 2^31 bytes must not kill the write."""
    _init_db_against(pg_engine, monkeypatch)
    with pg_engine.connect() as c:
        data_type = c.execute(
            text("SELECT data_type FROM information_schema.columns "
                 "WHERE table_name = 'metric_samples' AND column_name = 'mem_bytes'")
        ).scalar_one()
    assert data_type == "bigint", f"mem_bytes is {data_type}, not bigint"


def test_tickets_category_is_varchar_as_on_the_host(pg_engine, monkeypatch):
    """A fresh database must get the same column as a grown one.

    `create_all` must not create the native type `ticketcategory` while the migration writes
    VARCHAR — a new install would carry a DB constraint the grown database lacks. Exactly the
    kind of drift that is green in tests and breaks live.
    """
    _init_db_against(pg_engine, monkeypatch)
    with pg_engine.connect() as c:
        data_type, udt = c.execute(
            text("SELECT data_type, udt_name FROM information_schema.columns "
                 "WHERE table_name = 'tickets' AND column_name = 'category'")
        ).one()
    assert data_type == "character varying", f"tickets.category is {data_type}/{udt}, not varchar"


def test_the_category_value_comes_back_as_text(pg_engine, monkeypatch):
    """The price of the alignment: the enum VALUE is written and read, not its name.

    `TicketCategory` inherits from `str` — otherwise, after the switch to VARCHAR, the database
    would hold 'TicketCategory.bug_report' instead of 'bug_report'.
    """
    from sqlmodel import Session, select

    from app.models.ticket import Ticket, TicketCategory

    _init_db_against(pg_engine, monkeypatch)
    with Session(pg_engine) as s:
        s.add(Ticket(author_id=1, title="Sample", category=TicketCategory.bug_report))
        s.commit()
    with pg_engine.connect() as c:
        raw = c.execute(text("SELECT category FROM tickets WHERE title = 'Sample'")).scalar_one()
    assert raw == "bug_report", f"the database holds {raw!r}"
    with Session(pg_engine) as s:
        reloaded = s.exec(select(Ticket).where(Ticket.title == "Sample")).one()
    assert reloaded.category == TicketCategory.bug_report


def test_player_links_no_longer_has_the_dead_column(pg_engine, monkeypatch):
    """Not even when it was there before — the DROP must really heal the grown schema."""
    _init_db_against(pg_engine, monkeypatch)
    # Recreate the old state on purpose: the grown schema still carried this column.
    with pg_engine.begin() as c:
        c.execute(text("ALTER TABLE player_links ADD COLUMN IF NOT EXISTS server_id INTEGER NULL"))
    _init_db_against(pg_engine, monkeypatch)

    columns = {col["name"] for col in inspect(pg_engine).get_columns("player_links")}
    assert "server_id" not in columns, f"server_id is still alive: {sorted(columns)}"
    assert "game" in columns and "external_id" in columns  # the real scoping stays


def test_the_dead_sign_in_tables_are_dropped(pg_engine, monkeypatch):
    """`magic_tokens` and `invites` never had a reader or a writer; a grown schema loses them."""
    _init_db_against(pg_engine, monkeypatch)
    with pg_engine.begin() as c:
        c.execute(text("CREATE TABLE IF NOT EXISTS magic_tokens (id INTEGER PRIMARY KEY)"))
        c.execute(text("CREATE TABLE IF NOT EXISTS invites (id INTEGER PRIMARY KEY)"))
    _init_db_against(pg_engine, monkeypatch)

    tables = set(inspect(pg_engine).get_table_names(schema="public"))
    assert not {"magic_tokens", "invites"} & tables
