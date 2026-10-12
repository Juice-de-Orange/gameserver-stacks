"""Minecraft & Vintage Story drivers (DB/Docker-free: env, binds, parsers, backup)."""
import json
import os

import pytest

from app.models.server import GameServer, GameType
from app.services.drivers.minecraft import MinecraftDriver, parse_mc_list
from app.services.drivers.vintagestory import VintageStoryDriver, parse_vs_players


# ------------------------------------------------------------------ Minecraft

def test_mc_container_env():
    srv = GameServer(
        key="mc", game_type=GameType.minecraft, server_type="PAPER", game_version="1.21.10",
        rcon_port=25575, heap_max_mb=4096, heap_min_mb=2048,
    )
    env = MinecraftDriver().container_env(srv, "s3cr3t")
    assert env["EULA"] == "TRUE"
    assert env["TYPE"] == "PAPER" and env["VERSION"] == "1.21.10"
    assert env["ENABLE_RCON"] == "TRUE" and env["RCON_PORT"] == "25575" and env["RCON_PASSWORD"] == "s3cr3t"
    assert env["MAX_MEMORY"] == "4096M" and env["INIT_MEMORY"] == "2048M"


def test_mc_binds_and_backup(tmp_path):
    srv = GameServer(key="mc", game_type=GameType.minecraft, data_dir=str(tmp_path))
    drv = MinecraftDriver()
    assert drv.container_binds(srv) == [f"{tmp_path}/data:/data"]
    # Old layout (up to 1.21.x): the three world folders sit side by side.
    for d in ("world", "world_nether", "world_the_end"):
        (tmp_path / "data" / d).mkdir(parents=True)
    spec = drv.backup_spec(srv)
    assert spec.data_dir == f"{tmp_path}/data"
    assert spec.world_subdirs == ("world", "world_nether", "world_the_end")
    assert "backup" in drv.capabilities() and "console" in drv.capabilities()


# --- Quiescing: the hook must WORK or fail LOUDLY ---------------------------------------------
#
# `create_backup` deliberately calls `pre_hook()` OUTSIDE its `try`: a raising hook aborts the
# backup before an archive exists. A hook that swallows its RCON error (`except Exception: pass`)
# defeats that protection — if `save-off` failed, a torn snapshot of the 4 GB world was created
# and logged as a successful backup.

def test_mc_quiesce_fails_loudly_instead_of_silently(monkeypatch, tmp_path):
    from app.services.rcon import RconError

    srv = GameServer(key="mc", game_type=GameType.minecraft, data_dir=str(tmp_path))
    drv = MinecraftDriver()

    async def _dead_rcon(self, server, command, *, timeout=8.0):
        raise RconError("RCON unreachable")

    monkeypatch.setattr(MinecraftDriver, "console", _dead_rcon)

    with pytest.raises(RuntimeError, match="save-off"):
        drv.backup_spec(srv).pre_hook()


@pytest.mark.filterwarnings("ignore:coroutine .* was never awaited:RuntimeWarning")
def test_mc_quiesce_does_not_work_in_a_running_loop_and_says_so(monkeypatch, tmp_path):
    """The reason for `asyncio.to_thread` in the weekly run, pinned.

    `asyncio.run()` raises inside an already running loop. If the hook swallowed that, a weekly
    run WITHOUT `to_thread` would pass silently: archive there, save never made.
    """
    import asyncio

    srv = GameServer(key="mc", game_type=GameType.minecraft, data_dir=str(tmp_path))
    drv = MinecraftDriver()
    seen: list[str] = []

    async def _recorder(self, server, command, *, timeout=8.0):
        seen.append(command)
        return ""

    monkeypatch.setattr(MinecraftDriver, "console", _recorder)

    async def _from_a_running_loop():
        with pytest.raises(RuntimeError):
            drv.backup_spec(srv).pre_hook()

    asyncio.run(_from_a_running_loop())
    assert seen == [], "the hook sent nothing at all — that is exactly why it must be loud"


def test_mc_and_vs_do_not_deliver_their_passwords_in_plain_text(tmp_path):
    """`config_fields` must not turn EVERY line into a field without an exception list.

    Otherwise `rcon.password` is not only readable (gate: `can_edit_sandbox`, not
    `can_console_full`) but also writable via `PUT …/config`: the lock there checks
    `env_managed`. Whoever changes the password locks the dashboard out of its own server —
    including the quiesce hooks above, which would silently turn every backup into a torso.
    """
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "server.properties").write_text(
        "rcon.password=top-secret\nenable-rcon=true\nmotd=Hello\nlevel-name=world\n",
        encoding="utf-8",
    )
    fields = {f.key: f for f in MinecraftDriver().config_fields(
        GameServer(key="mc", game_type=GameType.minecraft, data_dir=str(tmp_path)))}

    assert fields["rcon.password"].env_managed
    assert "top-secret" not in json.dumps([f.value for f in fields.values()])
    assert fields["motd"].value == "Hello"          # ordinary fields stay untouched
    for locked in ("rcon.password", "enable-rcon", "level-name"):
        assert fields[locked].env_managed, f"{locked} can be overwritten via PUT …/config"

    # Vintage Story: same class, same file source.
    (tmp_path / "gamedata").mkdir()
    (tmp_path / "gamedata" / "serverconfig.json").write_text(
        json.dumps({"Password": "joinpw", "RconPassword": "rcon-secret", "ServerName": "Test"}),
        encoding="utf-8",
    )
    vs_fields = {f.key: f for f in VintageStoryDriver().config_fields(
        GameServer(key="vs", game_type=GameType.vintagestory, data_dir=str(tmp_path)))}
    values = json.dumps([f.value for f in vs_fields.values()])
    # Only the CONTROL CHANNEL is withheld. The join password is deliberately shown in plain
    # text — it is the invitation, and the operator needs it.
    assert "rcon-secret" not in values
    assert vs_fields["Password"].value == "joinpw" and vs_fields["Password"].kind == "join_password"
    assert vs_fields["ServerName"].value == "Test"


def test_mc_parse_list():
    assert parse_mc_list("There are 2 of a max of 20 players online: Alice, Bob") == {
        "count": 2, "names": ["Alice", "Bob"],
    }
    assert parse_mc_list("There are 0 of a max of 20 players online:") == {"count": 0, "names": []}
    assert parse_mc_list("garbage") == {"count": 0, "names": []}


def test_mc_parse_events():
    drv = MinecraftDriver()
    srv = GameServer(key="mc", game_type=GameType.minecraft)
    log = (
        "2026-07-05T10:00:00.000Z [10:00:00] [Server thread/INFO]: Alice joined the game\n"
        "2026-07-05T10:05:00.000Z [10:05:00] [Server thread/INFO]: Bob left the game\n"
        "2026-07-05T10:06:00.000Z [10:06:00] [Server thread/INFO]: Alice was slain by Zombie"
    )
    ev = drv.parse_events(srv, log)
    kinds = {(e.username, e.kind) for e in ev}
    assert ("Alice", "join") in kinds
    assert ("Bob", "leave") in kinds
    assert ("Alice", "death") in kinds
    # NO kind="chat" (chat is not a PlayerEvent)
    assert not any(e.kind == "chat" for e in ev)


def test_mc_chat_not_persisted_as_event():
    """A chat line creates NO PlayerEvent, but parse_chat picks it up."""
    drv = MinecraftDriver()
    srv = GameServer(key="mc", game_type=GameType.minecraft)
    chat_line = "2026-07-05T10:07:00.000Z [10:07:00] [Server thread/INFO]: <Alice> hello folks"
    assert drv.parse_events(srv, chat_line) == []  # no join/leave/death, no chat any more
    chat = drv.parse_chat(srv, chat_line)
    assert [(p, m) for p, m, _ in chat] == [("Alice", "hello folks")]


# ------------------------------------------------------------------ Vintage Story

def test_vs_container_env_and_binds():
    srv = GameServer(
        key="vs", game_type=GameType.vintagestory, game_version="1.19.8", data_dir="/srv/gameservers/vintagestory",
    )
    env = VintageStoryDriver().container_env(srv, "secret-not-in-env")
    # No heap (.NET), RCON password NOT in the env; the game runs as the API's uid.
    assert env == {"VERSION": "1.19.8", "PUID": str(os.getuid()), "PGID": str(os.getgid())}
    assert VintageStoryDriver().container_binds(srv) == ["/srv/gameservers/vintagestory/gamedata:/gamedata"]


def test_vs_env_no_version_when_unpinned():
    # Adoption "keep current version": empty game_version → NO VERSION env (image default),
    # START_ARGS and the like come from extra.env.
    import json as _json
    srv = GameServer(
        key="vs", game_type=GameType.vintagestory, game_version="",
        extra=_json.dumps({"env": {"START_ARGS": "--dataPath /gamedata"}}),
    )
    env = VintageStoryDriver().container_env(srv, "x")
    assert "VERSION" not in env
    assert env["START_ARGS"] == "--dataPath /gamedata"


def test_vs_backup_and_caps():
    """The default points to the REAL data path (`gamedata/vs`), not to `gamedata`.

    Pinning `("Saves",)` here pinned exactly the error that left the Vintage Story world unbacked
    for months: what got packed was a stale world file one level too high. If neither exists (as
    here, with an invented path), the real one applies: it is the path the server runs with.
    """
    srv = GameServer(key="vs", game_type=GameType.vintagestory, data_dir="/srv/gameservers/vintagestory")
    drv = VintageStoryDriver()
    spec = drv.backup_spec(srv)
    assert spec.data_dir == "/srv/gameservers/vintagestory/gamedata"   # `backups/` lives there
    assert spec.world_subdirs == ("vs/Saves",)
    assert spec.pre_hook is not None  # /genbackup attempt
    assert "console" in drv.capabilities() and "versions" in drv.capabilities()


def test_vs_parse_players():
    assert parse_vs_players("3 players online")["count"] == 3
    p = parse_vs_players("- Alice\n- Bob")
    assert p["count"] == 2 and p["names"] == ["Alice", "Bob"]


# ------------------------------------------------------------------ set_config hardening

def test_mc_set_config_rejects_newline_injection(tmp_path):
    drv = MinecraftDriver()
    srv = GameServer(key="mc", game_type=GameType.minecraft, data_dir="/srv/gameservers/minecraft")
    with pytest.raises(ValueError):
        drv.set_config(srv, {"motd": "hi\nenable-rcon=false"})  # newline in the value → key injection prevented
    with pytest.raises(ValueError):
        drv.set_config(srv, {"bad\nkey": "x"})
    # '=' in the value is fine. Against a real file, so that "no raise" means the write happened
    # and not a silent no-op on a missing path.
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "server.properties").write_text("motd=old\n", encoding="utf-8")
    real = GameServer(key="mc", game_type=GameType.minecraft, data_dir=str(tmp_path))
    drv.set_config(real, {"motd": "a=b"})
    assert "motd=a=b" in (tmp_path / "data" / "server.properties").read_text(encoding="utf-8")


def test_set_config_reports_an_unwritable_file_instead_of_staying_silent(tmp_path):
    """`server_ops.put_config` answers 200 and audits as soon as `set_config` returns.

    A silent `return` in `except OSError` turned that into a success report about a change that
    never happened — for MC and VS additionally with `restart_required: true`, so the operator
    restarted the server and lost play time for nothing. Worse: `config_fields` returns an empty
    list on the same OSError, so the lock list of env-managed keys disappears too.
    """
    empty = tmp_path / "missing"

    mc = GameServer(key="mc", game_type=GameType.minecraft, data_dir=str(empty))
    with pytest.raises(ValueError, match="server.properties"):
        MinecraftDriver().set_config(mc, {"motd": "Hello"})

    vs = GameServer(key="vs", game_type=GameType.vintagestory, data_dir=str(empty))
    with pytest.raises(ValueError, match="serverconfig.json"):
        VintageStoryDriver().set_config(vs, {"ServerName": "New"})


def test_vs_set_config_reports_broken_json(tmp_path):
    """Not only the missing file — unreadable content must not pass as success either."""
    d = tmp_path / "gamedata"
    d.mkdir()
    (d / "serverconfig.json").write_text("{ this is not json", encoding="utf-8")
    vs = GameServer(key="vs", game_type=GameType.vintagestory, data_dir=str(tmp_path))
    with pytest.raises(ValueError, match="serverconfig.json"):
        VintageStoryDriver().set_config(vs, {"ServerName": "New"})


def test_vs_set_config_types_and_structure(tmp_path):
    d = tmp_path / "gamedata"
    d.mkdir()
    (d / "serverconfig.json").write_text(
        json.dumps({"ServerName": "X", "MaxClients": 16, "Ratio": 1.5, "Pvp": True, "WorldConfig": {"seed": "s"}})
    )
    srv = GameServer(key="vs", game_type=GameType.vintagestory, data_dir=tmp_path.as_posix())
    VintageStoryDriver().set_config(srv, {
        "ServerName": "New", "MaxClients": "32", "Ratio": "2.0", "Pvp": "false",
        "WorldConfig": "HACK", "Unknown": "y",
    })
    data = json.loads((d / "serverconfig.json").read_text())
    assert data["ServerName"] == "New"
    assert data["MaxClients"] == 32 and isinstance(data["MaxClients"], int)
    assert data["Ratio"] == 2.0 and isinstance(data["Ratio"], float)
    assert data["Pvp"] is False
    assert data["WorldConfig"] == {"seed": "s"}  # nested NOT overwritten
    assert "Unknown" not in data                 # unknown key NOT created


def test_vs_genbackup_fails_visibly_but_does_not_break_the_archive(monkeypatch, tmp_path):
    """VS deliberately differs from Minecraft here.

    MC's `save-off` is load-bearing — without it the archive is a torso, so it must fail hard.
    VS' `genbackup` only asks the game for its own save point; the tar over `Saves/` runs
    independently and IS the archive. Whether the VintageRCon mod knows the command at all is
    unverified (the VS capabilities are an open measurement) — a hard abort would break every
    backup there.

    What no longer happens: staying silent.
    """
    from app.services.drivers.base import QuiesceWarning
    from app.services.rcon import RconError

    srv = GameServer(key="vs", game_type=GameType.vintagestory, data_dir=str(tmp_path))

    async def _dead_rcon(self, server, command, *, timeout=8.0):
        raise RconError("VintageRCon does not answer")

    monkeypatch.setattr(VintageStoryDriver, "console", _dead_rcon)

    with pytest.raises(QuiesceWarning, match="genbackup"):
        VintageStoryDriver().backup_spec(srv).pre_hook()


def test_a_quiesce_note_does_not_prevent_the_archive(tmp_path):
    """Cross-check on the backup side: `QuiesceWarning` is caught and reported, everything else
    aborts (that is how the Minecraft case is meant)."""
    from app.services import backup as bk
    from app.services.drivers.base import QuiesceWarning

    (tmp_path / "Saves").mkdir()
    (tmp_path / "Saves" / "world.vcdbs").write_text("x", encoding="utf-8")

    def _only_a_note():
        raise QuiesceWarning("Vintage Story save point (genbackup) failed: dead")

    result = bk.create_backup("weekly", data_dir=str(tmp_path), world_subdirs=("Saves",),
                                pre_hook=_only_a_note)
    assert result["size"] > 0
    assert "genbackup" in result["quiesce_warning"]

    def _hard_error():
        raise RuntimeError("Minecraft quiesce (save-off) failed: dead")

    with pytest.raises(RuntimeError, match="save-off"):
        bk.create_backup("weekly", data_dir=str(tmp_path), world_subdirs=("Saves",),
                         pre_hook=_hard_error)


def test_a_stopped_mc_server_can_still_be_backed_up(monkeypatch, tmp_path):
    """A hook that raises on EVERY RCON error would also raise for a stopped server, whose
    backup would then never be created. Minecraft runs on demand, so that is the normal case.
    A stopped server has nothing to quiesce: it does not write, its files are consistent.
    """
    from app.services.rcon import RconError

    srv = GameServer(key="mc", game_type=GameType.minecraft, data_dir=str(tmp_path))

    async def _dead_rcon(self, server, command, *, timeout=8.0):
        raise RconError("container stopped")

    class _Docker:
        async def state(self, name):
            return {"exists": True, "running": False}

    monkeypatch.setattr(MinecraftDriver, "console", _dead_rcon)
    monkeypatch.setattr("app.services.docker_client.docker", _Docker())

    MinecraftDriver().backup_spec(srv).pre_hook()          # no raise — there is nothing to do


def test_with_unknown_state_the_quiesce_error_stays_real(monkeypatch, tmp_path):
    """When in doubt the server is running. Otherwise a broken socket proxy would turn every
    quiesce error into "it is stopped anyway" — and the torso would be back."""
    from app.services.rcon import RconError

    srv = GameServer(key="mc", game_type=GameType.minecraft, data_dir=str(tmp_path))

    async def _dead_rcon(self, server, command, *, timeout=8.0):
        raise RconError("RCON dead")

    class _Docker:
        async def state(self, name):
            raise RuntimeError("dockerproxy unreachable")

    monkeypatch.setattr(MinecraftDriver, "console", _dead_rcon)
    monkeypatch.setattr("app.services.docker_client.docker", _Docker())

    with pytest.raises(RuntimeError, match="save-off"):
        MinecraftDriver().backup_spec(srv).pre_hook()


def test_vs_join_password_is_visible_the_control_channel_is_not(tmp_path):
    """`Password` is the invitation, everything else stays write-only.

    Unlike PZ and Minecraft, `serverconfig.json` is the ONLY source of these values for Vintage
    Story — they come from no compose env, `env_managed` would lock them forever. The difference
    lies in the purpose: every fellow player knows the join password anyway; the mod's RCON
    password is a control channel and is never delivered.
    """
    (tmp_path / "gamedata").mkdir()
    file = tmp_path / "gamedata" / "serverconfig.json"
    file.write_text(json.dumps(
        {"Password": "old", "ServerName": "Test", "RconPassword": "top-secret"}), encoding="utf-8")
    srv = GameServer(key="vs", game_type=GameType.vintagestory, data_dir=str(tmp_path))
    drv = VintageStoryDriver()

    fields = {f.key: f for f in drv.config_fields(srv)}
    assert fields["Password"].kind == "join_password" and fields["Password"].value == "old"
    assert not fields["Password"].env_managed, "then the password could never be changed again"
    assert fields["RconPassword"].kind == "password" and fields["RconPassword"].value == ""
    assert "top-secret" not in json.dumps([f.value for f in fields.values()])

    drv.set_config(srv, {"Password": "new", "ServerName": "Other"})
    data = json.loads(file.read_text(encoding="utf-8"))
    assert data["Password"] == "new" and data["ServerName"] == "Other"

    # An emptied JOIN field really opens the server...
    drv.set_config(srv, {"Password": "", "ServerName": "Again"})
    data = json.loads(file.read_text(encoding="utf-8"))
    assert data["Password"] == "" and data["ServerName"] == "Again"

    # ...an emptied CONTROL CHANNEL field does not. Otherwise any save of the page would lock
    # the dashboard out of its own server.
    drv.set_config(srv, {"RconPassword": ""})
    assert json.loads(file.read_text(encoding="utf-8"))["RconPassword"] == "top-secret"


def test_vs_backs_up_the_real_world_not_the_stale_one_next_to_it(tmp_path):
    """The layout found on a real host:

        gamedata/Saves/default.vcdbs        686 MB, last written in January   <- stale
        gamedata/vs/Saves/default.vcdbs      10 MB, last written in August    <- the real world

    The weekly run packed the stale one. The archive was 227 MB and looked entirely plausible,
    which is why nobody noticed for months. The driver knew all along — `mods_dir` pinned
    `VS_DATA_PATH=/gamedata/vs` as a verified trap, but only for mods.
    """
    gamedata = tmp_path / "gamedata"
    (gamedata / "Saves").mkdir(parents=True)                  # the old, stale one
    (gamedata / "Saves" / "default.vcdbs").write_text("january", encoding="utf-8")
    (gamedata / "vs" / "Saves").mkdir(parents=True)           # the real world
    (gamedata / "vs" / "Saves" / "default.vcdbs").write_text("august", encoding="utf-8")

    srv = GameServer(key="vs", game_type=GameType.vintagestory, data_dir=str(tmp_path))
    spec = VintageStoryDriver().backup_spec(srv)

    # The archive location stays `gamedata/` — otherwise the existing archives would be invisible.
    #
    # Separators normalised: the driver builds with `posixpath`, on purpose — these are paths
    # INSIDE the container, which is always Linux, whatever machine the code runs on. Without the
    # normalisation this test fails on a Windows workstation over a backslash although nothing
    # differs in production.
    assert spec.data_dir.replace("\\", "/") == gamedata.as_posix()
    assert spec.world_subdirs == ("vs/Saves",)

    # And the archive really contains the August world.
    from app.services import backup as bk

    result = bk.create_backup("weekly", data_dir=spec.data_dir, world_subdirs=spec.world_subdirs)
    import tarfile
    with tarfile.open(bk.backup_path(result["name"], data_dir=spec.data_dir)) as tar:
        names = tar.getnames()
        assert "vs/Saves/default.vcdbs" in names
        assert tar.extractfile("vs/Saves/default.vcdbs").read() == b"august"


def test_vs_falls_back_to_the_old_layout_when_the_new_one_is_missing(tmp_path):
    """A deviating installation stays usable instead of silently pointing at nothing."""
    gamedata = tmp_path / "gamedata"
    (gamedata / "Saves").mkdir(parents=True)
    (gamedata / "serverconfig.json").write_text(json.dumps({"ServerName": "Old"}), encoding="utf-8")

    srv = GameServer(key="vs", game_type=GameType.vintagestory, data_dir=str(tmp_path))
    assert VintageStoryDriver().backup_spec(srv).world_subdirs == ("Saves",)
    fields = {f.key: f for f in VintageStoryDriver().config_fields(srv)}
    assert fields["ServerName"].value == "Old"


def test_a_failed_flush_does_not_leave_autosave_off(monkeypatch, tmp_path):
    """`save-off` has already gone through when `save-all flush` fails — and then it stays.

    `create_backup` deliberately calls `pre_hook()` OUTSIDE its `try`; the `post_hook`
    (`save-on`) hangs on the `finally` of exactly that `try`. If the pre_hook raises between the
    two commands, `save-on` is NEVER reached — the autosave of the RUNNING server stays off until
    the next restart. That is no longer a backup problem but silent world loss on every crash.

    The path there is the expected one, not the exotic one: a flush with the 8 s default timeout,
    while save time scales with the loaded world (here 4.9 GB) — hence 60 s.
    """
    from app.services.rcon import RconError

    srv = GameServer(key="mc", game_type=GameType.minecraft, data_dir=str(tmp_path))
    seen: list[tuple[str, float]] = []

    async def _flush_dies(self, server, command, *, timeout=8.0):
        seen.append((command, timeout))
        if command == "save-all flush":
            raise RconError("timeout while flushing the 4.9 GB world")
        return ""

    class _Docker:
        async def state(self, name):
            return {"exists": True, "running": True}   # running → the quiesce error is real

    monkeypatch.setattr(MinecraftDriver, "console", _flush_dies)
    monkeypatch.setattr("app.services.docker_client.docker", _Docker())

    with pytest.raises(RuntimeError, match="save-all flush"):
        MinecraftDriver().backup_spec(srv).pre_hook()

    commands = [c for c, _ in seen]
    assert commands == ["save-off", "save-all flush", "save-on"], (
        f"save-off stayed on (seen: {commands}) — autosave is off until the next restart."
    )
    flush_timeout = next(t for c, t in seen if c == "save-all flush")
    assert flush_timeout >= 60, (
        f"The flush runs with {flush_timeout}s. Save time scales with the loaded world, so it "
        "needs 60 s — with the 8 s default, "
        "failure is the normal case."
    )


# --- Log patterns against the REAL stdout format ----------------------------------------------

def _mc_fixture() -> str:
    from pathlib import Path

    p = Path(__file__).parent / "fixtures" / "mc_console_log.txt"
    return "\n".join(z for z in p.read_text(encoding="utf-8").splitlines() if not z.startswith("#"))


def test_mc_patterns_match_the_real_stdout_format():
    """Measured from `docker logs minecraft`: the prefix is `[HH:MM:SS INFO]:`.

    `[HH:MM:SS] [Server thread/INFO]:` is what `logs/latest.log` holds — but stdout is what gets
    read (`chat_log_path` returns None for MC, the ingest pulls `docker.logs`). A chat regex
    anchored to the FILE form can **never** match on a live instance: the Minecraft side of the
    cross-server chat bridge would not work at all.

    This test pins both formats — the file form because it occurs in `logs/latest.log` and on
    older servers, and the stdout form because it is the one actually read.
    """
    drv = MinecraftDriver()
    srv = GameServer(key="mc", game_type=GameType.minecraft)

    ev = {(e.username, e.kind) for e in drv.parse_events(srv, _mc_fixture())}
    assert ("Steve", "join") in ev
    assert ("Steve", "leave") in ev
    assert ("Steve", "death") in ev

    chat = drv.parse_chat(srv, _mc_fixture())
    assert ("Steve", "hello folks") in [(p, m) for p, m, _ in chat]

    # The file form must keep working (older servers, logs/latest.log).
    file_form = "[10:00:00] [Server thread/INFO]: Alice joined the game"
    assert [(e.username, e.kind) for e in drv.parse_events(srv, file_form)] == [("Alice", "join")]


def test_a_player_cannot_write_an_event_into_the_chat():
    """The patterns must be anchored at the log prefix, not `search` over the WHOLE line.

    Unanchored, `<Steve> Friend2 joined the game` created a real join event for "Friend2" — a
    player could invent joins, leaves and deaths for arbitrary names. These lines land in
    `player_events` and from there in session times, leaderboard and event feed.

    The dashboard's own cross-server injection has the same gap: the bridge sends `say
    [Project Zomboid] Bob: <text>`, and `loop_guard.mark_injected` protects only the chat ingest,
    not `parse_events`. A PZ player could create Minecraft events that way.
    """
    drv = MinecraftDriver()
    srv = GameServer(key="mc", game_type=GameType.minecraft)
    ev = {(e.username, e.kind) for e in drv.parse_events(srv, _mc_fixture())}

    # ALL THREE event kinds separately, not just `join`. Checking only the join pattern would stay
    # green with the anchor missing on the death pattern — and the death pattern is the most
    # dangerous of the three, because `what` alone reaches very far and Minecraft has dozens of
    # forms of this message.
    for name, kind in (("Friend2", "join"), ("Friend2", "death"), ("Friend2", "leave")):
        assert (name, kind) not in ev, (
            f"A chat text created a {kind} event for '{name}' — a player can invent joins, "
            "deaths and leaves for arbitrary names with it."
        )
    # The dashboard's own cross-server injection (`say [Project Zomboid] Bob: …`) too, in both
    # directions: neither the PZ speaker nor the named player may get an event.
    for name, kind in (("Bob", "death"), ("Steve", "join"), ("Friend3", "join")):
        if (name, kind) == ("Steve", "join"):
            continue  # Steve DID really join (own line) — not a violation here
        assert (name, kind) not in ev, (
            "the dashboard's own cross-server injection must not create player events"
        )
    assert not any(u == "ServerCtrl" for u, _ in ev), (
        "a plugin banner line ([ServerCtrl] was coded by ...) must not be a death"
    )
    # Cross-check: the real lines still create their events — otherwise the test would also be
    # green with a pattern that finds nothing at all.
    assert {("Steve", "join"), ("Steve", "death"), ("Steve", "leave")} <= ev


def test_mc_backs_up_the_world_named_in_server_properties(tmp_path):
    """`level-name` is locked precisely because it determines the `world_subdirs`.

    With `level-name=MyWorld` and a hard-coded `world`, the weekly run would pack three folders
    that do not exist; `create_backup` skips missing directories SILENTLY and returns an archive
    of the bare gzip header. That empty archive then becomes the "newest backup" — also as the
    mandatory rollback before mod apply and version change.

    Reachable without the config editor: `container_env` passes `extra.env` through unchecked,
    and itzg maps `LEVEL=<name>` to `level-name`.
    """
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "server.properties").write_text(
        "level-name=MyWorld\nmotd=x\n", encoding="utf-8")
    for d in ("MyWorld", "MyWorld_nether", "MyWorld_the_end"):
        (tmp_path / "data" / d).mkdir()
    srv = GameServer(key="mc", game_type=GameType.minecraft, data_dir=str(tmp_path))

    spec = MinecraftDriver().backup_spec(srv)
    assert spec.world_subdirs == ("MyWorld", "MyWorld_nether", "MyWorld_the_end")


def test_mc_falls_back_to_world_without_server_properties(tmp_path):
    """The default stays `world` — otherwise every server without readable properties would break."""
    for d in ("world", "world_nether", "world_the_end"):
        (tmp_path / "data" / d).mkdir(parents=True)
    srv = GameServer(key="mc", game_type=GameType.minecraft, data_dir=str(tmp_path))
    assert MinecraftDriver().backup_spec(srv).world_subdirs == ("world", "world_nether", "world_the_end")


def test_mc_names_only_the_one_world_folder_from_26_1_on(tmp_path):
    """Since Minecraft 26.1 `<world>_nether`/`<world>_the_end` no longer exist.

    On first start Paper moves all dimensions to
    `<world>/dimensions/minecraft/{overworld,the_nether,the_end}` (`WorldFolderMigration`) and
    removes the two side folders (observed on the jump 1.21.10 → 26.1.2).

    Hard-wired they would be dead paths from then on: `_pack` skips missing ones SILENTLY, so the
    archive would stay complete (`<world>` now contains everything) — but the "not a single world
    folder" guard would hang on a single entry, and the definition would claim folders that do
    not exist.
    """
    (tmp_path / "data" / "world" / "dimensions" / "minecraft" / "the_nether").mkdir(parents=True)
    srv = GameServer(key="mc", game_type=GameType.minecraft, data_dir=str(tmp_path))
    assert MinecraftDriver().backup_spec(srv).world_subdirs == ("world",)


def test_a_never_set_vs_password_still_appears(tmp_path):
    """A fresh `serverconfig.json` holds `Password` as JSON `null`.

    That fell through the scalar check in `config_fields`: the editor hid the field exactly when
    one wants to SET it. It went unnoticed because a write-only field looked empty anyway.
    """
    (tmp_path / "gamedata").mkdir()
    file = tmp_path / "gamedata" / "serverconfig.json"
    file.write_text(json.dumps({"Password": None, "ServerName": "Test"}), encoding="utf-8")
    srv = GameServer(key="vs", game_type=GameType.vintagestory, data_dir=str(tmp_path))
    drv = VintageStoryDriver()

    fields = {f.key: f for f in drv.config_fields(srv)}
    assert "Password" in fields, "the field is missing exactly when it is needed"
    assert fields["Password"].kind == "join_password" and fields["Password"].value == ""

    drv.set_config(srv, {"Password": "set-for-the-first-time"})
    assert json.loads(file.read_text(encoding="utf-8"))["Password"] == "set-for-the-first-time"


def test_other_null_fields_are_not_guessed(tmp_path):
    """The limit of the exception: for the other fields the type would be guessed, and a wrongly
    typed field writes nonsense into the file on the next save."""
    (tmp_path / "gamedata").mkdir()
    (tmp_path / "gamedata" / "serverconfig.json").write_text(
        json.dumps({"Password": None, "SomeNull": None, "ServerName": "Test"}), encoding="utf-8")
    srv = GameServer(key="vs", game_type=GameType.vintagestory, data_dir=str(tmp_path))
    fields = {f.key for f in VintageStoryDriver().config_fields(srv)}
    assert "Password" in fields and "SomeNull" not in fields


def test_vs_genbackup_with_a_stopped_container_is_no_incident(tmp_path, monkeypatch):
    """Without a run-state check every weekly run recorded a `backup_quiesce_failed` incident,
    because the container is stopped. The archive was always created correctly — pure noise,
    which eventually hides the one real failure. `minecraft.py` has the same check.
    """
    from app.services.drivers.base import QuiesceWarning

    srv = GameServer(key="vs", game_type=GameType.vintagestory, container_name="vintagestory",
                     data_dir=str(tmp_path), rcon_host="vs", rcon_port=11223)
    d = VintageStoryDriver()

    async def _dead(server, command, *, timeout=8.0):
        raise RuntimeError("RCON unreachable")

    d.console = _dead

    async def _stopped(name):
        return {"exists": True, "running": False, "status": "exited"}

    monkeypatch.setattr("app.services.docker_client.docker.state", _stopped)
    d.backup_spec(srv).pre_hook()          # no raise: the container is stopped

    async def _running(name):
        return {"exists": True, "running": True, "status": "running"}

    monkeypatch.setattr("app.services.docker_client.docker.state", _running)
    with pytest.raises(QuiesceWarning):    # if it runs, the failure IS real
        d.backup_spec(srv).pre_hook()


def test_vs_runner_reads_the_index_the_catalog_offers():
    """Catalog and runner image must mean the same source.

    The catalog offers what `stable.json` lists with a `linuxserver` build; the image under
    `images/vintagestory/` fetches at boot exactly what `VERSION` says. If they read different
    indexes or keys, the dashboard would offer versions the container cannot fetch — and an image
    that does not read `VERSION` at all makes every version change a silent no-op.
    """
    from pathlib import Path

    from app.services.version_catalog import VS_STABLE_URL

    script = (Path(__file__).resolve().parents[2] / "images" / "vintagestory" / "entrypoint.sh")
    text = script.read_text(encoding="utf-8")
    assert VS_STABLE_URL in text
    assert ".linuxserver" in text
    assert '"${VERSION:-}"' in text and "md5sum -c" in text


def test_vs_without_version_is_rejected_at_creation():
    """The runner image does not start without `VERSION`. If that only shows at rebuild, the
    container hangs in a restart loop, and the rollback uses the same env."""
    from fastapi import HTTPException

    from app.routers.servers import _check_version_required

    def _srv(game_type, version):
        return GameServer(key="x", display_name="x", game_type=game_type, container_name="x",
                          data_dir="/srv/gameservers/x", game_version=version)

    with pytest.raises(HTTPException) as ei:
        _check_version_required(_srv(GameType.vintagestory, " "))
    assert ei.value.status_code == 400
    _check_version_required(_srv(GameType.vintagestory, "1.22.7"))
    # Games with a default version in the image stay untouched.
    _check_version_required(_srv(GameType.minecraft, ""))


def test_vs_version_form_like_the_image_and_extra_env_does_not_pin():
    """A typo like `v1.22.8` must fail before stop and swap, and a `VERSION` in `extra.env` must
    not win against every version change."""
    d = VintageStoryDriver()
    assert d.version_valid("1.22.7") and d.version_valid("1.22.0-rc.3")
    for wrong in ("v1.22.8", " 1.22.7", "1.22", "", "../x"):
        assert not d.version_valid(wrong), wrong

    srv = GameServer(key="vs", game_type=GameType.vintagestory, game_version="1.22.8",
                     data_dir="/srv/gameservers/vintagestory",
                     extra=json.dumps({"env": {"VERSION": "1.21.6", "FOO": "bar"}}))
    assert d.container_env(srv, "x") == {"VERSION": "1.22.8", "FOO": "bar",
                                         "PUID": str(os.getuid()), "PGID": str(os.getgid())}


def test_vs_chatlog_without_recursive_glob(tmp_path):
    """The binary cache under `gamedata/server-bin` must never be searched."""
    logs = tmp_path / "gamedata" / "vs" / "Logs"
    logs.mkdir(parents=True)
    (logs / "server-chat.log").write_text("x")
    foreign = tmp_path / "gamedata" / "server-bin" / "1.22.7" / "Logs"
    foreign.mkdir(parents=True)
    (foreign / "server-chat.log").write_text("x")
    srv = GameServer(key="vs", game_type=GameType.vintagestory, data_dir=str(tmp_path).replace("\\", "/"))
    path = VintageStoryDriver().chat_log_path(srv)
    assert path and "server-bin" not in path and path.replace("\\", "/").endswith("vs/Logs/server-chat.log")


# ------------------------------------------------------------------ uid of the game process

def test_mc_and_vs_run_as_the_api_uid(monkeypatch):
    """The API writes into the same data directory (config editor, whitelist, mods). Both images
    default to uid 1000 and hand the data to that uid at every start, so an API built with
    another `APP_UID` would lose write access after the first start. The API's own ids go in,
    never a fixed 1000 (`UID`/`GID` for the itzg image, `PUID`/`PGID` for the runner)."""
    monkeypatch.setattr("os.getuid", lambda: 1234)
    monkeypatch.setattr("os.getgid", lambda: 1235)

    mc = MinecraftDriver().container_env(GameServer(key="mc", game_type=GameType.minecraft), "pw")
    assert mc["UID"] == "1234" and mc["GID"] == "1235"

    vs_srv = GameServer(key="vs", game_type=GameType.vintagestory, game_version="1.22.6")
    vs = VintageStoryDriver().container_env(vs_srv, "")
    assert vs["PUID"] == "1234" and vs["PGID"] == "1235"


def test_vs_uid_from_extra_env_wins():
    """An operator who set PUID in `extra.env` on purpose keeps it."""
    srv = GameServer(key="vs", game_type=GameType.vintagestory, game_version="1.22.6",
                     extra=json.dumps({"env": {"PUID": "4000", "PGID": "4001"}}))
    env = VintageStoryDriver().container_env(srv, "")
    assert env["PUID"] == "4000" and env["PGID"] == "4001"
