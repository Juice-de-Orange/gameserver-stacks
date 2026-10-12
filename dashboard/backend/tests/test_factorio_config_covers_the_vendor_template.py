"""The Factorio editor knows every key the vendor provides — except one named exception.

The template is `server-settings.example.json` from `wube/factorio-data`, i.e. from the vendor
itself. Five keys were once missing from the editor: `non_blocking_saving` and the four segment
sizes. Not a defect — but a gap nobody had decided on, and "the dashboard can do everything the
game can" stands or falls with exactly that.

**The one deliberate exception is `password`.** That is the password of the factorio.com ACCOUNT
(the template says: "Your factorio.com login credentials"), not the server's — that one is called
`game_password` and is in the editor. Whoever reads the account password signs in to factorio.com
as the operator. The vendor names `token` explicitly as the replacement ("May be used instead of
'password'"), and a token can be revoked on its own. This exception stands here **with a reason**
so it stays a decision and does not become a forgotten gap — the same rule as in
`test_naming_rule.py`.
"""
from __future__ import annotations

import re

from app.models.server import GameServer, GameType
from app.services.drivers.factorio import FactorioDriver

# `wube/factorio-data/server-settings.example.json` as of Factorio 2.0.x.
TEMPLATE = {
    "name", "description", "tags", "max_players", "visibility.public", "visibility.lan",
    "username", "password", "token", "game_password", "require_user_verification",
    "max_upload_in_kilobytes_per_second", "max_upload_slots", "minimum_latency_in_ticks",
    "max_heartbeats_per_second", "ignore_player_limit_for_returning_players", "allow_commands",
    "autosave_interval", "autosave_slots", "afk_autokick_interval", "auto_pause",
    "auto_pause_when_players_connect", "only_admins_can_pause_the_game", "autosave_only_on_server",
    "non_blocking_saving", "minimum_segment_size", "minimum_segment_size_peer_count",
    "maximum_segment_size", "maximum_segment_size_peer_count",
}

# Not in the editor — with a reason. Whoever adds an entry here writes the reason too.
DELIBERATELY_LEFT_OUT = {
    "password": "Password of the factorio.com account, not of the server — `token` is the replacement.",
}


def _editor_keys() -> set[str]:
    return {field[0] for field in FactorioDriver._FIELDS}


def test_no_template_key_is_missing_without_a_reason() -> None:
    missing = TEMPLATE - _editor_keys() - set(DELIBERATELY_LEFT_OUT)
    assert not missing, (
        "These keys are in the vendor template but not in the editor — and not in the exception "
        f"list with a reason either:\n  {sorted(missing)}"
    )


def test_the_exception_is_still_an_exception() -> None:
    """Otherwise it would linger here long after someone added the key to the editor."""
    duplicated = set(DELIBERATELY_LEFT_OUT) & _editor_keys()
    assert not duplicated, f"in the editor AND in the exception list: {sorted(duplicated)}"
    for key, reason in DELIBERATELY_LEFT_OUT.items():
        assert len(reason) > 30, f"{key}: the reason is not a reason"


def test_the_editor_invents_nothing() -> None:
    """A key the template does not know lands, on save, in a file the server ignores — a field
    that can be operated and does nothing."""
    invented = _editor_keys() - TEMPLATE
    assert not invented, f"unknown to the vendor: {sorted(invented)}"


def test_the_join_password_is_visible_the_account_token_is_not() -> None:
    """The invitation is visible, the control channel is not."""
    kinds = {field[0]: field[2] for field in FactorioDriver._FIELDS}
    assert kinds["game_password"] == "join_password"
    assert kinds["token"] == "password"


def test_the_five_new_keys_are_really_there() -> None:
    """The five keys that were once missing, pinned down."""
    srv = GameServer(key="fac", game_type=GameType.factorio, container_name="factorio",
                     data_dir="/nothing", display_name="Factorio")
    srv.id = 1
    for key in ("non_blocking_saving", "minimum_segment_size",
                       "minimum_segment_size_peer_count", "maximum_segment_size",
                       "maximum_segment_size_peer_count"):
        assert key in _editor_keys()
    # And the vendor's warning about `non_blocking_saving` is in the help text, not only in the code.
    help_text = {f[0]: f[5] for f in FactorioDriver._FIELDS}["non_blocking_saving"]
    assert "experimental" in help_text.lower() or "risk" in help_text.lower()
    assert re.search(r"linux", help_text, re.I), "the help text must say it only works on Linux"
