"""Valheim driver: the server without a console.

What is checked here is above all that the driver fakes nothing — no console, no broadcast, no
kick, no invented player count. The Steam query is stubbed (`services/a2s.py` has its own tests
against real UDP), the configuration runs against an in-memory registry.
"""
import asyncio
import json
from pathlib import Path

import pytest
from sqlmodel import Session, select

from app.models.server import ConsoleKind, GameServer, GameType
from app.services.drivers import get_driver, version_source
from app.services.drivers.base import ConfigReload, QuiesceWarning, VersionSource
from app.services.drivers.valheim import ValheimDriver, parse_zdoid


def _srv(tmp_path=None, **over):
    base = dict(
        key="vh", game_type=GameType.valheim, display_name="Valhalla",
        data_dir=str(tmp_path) if tmp_path else "/srv/gameservers/valheim/vh",
        container_name="valheim", rcon_host="valheim", console_kind=ConsoleKind.none,
        game_ports='{"game": "2456/udp"}', image_repo="ghcr.io/community-valheim-tools/valheim-server",
        image_tag="latest", extra="{}",
    )
    base.update(over)
    return GameServer(**base)


@pytest.fixture()
def registry(mem_engine, monkeypatch):
    """A registry with one Valheim row — `set_config` really writes there."""
    monkeypatch.setattr("app.db.engine", mem_engine)
    srv = _srv()
    with Session(mem_engine) as s:
        s.add(srv)
        s.commit()
        s.refresh(srv)
    return srv, mem_engine


def _row(engine, key="vh") -> GameServer:
    with Session(engine) as s:
        return s.exec(select(GameServer).where(GameServer.key == key)).first()


def _a2s_stub(monkeypatch, *, info=None, players=None, info_error=None, players_error=None):
    async def fake_info(host, port, timeout=5.0):
        if info_error:
            raise info_error
        return info or {"players": 0, "max_players": 10, "version": "0.220.5", "name": "Valhalla"}

    async def fake_players(host, port, timeout=5.0):
        if players_error:
            raise players_error
        return players or []

    monkeypatch.setattr("app.services.a2s.info", fake_info)
    monkeypatch.setattr("app.services.a2s.players", fake_players)


# ------------------------------------------------------------------ What does not exist

def test_capabilities_without_console_and_without_broadcast():
    caps = ValheimDriver().capabilities()
    assert {"players", "events", "config", "mods", "backup", "versions"} <= caps
    assert "console" not in caps and "announce" not in caps


def test_the_console_fakes_nothing():
    """An empty string back would mean "command executed, no output" — and the dashboard console
    would look functional while every command goes nowhere."""
    with pytest.raises(NotImplementedError) as error:
        asyncio.run(ValheimDriver().console(_srv(), "help"))
    assert error.value.code == "console.valheim_none"


def test_broadcast_and_save_are_honest_no_ops():
    """Neither may inherit the PZ default: it would run into the NotImplementedError of `console`
    here and produce an error in the log on every scheduled restart."""
    d = ValheimDriver()
    assert asyncio.run(d.announce(_srv(), "Hello")) is None
    assert asyncio.run(d.save(_srv())) is None


def test_kicking_does_not_exist_and_the_reason_says_so():
    spec = ValheimDriver().moderation_spec(_srv())
    assert spec.kick is False and spec.ban is True
    assert "console" in spec.reasons["kick"]
    assert "platform ID" in spec.reasons["ban"]


def test_the_version_is_not_selectable():
    assert version_source(_srv()) is VersionSource.runtime
    assert isinstance(get_driver(_srv()), ValheimDriver)


def test_config_needs_a_recreate_not_a_restart():
    """The env can only be set when the container is CREATED. A restart would not pick up the
    change — and would look as if it had."""
    assert ValheimDriver().config_reload is ConfigReload.recreate


# ------------------------------------------------------------------ Player list (A2S)

def test_the_count_comes_from_the_steam_query(monkeypatch):
    _a2s_stub(monkeypatch, info={"players": 3, "version": "0.220.5"},
              players=[{"name": "Alice"}, {"name": "Bob"}, {"name": ""}])
    result = asyncio.run(ValheimDriver().players(_srv()))
    assert result == {"count": 3, "names": ["Alice", "Bob"]}


def test_a_missing_name_list_does_not_topple_the_count(monkeypatch):
    """Valheim does not always fill the name field. The count is what `idle_autostop` and
    `auto_update` depend on — it must not fail because of the names."""
    _a2s_stub(monkeypatch, info={"players": 2}, players_error=RuntimeError("broken"))
    assert asyncio.run(ValheimDriver().players(_srv())) == {"count": 2, "names": []}


def test_a_dead_query_fails_loudly(monkeypatch):
    """The most important case: unknown is not zero.

    `idle_autostop` reads an exception as "touch nothing", a 0 however as "nobody there". Valheim
    has no second source that would correct the mistake.
    """
    from app.services.a2s import A2sError

    _a2s_stub(monkeypatch, info_error=A2sError("no answer"))
    with pytest.raises(A2sError):
        asyncio.run(ValheimDriver().players(_srv()))


def test_the_query_port_sits_next_to_the_game_port():
    assert ValheimDriver()._query_port(_srv()) == 2457


def test_a_custom_query_port_wins():
    srv = _srv(game_ports='{"game": "2456/udp", "a2s": "2460/udp"}')
    assert ValheimDriver()._query_port(srv) == 2460


def test_the_version_comes_from_the_log_and_not_from_the_query():
    """The obvious source is the wrong one.

    For Valheim A2S_INFO returns the constant `1.0.0.0` as `version`, which has nothing to do
    with the game version. Showing it shows the same wrong number forever, and nobody notices
    because it looks plausible.
    """
    from app.services.drivers.valheim import _VERSION_RE

    line = "08/23/2026 20:20:31: Valheim version: l-0.221.12 (network version 36)"
    assert _VERSION_RE.search(line).group(1) == "0.221.12"


def test_the_platform_prefix_is_not_part_of_the_version():
    from app.services.drivers.valheim import _VERSION_RE

    assert _VERSION_RE.search("Valheim version: 0.220.5").group(1) == "0.220.5"
    assert _VERSION_RE.search("something else") is None


# ------------------------------------------------------------------ Events

def test_join_and_death_are_the_same_log_line():
    """`Got character ZDOID` appears for BOTH. The difference is the ZDOID `0:0`.

    Without separating them every death records a second join — the session times would be
    nonsense afterwards, and nobody could tell from the number where it came from.
    """
    assert parse_zdoid("Got character ZDOID from Alice : 12345:1") == ("Alice", False)
    assert parse_zdoid("Got character ZDOID from Alice : 0:0") == ("Alice", True)
    assert parse_zdoid("something else") is None


def test_events_are_sorted_correctly():
    text = (
        "2026-08-23T10:00:00.000000000Z Got character ZDOID from Alice : 12345:1\n"
        "2026-08-23T10:30:00.000000000Z Got character ZDOID from Alice : 0:0\n"
    )
    events = ValheimDriver().parse_events(_srv(), text)
    assert [(e.kind, e.username) for e in events] == [("join", "Alice"), ("death", "Alice")]


def test_a_leave_without_a_name_is_not_invented():
    """On disconnect Valheim only logs the connection, not the character. An event without a
    name would be worthless for session accounting and misleading in the feed."""
    text = "2026-08-23T10:00:00.000000000Z Closing socket 1234567890\n"
    assert ValheimDriver().parse_events(_srv(), text) == []


# ------------------------------------------------------------------ Container

def test_the_images_own_backup_is_off(tmp_path):
    """Otherwise the image backs up HOURLY alongside the dashboard, with its own retention —
    bypassing BACKUP_MAX_GB_PER_SERVER, into the same volume."""
    env = ValheimDriver().container_env(_srv(tmp_path), "")
    assert env["BACKUPS"] == "false"


def test_the_images_own_restart_schedule_is_off(tmp_path):
    """A restart at 05:10 UTC that nobody scheduled and the dashboard's restart schedule does not
    know about."""
    assert ValheimDriver().container_env(_srv(tmp_path), "")["RESTART_CRON"] == ""


def test_the_configuration_ends_up_in_the_env(tmp_path):
    srv = _srv(tmp_path, extra=json.dumps({"env": {"SERVER_NAME": "Valhalla", "SERVER_PASS": "secret"}}))
    env = ValheimDriver().container_env(srv, "")
    assert env["SERVER_NAME"] == "Valhalla" and env["SERVER_PASS"] == "secret"
    assert env["SERVER_PORT"] == "2456"


def test_without_settings_there_are_usable_defaults(tmp_path):
    env = ValheimDriver().container_env(_srv(tmp_path), "")
    assert env["SERVER_NAME"] == "Valhalla" and env["WORLD_NAME"] == "Dedicated"


def test_the_game_is_not_downloaded_again_on_every_start(tmp_path):
    """Without the second bind every container start downloads the complete game files again."""
    binds = ValheimDriver().container_binds(_srv(tmp_path))
    assert f"{tmp_path}/config:/config" in binds
    assert f"{tmp_path}/server:/opt/valheim" in binds


def test_stop_timeout_follows_the_images_recommendation():
    assert ValheimDriver().stop_timeout(_srv()) == 120


# ------------------------------------------------------------------ Config (writes the registry)

def test_config_roundtrip_through_the_registry(registry):
    srv, engine = registry
    d = ValheimDriver()
    d.set_config(srv, {"SERVER_NAME": "Valhalla North", "SERVER_PUBLIC": "false", "SERVER_PASS": "secret"})
    env = json.loads(_row(engine).extra)["env"]
    assert env["SERVER_NAME"] == "Valhalla North" and env["SERVER_PUBLIC"] == "false"
    # The join password goes into encrypted storage (`services/secrets.py`), not into the row.
    assert "SERVER_PASS" not in env
    assert d.container_env(_row(engine), "")["SERVER_PASS"] == "secret"
    # and the row passed in follows, so the same call does not deliver the old state
    fields = {f.key: f.value for f in d.config_fields(srv)}
    assert fields["SERVER_NAME"] == "Valhalla North"


def test_a_too_short_password_is_rejected_up_front(registry):
    """The image does NOT start at all with fewer than five characters. A server that no longer
    comes up after saving is the more expensive surprise."""
    srv, engine = registry
    with pytest.raises(ValueError) as error:
        ValheimDriver().set_config(srv, {"SERVER_PASS": "abc"})
    assert error.value.code == "config.valheim_password_too_short"
    assert "env" not in json.loads(_row(engine).extra)


def test_a_cleared_join_password_opens_the_server(registry):
    srv, engine = registry
    d = ValheimDriver()
    d.set_config(srv, {"SERVER_PASS": "secret123"})
    d.set_config(srv, {"SERVER_PASS": ""})
    assert json.loads(_row(engine).extra)["env"]["SERVER_PASS"] == ""


def test_the_join_password_is_visible(registry):
    """Every fellow player knows it anyway, and the operator needs the information.

    Valheim has no RCON password that would become visible along with it — the server has no
    console.
    """
    srv, _ = registry
    d = ValheimDriver()
    d.set_config(srv, {"SERVER_PASS": "secret123"})
    field = {f.key: f for f in d.config_fields(srv)}["SERVER_PASS"]
    assert field.value == "secret123"


def test_an_unknown_key_does_not_end_up_in_the_env(registry):
    srv, engine = registry
    ValheimDriver().set_config(srv, {"SERVER_NAME": "x", "RCON_PASSWORD": "takeover"})
    assert "RCON_PASSWORD" not in json.loads(_row(engine).extra)["env"]


def test_control_characters_do_not_get_into_an_env_variable(registry):
    srv, _ = registry
    with pytest.raises(ValueError) as error:
        ValheimDriver().set_config(srv, {"SERVER_NAME": "x\nSERVER_PASS=empty"})
    assert error.value.code == "config.valheim_control_chars"


def test_a_vanished_registry_row_reports_itself(registry, mem_engine):
    srv, engine = registry
    with Session(engine) as s:
        s.delete(_row(engine))
        s.commit()
    with pytest.raises(ValueError) as error:
        ValheimDriver().set_config(srv, {"SERVER_NAME": "x"})
    assert error.value.code == "config.registry_row_gone"


# ------------------------------------------------------------------ Backup

def test_backup_takes_the_whole_world_folder(tmp_path):
    """`.db` and `.fwl` belong inseparably together, and the `.old` saves next to them are exactly
    what one wants when the main file is broken. A folder is also robust against a changed
    WORLD_NAME — otherwise the weekly run would back up nothing."""
    spec = ValheimDriver().backup_spec(_srv(tmp_path))
    assert spec.data_dir == f"{tmp_path}/config"
    assert "worlds_local" in spec.world_subdirs


def test_backup_includes_the_moderation_lists_and_the_mods(tmp_path):
    """The archive must not contain ONLY `worlds_local`.

    The three list files sit next to it in the same folder, and for Valheim all moderation runs
    through them — there is no console. A restore without them brings the world back and then
    lets everyone in: a missing `permittedlist.txt` is not an empty whitelist but none at all.
    """
    spec = ValheimDriver().backup_spec(_srv(tmp_path))
    for file in ("adminlist.txt", "bannedlist.txt", "permittedlist.txt"):
        assert file in spec.world_subdirs, f"{file} ends up in no archive"
        assert file in spec.config_subdirs, f"{file} cannot be restored on its own"
    assert "bepinex" in spec.world_subdirs and "bepinex" in spec.config_subdirs


def test_backup_does_not_pack_the_backup_folder_into_itself(tmp_path):
    """`config/backups` is the folder in which the archives THEMSELVES live (and the image's).

    Packing it would nest every backup into the next — the archive size would double with every
    run until the disk is full.
    """
    spec = ValheimDriver().backup_spec(_srv(tmp_path))
    assert "backups" not in spec.world_subdirs


def test_valheim_can_restore_only_the_configuration():
    """Without the config part in the archive the button would structurally only answer 400."""
    assert "backup-config-only" in ValheimDriver().capabilities()


def test_the_backup_says_what_it_relies_on(tmp_path):
    """There is no save command. The warning does not topple the archive — it only states that it
    is a copy taken while running."""
    with pytest.raises(QuiesceWarning, match=r"\.old"):
        ValheimDriver().backup_spec(_srv(tmp_path)).pre_hook()


# ------------------------------------------------------------------ Mods

def test_plugins_without_a_loader_are_not_reported_as_active(tmp_path):
    """A list full of plugins on a server with BEPINEX=false is the most misleading display of
    all."""
    plugins = Path(tmp_path) / "config" / "bepinex" / "plugins"
    plugins.mkdir(parents=True)
    (plugins / "Thing.dll").write_bytes(b"")
    mods = ValheimDriver().list_mods(_srv(tmp_path))
    assert mods["plugins"] == ["Thing.dll"] and mods["loader_enabled"] is False
    assert ValheimDriver().overview_stats(_srv(tmp_path))["mods_count"] is None


def test_with_a_loader_the_plugins_count(tmp_path):
    plugins = Path(tmp_path) / "config" / "bepinex" / "plugins"
    plugins.mkdir(parents=True)
    (plugins / "Thing.dll").write_bytes(b"")
    srv = _srv(tmp_path, extra=json.dumps({"env": {"BEPINEX": "true"}}))
    assert ValheimDriver().list_mods(srv)["loader_enabled"] is True
    assert ValheimDriver().overview_stats(srv)["mods_count"] == 1


# ------------------------------------------------------------------ Moderation through files

def test_a_ban_ends_up_in_the_file(tmp_path):
    srv = _srv(tmp_path)
    (Path(tmp_path) / "config").mkdir(parents=True)
    message = asyncio.run(ValheimDriver().ban(srv, "76561198000000000"))
    content = (Path(tmp_path) / "config" / "bannedlist.txt").read_text(encoding="utf-8")
    assert "76561198000000000" in content
    # The response states the conservative case — when Valheim rereads the lists is not
    # established.
    assert "restart" in message


def test_a_second_ban_does_not_duplicate_the_entry(tmp_path):
    srv = _srv(tmp_path)
    (Path(tmp_path) / "config").mkdir(parents=True)
    asyncio.run(ValheimDriver().ban(srv, "76561198000000000"))
    asyncio.run(ValheimDriver().ban(srv, "76561198000000000"))
    lines = (Path(tmp_path) / "config" / "bannedlist.txt").read_text().splitlines()
    assert lines.count("Steam_76561198000000000") == 1


def test_unbanning_removes_only_that_one(tmp_path):
    srv = _srv(tmp_path)
    (Path(tmp_path) / "config").mkdir(parents=True)
    asyncio.run(ValheimDriver().ban(srv, "76561198000000111"))
    asyncio.run(ValheimDriver().ban(srv, "76561198000000222"))
    asyncio.run(ValheimDriver().unban(srv, "76561198000000111"))
    assert (Path(tmp_path) / "config" / "bannedlist.txt").read_text().split() == ["Steam_76561198000000222"]


def test_an_ip_ban_does_not_silently_become_a_normal_one(tmp_path):
    with pytest.raises(NotImplementedError):
        asyncio.run(ValheimDriver().ban(_srv(tmp_path), "76561198000000111", ip=True))


def test_a_player_name_instead_of_an_id_is_rejected(tmp_path):
    """A name in these files has no effect — but it would look like a ban."""
    with pytest.raises(ValueError) as error:
        asyncio.run(ValheimDriver().ban(_srv(tmp_path), "!!!"))
    assert error.value.code == "moderation.valheim_not_a_platform_id"


def test_the_template_comment_lines_are_not_read_as_ids(tmp_path):
    """Valheim ships the files with `// List admins...`. Without filtering, the comment would
    show up in the UI as a banned player."""
    (Path(tmp_path) / "config").mkdir(parents=True)
    (Path(tmp_path) / "config" / "permittedlist.txt").write_text(
        "// List permitted players\n76561198000000000\n", encoding="utf-8"
    )
    listing = ValheimDriver().whitelist_list(_srv(tmp_path))
    assert [u["username"] for u in listing["users"]] == ["76561198000000000"]


def test_the_list_shows_admin_and_ban_together(tmp_path):
    srv = _srv(tmp_path)
    (Path(tmp_path) / "config").mkdir(parents=True)
    asyncio.run(ValheimDriver().whitelist_add(srv, "76561198000000111"))
    asyncio.run(ValheimDriver().set_access(srv, "76561198000000111", "admin"))
    user = ValheimDriver().whitelist_list(srv)["users"][0]
    assert user["accesslevel"] == "admin" and user["banned"] is False


def test_connect_info_reports_a_set_password(tmp_path):
    srv = _srv(tmp_path, extra=json.dumps({"env": {"SERVER_PASS": "secret", "SERVER_NAME": "Valhalla"}}))
    info = ValheimDriver().connect_info(srv)
    assert info.password_required is True and info.uri == "" and info.port == 2456
    assert "secret" not in (info.hint + info.browser_name)


def test_public_listing_is_the_precondition_for_everything_else(tmp_path):
    """The most expensive finding of commissioning, which is why it is in the field's help text.

    With `SERVER_PUBLIC=false` Valheim does listen on the query port but **does not answer**.
    The dashboard would then have neither player count nor boot proof — and none of it would look
    like an error, because a missing answer counts as "unknown".
    """
    field = {f.key: f for f in ValheimDriver().config_fields(_srv(tmp_path))}["SERVER_PUBLIC"]
    assert "Steam queries" in field.help and "player count" in field.help


def test_the_server_runs_under_the_same_uid_as_the_api(tmp_path, monkeypatch):
    """By default the image runs as root, and afterwards every file under /config belongs to
    root. The backup then fails with "Permission denied" on config/backups — and the mod folder
    would be next.

    The ids are the API process's own (`APP_UID` at image build), not a fixed 1000."""
    monkeypatch.setattr("os.getuid", lambda: 1234)
    monkeypatch.setattr("os.getgid", lambda: 1235)
    env = ValheimDriver().container_env(_srv(tmp_path), "")
    assert env["PUID"] == "1234" and env["PGID"] == "1235"


def test_the_listing_is_on_at_creation(tmp_path):
    """Default "on", because "off" switches half of the information off without looking like an
    error."""
    env = ValheimDriver().container_env(_srv(tmp_path), "")
    assert env["SERVER_PUBLIC"] == "true"


def test_an_explicit_decision_wins_over_the_default(tmp_path):
    """Whoever wants to hide the server can — the default only applies where nothing is set."""
    srv = _srv(tmp_path, extra=json.dumps({"env": {"SERVER_PUBLIC": "false"}}))
    assert ValheimDriver().container_env(srv, "")["SERVER_PUBLIC"] == "false"


def test_a_cleared_password_really_reaches_the_container(registry):
    """Otherwise the display lies, which is worse than an inconvenience.

    The image has `SERVER_PASS=${SERVER_PASS-secret}` (with a dash, not `:-`). The default only
    applies when the variable is NOT SET at all. A filter `if str(v) != ""` in `container_env`
    would remove it exactly when the user switched the password off — the server would get
    "secret", and the field would look correctly empty.
    """
    srv, _ = registry
    d = ValheimDriver()
    d.set_config(srv, {"SERVER_PASS": "secret123"})
    assert d.container_env(srv, "")["SERVER_PASS"] == "secret123"

    d.set_config(srv, {"SERVER_PASS": ""})
    env = d.container_env(srv, "")
    assert "SERVER_PASS" in env, "the variable is missing — the image then takes its default 'secret'"
    assert env["SERVER_PASS"] == ""


def test_a_player_name_is_rejected_as_a_ban(tmp_path):
    """Valheim matches these files exclusively against platform IDs.

    Filtering only non-word characters and rejecting only when nothing is left is not enough: a
    player name consists of word characters only. "Bjorn" would land unchanged in
    `bannedlist.txt`, be acknowledged as entered and leave an audit entry — a dead string, and
    the griefer gets back in on the next join.
    """
    srv = _srv(tmp_path)
    (Path(tmp_path) / "config").mkdir(parents=True)
    with pytest.raises(ValueError) as error:
        asyncio.run(ValheimDriver().ban(srv, "Bjorn"))
    assert error.value.code == "moderation.valheim_not_a_platform_id"
    assert not (Path(tmp_path) / "config" / "bannedlist.txt").exists()

    # Both allowed forms pass and are written uniformly.
    asyncio.run(ValheimDriver().ban(srv, "Steam_76561198000000042"))
    asyncio.run(ValheimDriver().ban(srv, "76561198000000043"))
    assert (Path(tmp_path) / "config" / "bannedlist.txt").read_text().split() == [
        "Steam_76561198000000042", "Steam_76561198000000043",
    ]


def test_the_last_whitelist_entry_cannot_be_removed(tmp_path):
    """An empty `permittedlist.txt` lets EVERYONE in — `moderation_spec.reasons` says so.

    Without the check `_write_list` would quietly create a 0-byte file, the response would
    announce a tightening, and `whitelist_list` would then show "whitelist active, zero entries" —
    the most closed state on a maximally open server.
    """
    srv = _srv(tmp_path)
    (Path(tmp_path) / "config").mkdir(parents=True)
    drv = ValheimDriver()
    asyncio.run(drv.whitelist_add(srv, "76561198000000001"))
    asyncio.run(drv.whitelist_add(srv, "76561198000000002"))

    # The second to last may go.
    asyncio.run(drv.whitelist_remove(srv, "76561198000000001"))
    # The last may not.
    with pytest.raises(ValueError) as error:
        asyncio.run(drv.whitelist_remove(srv, "76561198000000002"))
    assert error.value.code == "moderation.valheim_last_whitelist_entry"
    assert drv.whitelist_list(srv)["users"], "the list must not have been emptied"


def test_an_empty_whitelist_is_reported_as_open(tmp_path):
    srv = _srv(tmp_path)
    (Path(tmp_path) / "config").mkdir(parents=True)
    response = ValheimDriver().whitelist_list(srv)
    assert response["users"] == []
    assert "to EVERYONE" in response["note"]


def test_an_unreadable_list_does_not_count_as_empty(tmp_path, monkeypatch):
    """"I cannot look" is not an answer.

    When reading it would turn into "nobody is listed" — for the `permittedlist` the most
    dangerous false statement of all. When WRITING it would be more expensive: `_edit_list` would
    overwrite the existing entries with an empty list.
    """
    srv = _srv(tmp_path)
    (Path(tmp_path) / "config").mkdir(parents=True)
    (Path(tmp_path) / "config" / "bannedlist.txt").write_text("Steam_76561198000000009\n")

    real = Path.read_text

    def _denies(self, *a, **kw):
        if self.name == "bannedlist.txt":
            raise PermissionError(13, "Permission denied")
        return real(self, *a, **kw)

    monkeypatch.setattr(Path, "read_text", _denies)
    with pytest.raises(ValueError) as error:
        ValheimDriver()._list(srv, "bannedlist.txt")
    assert error.value.code == "moderation.list_file_unreadable"


def test_a_respawn_is_not_a_second_join(tmp_path):
    """Valheim writes "Got character ZDOID" not only on connect but on every character
    assignment — so also on re-entry after death.

    Death is separated via `0:0`; without tracking the respawn, the line after it would still
    record a second join.
    """
    log = "\n".join([
        "Got character ZDOID from Bjorn : 12345:1",     # real join
        "Got character ZDOID from Bjorn : 0:0",         # death
        "Got character ZDOID from Bjorn : 12345:2",     # respawn — NOT a join
    ])
    kinds = [(e.kind, e.username) for e in ValheimDriver().parse_events(_srv(tmp_path), log)]
    assert kinds == [("join", "Bjorn"), ("death", "Bjorn")]


def test_after_a_disconnect_the_next_one_counts_as_a_join_again(tmp_path):
    """Whoever disconnects really starts over — otherwise the rule swallows real joins."""
    log = "\n".join([
        "Got character ZDOID from Bjorn : 12345:1",
        "Got character ZDOID from Bjorn : 0:0",
        "RPC_Disconnect",
        "Got character ZDOID from Bjorn : 12345:9",     # new session
    ])
    kinds = [(e.kind, e.username) for e in ValheimDriver().parse_events(_srv(tmp_path), log)]
    assert kinds == [("join", "Bjorn"), ("death", "Bjorn"), ("join", "Bjorn")]


# ------------------------------------------------------------------ Creation

def test_ensure_config_creates_the_lists(tmp_path):
    """Without `ensure_config` the moderation page could not enter anyone before the server had
    run once — the image creates the files itself only on first start, and `_edit_list` would
    write into a folder that does not exist yet.
    """
    ValheimDriver().ensure_config(_srv(tmp_path))
    cfg = tmp_path / "config"
    assert (cfg / "worlds_local").is_dir()
    for file in ("adminlist.txt", "bannedlist.txt", "permittedlist.txt"):
        assert (cfg / file).is_file(), f"{file} is missing — moderation would only work after the first start"
        assert (cfg / file).read_text(encoding="utf-8") == ""


def test_ensure_config_overwrites_nothing(tmp_path):
    """A second call must not empty a maintained list — for the `permittedlist` that would be the
    opposite of what one expects: the server would then be open to EVERYONE."""
    cfg = tmp_path / "config"
    cfg.mkdir(parents=True)
    (cfg / "permittedlist.txt").write_text("76561198000000001\n", encoding="utf-8")

    ValheimDriver().ensure_config(_srv(tmp_path))
    assert (cfg / "permittedlist.txt").read_text(encoding="utf-8") == "76561198000000001\n"


def test_creation_calls_ensure_config_at_all():
    """`routers/servers.create_server` only calls it IF the driver has it (`getattr`).

    Without this test a missing `ensure_config` goes unnoticed by everyone.
    """
    assert callable(getattr(ValheimDriver(), "ensure_config", None))
