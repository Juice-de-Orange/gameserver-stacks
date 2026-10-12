"""Arma Reforger driver: acemod env/binds, JSON config (nested + list fields + secret masking),
mods, moderation (name→#players ID, raise when offline), auto-seed, crossplay default, honest no-ops.

DB/RCON-free: `console` is stubbed (fixture `#players`/`#ban list`), async via `asyncio.run`.
"""
import asyncio
import json
import os
import subprocess
import types

import pytest

from app.models.server import GameServer, GameType
from app.services.drivers.reforger import ReforgerDriver, parse_reforger_players

PLAYERS_OUT = "ID  Name          GUID\n0   Alice         abc123\n1   Bob McTest    def456\n"
BAN_LIST = "0  Bob McTest  perm  griefing\n"

SRV_NS = types.SimpleNamespace(
    key="rf", data_dir="/tmp", display_name="RF", extra="{}", rcon_host="rf", rcon_port=19999
)


def _srv(data_dir="/srv/gameservers/reforger/rf", **over):
    base = dict(key="rf", game_type=GameType.arma_reforger, data_dir=data_dir, display_name="RF", rcon_port=19999)
    base.update(over)
    return GameServer(**base)


def _capture(players=PLAYERS_OUT, ban_list=BAN_LIST):
    d = ReforgerDriver()
    calls: list[str] = []

    async def fake(server, command, *, timeout=8.0):
        calls.append(command)
        if command == "#players":
            return players
        if command == "#ban list":
            return ban_list
        return "ok"

    d.console = fake
    return d, calls


# ------------------------------------------------------------------ Base / container (acemod)

def test_capabilities_honest():
    caps = ReforgerDriver().capabilities()
    # `versions` is included: the tab shows no selection list but information (running game
    # version + pinned image).
    assert {"console", "players", "events", "config", "mods", "backup", "versions"} <= caps
    assert "announce" not in caps


def test_container_env_and_binds_acemod():
    env = ReforgerDriver().container_env(_srv(), "secret")
    assert env["ARMA_CONFIG"] == "config.json"  # acemod reads the mounted file UNCHANGED
    assert env["RCON_PASSWORD"] == "secret"      # defensive (enables the RCON port)
    env2 = ReforgerDriver().container_env(_srv(extra=json.dumps({"env": {"FOO": "bar"}})), "x")
    assert env2["FOO"] == "bar"
    assert ReforgerDriver().container_binds(_srv()) == [
        "/srv/gameservers/reforger/rf/config:/reforger/Configs",
        "/srv/gameservers/reforger/rf/profile:/home/profile",
        "/srv/gameservers/reforger/rf/workshop:/reforger/workshop",
    ]


def test_the_game_and_steamcmd_are_bound_once_they_exist(tmp_path):
    """`install/` comes from `ensure_config`, `steamcmd/` once it holds the image's SteamCMD.
    A container created before either existed keeps its old binds on a recreate: an empty
    `/reforger` would download the whole game inside the boot check's window."""
    srv = _srv(data_dir=str(tmp_path))
    d = ReforgerDriver()
    assert not any(b.endswith((":/reforger", ":/steamcmd")) for b in d.container_binds(srv))

    d.ensure_config(srv, "rconpw")
    assert d.container_binds(srv)[0] == f"{tmp_path}/install:/reforger"
    assert not any(b.endswith(":/steamcmd") for b in d.container_binds(srv))

    (tmp_path / "steamcmd").mkdir()
    (tmp_path / "steamcmd" / "steamcmd.sh").write_text("#!/bin/sh\n", encoding="utf-8")
    assert d.container_binds(srv)[-1] == f"{tmp_path}/steamcmd:/steamcmd"
    assert d.image_seed_dirs(srv) == [("/steamcmd", f"{tmp_path}/steamcmd")]


def test_the_boot_proof_is_an_rcon_answer():
    """"Running for three rounds" reported a recreate as completed while SteamCMD was still
    downloading the game; `verify_boot` now waits for `#players` to answer."""
    assert ReforgerDriver().players_usable_as_boot_proof(_srv()) is True


def test_backup_spec_also_backs_up_the_archived_mode_slots():
    """`profile/` holds only the LIVE save. The saves of the other game modes sit next to it in
    `save-slots/`; without it they would be in no backup at all, weekly or manual."""
    spec = ReforgerDriver().backup_spec(_srv())
    assert spec.data_dir == "/srv/gameservers/reforger/rf"
    assert spec.world_subdirs == ("profile", "save-slots")


# ------------------------------------------------------------------ Players / moderation

def test_parse_players_best_effort():
    assert parse_reforger_players(PLAYERS_OUT) == [("0", "Alice"), ("1", "Bob McTest")]
    assert parse_reforger_players("garbage") == []


def test_players_count_and_names():
    d, _ = _capture()
    assert asyncio.run(d.players(SRV_NS)) == {"count": 2, "names": ["Alice", "Bob McTest"]}


def test_moderation_builds_id_based_commands():
    d, calls = _capture()
    asyncio.run(d.kick(SRV_NS, "Alice"))
    assert "#kick 0" in calls

    d, calls = _capture()
    asyncio.run(d.ban(SRV_NS, "Bob McTest", "spam"))
    assert "#ban create 1 0 spam" in calls

    d, calls = _capture()
    asyncio.run(d.unban(SRV_NS, "Bob"))
    assert "#ban remove 0" in calls


def test_moderation_offline_target_raises_no_command():
    # offline → ValueError (→ router 400 → real error toast), NOT a fake success.
    d, calls = _capture()
    with pytest.raises(ValueError):
        asyncio.run(d.kick(SRV_NS, "Ghost"))
    with pytest.raises(ValueError):
        asyncio.run(d.ban(SRV_NS, "Ghost"))
    assert not any(c.startswith("#kick") or c.startswith("#ban create") for c in calls)


def test_moderation_spec():
    spec = ReforgerDriver().moderation_spec(SRV_NS)
    assert spec.kick and spec.ban and spec.unban
    assert not spec.whitelist and spec.access_levels == ()


def test_announce_and_save_are_noops():
    d, calls = _capture()
    assert asyncio.run(d.announce(SRV_NS, "hi")) is None
    assert asyncio.run(d.save(SRV_NS)) is None
    assert calls == []


def test_safe_console_commands():
    assert ReforgerDriver().safe_console_commands() == frozenset({"#players", "#roles"})


# ------------------------------------------------------------------ Config (nested + secrets + lists)

def test_config_fields_and_set_config_roundtrip(tmp_path):
    cfg = tmp_path / "config"
    cfg.mkdir()
    (cfg / "config.json").write_text(
        json.dumps(
            {
                "game": {"name": "Old", "maxPlayers": 8, "password": "joinpw", "gameProperties": {"battlEye": True}},
                "rcon": {"password": "rconpw"},
            }
        )
    )
    srv = _srv(data_dir=tmp_path.as_posix())
    d = ReforgerDriver()

    fields = {f.key: f for f in d.config_fields(srv)}
    assert fields["game.name"].value == "Old"
    assert fields["game.maxPlayers"].kind == "int" and fields["game.maxPlayers"].value == "8"
    assert fields["game.gameProperties.battlEye"].value == "true"  # bool lowercased
    # `game.password` is the invitation and therefore VISIBLE; `rcon.password` is the control
    # channel and stays read-only masked. The difference is one of meaning, not technique:
    # whoever reads RCON has the full console.
    assert fields["game.password"].kind == "join_password"
    assert fields["game.password"].value == "joinpw"
    assert fields["rcon.password"].env_managed and fields["rcon.password"].value == "••••••"
    assert "rconpw" not in json.dumps([f.value for f in d.config_fields(srv)])

    d.set_config(
        srv,
        {
            "game.name": "New",
            "game.maxPlayers": "24",
            "game.gameProperties.battlEye": "false",
            "game.password": "HACK",   # password field editable → gets set
            "rcon.password": "NOPE",   # env-managed → ignored
            "unknown.path": "x",       # unknown → ignored
        },
    )
    data = json.loads((cfg / "config.json").read_text())
    assert data["game"]["name"] == "New"
    assert data["game"]["maxPlayers"] == 24 and isinstance(data["game"]["maxPlayers"], int)
    assert data["game"]["gameProperties"]["battlEye"] is False
    assert data["game"]["password"] == "HACK"        # password field: new value written
    assert data["rcon"]["password"] == "rconpw"       # RCON read-only → unchanged
    assert "unknown" not in data


def test_list_fields_roundtrip(tmp_path):
    cfg = tmp_path / "config"
    cfg.mkdir()
    (cfg / "config.json").write_text(json.dumps({"game": {"admins": ["AAA"], "supportedPlatforms": ["PLATFORM_PC"]}}))
    srv = _srv(data_dir=tmp_path.as_posix())
    d = ReforgerDriver()

    fields = {f.key: f for f in d.config_fields(srv)}
    assert fields["game.admins"].kind == "list" and json.loads(fields["game.admins"].value) == ["AAA"]
    # JSON array string → array; empty/whitespace entries are dropped
    d.set_config(
        srv,
        {
            "game.admins": json.dumps(["AAA", "BBB", "  "]),
            "game.supportedPlatforms": json.dumps(["PLATFORM_PC", "PLATFORM_XBL"]),
        },
    )
    data = json.loads((cfg / "config.json").read_text())
    assert data["game"]["admins"] == ["AAA", "BBB"]
    assert data["game"]["supportedPlatforms"] == ["PLATFORM_PC", "PLATFORM_XBL"]


def test_the_admin_twin_stays_write_only(tmp_path):
    """The separation that matters.

    `game.password` is the invitation: visible. `game.passwordAdmin` is a CONTROL CHANNEL:
    whoever has it makes themselves admin in game via `#login` (which is why it is also in
    `elevated_config_keys`). It stays write-only, and an empty field there still means
    "unchanged".
    """
    cfg = tmp_path / "config"
    cfg.mkdir()
    (cfg / "config.json").write_text(json.dumps({"game": {"name": "S"}, "rcon": {}}))
    srv = _srv(data_dir=tmp_path.as_posix())
    d = ReforgerDriver()
    d.set_config(srv, {"game.password": "joinpw", "game.passwordAdmin": "adminpw"})

    fields = {f.key: f for f in d.config_fields(srv)}
    assert fields["game.password"].value == "joinpw"         # visible
    assert fields["game.passwordAdmin"].value == ""          # write-only

    # And the reverse check: an empty admin field does not clear, an empty join field does.
    d.set_config(srv, {"game.password": "", "game.passwordAdmin": ""})
    raw = json.loads((cfg / "config.json").read_text(encoding="utf-8"))
    assert raw["game"]["passwordAdmin"] == "adminpw"
    assert raw["game"]["password"] == ""


def test_config_fields_absent_field_defaults_visible(tmp_path):
    # Full editor: unseeded fields appear too (with default), so everything is editable.
    cfg = tmp_path / "config"
    cfg.mkdir()
    (cfg / "config.json").write_text(json.dumps({"game": {"name": "X"}}))
    fields = {f.key: f for f in ReforgerDriver().config_fields(_srv(data_dir=tmp_path.as_posix()))}
    assert fields["game.admins"].value == "[]"            # list empty
    assert fields["operating.aiLimit"].value == ""        # int missing → empty (not written)
    assert fields["game.crossPlatform"].value == "false"  # bool missing → false


# ------------------------------------------------------------------ Mods + Seed-Guards

def test_mods_and_scenario_roundtrip(tmp_path):
    cfg = tmp_path / "config"
    cfg.mkdir()
    (cfg / "config.json").write_text(json.dumps({"game": {"mods": []}}))
    srv = _srv(data_dir=tmp_path.as_posix())
    d = ReforgerDriver()

    d.set_mods(srv, [{"modId": "AAA", "name": "a"}, {"modId": "BBB"}, {"modId": "AAA"}])
    assert [m["modId"] for m in d.list_mods(srv)["mods"]] == ["AAA", "BBB"]

    d.set_scenario(srv, "{X}Missions/Y.conf")
    assert d.list_mods(srv)["scenarioId"] == "{X}Missions/Y.conf"

    d.patch_config(srv, [("game.gameProperties.VONCanTransmitCrossFaction", True)])
    data = json.loads((cfg / "config.json").read_text())
    assert data["game"]["gameProperties"]["VONCanTransmitCrossFaction"] is True


def test_mod_writers_raise_when_unseeded(tmp_path):
    srv = _srv(data_dir=tmp_path.as_posix())  # no config.json → no partial write
    d = ReforgerDriver()
    with pytest.raises(ValueError):
        d.set_mods(srv, [{"modId": "AAA"}])
    with pytest.raises(ValueError):
        d.set_scenario(srv, "{X}Missions/Y.conf")
    with pytest.raises(ValueError):
        d.patch_config(srv, [("game.gameProperties.battlEye", True)])
    assert not (tmp_path / "config" / "config.json").exists()  # nothing written


# ------------------------------------------------------------------ Auto-Seed / initial_config

def test_ensure_config_seeds_complete_and_idempotent(tmp_path):
    srv = _srv(data_dir=tmp_path.as_posix(), game_ports=json.dumps({"game": "2001/udp", "a2s": "17777/udp"}))
    d = ReforgerDriver()
    d.ensure_config(srv, "rconpw")
    p = tmp_path / "config" / "config.json"
    assert p.exists() and (tmp_path / "profile").is_dir() and (tmp_path / "workshop").is_dir()
    data = json.loads(p.read_text())
    assert data["rcon"]["password"] == "rconpw" and data["rcon"]["address"] == "0.0.0.0"
    # idempotent: do not overwrite an existing file
    data["game"]["name"] = "KEEP"
    p.write_text(json.dumps(data))
    d.ensure_config(srv, "other")
    assert json.loads(p.read_text())["game"]["name"] == "KEEP"


def test_initial_config_pc_only_crossplay_and_binds():
    srv = _srv(game_ports=json.dumps({"game": "2001/udp", "a2s": "17777/udp"}), rcon_port=19999)
    cfg = ReforgerDriver().initial_config(srv, "rconsecret")
    # crossplay PC-only → mods stay active (crossPlatform:true would allow PSN = mods off)
    assert cfg["game"]["crossPlatform"] is False and cfg["game"]["supportedPlatforms"] == ["PLATFORM_PC"]
    assert cfg["game"]["gameProperties"]["battlEye"] is True and cfg["game"]["admins"] == []
    # RCON/network on 0.0.0.0 (otherwise no dashboard access via gameserver-net)
    assert cfg["rcon"]["address"] == "0.0.0.0" and cfg["bindAddress"] == "0.0.0.0" and cfg["a2s"]["address"] == "0.0.0.0"
    assert cfg["rcon"]["password"] == "rconsecret" and cfg["bindPort"] == 2001 and cfg["a2s"]["port"] == 17777
    # default mode Freedom Fighters (Everon) with VON patch
    assert "CAFEBEEFF0CACC1A" in [m["modId"] for m in cfg["game"]["mods"]]
    assert cfg["game"]["scenarioId"].endswith("FreedomFighters/Everon.conf")
    assert cfg["game"]["gameProperties"]["VONCanTransmitCrossFaction"] is True


def test_initial_config_rejects_invalid_rcon_password():
    # Reforger schema: rcon.password ≤32, no spaces (otherwise boot crash loop).
    with pytest.raises(ValueError):
        ReforgerDriver().initial_config(_srv(), "a" * 33)
    with pytest.raises(ValueError):
        ReforgerDriver().initial_config(_srv(), "has space")


def test_overview_stats(tmp_path):
    cfg = tmp_path / "config"
    cfg.mkdir()
    (cfg / "config.json").write_text(json.dumps({"game": {"maxPlayers": 40, "mods": [{"modId": "A"}]}}))
    assert ReforgerDriver().overview_stats(_srv(data_dir=tmp_path.as_posix())) == {"max_players": 40, "mods_count": 1}


def test_active_mode_and_save_slots(tmp_path):
    cfg = tmp_path / "config"
    cfg.mkdir()
    (cfg / "config.json").write_text(
        json.dumps({"game": {"scenarioId": "{64B2F8D8059EE270}Missions/FreedomFighters/Everon.conf"}})
    )
    srv = _srv(data_dir=tmp_path.as_posix())
    d = ReforgerDriver()
    # active mode derived from scenarioId
    assert d.active_mode(srv) == "freedom_fighters"
    # live save (active mode) + one archived slot
    active_db = tmp_path / "profile" / "profile" / ".db"
    active_db.mkdir(parents=True)
    (active_db / "x").write_bytes(b"12345")
    slot_db = tmp_path / "save-slots" / "overthrow_eden" / ".db"
    slot_db.mkdir(parents=True)
    (slot_db / "y").write_bytes(b"1")
    res = d.save_slots(srv)
    assert res["active_mode"] == "freedom_fighters"
    assert res["slots"]["freedom_fighters"]["active"] is True
    assert res["slots"]["overthrow_eden"]["size"] == 1
    # the swap script carries the slot logic (root helper)
    s = d.save_swap_script()
    assert "SWAP_DONE" in s and "save-slots" in s and "$FROM" in s and "$TO" in s


def test_active_mode_is_None_when_the_scenario_matches_no_preset(tmp_path):
    """`game.scenarioId` is a freely editable text field in the config editor.

    If someone enters their own mission `.conf` there, it matches no catalogue preset and
    `active_mode()` returns `None`. The same holds for a missing or unreadable `config.json`.
    This is the trigger of the data loss path below, so it is pinned here explicitly.
    """
    cfg = tmp_path / "config"
    cfg.mkdir()
    srv = _srv(data_dir=tmp_path.as_posix())
    d = ReforgerDriver()

    assert d.active_mode(srv) is None                       # no config.json at all
    (cfg / "config.json").write_text(json.dumps({"game": {"scenarioId": ""}}))
    assert d.active_mode(srv) is None                       # empty field
    (cfg / "config.json").write_text(json.dumps({"game": {"scenarioId": "{XYZ}Missions/Custom.conf"}}))
    assert d.active_mode(srv) is None                       # set by hand, no preset
    (cfg / "config.json").write_text("{broken")
    assert d.active_mode(srv) is None                       # unreadable


# --- The swap script is REALLY executed -------------------------------------------------------
#
# Checking substrings ("SWAP_DONE" in s) is not enough: if `rm -rf "$A/.db" "$A/.save"` sits
# AFTER the `fi` of the backup block, an empty $FROM backs up nothing, deletes everything, and
# `echo SWAP_DONE` still runs, so the caller reports success.

def _swap(tmp_path, *, frm: str, to: str) -> subprocess.CompletedProcess:
    """Run the real script against a real directory (`/data` → tmp_path)."""
    script = ReforgerDriver().save_swap_script().replace("/data", tmp_path.as_posix())
    return subprocess.run(
        ["sh", "-c", script], capture_output=True, text=True,
        env={"FROM": frm, "TO": to, "PATH": os.environ.get("PATH", "/usr/bin:/bin")},
    )


def _put_live_save(tmp_path, *, db="world", save="meta") -> None:
    active = tmp_path / "profile" / "profile"
    active.mkdir(parents=True, exist_ok=True)
    if db is not None:
        (active / ".db").write_text(db, encoding="utf-8")
    if save is not None:
        (active / ".save").write_text(save, encoding="utf-8")


def test_swap_deletes_nothing_when_the_source_slot_is_unknown(tmp_path):
    _put_live_save(tmp_path)

    p = _swap(tmp_path, frm="", to="overthrow_eden")

    assert "SWAP_DONE" not in p.stdout, "data loss was reported as success"
    assert p.returncode != 0
    assert (tmp_path / "profile" / "profile" / ".db").read_text(encoding="utf-8") == "world"
    assert (tmp_path / "profile" / "profile" / ".save").exists()


def test_swap_also_backs_up_a_save_without_db(tmp_path):
    """A backup block keyed on `-e "$A/.db"` misses a slot that has only `.save`, while the
    next line deletes the file anyway."""
    _put_live_save(tmp_path, db=None, save="meta-only")

    p = _swap(tmp_path, frm="freedom_fighters", to="overthrow_eden")

    assert "SWAP_DONE" in p.stdout, p.stderr
    assert (tmp_path / "save-slots" / "freedom_fighters" / ".save").read_text(encoding="utf-8") == "meta-only"


def test_swap_the_good_case_backs_up_and_loads(tmp_path):
    """The cross-check: with a known source slot the switch must keep working."""
    _put_live_save(tmp_path, db="old-world", save="old-meta")
    target = tmp_path / "save-slots" / "overthrow_eden"
    target.mkdir(parents=True)
    (target / ".db").write_text("new-world", encoding="utf-8")

    p = _swap(tmp_path, frm="freedom_fighters", to="overthrow_eden")

    assert "SWAP_DONE" in p.stdout, p.stderr
    # the old save landed in the slot …
    assert (tmp_path / "save-slots" / "freedom_fighters" / ".db").read_text(encoding="utf-8") == "old-world"
    assert (tmp_path / "save-slots" / "freedom_fighters" / ".save").read_text(encoding="utf-8") == "old-meta"
    # … and the target save is live (as a COPY: the slot stays as a checkpoint)
    assert (tmp_path / "profile" / "profile" / ".db").read_text(encoding="utf-8") == "new-world"
    assert (target / ".db").exists()


def test_set_config_reports_an_unseeded_config_instead_of_staying_silent(tmp_path):
    """`server_ops.put_config` answers 200 and audits as soon as `set_config` returns.

    A silent early return (`data = self._load(server); if not data: return`) has no `except`
    branch, so `tests/test_write_errors_are_loud.py` (which looks for the silent `except`)
    cannot catch it structurally; this behaviour test pins it. The mod/scenario writers use
    `_load_or_raise` for the same reason.
    """
    srv = _srv(data_dir=str(tmp_path / "does-not-exist"))
    with pytest.raises(ValueError) as error:
        ReforgerDriver().set_config(srv, {"game.name": "New"})
    assert error.value.code == "config.reforger_not_initialised"


def test_a_hanging_name_resolution_does_not_stall_the_event_loop(monkeypatch):
    """A blocking name lookup would stall the ONE uvicorn worker, with no time limit at all.

    It must also sit inside the `asyncio.wait_for`, within the documented 8-second budget. If
    Docker's embedded resolver does not answer (exactly the situation re-resolving exists for),
    `resolv.conf`'s `timeout:5, attempts:2` apply and the whole API stalls for up to ten
    seconds, triggered by the stats poller every minute without any human involved.

    So this test does NOT measure the duration of the whole call (that depends on the executor
    shutdown and would look long even when correct) but what matters: **does anything else
    still run meanwhile?**

    On its evidential value: this test pins the guarantee going forward; the red-first evidence
    for this class of bug is `tests/test_event_loop.py`.
    """
    import threading
    import time

    from app.services import bercon

    release = threading.Event()

    def _hang(*a, **kw):
        release.wait(30)  # blocks: in a worker thread, not in the loop
        raise OSError("resolver not responding")

    monkeypatch.setattr(bercon.socket, "getaddrinfo", _hang)

    async def scenario():
        ticks = 0

        async def ticker():
            nonlocal ticks
            while True:
                await asyncio.sleep(0.01)
                ticks += 1

        t = asyncio.create_task(ticker())
        t0 = time.monotonic()
        try:
            with pytest.raises(bercon.BerconError, match="timed out"):
                await bercon.bercon_command("reforger-rf-1", 19999, "pw", "#players", timeout=0.5)
            return time.monotonic() - t0, ticks
        finally:
            t.cancel()
            release.set()  # release the thread, otherwise the executor shutdown hangs 30 s

    duration, ticks = asyncio.run(scenario())
    assert ticks > 5, (
        f"Only {ticks}x did anything else run during the name lookup; the event loop stood "
        "still, and with it every HTTP response and every WebSocket."
    )
    assert duration < 2, f"The call took {duration:.1f}s instead of hitting its 0.5 s timeout."


def test_swap_also_loads_a_slot_that_has_only_save(tmp_path):
    """The backup creates such slots (see `test_swap_also_backs_up_a_save_without_db`), so
    loading must not depend on `.db` either.

    A load condition like `[ -n "$TO" ] && [ -e "$S/$TO/.db" ]` loads nothing without `.db`,
    after `rm -rf "$A/.db" "$A/.save"` one line earlier has already run: the mode starts silently
    from zero, and the slot stays behind unfindable if `save_slots` also lists only slots with
    `.db`.
    """
    _put_live_save(tmp_path, db="current-world", save="current-meta")
    target = tmp_path / "save-slots" / "overthrow_eden"
    target.mkdir(parents=True)
    (target / ".save").write_text("meta-only", encoding="utf-8")

    p = _swap(tmp_path, frm="freedom_fighters", to="overthrow_eden")

    assert "SWAP_DONE" in p.stdout, p.stderr
    active = tmp_path / "profile" / "profile"
    assert (active / ".save").read_text(encoding="utf-8") == "meta-only", (
        "The slot had only .save and was therefore not loaded at all; the mode starts from zero."
    )
    # And the previous live save is backed up in the source slot, not lost.
    assert (tmp_path / "save-slots" / "freedom_fighters" / ".db").read_text(encoding="utf-8") == "current-world"
