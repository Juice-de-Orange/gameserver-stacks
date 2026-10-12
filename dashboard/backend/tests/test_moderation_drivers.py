"""Moderation driver methods — per-game command building + ModerationSpec.

No DB, no RCON: `console` is stubbed and the built command captured (no real RCON).
Async methods are tested via `asyncio.run` (no pytest-asyncio needed).
"""
import asyncio
import types

import pytest

from app.services.drivers.minecraft import MinecraftDriver
from app.services.drivers.pz import PZDriver
from app.services.drivers.vintagestory import VintageStoryDriver

SRV = types.SimpleNamespace(key="s", data_dir="/tmp", display_name="X", extra="{}")


def _capture(cls):
    d = cls()
    calls: list[str] = []

    async def fake(server, command, *, timeout=8.0):
        calls.append(command)
        return "ok"

    d.console = fake  # instance override — the moderation methods call self.console
    return d, calls


def test_pz_moderation_commands_byte_identical():
    d, calls = _capture(PZDriver)
    asyncio.run(d.kick(SRV, "Alice", "afk"))
    asyncio.run(d.ban(SRV, "Bob", "spam", ip=True))
    asyncio.run(d.unban(SRV, "Bob"))
    asyncio.run(d.whitelist_add(SRV, "Cara"))
    assert calls == [
        'kickuser "Alice" -r "afk"',
        'banuser "Bob" -ip -r "spam"',
        'unbanuser "Bob"',
        'addusertowhitelist "Cara"',
    ]


def test_pz_access_rejects_unknown_level():
    d, _ = _capture(PZDriver)
    with pytest.raises(ValueError):
        asyncio.run(d.set_access(SRV, "Alice", "wizard"))


def test_mc_moderation_commands_and_name_hardening():
    d, calls = _capture(MinecraftDriver)
    asyncio.run(d.kick(SRV, "Alice", ""))
    asyncio.run(d.ban(SRV, "Bob", "", ip=True))
    asyncio.run(d.set_access(SRV, "Alice", "admin"))
    asyncio.run(d.set_access(SRV, "Alice", "none"))
    asyncio.run(d.whitelist_add(SRV, "Cara"))
    # Injection/spaces in the name are reduced to [A-Za-z0-9_] → no additional command:
    asyncio.run(d.kick(SRV, "Eve; op Eve", ""))
    assert calls == ["kick Alice", "ban-ip Bob", "op Alice", "deop Alice", "whitelist add Cara", "kick EveopEve"]


def test_vs_moderation_limited():
    d, calls = _capture(VintageStoryDriver)
    asyncio.run(d.kick(SRV, "Alice", ""))
    asyncio.run(d.ban(SRV, "Bob", ""))
    assert calls == ["kick Alice", "ban Bob"]


def test_moderation_specs_per_game():
    pz = PZDriver().moderation_spec(SRV)
    mc = MinecraftDriver().moderation_spec(SRV)
    vs = VintageStoryDriver().moderation_spec(SRV)
    assert pz.whitelist and pz.access_levels and pz.unban
    assert mc.whitelist and mc.access_levels == ("admin", "none")
    assert vs.kick and vs.ban and not vs.unban and not vs.whitelist and vs.access_levels == ()


def test_safe_console_commands_per_game():
    # The safe whitelist is per game, not fixed to PZ (otherwise MC `list` would be blocked).
    assert PZDriver().safe_console_commands() == frozenset({"players", "save", "servermsg"})
    assert MinecraftDriver().safe_console_commands() == frozenset({"list", "save-all", "say"})
    assert VintageStoryDriver().safe_console_commands() == frozenset({"list", "stats"})


# ------------------------------------------------------------------ reasons instead of hiding

def test_every_missing_sub_action_has_a_reason():
    """Whoever switches off a sub-action must say why.

    Hiding a capability a game will never have is honest — but here SUBSETS of an existing
    capability are missing, and hiding them reads wrong: "unban" missing for Vintage Story looks
    like "does not exist", while the truth is "unproven whether the RCON mod can do it".
    """
    from app.services.drivers.minecraft import MinecraftDriver
    from app.services.drivers.pz import PZDriver
    from app.services.drivers.reforger import ReforgerDriver
    from app.services.drivers.vintagestory import VintageStoryDriver

    fields = ("kick", "ban", "ban_ip", "unban", "whitelist")
    for drv in (PZDriver(), MinecraftDriver(), VintageStoryDriver(), ReforgerDriver()):
        spec = drv.moderation_spec(SRV)
        for field in fields:
            if not getattr(spec, field):
                assert spec.reasons.get(field), (
                    f"{type(drv).__name__}: '{field}' is off but has no reason — "
                    "the UI would grey it out without explanation"
                )
        if not spec.access_levels:
            assert spec.reasons.get("access_levels"), f"{type(drv).__name__}: access_levels without a reason"


def test_the_reasons_are_sentences_not_keywords():
    """A tooltip saying "not supported" would be worth as much as a hidden field."""
    from app.services.drivers.reforger import ReforgerDriver
    from app.services.drivers.vintagestory import VintageStoryDriver

    for drv in (VintageStoryDriver(), ReforgerDriver()):
        for field, text in drv.moderation_spec(SRV).reasons.items():
            assert len(text) > 40, f"{type(drv).__name__}.{field}: too short for an explanation"


def test_available_actions_may_also_name_a_condition():
    """Reforger kick/ban need an ONLINE player — otherwise the UI shows a button that ends in
    an error without explanation."""
    from app.services.drivers.reforger import ReforgerDriver

    spec = ReforgerDriver().moderation_spec(SRV)
    assert spec.kick and spec.ban          # the actions DO exist …
    for field in ("kick", "ban"):           # … but not for absent players
        assert "player list" in spec.reasons[field]


def test_the_spec_serialises_through_asdict():
    """`routers/moderation.py` returns `asdict(spec)` directly as JSON."""
    import json
    from dataclasses import asdict

    from app.services.drivers.vintagestory import VintageStoryDriver

    raw = json.dumps(asdict(VintageStoryDriver().moderation_spec(SRV)))
    assert "reasons" in json.loads(raw)


def test_two_specs_do_not_share_a_reasons_dict():
    """`frozen=True` forbids only assignment, not sharing a mutable default."""
    from app.services.drivers.base import ModerationSpec

    a, b = ModerationSpec(), ModerationSpec()
    a.reasons["x"] = "y"
    assert b.reasons == {}
