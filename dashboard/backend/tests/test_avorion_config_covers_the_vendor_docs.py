"""The Avorion editor knows every key of the vendor documentation — or says why not.

Next to its `server.ini`, Avorion writes a file `server.ini - readme.txt` that lists **every** key
with an explanation. Each key there is either an editor field or a **named exception** below.

The exceptions live here and not as a comment in the driver, for the same reason as in
`test_naming_rule.py`: a list that demands a reason stays a decision, a comment becomes a forgotten
gap. And when the next Avorion version adds a key, it shows up here instead of slipping through.

The reference file is a fixture in the repo (`fixtures/avorion_server_ini_readme.txt`, read from a
running Avorion 2.5.13 server), so the test needs no running server.
"""
from __future__ import annotations

import pathlib
import re

from app.services.drivers.avorion import AvorionDriver

READ_ME = pathlib.Path(__file__).resolve().parent / "fixtures" / "avorion_server_ini_readme.txt"

# Not in the editor — every entry with the reason why it should stay that way.
DELIBERATELY_LEFT_OUT = {
    "Game.Seed":
        "The vendor says: “Changing this after the server has been running and "
        "players logged in can lead to broken saves.” A field that destroys the galaxy "
        "does not belong in an editor with a save button.",
    "Game.Scenario":
        "The same vendor warning as for the seed — switching Creative/Normal afterwards "
        "can make the save unusable.",
    "Game.Version":
        "“Don't set this, will be overridden every time.” A field the server overwrites on "
        "every start would be a display that lies.",
    "Meta.branch":
        "“Only used in Singleplayer … Will be ignored completely.” No effect on a server.",
    "Game.SafePlayerInput":
        "Marked by the vendor as “Deprecated, don't use this”, adding that it can cause "
        "massive perceived delays. A field whose own documentation advises against using it "
        "does not belong in the editor.",
    "Game.PlayTutorial":
        "“Always disabled for multiplayer.” A switch without effect is worse than no "
        "switch — you believe you have done something.",
    "Game.DevMode":
        "“This should only be used for developing mods, as it WILL suck the fun out of the game.” "
        "Whoever really needs it will find the file.",
    "System.simulateHighLoadServer":
        "Deliberately simulates bad response times. Only for mod development.",
    "Administration.steamIdOverride":
        "Signs the server in as a specific player. The vendor: “Should only be used "
        "for finding errors” — and with two sign-ins the behaviour is undefined.",
    "Game.startUpScript":
        "Path of a Lua script the server runs at start. Whoever may set it runs "
        "arbitrary code in the server — that is not configuration, that is access.",
    "Game.startSectorScript":
        "Path of the script that generates new sectors — the same reason as for the "
        "start-up script: whoever may set it runs their own Lua code in the server.",
    "System.commandsFile":
        "A path the server reads console commands from and then deletes the file. Whoever "
        "redirects it builds a second control channel past RCON and the permission system.",
    "Networking.rconIp":
        "The address RCON listens on. A wrong value takes away exactly the channel the dashboard "
        "uses to operate this server — and does so unnoticed until the next start. "
        "`rconPort` and `rconPassword` are in as control-channel fields, the bind address is not.",
}


def _doc_keys() -> set[str]:
    key: set[str] = set()
    section = ""
    for line in READ_ME.read_text(encoding="utf-8").splitlines():
        line = line.rstrip()
        if line.startswith("["):
            section = line.strip("[]")
            continue
        hits = re.match(r"^([A-Za-z]\w*)=", line)
        if hits:
            key.add(f"{section}.{hits.group(1)}")
    return key


def _editor_keys() -> set[str]:
    return {field[0] for field in AvorionDriver._FIELDS}


def test_the_reference_file_really_is_the_documentation() -> None:
    """Otherwise everything below checks against an empty set."""
    assert len(_doc_keys()) >= 110


def test_no_key_is_missing_without_a_reason() -> None:
    missing = _doc_keys() - _editor_keys() - set(DELIBERATELY_LEFT_OUT)
    assert not missing, (
        "These keys are in Avorion's own documentation, but neither in the editor nor in "
        f"the exception list:\n  {sorted(missing)}\n"
        "Either add them — or exclude them here with a reason."
    )


def test_the_editor_invents_nothing() -> None:
    """A key Avorion does not know is removed from the `server.ini` on the next start — the
    server rewrites it on every start. The field would look as if it had taken effect."""
    invented = _editor_keys() - _doc_keys()
    assert not invented, f"unknown to Avorion: {sorted(invented)}"


def test_every_exception_has_a_reason() -> None:
    for key, reason in DELIBERATELY_LEFT_OUT.items():
        assert len(reason) > 60, f"{key}: that is not a reason, that is a note"


def test_the_exceptions_are_still_exceptions() -> None:
    """Whoever adds a key to the editor later also takes it out of the list here."""
    duplicate = set(DELIBERATELY_LEFT_OUT) & _editor_keys()
    assert not duplicate, f"in the editor AND in the exception list: {sorted(duplicate)}"


def test_the_dangerous_switches_need_moderation_permission() -> None:
    """Some fields are security switches, not matters of taste.

    `isMultiplayer=false` lets only a single administrator in — that is an access switch for the
    whole server, and the server looks perfectly healthy meanwhile.
    `ExplicitCallables=false` opens, according to the vendor, "doors for a lot of cheats and hacks".

    Checked is `elevated_config_keys()` — NOT the fifth tuple element. That one is `env_managed`
    and means "read-only here", something entirely different: these switches are meant to be
    editable, just not by everyone.
    """
    class _Row:
        game_type = None
        key = "av"
        data_dir = "/nothing"

    elevated = AvorionDriver().elevated_config_keys(_Row())
    for key in ("Networking.isMultiplayer", "Game.ExplicitCallables",
                       "Administration.accessListMode"):
        assert key in elevated, f"{key} hangs only on `can_edit_sandbox`"


def test_the_security_switches_are_not_read_only() -> None:
    """The other direction: elevated permission means "not everyone", not "no one"."""
    read_only = {field[0] for field in AvorionDriver._FIELDS if field[4]}
    for key in ("Networking.isMultiplayer", "Game.ExplicitCallables",
                       "Game.BlockOverlapExploit"):
        assert key not in read_only
