"""Guard: the version path asks for the VERSION SOURCE, not for the game.

**The bug it stands against.** `version_manager`, `version_catalog` and `routers/versions` asked
`server.game_type == GameType.pz` in **nine** places. What was meant was never "is this Project
Zomboid" but "does the version live in the image tag" — the branches decide whether an image is
pulled before the swap, whether the rollback goes via `previous_image_tag` or
`previous_game_version`, and whether the UI may show "already loaded".

Factorio and Avorion are two more games for which the same holds. Entering three game names in
nine places is the dominant bug class of this project: the rule then stands there nine times, and
the tenth caller forgets it. A forgotten branch is not harmless here — it runs a version change
without a previously obtained image, i.e. in the middle of the swap instead of before it.

**The rule.** The files of the version path contain no comparison against `game_type`. The
question goes through `drivers.versioned_by_image_tag(server)` or `drivers.version_source(server)`.

**Exception list: one entry** (`EXCEPTIONS`), with its reason — and
`test_the_exception_still_applies` pins down that it still exists. If it were empty, either the
rule would be wrong or the exception would have silently turned into something else.
"""
import ast
import pathlib

APP = pathlib.Path(__file__).resolve().parent.parent / "app"

# The files that run the version change and fill the catalog.
VERSION_PATH = (
    "services/version_manager.py",
    "services/version_catalog.py",
    "routers/versions.py",
)

# Exactly one exception, and it asks something else: `variant_for` wants to know whether the
# version list is kept separately per server TYPE — Minecraft does not build every version for
# Paper. That is a statement about Minecraft, not about the version source, and `version_source`
# could not replace it.
EXCEPTIONS = {("services/version_catalog.py", "variant_for")}


def _enclosing_function(tree: ast.AST) -> dict[int, str]:
    """Line number → name of the function it is in (for the exception check)."""
    mapping: dict[int, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            end = getattr(node, "end_lineno", node.lineno) or node.lineno
            for line in range(node.lineno, end + 1):
                mapping.setdefault(line, node.name)
    return mapping


def _is_game_type(node: ast.AST) -> bool:
    """`server.game_type`, `row.game_type`, … — the access this is about."""
    return isinstance(node, ast.Attribute) and node.attr == "game_type"


def _names_a_game(node: ast.AST) -> bool:
    """Does this expression contain a game name as a literal — `GameType.pz`, also in `(a, b)`?

    **Both must hold** for a place to count as a violation: a `game_type` access AND a hard-coded
    game name. The reason lies in the places this does NOT report, each of which would have been
    a false alarm:

    * `VersionCatalogEntry.game_type == game_type` (`_store`, `_rows_for`) — an SQL WHERE clause
      on a table column, not a game branch.
    * `server.game_type not in FETCHERS` (`entries_for`, `_registered_targets`) — a lookup in a
      mapping. Exactly the form that picks up a new game automatically as soon as it has a
      fetcher; forbidding it would forbid the solution.
    """
    for k in ast.walk(node):
        if isinstance(k, ast.Attribute) and isinstance(k.value, ast.Name) and k.value.id == "GameType":
            return True
    return False


def _game_type_comparisons(source: str, filename: str = "<test>") -> list[str]:
    """Every place that holds `game_type` against a hard-coded game name."""
    tree = ast.parse(source, filename=filename)
    in_function = _enclosing_function(tree)
    hits: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Compare):
            continue
        operands = [node.left, *node.comparators]
        if not any(_is_game_type(k) for k in operands):
            continue
        if not any(_names_a_game(k) for k in operands):
            continue
        hits.append(f"{in_function.get(node.lineno, '<module level>')} (line {node.lineno})")
    return hits


def test_the_version_path_does_not_ask_for_the_game_type():
    violations: list[str] = []
    for rel in VERSION_PATH:
        path = APP / rel
        for place in _game_type_comparisons(path.read_text(encoding="utf-8"), str(path)):
            function = place.split(" (")[0]
            if (rel, function) in EXCEPTIONS:
                continue
            violations.append(f"{rel}:{place}")
    assert not violations, (
        "These places ask for the game where the version source is meant. A new game with "
        "image-tag versioning gets forgotten here — and then runs a version change without a "
        "previously obtained image:\n  " + "\n  ".join(violations)
        + "\n\nInstead: drivers.versioned_by_image_tag(server) / drivers.version_source(server)."
    )


def test_the_guard_would_find_the_old_code():
    """Cross-check — `current_ref`, verbatim as it used to be."""
    old = (
        "def current_ref(server: GameServer) -> str:\n"
        "    return server.image_tag if server.game_type == GameType.pz else server.game_version\n"
    )
    assert _game_type_comparisons(old) == ["current_ref (line 2)"]


def test_the_negation_is_caught_too():
    """`!=` was the form in `_pz_image_target` — it must not slip through."""
    old = (
        "def _pz_image_target(server, tag):\n"
        "    if server.game_type != GameType.pz or not server.image_repo:\n"
        "        raise HTTPException(400)\n"
    )
    assert _game_type_comparisons(old) == ["_pz_image_target (line 2)"]


def test_a_set_is_caught_too():
    """The obvious "fix" that only widens the bug: three names instead of one."""
    old = (
        "def change_version(server):\n"
        "    if server.game_type in (GameType.pz, GameType.factorio, GameType.avorion):\n"
        "        return server.image_tag\n"
    )
    assert _game_type_comparisons(old) == ["change_version (line 2)"]


def test_the_seam_itself_is_not_a_violation():
    """The target state — otherwise the guard would report the solution as a bug."""
    good = (
        "def change_version(server):\n"
        "    if versioned_by_image_tag(server):\n"
        "        return server.image_tag\n"
        "    return server.game_version\n"
    )
    assert _game_type_comparisons(good) == []


def test_the_exception_still_applies():
    """Rot protection: every exception must still be a real match.

    If `variant_for` disappears or starts deciding about the version source, that must show — an
    exception nobody needs any more would otherwise end up covering something else.
    """
    for rel, function in EXCEPTIONS:
        places = _game_type_comparisons((APP / rel).read_text(encoding="utf-8"), rel)
        assert any(s.startswith(f"{function} (") for s in places), (
            f"Exception {rel}:{function} no longer applies — remove it from EXCEPTIONS."
        )


def test_a_database_filter_is_not_a_violation():
    """`VersionCatalogEntry.game_type == game_type` filters a table — not a game branch.

    Without this distinction the guard would report `_store` and `_rows_for`, and the only remedy
    would have been an exception list longer than the rule.
    """
    sql = (
        "def _rows_for(game_type, variant):\n"
        "    return s.exec(select(VersionCatalogEntry).where(\n"
        "        VersionCatalogEntry.game_type == game_type,\n"
        "    )).all()\n"
    )
    assert _game_type_comparisons(sql) == []


def test_a_mapping_lookup_is_not_a_violation():
    """`game_type not in FETCHERS` is the solution, not the bug — a new game with a fetcher is
    picked up automatically."""
    good = (
        "def entries_for(server):\n"
        "    if server.game_type not in FETCHERS:\n"
        "        return {}\n"
    )
    assert _game_type_comparisons(good) == []
