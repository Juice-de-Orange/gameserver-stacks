"""No button for a command that does not exist.

Hosting wikis list twelve commands for the Avorion action catalogue. The `admin.xml` that the
server itself writes into its galaxy folder **does not know seven of them**: `/tp`,
`/maxplayers`, `/password`, `/pvp`, `/sector`, `/regeneratesector`, `/clearsector`. (`/teleport`
exists, `/tp` does not.) Had they gone into the catalogue, the actions page would have had seven
buttons that reach into nothing, and the user would have looked for the error on their side.

The header of `action_catalog.py` demands that every command is checked through the free-text
console first. This guard turns that sentence into a check.

**The evidence lives in the repo**, so the test does not depend on a running server:
* `fixtures/avorion_commands.txt`: the command names from the server's `admin.xml`.
* `fixtures/factorio_console.txt`: the commands with their response, measured over RCON.

**Boundary, stated honestly:** the VERB is checked, not the argument form. A `/playerinfo` with a
wrong signature would get through; such entries therefore carry a `note` that says exactly that.
For PZ, Minecraft and Vintage Story there is no such measurement file (yet); their catalogues are
older than this rule and are deliberately not checked here, rather than inventing an assurance.
"""
from __future__ import annotations

import pathlib
import re

import pytest

from app.models.server import GameType
from app.services.action_catalog import catalog_for

FIXTURES = pathlib.Path(__file__).resolve().parent / "fixtures"

# game -> evidence file
MEASURED = {
    GameType.avorion: "avorion_commands.txt",
    GameType.factorio: "factorio_console.txt",
}


def _measured_verbs(file: str) -> set[str]:
    text = (FIXTURES / file).read_text(encoding="utf-8")
    verbs: set[str] = set()
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        # Two formats: `/command: description` (Avorion's `/help`) or
        # `/command … -> "response"` (the Factorio measurement).
        word = line.split()[0].lstrip("/").rstrip(":")
        if word:
            verbs.add(word)
    return verbs


def _verb(template: str) -> str:
    """The first word of the template, without slash: `"/whitelist get"` → `whitelist`."""
    return re.split(r"\s+", template.strip())[0].lstrip("/")


@pytest.mark.parametrize("game_type", sorted(MEASURED, key=lambda g: g.value))
def test_every_catalog_entry_is_measured(game_type: GameType) -> None:
    measured = _measured_verbs(MEASURED[game_type])
    assert measured, "the evidence file is empty, so this test checks nothing"

    unmeasured = sorted(
        {_verb(a.template) for a in catalog_for(game_type)} - measured
    )
    assert not unmeasured, (
        f"These {game_type.value} commands are in the catalogue but in no measurement:\n  "
        + "\n  ".join(unmeasured)
        + "\nEither measure them on the running server and extend the evidence file, or drop them."
    )


@pytest.mark.parametrize("game_type", sorted(MEASURED, key=lambda g: g.value))
def test_the_catalogs_are_not_empty(game_type: GameType) -> None:
    """Otherwise the test above would be green because it runs over nothing."""
    assert len(catalog_for(game_type)) >= 5


def test_the_wiki_commands_that_do_not_exist_are_not_in_it_either() -> None:
    """The concrete case, nailed down so it does not come back in the next round."""
    invented = {"tp", "maxplayers", "password", "pvp", "sector", "regeneratesector", "clearsector"}
    in_catalog = {_verb(a.template) for a in catalog_for(GameType.avorion)}
    assert not (in_catalog & invented), (
        "A command from the hosting wikis has come back that Avorion's own admin.xml "
        "does not know."
    )
