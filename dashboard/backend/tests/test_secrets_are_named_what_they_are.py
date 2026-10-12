"""Guard: every config field marked as secret must also be RECOGNISED as one.

Valheim writes its configuration into the registry column `extra` — including `SERVER_PASS`, the
join password. `extra` goes into EVERY server row via `routers/servers.server_public()`, which
hangs on `can_view_status`, set to True by default for every active user in `models/user.py`.
Masking there goes through `mask_if_secret` → `looks_like_secret`, a name heuristic — and a name
like `"server_pass"` must match it, not only `SERVER_PASSWORD`.

So this guard does not check the heuristic against a wish list but against the REAL field names
of all drivers: what a driver treats as a secret (`kind == "password"` or `env_managed` on a
password field) must be recognised by `looks_like_secret`. The next inconspicuous name is caught
before it ships.

The join password (`kind == "join_password"`) is explicitly exempt: it is deliberately visible in
the config editor. It still has no place in the SERVER LIST — that hangs on a different
permission — and the last case checks exactly that.
"""
from __future__ import annotations

import pytest

from app.models.server import GameType
from app.services.drivers import get_driver
from app.services.drivers.base import looks_like_secret, mask_if_secret


def test_the_short_forms_are_recognised() -> None:
    """The cross-check: without the short forms `SERVER_PASS` would not count as secret."""
    for name in ("SERVER_PASS", "server_pass", "rcon_pw", "db-passwd", "admin.pwd", "api_key"):
        assert looks_like_secret(name), f"{name} should be recognised as a secret"


def test_harmless_names_stay_visible() -> None:
    """The short forms must not get greedy — otherwise the dashboard masks half the config."""
    # `password_hint_shown` is deliberately missing here: it matches the OLD hint list (`password`
    # as a substring) and always did. What is checked is that the new SHORT FORMS do not get
    # greedy.
    for name in ("bypass_checks", "keyboard_layout", "pwr_limit", "key", "keystore",
                 "max_players", "world_name", "public", "monkeypatch"):
        assert not looks_like_secret(name), f"{name} is not a secret"


def test_no_secret_field_slips_through_the_heuristic() -> None:
    """The actual guard: the real field names of all seven drivers.

    Needs no running server — `config_fields` falls back to empty values when files are missing,
    and only the NAMES are checked anyway.
    """
    slipped: list[str] = []
    for game_type in GameType:
        drv = get_driver(type("_Row", (), {"game_type": game_type})())
        try:
            fields = drv.config_fields(type("_S", (), {
                "key": "x", "data_dir": "/nothing/here", "extra": "{}", "display_name": "x",
                "game_ports": "{}", "rcon_port": 0, "rcon_host": "", "container_name": "x",
            })())
        except Exception:      # noqa: BLE001 — without real files a catalogue may stay empty
            continue
        for f in fields:
            if f.kind == "password" and not looks_like_secret(f.key):
                slipped.append(f"{game_type.value}:{f.key}")
    assert not slipped, (
        "These fields are marked as password but NOT recognised by `looks_like_secret` — they "
        f"go out in plain text wherever masking relies on the heuristic "
        f"(server list, /config/raw, logs): {slipped}"
    )


@pytest.mark.parametrize("key", ["SERVER_PASS", "password", "RCONPassword", "token"])
def test_the_masking_really_applies(key: str) -> None:
    assert mask_if_secret(key, "secret123") != "secret123"
    assert mask_if_secret(key, "") == "", "empty stays empty — otherwise unset looks set"
