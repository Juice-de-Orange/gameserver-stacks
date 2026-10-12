"""Avorion driver: INI config on the STOPPED server, RCON, workshop mods, honest gaps.

No DB, no RCON: `console` and `docker.state` are stubbed, async via `asyncio.run`.
"""
import asyncio
import configparser
from pathlib import Path

import pytest

from app.models.server import ConsoleKind, GameServer, GameType
from app.services.drivers import get_driver, version_source
from app.services.drivers.avorion import AvorionDriver, parse_avorion_players, parse_modconfig
from app.services.drivers.base import QuiesceWarning, VersionSource
from tests.file_permissions import ONLY_WITH_FILE_MODE

PLAYERS_OUT = "Players online: 2\nAlice\nBob McTest\n"


def _srv(tmp_path=None, **over):
    base = dict(
        key="av", game_type=GameType.avorion, display_name="Galaxy",
        data_dir=str(tmp_path) if tmp_path else "/srv/gameservers/avorion/av",
        container_name="avorion", rcon_host="avorion", rcon_port=27015,
        console_kind=ConsoleKind.source_rcon, game_ports='{"game": "27000/tcp"}',
        image_repo="rfvgyhn/avorion", image_tag="2.5.13.44140",
    )
    base.update(over)
    return GameServer(**base)


def _stub(response="ok"):
    d = AvorionDriver()
    calls: list[str] = []

    async def fake(server, command, *, timeout=8.0):
        calls.append(command)
        return PLAYERS_OUT if command == "/players" else response

    d.console = fake
    return d, calls


def _as_coroutine(value):
    async def _f():
        return value
    return _f()


def _docker_says(monkeypatch, **state):
    monkeypatch.setattr(
        "app.services.docker_client.docker.state", lambda name: _as_coroutine(state)
    )


@pytest.fixture()
def stopped(tmp_path, monkeypatch):
    """A server with a seeded `server.ini` whose container is verifiably stopped."""
    srv = _srv(tmp_path)
    AvorionDriver().ensure_config(srv, secret="secret123")
    _docker_says(monkeypatch, running=False)
    return srv


def _ini(srv) -> configparser.ConfigParser:
    p = configparser.ConfigParser(interpolation=None)
    p.optionxform = str
    p.read_string((Path(srv.data_dir) / "galaxy" / "server.ini").read_text(encoding="utf-8"))
    return p


# ------------------------------------------------------------------ Basics

def test_capabilities_hide_nothing():
    """What is missing is visibly missing — and what is added comes with a measurement.

    `announce` is backed by `/help` on a running server, which literally lists
    "/say: Send chat message to all players on the server".

    `events` is missing because no join line has ever been observed on this server, so there is
    no line to calibrate a pattern against.
    """
    caps = AvorionDriver().capabilities()
    assert {"console", "players", "config", "mods", "backup", "versions", "announce"} <= caps
    assert "events" not in caps


def test_the_driver_hangs_on_the_enum():
    assert isinstance(get_driver(_srv()), AvorionDriver)
    assert version_source(_srv()) is VersionSource.image_tag


def test_no_events_are_invented():
    """Better nothing than guessed joins — they would end up in session times and the leaderboard."""
    text = "Fri Mar 24 18:43:59 2017| Player Alice logged in\n"
    assert AvorionDriver().parse_events(_srv(), text) == []


def test_the_broadcast_uses_say_and_not_the_pz_command():
    """The inherited default would be `servermsg` — a PZ command that only produces an error here.

    Three features hang on this method: the broadcast tab, the warning before scheduled restarts
    and the cross-server bridge.
    """
    d, calls = _stub()
    asyncio.run(d.announce(_srv(), "Hello"))
    assert calls == ["/say Hello"]
    assert not any("servermsg" in c for c in calls)


# ------------------------------------------------------------------ Player list

def test_player_list_with_header():
    assert parse_avorion_players(PLAYERS_OUT) == {"count": 2, "names": ["Alice", "Bob McTest"]}


def test_player_list_without_header_counts_the_lines():
    assert parse_avorion_players("Alice\nBob\n") == {"count": 2, "names": ["Alice", "Bob"]}


def test_a_numbered_list_does_not_become_the_number():
    assert parse_avorion_players("1. Alice\n2. Bob\n")["names"] == ["Alice", "Bob"]


@pytest.mark.parametrize("response", ["", "   ", "Unknown command: /players", "Error: not admin"])
def test_a_misunderstood_answer_fails_loudly(response):
    """The most important case of this driver.

    `idle_autostop`, `watchdog` and `auto_update` depend on `players()`. All three treat an
    exception as "unknown — touch nothing", but a reported 0 as "nobody there". Were the parser
    lenient here, the idle guard would stop a server someone is playing on — and nobody could
    tell afterwards why.
    """
    with pytest.raises(ValueError):
        parse_avorion_players(response)


def test_nobody_online_is_only_unambiguous_with_a_header():
    """With a header, 0 is a statement; without it, "empty" cannot be told apart from "not
    understood" — and then it counts as unknown."""
    assert parse_avorion_players("Players online: 0\n") == {"count": 0, "names": []}


def test_players_goes_through_the_console():
    d, calls = _stub()
    assert asyncio.run(d.players(_srv()))["count"] == 2
    assert calls == ["/players"]


# ------------------------------------------------------------------ Container

def test_container_env_is_empty_because_the_image_knows_none(tmp_path):
    """An invented variable would look like a setting and have no effect."""
    assert AvorionDriver().container_env(_srv(tmp_path), "secret") == {}


def test_extras_are_passed_through_anyway(tmp_path):
    srv = _srv(tmp_path, extra='{"env": {"TZ": "Europe/Vienna"}}')
    assert AvorionDriver().container_env(srv, "secret") == {"TZ": "Europe/Vienna"}


def test_the_bind_points_to_the_galaxy_folder(tmp_path):
    binds = AvorionDriver().container_binds(_srv(tmp_path))
    assert binds == [f"{tmp_path}/galaxy:/home/steam/.avorion/galaxies/avorion_galaxy"]


def test_the_signal_is_enough(tmp_path):
    """The server is PID 1 and saves on SIGTERM by itself — unlike PZ."""
    assert asyncio.run(AvorionDriver().graceful_shutdown(_srv(tmp_path))) is False
    assert AvorionDriver().stop_timeout(_srv(tmp_path)) >= 180


# ------------------------------------------------------------------ Auto-Seed

def test_ensure_config_arms_rcon(stopped):
    """Without `rconPassword` Avorion does NOT start the RCON interface AT ALL — and that would
    look like anything but a configuration error."""
    ini = _ini(stopped)
    assert ini["Networking"]["rconPassword"] == "secret123"
    assert ini["Networking"]["rconPort"] == "27015"
    assert ini["Networking"]["port"] == "27000"


def test_ensure_config_does_not_overwrite_an_existing_file(stopped):
    AvorionDriver().set_config(stopped, {"Administration.name": "My galaxy"})
    AvorionDriver().ensure_config(stopped, secret="other")
    ini = _ini(stopped)
    assert ini["Administration"]["name"] == "My galaxy"
    assert ini["Networking"]["rconPassword"] == "secret123"


@ONLY_WITH_FILE_MODE
def test_the_ini_is_readable_only_by_the_owner(stopped):
    import stat

    path = Path(stopped.data_dir) / "galaxy" / "server.ini"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


# ------------------------------------------------------------------ Config: the core rule

def test_a_running_server_is_not_written_to(stopped, monkeypatch):
    """The rule that shapes this driver.

    Avorion rewrites its `server.ini` on every start. A change on a running server does not
    "take effect after restart", it "is gone after the restart" — and until then the editor shows
    it as if it were set. The check lives in the driver and not as a note in the UI: a rule you
    have to read is no rule.
    """
    _docker_says(monkeypatch, running=True)
    with pytest.raises(ValueError) as error:
        AvorionDriver().set_config(stopped, {"Administration.name": "New"})
    assert error.value.code == "config.avorion_must_be_stopped"
    assert _ini(stopped)["Administration"]["name"] == "Galaxy"   # unchanged (seed value)


def test_a_restarting_server_counts_as_running_too(stopped, monkeypatch):
    """`docker.state()` knows `restarting` — whoever reads only `running` writes right into the
    window in which the server is about to rewrite its file."""
    _docker_says(monkeypatch, running=False, restarting=True)
    with pytest.raises(ValueError) as error:
        AvorionDriver().set_config(stopped, {"Administration.name": "New"})
    assert error.value.code == "config.avorion_must_be_stopped"


def test_an_unclear_state_counts_as_running(stopped, monkeypatch):
    """When in doubt, do not write: a lost value costs more than a rejected click."""
    def broken(name):
        raise RuntimeError("docker proxy dead")

    monkeypatch.setattr("app.services.docker_client.docker.state", broken)
    with pytest.raises(ValueError) as error:
        AvorionDriver().set_config(stopped, {"Administration.name": "New"})
    assert error.value.code == "config.avorion_state_unknown"


# ------------------------------------------------------------------ Config-Editor

def test_config_roundtrip_across_all_types(stopped):
    d = AvorionDriver()
    d.set_config(stopped, {
        "Administration.name": "Galaxy North",
        "Administration.maxPlayers": "20",
        "Game.PlayerToPlayerDamage": "false",
        "Game.motd": "Welcome",
        "Administration.accessListMode": "Whitelist",
    })
    fields = {f.key: f for f in d.config_fields(stopped)}
    assert fields["Administration.name"].value == "Galaxy North"
    assert fields["Administration.maxPlayers"].value == "20"
    assert fields["Game.PlayerToPlayerDamage"].value == "false"
    ini = _ini(stopped)
    assert ini["Game"]["PlayerToPlayerDamage"] == "false" and ini["Game"]["motd"] == "Welcome"


def test_the_case_of_the_keys_is_preserved(stopped):
    """`configparser` lowercases keys by default. Avorion reads `maxPlayers` —
    `maxplayers` would be a second, ignored entry in the same file."""
    AvorionDriver().set_config(stopped, {"Administration.maxPlayers": "42"})
    raw = (Path(stopped.data_dir) / "galaxy" / "server.ini").read_text(encoding="utf-8")
    assert "maxPlayers = 42" in raw and "maxplayers" not in raw


def test_the_control_channel_is_read_only(stopped):
    """Whoever may change the RCON password or port can switch off the dashboard's console."""
    d = AvorionDriver()
    d.set_config(stopped, {"Networking.rconPassword": "takeover", "Networking.rconPort": "1"})
    ini = _ini(stopped)
    assert ini["Networking"]["rconPassword"] == "secret123" and ini["Networking"]["rconPort"] == "27015"


def test_the_rcon_password_is_delivered_masked(stopped):
    """`env_managed` means read-only; it does not withhold the value."""
    fields = {f.key: f for f in AvorionDriver().config_fields(stopped)}
    assert "secret123" not in fields["Networking.rconPassword"].value
    assert fields["Networking.rconPassword"].env_managed is True


def test_the_join_password_is_visible_and_editable(stopped):
    """Visible — everyone who plays knows it anyway.

    The control channel stays sealed: `Networking.rconPassword` is still delivered masked
    (separate case above). The difference is about content, not technology: whoever reads RCON has
    the full console; whoever reads the join password has what the invitation says.
    """
    d = AvorionDriver()
    d.set_config(stopped, {"Administration.password": "joinpw"})
    fields = {f.key: f for f in d.config_fields(stopped)}
    assert fields["Administration.password"].value == "joinpw"
    assert _ini(stopped)["Administration"]["password"] == "joinpw"


def test_a_cleared_join_password_opens_the_server(stopped):
    """Visible fields need the opposite rule: empty means empty, otherwise a password could never
    be switched off again."""
    d = AvorionDriver()
    d.set_config(stopped, {"Administration.password": "joinpw"})
    d.set_config(stopped, {"Administration.password": ""})
    assert _ini(stopped)["Administration"]["password"] == ""


def test_an_unparsable_number_does_not_land_in_the_number_field(stopped):
    """LOUD instead of silent.

    `coerce_value("int", "")` does not raise but returns the raw value; skipping it silently
    would let `put_config` answer 200 with an audit entry while the editor shows the never-written
    value as saved. A `type="number"` field sends exactly this empty string when cleared.
    """
    with pytest.raises(ValueError) as error:
        AvorionDriver().set_config(stopped, {"Administration.maxPlayers": "many"})
    assert error.value.code == "config.driver_integer_expected"
    assert _ini(stopped)["Administration"]["maxPlayers"] == "10"


def test_a_line_break_cannot_create_a_second_key(stopped):
    """Otherwise a value like `x\\nrconPassword = empty` would write off the console."""
    with pytest.raises(ValueError) as error:
        AvorionDriver().set_config(stopped, {"Administration.name": "x\nrconPassword = empty"})
    assert error.value.code == "config.avorion_line_break"


def test_an_unreadable_ini_reports_loudly(tmp_path, monkeypatch):
    srv = _srv(tmp_path)
    _docker_says(monkeypatch, running=False)
    assert AvorionDriver().config_fields(srv) != []          # READING may come back empty
    with pytest.raises(ValueError) as error:
        AvorionDriver().set_config(srv, {"Administration.name": "x"})
    assert error.value.code == "config.avorion_ini_unreadable"


# ------------------------------------------------------------------ Backup

def test_backup_takes_the_whole_galaxy_folder(tmp_path):
    spec = AvorionDriver().backup_spec(_srv(tmp_path))
    assert spec.data_dir == str(tmp_path) and spec.world_subdirs == ("galaxy",)
    # Empty: the server.ini lives IN the galaxy folder, there is nothing to restore separately.
    assert spec.config_subdirs == ()


def test_a_failed_save_does_not_topple_the_archive(tmp_path, monkeypatch):
    d = AvorionDriver()

    async def broken(server, command, *, timeout=8.0):
        raise RuntimeError("RCON dead")

    d.console = broken
    _docker_says(monkeypatch, running=True)
    with pytest.raises(QuiesceWarning):
        d.backup_spec(_srv(tmp_path)).pre_hook()


def test_a_stopped_server_has_nothing_to_save(tmp_path, monkeypatch):
    d = AvorionDriver()

    async def broken(server, command, *, timeout=8.0):
        raise RuntimeError("RCON dead")

    d.console = broken
    _docker_says(monkeypatch, running=False)
    d.backup_spec(_srv(tmp_path)).pre_hook()                 # does not raise


# ------------------------------------------------------------------ Mods

def test_workshop_ids_are_read():
    lua = '''
    modLocation = ""
    forceEnabling = false
    mods = {
        {workshopid = "1819452708"},
        {workshopid = "2909895451"},
        {workshopid = "1819452708"},
    }
    allowed = {}
    '''
    assert parse_modconfig(lua) == ["1819452708", "2909895451"]     # order kept, no duplicates


def test_without_modconfig_nothing_is_claimed(tmp_path):
    mods = AvorionDriver().list_mods(_srv(tmp_path))
    assert mods["present"] is False and mods["workshop_ids"] == []


def test_mods_come_from_the_file(stopped):
    (Path(stopped.data_dir) / "galaxy" / "modconfig.lua").write_text(
        'mods = { {workshopid = "123"} }', encoding="utf-8"
    )
    mods = AvorionDriver().list_mods(stopped)
    assert mods["present"] is True and mods["workshop_ids"] == ["123"]


def test_the_overview_says_nothing_about_unread_mods(tmp_path):
    """Without `modconfig.lua` the number is unknown — not zero."""
    assert AvorionDriver().overview_stats(_srv(tmp_path))["mods_count"] is None


# ------------------------------------------------------------------ Moderation

def test_moderation_also_says_what_is_missing():
    spec = AvorionDriver().moderation_spec(_srv())
    assert spec.kick and spec.ban and spec.ban_ip and spec.unban and spec.whitelist
    assert spec.access_levels == ()
    assert "server.ini" in spec.reasons["access_levels"]
    assert "Whitelist" in spec.reasons["whitelist"]


def test_moderation_commands_look_right():
    d, calls = _stub()
    srv = _srv()
    asyncio.run(d.kick(srv, "Alice", "AFK"))
    asyncio.run(d.ban(srv, "Bob McTest"))
    asyncio.run(d.ban(srv, "1.2.3.4", ip=True))
    asyncio.run(d.unban(srv, "Bob McTest"))
    assert calls == ["/kick Alice AFK", "/ban Bob McTest", "/banip 1.2.3.4", "/unban Bob McTest"]


def test_a_name_with_spaces_stays_whole():
    """Unlike Factorio: Avorion names may contain spaces, and a name boiled down to `\\w`
    would hit the wrong player — or none at all."""
    assert AvorionDriver()._name("Bob McTest") == "Bob McTest"


def test_a_name_cannot_start_a_second_command():
    d, calls = _stub()
    asyncio.run(d.kick(_srv(), 'Alice\n/ban Innocent', ""))
    assert "\n" not in calls[0] and calls[0] == "/kick Alice/ban Innocent"


# ------------------------------------------------------------------ Polish

def test_safe_mode_cannot_halt_the_galaxy():
    safe = AvorionDriver().safe_console_commands()
    assert "/players" in safe and "/save" not in safe


def test_connect_info_reports_a_set_password(stopped):
    AvorionDriver().set_config(stopped, {"Administration.password": "joinpw"})
    info = AvorionDriver().connect_info(stopped)
    assert info.password_required is True and info.uri == ""
    assert "joinpw" not in (info.hint + info.browser_name)


# ------------------------------------------------------------------ Mods: apply and preflight

def test_the_allowed_block_is_read_whole():
    """A non-greedy regex ends at the FIRST closing brace — the one of the first entry.

    Measured on our own output, that turned three allowed IDs into `allowed=['111']` and
    `duplicates=['222','333']`: false AV_ID_DUPLICATE and AV_NOT_ALLOWED findings, and — worse —
    an ID that is only in `allowed` (client mods) landed in `mods`, was checked against Steam
    and could abort the apply with 409.
    """
    from app.services.mods.avorion_apply import build_modconfig
    from app.services.mods.preflight_avorion import ids_and_allowed

    lua = build_modconfig(["111", "222", "333"], ["111", "222", "333"])
    mods, allowed, duplicates = ids_and_allowed(lua)
    assert mods == ["111", "222", "333"]
    assert allowed == ["111", "222", "333"]
    assert duplicates == []

    # An ID that is only ALLOWED must not count as a mod.
    mods, allowed, _ = ids_and_allowed(build_modconfig(["111"], ["111", "999"]))
    assert mods == ["111"] and allowed == ["111", "999"]


def test_list_mods_separates_mods_and_access_list(tmp_path):
    """`workshop_ids` must not be `mods ∪ allowed` — applying would turn it into real mods.

    The mods page puts this list into the edit field and sends `allowed: []` back. The server
    would then load mods that were only allowed before, and the access list would be gone. The
    rollback does not catch it: extra mods do not prevent the boot, `verify_boot` reports success.
    """
    from app.services.mods.avorion_apply import build_modconfig

    srv = _srv(tmp_path)
    galaxy = Path(AvorionDriver()._galaxy(srv))
    galaxy.mkdir(parents=True, exist_ok=True)
    (galaxy / "modconfig.lua").write_text(build_modconfig(["111"], ["111", "222"]), encoding="utf-8")

    response = AvorionDriver().list_mods(srv)
    assert response["workshop_ids"] == ["111"]
    assert response["allowed"] == ["111", "222"]


def test_manual_edits_survive_the_mod_apply():
    """The file header promises it literally.

    Generating the WHOLE file from a fixed template resets a hand-set `forceEnabling = true` on
    every apply, with no way back: the rollback only runs when the boot proof fails — and a reset
    `forceEnabling` does not prevent the start.
    """
    from app.services.mods.avorion_apply import build_modconfig

    old = build_modconfig(["111"]).replace("forceEnabling = false", "forceEnabling = true")
    old = old.replace('modLocation = ""', 'modLocation = "/own/path"')
    new = build_modconfig(["111", "222"], before=old)
    assert "forceEnabling = true" in new
    assert 'modLocation = "/own/path"' in new
    # Without a previous file it stays at the defaults.
    assert "forceEnabling = false" in build_modconfig(["111"])


def test_a_title_instead_of_an_id_is_caught_while_building():
    """The digit check must not live only in the apply endpoint.

    `_ID_RE` requires plain digits and finds nothing in such a draft, so the preflight `check()`
    would return an EMPTY, i.e. green, result: "checked, no blockers" for a selection of which no
    line was checked. The click on apply would then answer 400.
    """
    from app.services.mods.avorion_apply import build_modconfig

    with pytest.raises(ValueError, match="is not a workshop ID"):
        build_modconfig(["https://steamcommunity.com/sharedfiles/filedetails/?id=111"])
    with pytest.raises(ValueError, match="is not a workshop ID"):
        build_modconfig(["111"], ["My favourite mod"])


def test_a_failed_stop_does_not_report_two_false_states():
    """`stop_and_confirm` returns `stopped=False` precisely when the server is RUNNING.

    The message must not claim two false things — that the file could not be written back
    (it was never tried) and that the server did not come up (it was running) — but tell the
    operator the true state and the way back.
    """
    from app.services.mods.avorion_apply import _rollback_text

    text = _rollback_text(False, False, stopped=False, was_started=True, backup="pre-avorionmods")
    assert "never started" in text
    assert "NEW list" in text
    assert "pre-avorionmods" in text

    # And the good case stays good — also on a server that was stopped on purpose.
    assert "stays stopped" in _rollback_text(True, False, stopped=True, was_started=False)
    assert "The server is running." in _rollback_text(True, True, stopped=True, was_started=True)
