"""Guard: `config_subdirs` must be a subset of `world_subdirs`.

`backup._pack` packs only `world_subdirs` (`for sub in world_subdirs: tar.add(...)`);
`config_subdirs` serves only `server_ops._resolve_parts` on RESTORE. The docstring of
`BackupSpec` says exactly that — "which part OF IT is ONLY configuration". For PZ,
`config_subdirs=("Server",)` is a subset of `("Saves","Server")`; for most other drivers it is
empty.

A driver that names a config folder outside `world_subdirs` never gets it into any archive — for
Factorio that meant the mods, without which a Factorio world cannot be loaded at all, and the
"config only" restore could structurally only answer 400.

That is why the rule lives here and not as a comment on every driver.
"""
from __future__ import annotations

import pytest

from app.models.server import GameType
from app.services.drivers import get_driver


class _Row:
    """As much GameServer as `backup_spec` needs."""

    def __init__(self, game_type: GameType) -> None:
        self.game_type = game_type
        self.key = "x"
        self.container_name = "x"
        self.data_dir = "/nothing/here"
        self.extra = "{}"
        self.display_name = "x"
        self.game_ports = "{}"
        self.rcon_port = 0
        self.rcon_host = ""
        self.id = 1


@pytest.mark.parametrize("game_type", list(GameType))
def test_config_subdirs_are_contained_in_world_subdirs(game_type: GameType) -> None:
    drv = get_driver(_Row(game_type))
    spec = drv.backup_spec(_Row(game_type))
    missing = set(spec.config_subdirs) - set(spec.world_subdirs)
    assert not missing, (
        f"{drv.__class__.__name__}.backup_spec names {sorted(missing)} as config_subdirs, but "
        f"not as world_subdirs. ONLY world_subdirs are packed — so these folders end up in "
        f"no archive, and the 'config only' restore can only answer 400 for them."
    )


def test_the_guard_turns_red_on_the_old_layout() -> None:
    """The cross-check: Factorio's former layout with config and mods outside `world_subdirs`."""
    old_world, old_config = ("saves",), ("config", "mods")
    assert set(old_config) - set(old_world) == {"config", "mods"}
    new_world = ("saves", "config", "mods")
    assert set(old_config) - set(new_world) == set()
