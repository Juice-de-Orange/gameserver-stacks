"""A Project Zomboid backup must bring the player accounts back, not only the world.

Accounts, their passwords, access levels, bans and the whitelist live in
`Zomboid/db/<servername>.db`. The archive held only `Saves` and `Server`, so a restore brought the
world back without a single account; on a server with `Open=false` nobody could join any more.
The block's own `games/project-zomboid/backup.list` always had `db`.
"""
import os
import sqlite3
import tarfile

from app.models.server import GameServer, GameType
from app.services import backup as bk
from app.services.drivers.pz import PZDriver


def _server(tmp_path) -> GameServer:
    return GameServer(key="pz-1", game_type=GameType.pz, data_dir=str(tmp_path))


def _accounts(db_path) -> list[tuple[str, str]]:
    with sqlite3.connect(db_path) as con:
        return con.execute("SELECT username, accesslevel FROM whitelist ORDER BY username").fetchall()


def _world(data) -> str:
    (data / "Saves" / "Multiplayer" / "pz").mkdir(parents=True)
    (data / "Saves" / "Multiplayer" / "pz" / "map_t.bin").write_bytes(b"world")
    (data / "Server").mkdir()
    (data / "Server" / "pz.ini").write_text("Open=false\n", encoding="utf-8")
    (data / "db").mkdir()
    db_path = str(data / "db" / "pz.db")
    with sqlite3.connect(db_path) as con:
        con.execute("CREATE TABLE whitelist (username TEXT, password TEXT, accesslevel TEXT)")
        con.executemany("INSERT INTO whitelist VALUES (?, ?, ?)",
                        [("admin", "hash-a", "admin"), ("alice", "hash-b", "")])
    return db_path


def test_the_archive_holds_the_account_database(tmp_path):
    spec = PZDriver().backup_spec(_server(tmp_path))
    data = tmp_path / "data"
    _world(data)
    res = bk.create_backup("manual", data_dir=spec.data_dir, world_subdirs=spec.world_subdirs,
                           config_subdirs=spec.config_subdirs)
    with tarfile.open(data / "backups" / res["name"], "r:gz") as tar:
        tops = {n.split("/")[0] for n in tar.getnames()}
    assert tops == {"Saves", "Server", "db"}


def test_a_restore_brings_a_deleted_account_back(tmp_path):
    spec = PZDriver().backup_spec(_server(tmp_path))
    data = tmp_path / "data"
    db_path = _world(data)
    res = bk.create_backup("manual", data_dir=spec.data_dir, world_subdirs=spec.world_subdirs,
                           config_subdirs=spec.config_subdirs)
    with sqlite3.connect(db_path) as con:
        con.execute("DELETE FROM whitelist WHERE username = 'alice'")
    assert _accounts(db_path) == [("admin", "admin")]

    bk.extract_backup(res["name"], data_dir=spec.data_dir)

    assert _accounts(db_path) == [("admin", "admin"), ("alice", "")]


def test_the_accounts_are_not_part_of_the_configuration(tmp_path):
    """"Restore the configuration only" must not reset the accounts to an old state."""
    spec = PZDriver().backup_spec(_server(tmp_path))
    assert "db" in spec.world_subdirs and "db" not in spec.config_subdirs


def test_an_archive_from_before_leaves_the_current_accounts_alone(tmp_path):
    """Archives taken before `db` was added have no `db` folder; restoring one must not wipe it."""
    spec = PZDriver().backup_spec(_server(tmp_path))
    data = tmp_path / "data"
    db_path = _world(data)
    old = bk.create_backup("manual", data_dir=spec.data_dir, world_subdirs=("Saves", "Server"))
    bk.extract_backup(old["name"], data_dir=spec.data_dir)
    assert os.path.exists(db_path)
    assert _accounts(db_path) == [("admin", "admin"), ("alice", "")]
