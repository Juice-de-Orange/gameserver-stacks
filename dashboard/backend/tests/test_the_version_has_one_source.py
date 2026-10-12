"""For a game whose version is in the image tag there is no second truth.

The case: a server carried `image_tag = "2.0.77-rootless"` — the container demonstrably ran with
it — and `game_version = "2.1.14-rootless"`. A version change to 2.1.14 had failed on the boot
proof and been rolled back; the tag came back, the version display did not. The `previous_*`
fields show the gap too: `previous_image_tag` set, `previous_game_version` empty.

**The cost was more than a wrong number.** The mod portal search filters results against the
running game series. It offered mods built for 2.1 to a 2.0 server — for Factorio a mismatched mod
set is no cosmetic flaw but a door that stays shut: the client compares exactly.

**The answer is not to maintain the second column better.** For these games it is not asked at
all anymore: `drivers.running_version` takes the tag when `version_source = image_tag`. For `env`
and `runtime` games `game_version` stays the source — there the tag really says nothing (Minecraft
hangs on `VERSION`, Reforger and Valheim fetch the game on every start).

The display must not read the wrong side — here both sides sit in THE SAME table.
"""
from __future__ import annotations

import pytest

from app.models.server import GameServer, GameType
from app.services.drivers import VersionSource, running_version, version_source


def _srv(game_type: GameType, *, tag: str, column: str) -> GameServer:
    s = GameServer(key="x", game_type=game_type, container_name="x", data_dir="/x",
                   image_repo="repo", image_tag=tag, game_version=column)
    s.id = 1
    return s


# Exactly the three whose version is in the tag.
IN_TAG = [GameType.pz, GameType.factorio, GameType.avorion]
NOT_IN_TAG = [GameType.minecraft, GameType.vintagestory, GameType.valheim, GameType.arma_reforger]


@pytest.mark.parametrize("game_type", IN_TAG)
def test_for_tag_games_the_tag_wins(game_type):
    """The concrete case — tag 2.0.77, column 2.1.14 — pinned down."""
    assert version_source(_srv(game_type, tag="a", column="b")) is VersionSource.image_tag
    srv = _srv(game_type, tag="2.0.77-rootless", column="2.1.14-rootless")
    assert running_version(srv) == "2.0.77-rootless", (
        "the stale column wins again — exactly the state this test exists for"
    )


@pytest.mark.parametrize("game_type", NOT_IN_TAG)
def test_for_the_others_the_column_stays_the_source(game_type):
    """The opposite direction. For Minecraft the version hangs on `VERSION`, for Reforger and
    Valheim the image fetches the game itself on every start — the tag really says nothing there."""
    assert version_source(_srv(game_type, tag="a", column="b")) is not VersionSource.image_tag
    srv = _srv(game_type, tag="latest", column="1.21.4")
    assert running_version(srv) == "1.21.4"


@pytest.mark.parametrize("game_type", IN_TAG)
def test_an_empty_tag_falls_back_to_the_column(game_type):
    """A freshly created row has no tag yet — then the column is better than nothing."""
    assert running_version(_srv(game_type, tag="", column="42.20.0")) == "42.20.0"


def test_without_either_an_empty_string_comes_back_not_none():
    """The server header and the fleet table render the value directly."""
    assert running_version(_srv(GameType.pz, tag="", column="")) == ""


def test_nobody_reads_the_column_directly_anymore():
    """The guard against regression.

    Readers that served `server.game_version` directly (`server_public`, the fleet row, the mod
    portal routes) now use `running_version`. Whoever builds a new one is caught here —
    otherwise the second truth comes back at a new place, just as silently as the first time.
    """
    import ast
    import pathlib

    # Every exception with a reason — otherwise the list grows until the guard holds nothing.
    allowed = {
        # --- The writers. They MUST touch the column. ---
        "services/version_manager.py":
            "writes the column on a version change and restores it on rollback",
        "services/stats_poller.py":
            "writes the version REPORTED by the server — the only source for Arma Reforger",
        # --- The definition itself. ---
        "services/drivers/__init__.py": "`running_version` lives here",
        # --- Readers that EXPLICITLY mean the env case. ---
        # For these games the column is the right and only source: Minecraft and Vintage Story
        # pass it as `VERSION` into the container env, the image tag says nothing there.
        "services/drivers/minecraft.py": "`VERSION` env; env-based game",
        "services/drivers/vintagestory.py": "`VERSION` env; env-based game",
        "services/auto_update.py":
            "checks `version_source is env` explicitly and THEN asks the column — that is the "
            "distinction itself, not a reader that forgets it",
    }

    # **Via the AST, not a text search.** A regex over the file trips on COMMENTS that quote the
    # old line (`routers/servers.py` has one). A guard that fires on prose gets dismissed next
    # time instead of read.
    base = pathlib.Path(__file__).resolve().parent.parent / "app"
    hits: list[str] = []
    for path in sorted(base.rglob("*.py")):
        rel = str(path.relative_to(base)).replace("\\", "/")
        if rel in allowed:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        # Only accesses on a SERVER row. `body.game_version` is the request body (what the
        # caller wants), not the stored column — the three places in `routers/versions.py` show
        # that "the attribute is called game_version" is the wrong question.
        holders = {"server", "srv", "s", "row", "fresh"}
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Attribute) and node.attr == "game_version"):
                continue
            if isinstance(node.value, ast.Name) and node.value.id in holders:
                hits.append(f"{rel}:{node.lineno}")
    assert not hits, (
        "these places read the column directly instead of `drivers.running_version`:\n  "
        + "\n  ".join(hits)
        + "\n(A legitimate reader goes into the list above WITH A REASON.)"
    )


def test_every_exception_to_the_column_rule_has_a_reason():
    """Otherwise the list grows until the guard holds nothing."""
    import ast
    import pathlib

    # Read from `__file__`, not via `inspect.getsource`: that resolves the path baked into the
    # cached bytecode (`co_filename`) and fails with "could not get source code" as soon as the
    # same tree is run from a different directory or mount point.
    text = pathlib.Path(__file__).read_text(encoding="utf-8")
    name = test_nobody_reads_the_column_directly_anymore.__name__
    node = next(
        k for k in ast.parse(text).body if isinstance(k, ast.FunctionDef) and k.name == name
    )
    source = ast.get_source_segment(text, node) or ""
    assert source.count('":') >= 6, "the exception list is no longer a mapping"
