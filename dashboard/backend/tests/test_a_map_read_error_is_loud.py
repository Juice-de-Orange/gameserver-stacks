"""A read error on the map tree must not pass as a result.

`services/mods/cells.py` used to answer a decision question silently on a read error:
`cells_of_map_dir` returned an empty cell set, `find_map_dirs` an empty dict, `isoregion_files`
an empty list, and none of these answers could be told apart from the real one. Two decisions
hang on them:

* **`MAP_WITHOUT_PROVIDER`** (preflight): a map in `Map=` for which no item provides cells is a
  blocker, the join crash the whole mod check was built against. A map folder that is merely not
  READABLE looked exactly like "is a spawn overlay": no blocker. Fail-open, at the very place
  meant to be fail-closed.
* **World binding** (`apply.plan_world_reset`): which world files go along when a map is
  removed. An empty `isoregiondata/` means "nothing to move".

**The trap:** `os.walk` does not raise. It reports errors to an `onerror` callback and drops them
by default (`onerror=None`), so an `except OSError` around it is dead code. That is why this guard
checks both paths separately: the error on opening AND the error *during* the walk.

**The boundary that must not slip:** a MISSING directory is not a read error. A save without
`isoregiondata/` is normal, and so is an item without `media/maps`. Only an access that is denied
or aborted is one; otherwise the fail-open turns into a guard that blocks every fresh server.
"""
from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

from app.services.mods import cells, inventory


# --------------------------------------------------------------- error ON OPENING

def test_cells_of_map_dir_raises_on_denied_access(monkeypatch):
    def walk_raises(top, **kw):
        raise PermissionError(13, "Permission denied", top)
    monkeypatch.setattr(cells.os, "walk", walk_raises)
    with pytest.raises(cells.ModTreeReadError):
        cells.cells_of_map_dir("/somewhere/media/maps/Newtown")


def test_find_map_dirs_raises_on_denied_access(monkeypatch):
    def walk_raises(top, **kw):
        raise PermissionError(13, "Permission denied", top)
    monkeypatch.setattr(cells.os, "walk", walk_raises)
    with pytest.raises(cells.ModTreeReadError):
        cells.find_map_dirs("/somewhere/108600/111")


def test_isoregion_files_raises_on_denied_access(monkeypatch):
    def listdir_raises(path):
        raise PermissionError(13, "Permission denied", path)
    monkeypatch.setattr(cells.os, "listdir", listdir_raises)
    with pytest.raises(cells.ModTreeReadError):
        cells.isoregion_files("/somewhere/Saves/Multiplayer/world")


# --------------------------------------------------------- error DURING the walk

def test_an_error_during_the_walk_is_not_swallowed(monkeypatch):
    """By default `os.walk` swallows everything it meets on the way.

    This is where the dead code was: the `except OSError` could never fire, because `os.walk`
    does not raise. If this test is red, the `onerror` callback is probably missing.
    """
    seen = {}

    def walk_reports_on_the_way(top, onerror=None, **kw):
        seen["onerror"] = onerror
        assert onerror is not None, "os.walk without onerror drops errors during the walk"
        onerror(PermissionError(13, "Permission denied", os.path.join(top, "deep")))
        return iter(())

    monkeypatch.setattr(cells.os, "walk", walk_reports_on_the_way)
    with pytest.raises(cells.ModTreeReadError):
        cells.cells_of_map_dir("/somewhere/media/maps/Newtown")

    monkeypatch.setattr(cells.os, "walk", walk_reports_on_the_way)
    with pytest.raises(cells.ModTreeReadError):
        cells.find_map_dirs("/somewhere/108600/111")


# ------------------------------------------------- the boundary: missing is NOT unreadable

def test_a_missing_directory_stays_an_empty_answer(tmp_path):
    """A fresh server has no `media/maps`, a young save no `isoregiondata/`.

    If that raised, the guard would block normal operation: fail-open would have become
    fail-shut, and that is not an improvement but the next incident.
    """
    missing = str(tmp_path / "doesnotexist")
    assert cells.cells_of_map_dir(missing) == set()
    assert cells.find_map_dirs(missing) == {}
    assert cells.isoregion_files(missing) == []
    assert cells.is_tile_map(missing) is False


def test_an_error_during_the_walk_does_not_report_missing_as_a_read_error(monkeypatch):
    """`os.walk` also reports a vanished directory to `onerror`; that is not a read error."""
    def walk_reports_missing(top, onerror=None, **kw):
        assert onerror is not None
        onerror(FileNotFoundError(2, "No such file or directory", top))
        return iter(())
    monkeypatch.setattr(cells.os, "walk", walk_reports_missing)
    assert cells.cells_of_map_dir("/somewhere/media/maps/Newtown") == set()


# ----------------------------------------------------------- the answer stays right

def test_the_good_case_stays_unchanged(tmp_path):
    """Cross-check: without a read error the guard breaks nothing in the count."""
    map_dir = tmp_path / "42" / "media" / "maps" / "Newtown"
    map_dir.mkdir(parents=True)
    (map_dir / "30_31.lotheader").write_text("x", encoding="utf-8")
    (map_dir / "30_32.lotheader").write_text("x", encoding="utf-8")

    assert cells.cells_of_map_dir(str(map_dir)) == {(30, 31), (30, 32)}
    assert cells.find_map_dirs(str(tmp_path)) == {"Newtown": str(map_dir)}
    assert cells.is_tile_map(str(map_dir)) is True


def test_is_tile_map_does_not_hide_the_read_error(monkeypatch):
    """Otherwise a locked map looks like a spawn overlay, and `Map=` stays unchecked."""
    def walk_raises(top, **kw):
        raise PermissionError(13, "Permission denied", top)
    monkeypatch.setattr(cells.os, "walk", walk_raises)
    with pytest.raises(cells.ModTreeReadError):
        cells.is_tile_map("/somewhere/media/maps/Newtown")


# ------------------------------------------------------- the caller decides, loudly

def test_the_inventory_reports_unreadable_instead_of_empty(tmp_path, monkeypatch):
    """The documented path: `Inventory.readable=False` is a blocker in the preflight.

    That puts the read error where the decision is made anyway, instead of letting it wander
    through three computations as an empty set.
    """
    item = tmp_path / "workshop-mods" / "content" / "108600" / "111" / "42"
    item.mkdir(parents=True)
    (item / "mod.info").write_text("id=TownMod\n", encoding="utf-8")

    def find_raises(item_dir):
        raise cells.ModTreeReadError(f"map tree not readable: {item_dir}")
    monkeypatch.setattr(cells, "find_map_dirs", find_raises)

    inv = inventory.scan(SimpleNamespace(data_dir=str(tmp_path), key="pz-test"))
    assert inv.readable is False
    assert inv.detail, "without a detail nobody knows WHAT was unreadable"
    assert "111" in inv.detail, "the message must name the item"
