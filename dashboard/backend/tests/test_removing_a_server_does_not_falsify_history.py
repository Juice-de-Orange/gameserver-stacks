"""Taking a server out of the registry is a DATA change, and it has a trap.

"Delete the row, done" would cause silent damage in two ways:

1. **"Releasing" dependent rows leaves orphans.** The obvious way is to set `server_id` to
   NULL so nothing points into the void. A row without a server belongs to nobody: no
   server-scoped view shows it any more, and nothing ever removes it. Telemetry of a removed
   server is therefore deleted, not released.

2. **Records are not telemetry.** `audit_log` and `tickets` record what PEOPLE did. They
   outlive the server they are about; a `server_id` pointing into the void is more honest there
   than a wrong one or no entry at all.

This guard pins both.
"""
import inspect
import re

from sqlmodel import Session, select

from app import cli
from app.services import provisioning
from app.models import AuditLog, GameServer, GameType, ManagementMode, ServerIncident


def _server(session, key="pz-test") -> GameServer:
    s = GameServer(key=key, game_type=GameType.pz, display_name=key,
                   management_mode=ManagementMode.dashboard, container_name=key,
                   data_dir=f"/srv/gameservers/{key}", enabled=False)
    session.add(s)
    session.commit()
    session.refresh(s)
    return s


def _inventory(engine, sid):
    with Session(engine) as s:
        server = s.exec(select(GameServer).where(GameServer.id == sid)).first()
        incidents = s.exec(select(ServerIncident).where(ServerIncident.server_id == sid)).all()
        audit = s.exec(select(AuditLog).where(AuditLog.server_id == sid)).all()
    return server, len(incidents), len(audit)


def _with_engine(monkeypatch, engine):
    """`cli._remove_server` and `provisioning` take `engine` from `app.db` at call time; redirect it there."""
    import app.db
    monkeypatch.setattr(app.db, "engine", engine)


def test_a_dry_run_changes_nothing(mem_engine, monkeypatch, capsys):
    """Without `--write` it reports, it does not act."""
    with Session(mem_engine) as s:
        srv = _server(s)
        s.add(ServerIncident(kind="rcon_hang", server_id=srv.id))
        s.add(AuditLog(action="server.start.forced", username="admin", server_id=srv.id))
        s.commit()
        sid = srv.id

    _with_engine(monkeypatch, mem_engine)
    assert cli._remove_server("pz-test", write=False) == 0
    assert "DRY RUN" in capsys.readouterr().out

    server, incidents, audit = _inventory(mem_engine, sid)
    assert server is not None, "the dry run removed the registry row"
    assert (incidents, audit) == (1, 1), "the dry run touched data"


def test_telemetry_goes_records_stay(mem_engine, monkeypatch):
    """Incidents disappear with their server, the audit log does not."""
    with Session(mem_engine) as s:
        srv = _server(s)
        s.add(ServerIncident(kind="rcon_hang", server_id=srv.id))
        s.add(AuditLog(action="server.start.forced", username="admin", server_id=srv.id))
        s.commit()
        sid = srv.id

    _with_engine(monkeypatch, mem_engine)
    assert cli._remove_server("pz-test", write=True) == 0

    server, incidents, audit = _inventory(mem_engine, sid)
    assert server is None, "the registry row is still there"
    assert incidents == 0, "telemetry of the removed server was left behind"
    assert audit == 1, (
        "the audit log was deleted too; it records what people did "
        "and outlives the server"
    )


def test_another_server_stays_untouched(mem_engine, monkeypatch):
    """Cross-check: exactly ONE row is removed, not its neighbours."""
    with Session(mem_engine) as s:
        removed = _server(s, "pz-test")
        kept = _server(s, "pz-1")
        s.add(ServerIncident(kind="rcon_hang", server_id=kept.id))
        s.commit()
        removed_id, kept_id = removed.id, kept.id

    _with_engine(monkeypatch, mem_engine)
    cli._remove_server("pz-test", write=True)

    assert _inventory(mem_engine, removed_id)[0] is None
    server, incidents, _ = _inventory(mem_engine, kept_id)
    assert server is not None and incidents == 1, "another server was removed along with it"


def test_an_unknown_key_is_not_an_error(mem_engine, monkeypatch, capsys):
    """Idempotence: removing twice must not fail."""
    _with_engine(monkeypatch, mem_engine)
    assert cli._remove_server("does-not-exist", write=True) == 0
    assert "nothing to do" in capsys.readouterr().out


# ---- The actual guard ----

def test_server_id_is_NEVER_set_to_null():
    """Dependent rows are deleted, never released: a NULL `server_id` is an orphan that no
    server-scoped view shows and nothing cleans up."""
    source = inspect.getsource(provisioning.delete_server_rows) + inspect.getsource(cli._remove_server)
    assert not re.search(r"server_id\s*=\s*(NULL|None)", source, re.IGNORECASE), (
        "`server_id` is set to NULL — that leaves orphan rows instead of deleting telemetry"
    )


def test_records_are_not_in_the_delete_list():
    """Checks the list the DELETE loop REALLY uses.

    Comparing `RECORDS` against a repetition of its own literals could never fail as long as
    the constant exists; so the source of the loop is queried instead.
    """
    assert not (provisioning.RECORDS & provisioning.TELEMETRY), (
        "a table is in BOTH lists; it would be deleted AND reported as kept"
    )
    source = inspect.getsource(provisioning.delete_server_rows)
    assert "for tbl in to_delete:" in source, (
        "the DELETE loop no longer iterates over `to_delete`; this guard points into the void"
    )
    assert "to_delete = [t for t in tables if t in TELEMETRY]" in source, (
        "the delete list is no longer built from TELEMETRY"
    )


def test_the_five_forgotten_tables_are_listed_by_name():
    """These tables are named here, not derived.

    Comparing `_per_server_tables()` against a literal copy of its own body would move along
    with it; naming the tables keeps the check independent.
    """
    for tbl in ("operations", "mod_sets", "mod_changes", "version_change",
                "chat_bridge_messages"):
        assert tbl in provisioning.TELEMETRY, f"{tbl} is missing from the delete list again"


def test_every_per_server_table_is_classified():
    """No table with `server_id` may be left unconsidered, and guessing is forbidden.

    Deriving from the model is the REPORT. The delete list is enumerated explicitly: a future
    table that holds records and gets a `server_id` would otherwise land silently in the
    DELETE. If this test fails, it means "classify it", not "extend the derivation".
    """
    open_ = set(provisioning.per_server_tables()) - provisioning.TELEMETRY - provisioning.RECORDS
    assert not open_, (
        f"these per-server tables are neither telemetry nor records: {sorted(open_)}; "
        f"add them to `provisioning.TELEMETRY` or `provisioning.RECORDS`"
    )


def test_an_unknown_table_aborts_instead_of_guessing(mem_engine, monkeypatch, capsys):
    """Cross-check: the command deletes nothing it cannot classify."""
    with Session(mem_engine) as s:
        _server(s, "pz-test")
    _with_engine(monkeypatch, mem_engine)
    monkeypatch.setattr(provisioning, "per_server_tables",
                        lambda: ["audit_log", "server_incidents", "brand_new_table"])
    assert cli._remove_server("pz-test", write=True) == 2
    output = capsys.readouterr().out
    assert "NOT CLASSIFIED" in output and "brand_new_table" in output


def test_an_enabled_server_is_refused(mem_engine, monkeypatch, capsys):
    """A typo must not take the production server out of the registry.

    The command is irreversible. `remove-server pz-1 --write` would delete the registry row of
    the running server, with its restart schedules, broadcasts and metric series.
    """
    with Session(mem_engine) as s:
        srv = _server(s, "pz-1")
        srv.enabled = True
        s.add(srv)
        s.commit()
        sid = srv.id

    _with_engine(monkeypatch, mem_engine)
    assert cli._remove_server("pz-1", write=True) == 2
    assert "ABORTED" in capsys.readouterr().out
    assert _inventory(mem_engine, sid)[0] is not None, "an enabled server was removed"

    # ... but with an explicit opt-in it works
    assert cli._remove_server("pz-1", write=True, include_active=True) == 0
    assert _inventory(mem_engine, sid)[0] is None


def test_a_flag_is_not_a_key(capsys):
    """`remove-server --write` (key forgotten) must not report success and do nothing."""
    assert cli.main(["remove-server", "--write"]) == 2
    assert "needs a server key" in capsys.readouterr().out
