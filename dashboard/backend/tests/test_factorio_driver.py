"""Factorio driver: RCON control, JSON config, backup semantics, moderation, auto-seed.

No DB, no RCON: `console` is stubbed, async via `asyncio.run`. The configuration is created
fresh under `tmp_path` per test — no shared state between cases.
"""
import asyncio
import json
import os
from pathlib import Path

import pytest

from app.models.server import ConsoleKind, GameServer, GameType
from app.services.drivers import get_driver, version_source
from app.services.drivers.base import QuiesceWarning, VersionSource
from app.services.drivers.factorio import FactorioDriver, parse_factorio_players
from tests.file_permissions import ONLY_WITH_FILE_MODE

PLAYERS_OUT = "Online players (2):\n  Alice (online)\n  Bob Builder (online)\n"


def _srv(tmp_path=None, **over):
    base = dict(
        key="fac", game_type=GameType.factorio, display_name="Factory",
        data_dir=str(tmp_path) if tmp_path else "/srv/gameservers/factorio/fac",
        rcon_host="factorio", rcon_port=27015, console_kind=ConsoleKind.source_rcon,
        game_ports='{"game": "34197/udp"}', image_repo="factoriotools/factorio", image_tag="2.1.14-rootless",
    )
    base.update(over)
    return GameServer(**base)


def _stub(response="ok"):
    """Driver with a stubbed console; returns (driver, list of calls)."""
    d = FactorioDriver()
    calls: list[str] = []

    async def fake(server, command, *, timeout=8.0):
        calls.append(command)
        if command == "/players online":
            return PLAYERS_OUT
        return response

    d.console = fake
    return d, calls


@pytest.fixture()
def seeded(tmp_path):
    """A server with its configuration created — the state after `ensure_config`."""
    srv = _srv(tmp_path)
    FactorioDriver().ensure_config(srv, secret="secret123")
    return srv


# ------------------------------------------------------------------ Basics

def test_capabilities_are_honest():
    caps = FactorioDriver().capabilities()
    assert {"console", "players", "events", "config", "mods", "backup", "versions", "announce"} <= caps
    # No `sandbox` — that is a Project Zomboid feature.
    assert "sandbox" not in caps


def test_the_driver_hangs_off_the_enum():
    assert get_driver(_srv()) is get_driver(_srv())          # one singleton per game type
    assert isinstance(get_driver(_srv()), FactorioDriver)


def test_the_version_comes_from_the_image_tag():
    """As with PZ — this decides whether an image is pulled before the swap."""
    assert version_source(_srv()) is VersionSource.image_tag


# ------------------------------------------------------------------ Player list

def test_player_list_reads_count_and_names():
    assert parse_factorio_players(PLAYERS_OUT) == {
        "count": 2, "names": ["Alice", "Bob Builder"],
    }


def test_an_empty_server_is_not_broken():
    assert parse_factorio_players("Online players (0):\n") == {"count": 0, "names": []}


def test_the_header_count_wins_over_a_truncated_name_list():
    """The server says three, the list shows two — then it is three.

    The count comes from the server; the name list can be cut off at the RCON packet boundary.
    Counting the names instead reports the server as emptier than it is — and `auto_update` or
    `idle_autostop` then runs over players who are still on.
    """
    out = "Online players (3):\n  Alice (online)\n  Bob (online)\n"
    assert parse_factorio_players(out)["count"] == 3


def test_garbage_yields_no_ghost_players():
    assert parse_factorio_players("RCON: unknown command") == {"count": 0, "names": []}
    assert parse_factorio_players("") == {"count": 0, "names": []}


def test_players_goes_through_the_console():
    d, calls = _stub()
    assert asyncio.run(d.players(_srv()))["count"] == 2
    assert calls == ["/players online"]


# ------------------------------------------------------------------ Events & chat

def test_join_and_leave_are_detected():
    d = FactorioDriver()
    text = (
        "2026-08-23 12:00:00 [JOIN] Alice joined the game\n"
        "2026-08-23 12:30:00 [LEAVE] Alice left the game\n"
    )
    events = d.parse_events(_srv(), text)
    assert [(e.kind, e.username) for e in events] == [("join", "Alice"), ("leave", "Alice")]


def test_a_chat_line_cannot_forge_an_event():
    """The Minecraft driver's bug, anticipated here.

    Without anchoring at the start of the line, `[JOIN] Victim joined the game` — typed in chat —
    recorded a real join. That lands in session times and the leaderboard, and nothing shows that
    it was made up.
    """
    d = FactorioDriver()
    text = "2026-08-23 12:00:00 [CHAT] Attacker: [JOIN] Victim joined the game\n"
    assert d.parse_events(_srv(), text) == []


def test_chat_is_read_but_not_as_an_event():
    d = FactorioDriver()
    text = "2026-08-23 12:00:00 [CHAT] Alice: hello everyone\n"
    assert d.parse_events(_srv(), text) == []           # chat is not a PlayerEvent
    assert d.parse_chat(_srv(), text)[0][:2] == ("Alice", "hello everyone")


def test_our_own_injection_does_not_come_back():
    """Loop protection: what the dashboard sends appears as `<server>` and must not be
    re-ingested as player chat — otherwise the bridge runs in circles."""
    d = FactorioDriver()
    text = "2026-08-23 12:00:00 [CHAT] <server>: [Minecraft] Bob: hi\n"
    assert d.parse_chat(_srv(), text) == []


def test_the_docker_timestamp_is_stripped():
    d = FactorioDriver()
    text = "2026-08-23T10:00:00.123456789Z 2026-08-23 12:00:00 [JOIN] Alice joined the game\n"
    events = d.parse_events(_srv(), text)
    assert len(events) == 1 and events[0].at.year == 2026


# ------------------------------------------------------------------ Broadcast / injection

def test_broadcast_goes_out_as_silent_command():
    d, calls = _stub()
    asyncio.run(d.announce(_srv(), "Restart in 5 minutes"))
    assert calls == ['/silent-command game.print("Restart in 5 minutes")']


def test_a_broadcast_cannot_break_out_of_the_lua_string():
    """The backslash is what `sanitize_message` leaves over.

    `sanitize_message` removes `"`, `;`, `<`, `>` and control characters. If the backslash
    stayed, a `\\` at the end of the line would escape the closing quote — and everything after it
    would be Lua code on a server whose console otherwise only admins have.
    """
    d, calls = _stub()
    asyncio.run(d.announce(_srv(), 'evil\\" game.player.force.chart_all() --'))
    command = calls[0]
    assert "\\" not in command
    assert command.count('"') == 2                      # exactly the two delimiters
    assert command.startswith('/silent-command game.print("') and command.endswith('")')


def test_a_broadcast_cannot_start_a_second_line():
    d, calls = _stub()
    asyncio.run(d.announce(_srv(), "hello\nrcon: /promote Attacker"))
    assert "\n" not in calls[0]


# ------------------------------------------------------------------ Container

def test_container_env_and_binds(tmp_path):
    srv = _srv(tmp_path)
    env = FactorioDriver().container_env(srv, "topsecret42")
    assert env["PORT"] == "34197" and env["RCON_PORT"] == "27015"
    # Mod updates at start would change the mod set without a preflight having seen it —
    # and Factorio lets nobody in whose mod set does not match exactly.
    assert env["UPDATE_MODS_ON_START"] == "false"
    assert FactorioDriver().container_binds(srv) == [f"{tmp_path}/data:/factorio"]


def test_the_rcon_password_is_not_in_the_env(tmp_path):
    """The image knows no RCON env variable — it reads `config/rconpw`.

    A made-up `RCON_PASSWORD` variable would have no effect but would look like a safeguard:
    the server would keep running with a random password, and the dashboard console would stay
    dead for no visible reason.
    """
    env = FactorioDriver().container_env(_srv(tmp_path), "topsecret42")
    assert not any("topsecret42" in v for v in env.values())
    assert not any("RCON_PASS" in k.upper() for k in env)


def test_stop_timeout_gives_saving_room():
    assert FactorioDriver().stop_timeout(_srv()) >= 120


# ------------------------------------------------------------------ Auto-seed

def test_ensure_config_creates_everything(seeded):
    base = Path(seeded.data_dir) / "data"
    assert (base / "config" / "server-settings.json").exists()
    assert (base / "mods").is_dir() and (base / "saves").is_dir()
    assert (base / "config" / "rconpw").read_text(encoding="utf-8").strip() == "secret123"


def test_ensure_config_is_idempotent_and_overwrites_nothing(seeded):
    path = Path(seeded.data_dir) / "data" / "config" / "server-settings.json"
    FactorioDriver().set_config(seeded, {"name": "My Factory"})
    FactorioDriver().ensure_config(seeded, secret="other")
    assert json.loads(path.read_text(encoding="utf-8"))["name"] == "My Factory"
    rcon = Path(seeded.data_dir) / "data" / "config" / "rconpw"
    assert rcon.read_text(encoding="utf-8").strip() == "secret123"   # not overwritten


def test_without_an_account_nothing_is_advertised_publicly(seeded):
    """`public: true` without a factorio.com account is a lie: the server is never announced,
    but in the editor it would look as if it were on the list."""
    cfg = json.loads((Path(seeded.data_dir) / "data" / "config" / "server-settings.json").read_text())
    assert cfg["visibility"]["public"] is False


@ONLY_WITH_FILE_MODE
def test_the_rcon_password_is_readable_by_the_owner_only(seeded):
    """0600 — possible because `container_user` runs the server under the same uid as the
    `console-api`. With the rootless image variant a group grant would be more open than
    needed."""
    import stat

    path = Path(seeded.data_dir) / "data" / "config" / "rconpw"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_the_container_runs_under_the_same_uid_as_the_api(monkeypatch):
    """Otherwise, after the first start, every file belongs to the game server and the config
    editor fails with "Permission denied" from then on — reproduced on a host.

    The ids are the API process's own (`APP_UID` at image build), not a fixed 1000.
    Applies to the `-rootless` image variant; for the regular image see
    `test_container_user_depends_on_the_image_tag`.
    """
    monkeypatch.setattr("os.getuid", lambda: 1234)
    monkeypatch.setattr("os.getgid", lambda: 1235)
    assert FactorioDriver().container_user(_srv()) == "1234:1235"


def test_the_console_does_not_wait_for_a_sentinel_that_never_comes():
    """Measured, not assumed: Factorio does not echo the multi-packet sentinel.

    Every command ran into the read timeout — 8 seconds instead of 14 milliseconds. And the
    reason for `multi` does not apply here: an 8999-character response arrived complete in a
    single packet too.
    """
    import inspect

    source = inspect.getsource(FactorioDriver.console)
    assert "multi=False" in source


# ------------------------------------------------------------------ Config editor

def test_config_roundtrip_over_all_types(seeded):
    d = FactorioDriver()
    d.set_config(seeded, {
        "name": "Factory North",
        "max_players": "12",
        "auto_pause": "false",
        "tags": '["coop", "vanilla"]',
        "allow_commands": "admins-only",
    })
    fields = {f.key: f for f in d.config_fields(seeded)}
    assert fields["name"].value == "Factory North"
    assert fields["max_players"].value == "12"
    assert fields["auto_pause"].value == "false"
    assert json.loads(fields["tags"].value) == ["coop", "vanilla"]
    # and really in the JSON, with the right types
    raw = json.loads((Path(seeded.data_dir) / "data" / "config" / "server-settings.json").read_text())
    assert raw["max_players"] == 12 and raw["auto_pause"] is False and raw["tags"] == ["coop", "vanilla"]


def test_nested_keys_hit_the_right_branch(seeded):
    d = FactorioDriver()
    d.set_config(seeded, {"visibility.lan": "false"})
    raw = json.loads((Path(seeded.data_dir) / "data" / "config" / "server-settings.json").read_text())
    assert raw["visibility"] == {"public": False, "lan": False}   # the neighbour stays


def test_an_unparsable_number_does_not_land_in_the_number_field(seeded):
    """LOUD: a silent skip would let the editor report success."""
    d = FactorioDriver()
    with pytest.raises(ValueError) as error:
        d.set_config(seeded, {"max_players": "many"})
    assert error.value.code == "config.driver_integer_expected"
    raw = json.loads((Path(seeded.data_dir) / "data" / "config" / "server-settings.json").read_text())
    assert raw["max_players"] == 0                                # unchanged, no junk


def test_an_unknown_key_is_not_written(seeded):
    d = FactorioDriver()
    d.set_config(seeded, {"rcon_password": "takeover", "doesnotexist": "x"})
    raw = json.loads((Path(seeded.data_dir) / "data" / "config" / "server-settings.json").read_text())
    assert "rcon_password" not in raw and "doesnotexist" not in raw


def test_the_control_channel_stays_sealed_the_join_password_does_not(seeded):
    """Control secrets stay write-only — but not every field with "password" in its name.

    The factorio.com `token` is a control secret: whoever reads it can download mods in the
    account's name. It stays write-only. The join password, on the other hand, is known to
    everyone who plays anyway — keeping it write-only protects nobody and costs the operator
    exactly the information they need every day.
    """
    d = FactorioDriver()
    d.set_config(seeded, {"game_password": "joinpw", "token": "abc123"})
    fields = {f.key: f for f in d.config_fields(seeded)}
    assert fields["game_password"].value == "joinpw"        # visible
    assert fields["token"].value == ""                      # write-only
    assert "abc123" not in fields["token"].help


def test_an_empty_join_password_really_removes_it(seeded):
    """Visible fields need the opposite rule from write-only fields.

    For `password`, empty means "unchanged", because the field always arrives empty on load.
    Here the user sees the real value — so a cleared field must really clear, otherwise a
    password can never be switched off again.
    """
    d = FactorioDriver()
    d.set_config(seeded, {"game_password": "joinpw"})
    d.set_config(seeded, {"game_password": ""})
    raw = json.loads((Path(seeded.data_dir) / "data" / "config" / "server-settings.json").read_text())
    assert raw["game_password"] == ""


def test_an_empty_write_only_field_still_deletes_nothing(seeded):
    """The cross-check: for the `token` the old rule applies unchanged."""
    d = FactorioDriver()
    d.set_config(seeded, {"token": "abc123"})
    d.set_config(seeded, {"name": "Other", "token": ""})
    raw = json.loads((Path(seeded.data_dir) / "data" / "config" / "server-settings.json").read_text())
    assert raw["token"] == "abc123" and raw["name"] == "Other"


def test_an_unreadable_config_speaks_up_loudly(tmp_path):
    """READING may come out empty, WRITING may not.

    `server_ops.put_config` decides on this alone whether it reports 200 plus an audit entry. A
    silent `return` would be a log entry about a change that never happened.
    """
    srv = _srv(tmp_path)
    assert FactorioDriver().config_fields(srv) != []      # returns fields with empty values
    with pytest.raises(ValueError) as error:
        FactorioDriver().set_config(srv, {"name": "x"})
    assert error.value.code == "config.factorio_settings_unreadable"


def test_broken_json_is_not_silently_overwritten(seeded):
    path = Path(seeded.data_dir) / "data" / "config" / "server-settings.json"
    path.write_text("{broken", encoding="utf-8")
    with pytest.raises(ValueError) as error:
        FactorioDriver().set_config(seeded, {"name": "x"})
    assert error.value.code == "config.factorio_settings_invalid_json"
    assert path.read_text(encoding="utf-8") == "{broken"     # nothing touched


# ------------------------------------------------------------------ Backup

def test_backup_spec_separates_world_and_configuration(tmp_path):
    spec = FactorioDriver().backup_spec(_srv(tmp_path))
    assert spec.data_dir == f"{tmp_path}/data"
    # **`config` and `mods` are ALSO in `world_subdirs`** — only this list is packed
    # (`backup._pack`); `config_subdirs` only steers the restore. With them only below, NO
    # Factorio archive ever contained the mods — without which a Factorio world does not load.
    assert spec.world_subdirs == ("saves", "config", "mods")
    # Mods belong to the configuration: a Factorio world does not load without exactly the same mod set.
    assert spec.config_subdirs == ("config", "mods")
    assert set(spec.config_subdirs) <= set(spec.world_subdirs)


def test_a_failed_save_command_does_not_sink_the_archive(tmp_path, monkeypatch):
    """The difference from Minecraft, and it has a real reason.

    In MC the world is a folder that keeps being written during the tar run — without
    `save-off` the result is a torso, so the hook must throw hard. Here every save is a closed
    zip; an autosave creates a NEW file. A failed `/server-save` costs the minutes since the
    last autosave, nothing more.
    """
    d = FactorioDriver()

    async def broken(server, command, *, timeout=8.0):
        raise RuntimeError("RCON dead")

    d.console = broken
    monkeypatch.setattr(
        "app.services.docker_client.docker.state",
        lambda name: _as_coroutine({"running": True}),
    )
    with pytest.raises(QuiesceWarning):
        d.backup_spec(_srv(tmp_path)).pre_hook()


def test_a_stopped_server_has_nothing_to_quiesce(tmp_path, monkeypatch):
    """Otherwise an on-demand server would get no backup at all — and that is the normal case
    here, not the edge."""
    d = FactorioDriver()

    async def broken(server, command, *, timeout=8.0):
        raise RuntimeError("RCON dead")

    d.console = broken
    monkeypatch.setattr(
        "app.services.docker_client.docker.state",
        lambda name: _as_coroutine({"running": False}),
    )
    d.backup_spec(_srv(tmp_path)).pre_hook()             # does not throw


def _as_coroutine(value):
    async def _f():
        return value
    return _f()


# ------------------------------------------------------------------ Shutdown

def _quit_fails(monkeypatch, states):
    """`/quit` without a reply, and the container's run state over time (last one repeats)."""
    d = FactorioDriver()
    sent: list[str] = []

    async def no_reply(server, command, *, timeout=8.0):
        sent.append(command)
        raise RuntimeError("RCON error while sending/reading the response")

    async def state(name):
        return {"running": states.pop(0) if len(states) > 1 else states[0]}

    async def no_wait(_seconds):
        return None

    d.console = no_reply
    monkeypatch.setattr("app.services.docker_client.docker.state", state)
    monkeypatch.setattr("asyncio.sleep", no_wait)
    return d, sent


def test_a_quit_without_reply_followed_by_the_exit_is_a_clean_shutdown(monkeypatch):
    """The normal stop: `/quit` (or the parallel SIGTERM) ends the server, the reply never comes,
    the container goes down. Reported as a failure, `lifecycle` logged a WARNING on every stop."""
    d, sent = _quit_fails(monkeypatch, [True, True, False])
    assert asyncio.run(d.graceful_shutdown(_srv())) is True
    assert sent == ["/server-save", "/quit"]


def test_a_server_that_keeps_running_is_still_reported(monkeypatch):
    """The console did not cooperate and the game is still up: the grace period may end in a hard
    kill, and that warning must stay."""
    d, _ = _quit_fails(monkeypatch, [True])
    assert asyncio.run(d.graceful_shutdown(_srv())) is False


def test_an_unreadable_state_is_no_proof_of_a_clean_end(monkeypatch):
    d, _ = _quit_fails(monkeypatch, [True])

    async def broken(name):
        raise OSError("socket proxy gone")

    monkeypatch.setattr("app.services.docker_client.docker.state", broken)
    assert asyncio.run(d.graceful_shutdown(_srv())) is False


# ------------------------------------------------------------------ Mods

def test_mods_reads_files_and_the_on_off_state(seeded):
    base = Path(seeded.data_dir) / "data" / "mods"
    (base / "Krastorio2_1.3.24.zip").write_bytes(b"")
    (base / "mod-list.json").write_text(json.dumps({"mods": [
        {"name": "base", "enabled": True},
        {"name": "Krastorio2", "enabled": True},
        {"name": "DisabledThing", "enabled": False},
    ]}), encoding="utf-8")
    mods = FactorioDriver().list_mods(seeded)
    assert mods["files"] == ["Krastorio2_1.3.24.zip"]
    assert mods["enabled"]["Krastorio2"] is True and mods["enabled"]["DisabledThing"] is False
    # `base` has no file and must still not count as missing.
    assert mods["builtin"] == ["base"]


def test_without_a_mod_folder_there_is_no_exception(tmp_path):
    assert FactorioDriver().list_mods(_srv(tmp_path)) == {"files": [], "enabled": {}, "builtin": []}


# ------------------------------------------------------------------ Moderation

def test_moderation_also_says_what_is_missing():
    spec = FactorioDriver().moderation_spec(_srv())
    assert spec.kick and spec.ban and spec.unban and spec.whitelist
    assert spec.ban_ip is False
    assert "IP" in spec.reasons["ban_ip"]          # and WHY, not just THAT


def test_an_ip_ban_does_not_silently_become_a_normal_one():
    d, _ = _stub()
    with pytest.raises(NotImplementedError):
        asyncio.run(d.ban(_srv(), "Villain", ip=True))


def test_moderation_commands_look_right():
    d, calls = _stub()
    srv = _srv()
    asyncio.run(d.kick(srv, "Alice", "AFK"))
    asyncio.run(d.ban(srv, "Bob", "Griefing"))
    asyncio.run(d.unban(srv, "Bob"))
    asyncio.run(d.set_access(srv, "Alice", "admin"))
    asyncio.run(d.set_access(srv, "Alice", "none"))
    asyncio.run(d.whitelist_add(srv, "Carl"))
    assert calls == [
        "/kick Alice AFK", "/ban Bob Griefing", "/unban Bob",
        "/promote Alice", "/demote Alice", "/whitelist add Carl",
    ]


def test_a_player_name_cannot_start_a_second_command():
    """A name with a line break would otherwise append a second RCON statement — and
    `/promote` hands out admin rights."""
    d, calls = _stub()
    asyncio.run(d.kick(_srv(), "Alice\n/promote Attacker", ""))
    assert calls == ["/kick AlicepromoteAttacker"]


def test_whitelist_is_read_from_the_file(seeded):
    path = Path(seeded.data_dir) / "data" / "config" / "server-whitelist.json"
    path.write_text(json.dumps(["Alice", "Bob"]), encoding="utf-8")
    listing = FactorioDriver().whitelist_list(seeded)
    assert listing["available"] is True
    assert [u["username"] for u in listing["users"]] == ["Alice", "Bob"]


def test_without_a_whitelist_file_nothing_is_claimed(tmp_path):
    assert FactorioDriver().whitelist_list(_srv(tmp_path)) == {"available": False, "users": []}


# ------------------------------------------------------------------ Polish

def test_safe_mode_cannot_halt_the_round():
    """`/server-save` blocks the server for as long as it writes — nobody in safe mode has
    business doing that."""
    safe = FactorioDriver().safe_console_commands()
    assert "/players" in safe
    assert "/server-save" not in safe and "/quit" not in safe


def test_overview_does_not_count_builtin_mods(seeded):
    base = Path(seeded.data_dir) / "data" / "mods"
    (base / "Krastorio2_1.3.24.zip").write_bytes(b"")
    (base / "mod-list.json").write_text(
        json.dumps({"mods": [{"name": "base", "enabled": True}, {"name": "Krastorio2", "enabled": True}]}),
        encoding="utf-8",
    )
    stats = FactorioDriver().overview_stats(seeded)
    assert stats["mods_count"] == 1 and stats["max_players"] == 0


def test_connect_info_promises_no_click():
    """Factorio has no URL scheme — a made-up link would be a button that does nothing."""
    info = FactorioDriver().connect_info(_srv())
    assert info.uri == "" and info.port == 34197 and info.protocol == "udp"
    assert "address" in info.hint


def test_connect_info_reports_a_set_password(seeded):
    FactorioDriver().set_config(seeded, {"game_password": "joinpw"})
    info = FactorioDriver().connect_info(seeded)
    assert info.password_required is True
    assert "joinpw" not in (info.hint + info.browser_name)      # no plain text here either


def test_container_user_depends_on_the_image_tag(tmp_path):
    """The condition used to live only in a docstring.

    `1000:1000` works only with the `-rootless` image variant; the regular image fails under a
    foreign uid on a `sed -i` in its own file system (tried, not assumed). Set unconditionally,
    `create-container` created a container that never came up — and neither the setup nor the
    catalogue said why.
    """
    rootless = _srv(tmp_path)
    rootless.image_tag = "2.1.14-rootless"
    assert FactorioDriver().container_user(rootless) == f"{os.getuid()}:{os.getgid()}"

    regular = _srv(tmp_path)
    regular.image_tag = "2.0.77"
    assert FactorioDriver().container_user(regular) == "", (
        "the regular image must not get a foreign uid forced on it — the container does not start otherwise"
    )


def test_an_unparsable_number_field_is_rejected_loudly(seeded):
    """The rejection must reach the user, not just the log.

    `coerce_value("int", "")` does not throw but returns the raw value; `set_config` skipped it
    with `continue`, `put_config` only checks `env_managed` and then answered 200 plus an audit
    entry. The editor reported "Saved" and took the never-written value as the new current state
    — the rollback only became visible on the next page reload. A `type="number"` field sends
    exactly this empty string when cleared.
    """
    before = Path(FactorioDriver()._config_path(seeded, "server-settings.json")).read_text()
    with pytest.raises(ValueError) as error:
        FactorioDriver().set_config(seeded, {"afk_autokick_interval": ""})
    assert error.value.code == "config.driver_integer_expected"
    assert Path(FactorioDriver()._config_path(seeded, "server-settings.json")).read_text() == before
