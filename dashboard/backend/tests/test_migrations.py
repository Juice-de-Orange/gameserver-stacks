"""Static checks of the migration statements in `db.py` against the models.

The suite runs on SQLite and never calls `init_db()` (`conftest.py` only does `create_all`); the
only caller is `main.py` at startup. A typo in a statement would only show at boot on the
production system — and there it would prevent the start.

Deliberately NO Alembic: everything here is idempotent DDL, nothing reshapes data, there is one
database and one operator. Alembic would put a second truth next to `create_all`, on which all
other tests run. Worth adding once one of these holds: a second operator, a need to downgrade, or
the first migration that reshapes data instead of appending columns.

Instead two cheap mechanisms. This test is the first: it reads the statements as text and holds
them against the models. It needs no database and reports no false positives. The second
(`test_migrations_pg.py`, marker `pg`) really executes them.
"""
import re

import pytest
from sqlmodel import SQLModel

from app import models  # noqa: F401  — registers all tables on SQLModel.metadata
from app.db import _COLUMN_MIGRATIONS

ADD_COLUMN = re.compile(
    r"^ALTER TABLE\s+(\w+)\s+ADD COLUMN IF NOT EXISTS\s+(\w+)\s+(\w+)(.*)$", re.IGNORECASE
)
ALTER_COLUMN = re.compile(
    r"^ALTER TABLE\s+(\w+)\s+ALTER COLUMN\s+(\w+)\s+TYPE\s+(\w+)", re.IGNORECASE
)
ADD_ENUM_VALUE = re.compile(
    r"^ALTER TYPE\s+(\w+)\s+ADD VALUE IF NOT EXISTS\s+'([^']+)'", re.IGNORECASE
)
# A dropped column is the inverse of ADD_COLUMN: it must NOT exist on the model. Without this
# form `_parsed()` would reject the statement with `pytest.fail`.
DROP_COLUMN = re.compile(
    r"^ALTER TABLE\s+(\w+)\s+DROP COLUMN IF EXISTS\s+(\w+)", re.IGNORECASE
)
# A dropped table is the inverse of a model: no model may define it any more.
DROP_TABLE = re.compile(r"^DROP TABLE IF EXISTS\s+(\w+)$", re.IGNORECASE)
# `create_all(checkfirst=True)` creates indexes ONLY when it creates the table. On a grown
# database the table exists long since, `create_all` skips it together with its indexes, and an
# `ADD COLUMN` migration only appends the column. An `index=True` on the model is therefore
# ineffective on a grown database — e.g. `metric_samples.server_id`, filtered by exactly that
# column over a large table. Whether an index is unique is a promise about the DATA —
# `test_a_unique_index_matches_the_model` pins that model and migration agree on it. An index
# unique on one side and not on the other would be exactly the drift this file is built against.
CREATE_INDEX = re.compile(
    r"^CREATE (?:UNIQUE )?INDEX IF NOT EXISTS\s+(\w+)\s+ON\s+(\w+)\s*\((\w+)\)",
    re.IGNORECASE,
)

# SQL type -> which SQLAlchemy type names match it. Only those occurring in the repo.
TYPE_MATCHES = {
    # The right side holds SQLAlchemy type names, not SQL keywords: `BigInteger` is called
    # `BIGINTEGER` there, SQLModel maps `str` to `AutoString`, and an explicit `sa_type=String`
    # (tickets.category) is simply `STRING`.
    "VARCHAR": {"VARCHAR", "TEXT", "AUTOSTRING", "STRING"},
    "BOOLEAN": {"BOOLEAN"},
    "INTEGER": {"INTEGER", "BIGINTEGER"},
    "BIGINT": {"INTEGER", "BIGINTEGER"},  # widening is exactly the purpose of the ALTER statement
    "TIMESTAMP": {"DATETIME", "TIMESTAMP"},
}

# Named deviations, measured on a LIVE database. The mechanism stays so a future, deliberately
# accepted drift lands here BY NAME instead of disappearing silently.
#
# The list is currently EMPTY, and that is the message: earlier entries were fixed, not defined
# away.
#   - `tickets.category`: the model had an enum, the migration created VARCHAR — a fresh database
#     got a DB constraint the grown one lacks. Aligned to the VARCHAR side (`sa_type=String` in
#     models/ticket.py, reason given there).
#   - `player_links.server_id`: a dead column nobody read. The migration no longer creates it but
#     removes it (db.py) — otherwise it would stay on grown databases forever.
KNOWN_DEVIATIONS: dict[tuple[str, str], str] = {}


def _tables():
    return SQLModel.metadata.tables


def _parsed():
    """Assign every statement to exactly one form — unknown forms are an error."""
    out = []
    for stmt in _COLUMN_MIGRATIONS:
        s = " ".join(stmt.split())
        for kind, pattern in (
            ("add", ADD_COLUMN), ("drop", DROP_COLUMN), ("alter", ALTER_COLUMN),
            ("enum", ADD_ENUM_VALUE), ("index", CREATE_INDEX), ("drop_table", DROP_TABLE),
        ):
            m = pattern.match(s)
            if m:
                out.append((kind, m, stmt))
                break
        else:
            pytest.fail(f"Unknown migration form, not checked by the test:\n  {stmt}")
    return out


def test_every_statement_has_a_known_form():
    assert len(_parsed()) == len(_COLUMN_MIGRATIONS)


def test_tables_exist():
    """Catches typos and statements for tables renamed or removed since."""
    tables = _tables()
    for kind, m, stmt in _parsed():
        if kind in ("enum", "drop_table"):
            continue
        # For CREATE INDEX the index name is in group 1, the table in group 2.
        tab = m.group(2) if kind == "index" else m.group(1)
        assert tab in tables, f"Table '{tab}' does not exist (any more):\n  {stmt}"


def test_columns_exist_on_the_model():
    """A migration for a column no model carries any more is dead weight — and a sign that
    someone removed a field and forgot the statement."""
    tables = _tables()
    for kind, m, stmt in _parsed():
        if kind in ("enum", "drop", "drop_table"):
            continue
        tab, column = (m.group(2), m.group(3)) if kind == "index" else (m.group(1), m.group(2))
        if (tab, column) in KNOWN_DEVIATIONS:
            continue
        assert column in tables[tab].columns, f"'{tab}.{column}' does not exist on the model:\n  {stmt}"


def test_dropped_columns_are_on_no_model():
    """The inverse: whoever drops a column must not keep it on the model at the same time.

    Otherwise `create_all` creates it on a fresh database and `init_db()` throws it away in the
    next breath — and the application would write into nothing.
    """
    tables = _tables()
    for kind, m, stmt in _parsed():
        if kind != "drop":
            continue
        tab, column = m.group(1), m.group(2)
        assert column not in tables[tab].columns, (
            f"'{tab}.{column}' is dropped but still on the model:\n  {stmt}"
        )


def test_dropped_tables_are_on_no_model():
    """Whoever drops a table must not keep its model: `create_all` would create it on every
    start and `init_db()` would drop it again right after."""
    tables = _tables()
    dropped = [(m.group(1), stmt) for kind, m, stmt in _parsed() if kind == "drop_table"]
    assert dropped, "the DROP TABLE form is no longer used — remove it from this test"
    for tab, stmt in dropped:
        assert tab not in tables, f"'{tab}' is dropped but still a model:\n  {stmt}"


def test_types_are_compatible():
    """Would have found the `mem_bytes` incident: the model was BIGINT, the column INTEGER,
    and the container RSS exceeded 2^31 bytes."""
    tables = _tables()
    for kind, m, stmt in _parsed():
        # `index` has no type — it puts an index on a column already checked.
        if kind in ("enum", "drop", "index", "drop_table"):
            continue
        tab, column, sql_type = m.group(1), m.group(2), m.group(3).upper()
        if (tab, column) in KNOWN_DEVIATIONS:
            continue
        model_type = type(tables[tab].columns[column].type).__name__.upper()
        allowed = TYPE_MATCHES.get(sql_type)
        assert allowed, f"Unknown SQL type '{sql_type}' — extend the test table:\n  {stmt}"
        assert model_type in allowed, (
            f"'{tab}.{column}': migration says {sql_type}, model says {model_type}:\n  {stmt}"
        )


def test_the_exception_list_has_not_gone_stale():
    """Every named exception must still refer to an EXISTING statement.

    Otherwise the list would silently keep growing and eventually cover drift that no longer
    exists — an exception nobody remembers is worse than none.
    """
    affected = {
        (m.group(1), m.group(2))
        for kind, m, _ in _parsed() if kind not in ("enum", "drop", "drop_table")
    }
    orphaned = set(KNOWN_DEVIATIONS) - affected
    assert not orphaned, f"Exception without a matching migration (please remove): {orphaned}"


def test_not_null_default_matches_the_model():
    """Exactly what the comment in db.py demands ("the DB default must match the model default
    exactly") — checked here.

    If the DB default differs, existing rows get a different value than new ones. That never
    shows as an error, only as "why does it say something else".
    """
    tables = _tables()
    checked = 0
    for kind, m, stmt in _parsed():
        if kind != "add":
            continue
        tab, column, rest = m.group(1), m.group(2), m.group(4)
        db_default = re.search(r"DEFAULT\s+('[^']*'|\S+)", rest, re.IGNORECASE)
        if not db_default:
            continue
        if (tab, column) in KNOWN_DEVIATIONS:
            continue
        checked += 1
        raw = db_default.group(1).strip().strip("'")
        col = tables[tab].columns[column]
        model = col.default.arg if col.default is not None else None
        if model is None:
            pytest.fail(f"'{tab}.{column}': migration sets DEFAULT {raw!r}, model has none:\n  {stmt}")
        # Make booleans and numbers comparable (SQL 'FALSE' vs. Python False).
        norm = {"true": True, "false": False}.get(raw.lower(), raw)
        if isinstance(norm, str) and re.fullmatch(r"-?\d+", norm):
            norm = int(norm)  # the first integer default: users.token_version
        assert norm == model, (
            f"'{tab}.{column}': DB default {norm!r} != model default {model!r}:\n  {stmt}"
        )

    # Guard against silently checking nothing: if a future regex change swallowed all hits, this
    # test would be green without having checked anything. The number may grow, never collapse.
    assert checked >= 10, f"Only {checked} DEFAULT clauses checked — does the pattern still match?"


def test_enum_values_exist_in_python():
    """`ALTER TYPE … ADD VALUE` extends a Postgres ENUM. If the value is missing from the Python
    enum, the application never writes it — the statement would be decoration."""
    import enum as _enum

    from app import models as m_

    known: dict[str, set[str]] = {}
    for name in dir(m_):
        obj = getattr(m_, name)
        if isinstance(obj, type) and issubclass(obj, _enum.Enum):
            known[name.lower()] = {e.value for e in obj}

    for kind, m, stmt in _parsed():
        if kind != "enum":
            continue
        ptype, value = m.group(1).lower(), m.group(2)
        assert ptype in known, f"Postgres type '{ptype}' has no Python enum:\n  {stmt}"
        assert value in known[ptype], f"'{value}' is missing from the Python enum '{ptype}':\n  {stmt}"


def test_no_duplicate_statements():
    """A duplicate line is harmless (idempotent) but a sign of a bad merge — and the list is the
    only place where the schema history is written down."""
    seen = [" ".join(s.split()) for s in _COLUMN_MIGRATIONS]
    duplicates = {s for s in seen if seen.count(s) > 1}
    assert not duplicates, f"Duplicate migration statements: {duplicates}"


# --- Columns added later with index=True need a CREATE INDEX ------------------------------------
#
# Deliberately without index — every entry with a reason. Empty means: there is no such case.
DELIBERATELY_WITHOUT_INDEX: dict[tuple[str, str], str] = {}


def _model_indexed_columns() -> set[tuple[str, str]]:
    """(table, column) for every column that carries `index=True` on the model."""
    out = set()
    for name, tab in _tables().items():
        for col in tab.columns:
            if col.index:
                out.add((name, col.name))
    return out


def test_indexes_of_added_columns_are_created_too():
    """A column added via ALTER TABLE with `index=True` needs its own CREATE INDEX.

    Otherwise the fresh database (from `create_all`) has the index and the grown one does not —
    the deviation `pytest -m pg` was built for but in principle cannot see: it runs against an
    EMPTY database in which `create_all` builds everything completely.
    """
    added_later = {
        (m.group(1).lower(), m.group(2).lower())
        for kind, m, _ in _parsed() if kind == "add"
    }
    created = {
        (m.group(2).lower(), m.group(3).lower())
        for kind, m, _ in _parsed() if kind == "index"
    }
    missing = sorted(
        f"{tab}.{col}"
        for tab, col in (_model_indexed_columns() & added_later)
        if (tab, col) not in created and (tab, col) not in DELIBERATELY_WITHOUT_INDEX
    )
    assert not missing, (
        "These columns carry `index=True` on the model but were added via ALTER TABLE —\n"
        "on the grown database their index is missing, and every query on them is a\n"
        "full scan:\n  " + "\n  ".join(missing)
    )


def test_index_names_match_those_of_create_all():
    """The name must be exactly the one SQLAlchemy generates (`ix_<table>_<column>`).

    Otherwise the fresh database carries `ix_…` and the grown one a different name — the same
    drift one level deeper, and `KNOWN_DEVIATIONS` is deliberately empty.
    """
    wrong = [
        f"{m.group(1)} (expected: ix_{m.group(2).lower()}_{m.group(3).lower()})"
        for kind, m, _ in _parsed() if kind == "index"
        and m.group(1).lower() != f"ix_{m.group(2).lower()}_{m.group(3).lower()}"
    ]
    assert not wrong, "Index names deviate from SQLAlchemy's scheme:\n  " + "\n  ".join(wrong)


def test_a_unique_index_matches_the_model():
    """`UNIQUE` in the migration ⟺ `unique=True` on the model — in BOTH directions.

    An index unique in the grown database and not in the fresh one (or the other way round) is
    the same drift as a deviating type, only harder to notice: it shows only when someone inserts
    a duplicate value — on one of the installations.
    """
    checked = 0
    for kind, m, stmt in _parsed():
        if kind != "index":
            continue
        tab, column = m.group(2).lower(), m.group(3).lower()
        migration_unique = " ".join(stmt.split()).upper().startswith("CREATE UNIQUE INDEX")
        t = _tables().get(tab)
        assert t is not None, f"{stmt}: table {tab!r} does not exist on the model"
        columns = {c.name.lower(): c for c in t.columns}
        assert column in columns, f"{stmt}: column {tab}.{column} does not exist on the model"
        model_unique = bool(columns[column].unique)
        assert migration_unique == model_unique, (
            f"{tab}.{column}: migration {'UNIQUE' if migration_unique else 'not unique'}, "
            f"model {'unique=True' if model_unique else 'unique=False'} — "
            f"the fresh and the grown database would get different constraints"
        )
        checked += 1
    assert checked >= 5, "hardly any indexes checked — does the pattern still match?"


def test_the_index_exception_list_has_not_gone_stale():
    dead = set(DELIBERATELY_WITHOUT_INDEX) - _model_indexed_columns()
    assert not dead, f"These exceptions no longer apply to anything: {dead}"
