"""What the spec promises must also be displayable.

`AvorionDriver.moderation_spec` reports `whitelist=True`, and `whitelist_add`/`whitelist_remove`
send `/whitelist add|remove` to the server. Without a `whitelist_list`, the base default
`{"available": False, ...}` applies, and the moderation page says "not available" right next to
two buttons that do work — whoever adds someone never sees the result.

The list comes from the file in the galaxy folder. That is not a guess: Avorion's own
documentation (`server.ini - readme.txt`, next to it) says about `accessListMode` literally
"Black/Whitelist files can be found in the galaxy folder". And that `/whitelist` exists at all is
stated in the `admin.xml` the server writes itself — one of the 32 commands of the group
"Moderators".
"""
from __future__ import annotations

import pytest

from app.models.server import GameServer, GameType
from app.services.drivers.avorion import AvorionDriver
from tests.file_permissions import ONLY_WITH_PERMISSION_REVOCATION


def _srv(tmp_path, mode="Blacklist") -> GameServer:
    galaxy = tmp_path / "galaxy"
    galaxy.mkdir(parents=True, exist_ok=True)
    (galaxy / "server.ini").write_text(
        "[Administration]\nname = Avorion\naccessListMode = %s\n" % mode, encoding="utf-8"
    )
    s = GameServer(key="av", game_type=GameType.avorion, container_name="avorion",
                   data_dir=str(tmp_path), display_name="Avorion")
    s.id = 1
    return s


def test_the_spec_and_the_display_do_not_contradict_each_other(tmp_path):
    srv = _srv(tmp_path)
    drv = AvorionDriver()
    assert drv.moderation_spec(srv).whitelist is True
    assert drv.whitelist_list(srv)["available"] is True


def test_the_entries_come_from_the_file(tmp_path):
    srv = _srv(tmp_path, mode="whitelist")
    (tmp_path / "galaxy" / "whitelist.txt").write_text(
        "76561198000000001\n\n76561198000000002\n", encoding="utf-8")
    (tmp_path / "galaxy" / "blacklist.txt").write_text("76561198000000002\n", encoding="utf-8")

    result = AvorionDriver().whitelist_list(srv)
    names = [u["username"] for u in result["users"]]
    assert names == ["76561198000000001", "76561198000000002"]
    assert result["users"][1]["banned"] is True
    assert result["note"] == "", "with an active whitelist there is nothing to warn about"


def test_an_ineffective_whitelist_says_so(tmp_path):
    """`accessListMode` defaults to `Blacklist` — the list then does nothing at all.

    Without this note the page looks like a closed server while in truth everyone gets in. The
    same confusion as with Valheim's empty `permittedlist`.
    """
    srv = _srv(tmp_path, mode="Blacklist")
    (tmp_path / "galaxy" / "whitelist.txt").write_text("76561198000000001\n", encoding="utf-8")
    note = AvorionDriver().whitelist_list(srv)["note"]
    assert "no effect" in note["message"].lower() and "accessListMode" in note["message"]
    # Coded, so the page shows it in the reader's language with the mode filled in.
    assert note["code"] == "moderation.avorion_whitelist_inactive"
    assert note["params"] == {"mode": "Blacklist"}


def test_a_missing_file_is_empty(tmp_path):
    """The normal case before the first entry — not an error."""
    assert AvorionDriver().whitelist_list(_srv(tmp_path))["users"] == []


@ONLY_WITH_PERMISSION_REVOCATION
def test_an_unreadable_file_is_NOT_empty(tmp_path):
    """"I cannot look" must not become "nobody is listed".

    For a whitelist that is the most dangerous confusion of all — it would reach the UI as
    "whitelist active, zero entries". The same rule as in `ValheimDriver._list`.
    """
    srv = _srv(tmp_path)
    path = tmp_path / "galaxy" / "whitelist.txt"
    path.write_text("76561198000000001\n", encoding="utf-8")
    path.chmod(0o000)
    try:
        with pytest.raises(ValueError) as error:
            AvorionDriver().whitelist_list(srv)
        assert error.value.code == "moderation.list_file_unreadable"
    finally:
        path.chmod(0o644)
