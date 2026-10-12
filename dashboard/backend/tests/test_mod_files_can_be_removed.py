"""Mod files can be removed — by moving them, not by deleting them.

Without a removal route for file-based games, `mods.disabled` grows without bound. For Factorio
that is more than ballast: every file name carries a version, and a folder full of old versions
is a source of errors when dependencies are resolved.

**Two rules that make the difference:**

1. **Moved, not deleted** — the same line as the quarantine in `mods/apply.py`. A mod file is
   sometimes the only copy of a version that no longer exists online. After 30 days the same
   daily job that cleans up the PZ quarantine cleans up here.
2. **An ACTIVE file is not touched.** It would be gone the next moment while the running server
   still has it loaded — on Factorio no client gets in afterwards. The way there is disable +
   apply, where preflight, mandatory backup and boot proof take hold.
"""
from __future__ import annotations

import os

import pytest

from app.models.server import GameServer, GameType
from app.services.mods import foreign
from tests.file_permissions import ONLY_WITH_PERMISSION_REVOCATION


def _srv(tmp_path, game=GameType.factorio) -> GameServer:
    s = GameServer(key="fac", game_type=game, container_name="factorio",
                   data_dir=str(tmp_path), display_name="Factorio", server_type="")
    s.id = 1
    return s


def _create(server, name, *, active: bool) -> str:
    folder = foreign.mods_dir(server) if active else foreign.disabled_dir(server)
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, name)
    with open(path, "wb") as f:
        f.write(b"PK\x03\x04")
    return path


def test_a_disabled_file_is_set_aside(tmp_path):
    server = _srv(tmp_path)
    source = _create(server, "OldStuff_1.0.0.zip", active=False)

    result = foreign.remove_file(server, "OldStuff_1.0.0.zip", active=set())

    assert not os.path.exists(source), "the file is still in the disabled folder"
    assert os.path.isfile(result["set_aside"]), "it was deleted instead of moved"
    assert result["set_aside"].endswith("-OldStuff_1.0.0.zip")


def test_an_active_file_is_rejected(tmp_path):
    """The most important case: it is loaded right now."""
    server = _srv(tmp_path)
    source = _create(server, "Running_1.0.0.zip", active=True)

    with pytest.raises(ValueError, match="is active"):
        foreign.remove_file(server, "Running_1.0.0.zip", active={"Running_1.0.0.zip"})
    assert os.path.exists(source), "it was touched anyway"


def test_an_unknown_file_is_reported(tmp_path):
    with pytest.raises(FileNotFoundError):
        foreign.remove_file(_srv(tmp_path), "doesnotexist.zip", active=set())


def test_no_escape_from_the_mod_folder(tmp_path):
    """`check_filename` comes first — otherwise this would be a path traversal."""
    with pytest.raises(ValueError):
        foreign.remove_file(_srv(tmp_path), "../../../etc/passwd", active=set())


def test_set_aside_files_can_be_found(tmp_path):
    """"Not deleted" only helps if you can also see it."""
    server = _srv(tmp_path)
    _create(server, "Gone_1.0.0.zip", active=False)
    foreign.remove_file(server, "Gone_1.0.0.zip", active=set())

    listing = foreign.list_removed(server)
    assert [e["name"].endswith("-Gone_1.0.0.zip") for e in listing] == [True]
    assert listing[0]["size"] == 4


@ONLY_WITH_PERMISSION_REVOCATION
def test_an_unreadable_folder_is_not_empty(tmp_path):
    """Otherwise the UI claims "nothing here" about a folder it could not look into."""
    server = _srv(tmp_path)
    _create(server, "Gone_1.0.0.zip", active=False)
    foreign.remove_file(server, "Gone_1.0.0.zip", active=set())

    folder = foreign.removed_dir(server)
    os.chmod(folder, 0o000)
    try:
        with pytest.raises(ValueError, match="cannot be read"):
            foreign.list_removed(server)
    finally:
        os.chmod(folder, 0o755)


def test_without_folder_the_list_is_empty(tmp_path):
    """The normal case before anything was ever removed — no error."""
    assert foreign.list_removed(_srv(tmp_path)) == []


def test_the_cleanup_keeps_the_retention_period(tmp_path):
    """Without a retention period, "moved instead of deleted" is just a slower way to fill the disk."""
    import time

    server = _srv(tmp_path)
    _create(server, "Old_1.0.0.zip", active=False)
    result = foreign.remove_file(server, "Old_1.0.0.zip", active=set())
    # backdate by 40 days
    old = time.time() - 40 * 86400
    os.utime(result["set_aside"], (old, old))

    assert foreign.purge_removed(server, days=30) == 1
    assert foreign.list_removed(server) == []


def test_the_cleanup_spares_fresh_files(tmp_path):
    server = _srv(tmp_path)
    _create(server, "New_1.0.0.zip", active=False)
    foreign.remove_file(server, "New_1.0.0.zip", active=set())
    assert foreign.purge_removed(server, days=30) == 0
    assert len(foreign.list_removed(server)) == 1
