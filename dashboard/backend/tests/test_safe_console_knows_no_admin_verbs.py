"""Guard: the safe console must not carry an admin verb.

A safe list entry such as a bare `/whitelist` or `/banlist` would let `/whitelist clear` or
`/banlist add <other player>` through for everyone who has `can_console_safe` but not
`can_moderate` — the same commands are explicitly bound to the moderation permission in
`routers/moderation.py`.

The rule therefore lives here and not as a comment on seven drivers: a verb that MANAGES
something does not belong in the safe list — unless it is listed with its read subcommand
(`/whitelist get`), which the gate in `routers/console.py` can tell apart.

**Cross-check:** `test_the_guard_fails_on_the_old_lists` rebuilds the old sets and expects the
rule to reject them.
"""
from __future__ import annotations

import pytest

from app.models.server import GameType
from app.services.drivers import get_driver

# Verbs that can CHANGE something. Deliberately broad: the list costs nothing, a hole does.
ADMIN_VERBS = frozenset({
    "ban", "banlist", "unban", "kick", "whitelist", "blacklist", "permit", "permittedlist",
    "op", "deop", "adminlist", "admin", "promote", "demote", "pardon",
})
# Subcommands that demonstrably only READ. Only these make an admin verb acceptable again.
READ_SUBCOMMANDS = frozenset({"get", "list", "show", "print"})


def violations(commands) -> list[str]:
    """Which entries of this set break the rule?"""
    bad = []
    for raw in commands:
        parts = str(raw).strip().lower().lstrip("/").split()
        if not parts:
            continue
        if parts[0] not in ADMIN_VERBS:
            continue
        if len(parts) >= 2 and parts[1] in READ_SUBCOMMANDS:
            continue
        bad.append(str(raw))
    return bad


class _Row:
    """Just as much GameServer as `get_driver` needs."""

    def __init__(self, game_type: GameType) -> None:
        self.game_type = game_type


@pytest.mark.parametrize("game_type", list(GameType))
def test_no_driver_allows_admin_verbs_in_safe_mode(game_type: GameType) -> None:
    drv = get_driver(_Row(game_type))
    bad = violations(drv.safe_console_commands())
    assert not bad, (
        f"{drv.__class__.__name__}.safe_console_commands() contains admin verbs: "
        f"{bad}. They are bound to can_moderate in routers/moderation.py and would be "
        f"reachable here without that permission. If they are only meant to read, include the "
        f"read subcommand (e.g. '/whitelist get')."
    )


def test_the_guard_fails_on_the_old_lists() -> None:
    """The cross-check: the old Factorio and Avorion sets with bare admin verbs must be caught."""
    old_factorio = {"/players", "/admins", "/banlist", "/whitelist", "/time", "/seed", "/version"}
    old_avorion = {"/players", "/status", "/version", "/whitelist", "/blacklist"}
    assert sorted(violations(old_factorio)) == ["/banlist", "/whitelist"]
    assert sorted(violations(old_avorion)) == ["/blacklist", "/whitelist"]
    # And the new form passes.
    assert violations({"/banlist get", "/whitelist get", "/players"}) == []


def test_the_gate_tells_subcommands_apart() -> None:
    """`_is_safe` is half of the protection — without it '/whitelist get' would be pointless."""
    from app.routers.console import _is_safe

    allowed = frozenset({"/players", "/whitelist get"})
    assert _is_safe("/players", allowed)
    assert _is_safe("/players online", allowed), "single-word entries still allow arguments"
    assert _is_safe("/whitelist get", allowed)
    assert _is_safe("/WHITELIST  GET", allowed), "case and whitespace do not matter"
    assert not _is_safe("/whitelist clear", allowed)
    assert not _is_safe("/whitelist add steve", allowed)
    assert not _is_safe("/whitelistgetter", allowed), "no prefix match without a word boundary"
