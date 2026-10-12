"""The curated PZ config editor works on the RIGHT server.

A global editor bound to one fixed PZ data folder would, with a second PZ server, let that
server's config page edit the files of the PRODUCTION server.

So this checks exactly what a look at the route does not show: that path and console target come
from the registry row passed in — and that other games are rejected instead of silently redirected.
"""
import json
import os

import pytest
from fastapi import HTTPException

from app.models.server import GameServer, GameType
from app.services.drivers.base import ConfigField
from app.routers import config as cfg
from app.services import ini_editor as ini
from app.services import sandbox as sb


def _srv(tmp_path, key="pz-test", servername="servertest", game=GameType.pz) -> GameServer:
    s = GameServer(
        key=key, game_type=game, container_name=key, data_dir=str(tmp_path),
        extra=json.dumps({"servername": servername}),
    )
    s.id = 7
    return s


def _create_files(tmp_path, servername="servertest", ini_content="PVP=true\nMaxPlayers=16\n"):
    d = tmp_path / "data" / "Server"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{servername}.ini").write_text(ini_content, encoding="utf-8")
    (d / f"{servername}_SandboxVars.lua").write_text(
        "SandboxVars = {\n    Zombies = 3,\n    XpMultiplier = 1.0,\n}\n", encoding="utf-8"
    )
    return d


# --- Path resolution -------------------------------------------------------------------------


def test_paths_follow_the_registry_row(tmp_path):
    srv = _srv(tmp_path)
    assert ini.ini_path(srv) == os.path.join(str(tmp_path), "data", "Server", "servertest.ini")
    assert ini.sandbox_path(srv).endswith(os.path.join("Server", "servertest_SandboxVars.lua"))
    # ...and NOT to the production path.
    assert "pz-1" not in ini.ini_path(srv)


def test_the_file_of_the_given_server_is_read(tmp_path):
    _create_files(tmp_path, ini_content="PVP=false\nMaxPlayers=99\n")
    srv = _srv(tmp_path)
    data = ini.get_ini_at(ini.ini_path(srv))
    assert data["MaxPlayers"] == "99"


def test_the_file_of_the_given_server_is_written(tmp_path):
    d = _create_files(tmp_path)
    srv = _srv(tmp_path)
    ini.set_ini_keys_at(ini.ini_path(srv), {"MaxPlayers": "24"})
    assert "MaxPlayers=24" in (d / "servertest.ini").read_text(encoding="utf-8")


def test_sandbox_reads_and_writes_per_server(tmp_path):
    d = _create_files(tmp_path)
    srv = _srv(tmp_path)
    assert sb.read_values(srv)["Zombies"] == "3"
    sb.write_values({"Zombies": "1"}, srv)
    assert "Zombies = 1" in (d / "servertest_SandboxVars.lua").read_text(encoding="utf-8")


# --- The gate ----------------------------------------------------------------------------------


# Derived from the enum instead of listed: a hand-written list would miss the next game — and
# exactly this gate prevents the silent fallback to pz-1.
@pytest.mark.parametrize("game", [g for g in GameType if g is not GameType.pz])
def test_other_games_are_rejected_instead_of_redirected(tmp_path, game):
    """The core: NO silent fallback to pz-1. A telling 400 instead."""
    with pytest.raises(HTTPException) as e:
        cfg._pz_only(_srv(tmp_path, key="mc", game=game))
    assert e.value.status_code == 400
    assert game.value in str(e.value.detail)


def test_pz_gets_through(tmp_path):
    srv = _srv(tmp_path)
    assert cfg._pz_only(srv) is srv


# --- The PZ driver (generic editor + join page) ----------------------------------------------


def test_driver_config_and_mods_follow_the_server_too(tmp_path):
    """`PZDriver.config_fields`/`set_config`/`list_mods` must use the given server.

    `/api/servers/{key}/config` (generic) and `/api/servers/{key}/join-info` run through them —
    with server-less variants a `PUT` on a second PZ server would write into the production `.ini`.
    """
    from app.services.drivers.pz import PZDriver

    d = _create_files(tmp_path, ini_content="PVP=true\nMods=AlphaMod\nWorkshopItems=123\n")
    srv = _srv(tmp_path)
    drv = PZDriver()

    assert any(f.key == "Mods" and f.value == "AlphaMod" for f in drv.config_fields(srv))
    assert drv.list_mods(srv) == {"workshop_items": ["123"], "mods": ["AlphaMod"]}

    drv.set_config(srv, {"PVP": "false"})
    assert "PVP=false" in (d / "servertest.ini").read_text(encoding="utf-8")


def test_the_env_bound_keys_go_to_the_registry_not_the_ini(tmp_path, monkeypatch, mem_engine):
    """The image writes Public/PublicName/Password from the container env on every boot —
    setting them in the `.ini` would be a change that vanishes without a trace at the next start.

    So they go to the place the image really reads — the registry — and the `.ini` becomes its
    copy at the next boot.
    """
    import json as _json

    from sqlmodel import Session, select

    from app.models.server import GameServer as Row
    from app.services.drivers.pz import PZDriver

    monkeypatch.setattr("app.db.engine", mem_engine)
    d = _create_files(tmp_path, ini_content="PVP=true\nPublic=true\n")
    srv = _srv(tmp_path)
    with Session(mem_engine) as s:
        s.add(srv)
        s.commit()
        s.refresh(srv)

    PZDriver().set_config(srv, {"Public": "false", "PublicName": "New name", "PVP": "false"})

    text = (d / "servertest.ini").read_text(encoding="utf-8")
    assert "Public=true" in text   # the .ini stays untouched (it has no effect there)
    assert "PVP=false" in text     # normal keys are still written there

    with Session(mem_engine) as s:
        stored = s.exec(select(Row).where(Row.key == srv.key)).first()
    assert stored.presence_visible is False
    # **`PublicName` stays out, and that is the core of this test.** It hangs on the same
    # registry value as `SERVERNAME`, which for PZ decides the file names AND the world folder
    # (`Saves/Multiplayer/<name>`). A "name in the server browser" field that switches the world
    # at the next recreate would be the costliest kind of misuse.
    assert _json.loads(stored.extra)["servername"] == "servertest", (
        "the world folder name was overwritten — a browser name field must never do that"
    )


def test_the_visibility_really_lands_in_the_container_env(tmp_path):
    """The proof: what is in the registry must arrive at the recreate."""
    from app.services.drivers.pz import PZDriver

    srv = _srv(tmp_path)
    srv.presence_visible = False
    assert PZDriver().container_env(srv, "rcon")["PUBLIC"] == "false"
    srv.presence_visible = True
    assert PZDriver().container_env(srv, "rcon")["PUBLIC"] == "true"


# --- Secrets do not leave the server ----------------------------------------------------------

def test_pz_holds_back_the_control_channel_not_the_invitation(tmp_path, monkeypatch):
    """`env_managed` means "read-only here" — it never held the value back.

    PZ masks `RCONPassword`, otherwise it goes out in plain text to everyone with
    `can_edit_sandbox`. **The join password is the named exception** — every fellow player knows
    it anyway, and the operator needs the information. The control channel stays closed: whoever
    reads `RCONPassword` has the full console as soon as they reach the internal network.

    The path differs from the other fields: `Password` in the `.ini` has no effect (the image
    overwrites it from the container env on every boot), so the displayed value comes from the
    registry and goes back there too.
    """
    import json

    from app.services import ini_editor as ini
    from app.services.drivers.pz import PZDriver

    path = tmp_path / "servertest.ini"
    path.write_text(
        "Public=true\nPublicName=servertest\nPassword=sits-in-the-ini\n"
        "RCONPassword=rcon-secret\nPVP=true\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(ini, "ini_path", lambda s: str(path))

    srv = _srv(tmp_path)
    srv.extra = json.dumps({"password": "join-from-the-registry"})
    fields = {f.key: f for f in PZDriver().config_fields(srv)}

    # Control channel: masked and read-only.
    assert fields["RCONPassword"].env_managed
    assert "rcon-secret" not in json.dumps([f.value for f in fields.values()])

    # Invitation: visible, editable, and from the registry — NOT from the .ini, because the value
    # there is the one the image wrote last.
    assert fields["Password"].kind == "join_password"
    assert fields["Password"].value == "join-from-the-registry"
    assert not fields["Password"].env_managed
    assert fields["Password"].reload == "recreate"

    # Non-secrets stay readable. `PublicName` and `Public` come from the REGISTRY instead of the
    # .ini — writing there would have no effect, because the image refills the file from the
    # container env on every boot.
    assert fields["PVP"].value == "true"
    assert fields["Public"].kind == "bool" and fields["Public"].reload == "recreate"
    # `PublicName` stays read-only: it hangs on the same value as the WORLD FOLDER name.
    assert fields["PublicName"].env_managed


def test_the_raw_editor_neither_shows_nor_destroys_the_passwords(tmp_path):
    """`GET /config/raw` returns the COMPLETE .ini, and `Config.tsx` loads it on every page view
    — unmasked, that is a plain-text leak.

    Masking alone is not enough — a save would otherwise write the dots into the file as the
    password. That is why the pair is reversible.
    """
    from app.services import ini_editor as ini

    original = (
        "# comment with Password=do-not-touch\n"
        "Public=true\n"
        "RCONPassword=rcon-secret\n"
        "Password=join-secret\n"
        "PVP=true\n"
        "PublicDescription=An = sign in the value\n"
    )

    masked = ini.mask_env_managed(original)
    assert "rcon-secret" not in masked and "join-secret" not in masked
    assert "RCONPassword=••••••" in masked
    assert "PVP=true" in masked
    assert "PublicDescription=An = sign in the value" in masked   # split only at the FIRST `=`
    assert "# comment with Password=do-not-touch" in masked   # comments untouched

    # The user changes something else and leaves the dots …
    edited = masked.replace("PVP=true", "PVP=false")
    restored = ini.restore_env_managed(edited, original)
    assert "RCONPassword=rcon-secret" in restored
    assert "Password=join-secret" in restored
    assert "PVP=false" in restored

    # … and even overwriting the dots does not change these four values: they come from the
    # Compose env and would be overwritten at the next boot anyway.
    tampered = masked.replace("RCONPassword=••••••", "RCONPassword=taken-over")
    assert "RCONPassword=rcon-secret" in ini.restore_env_managed(tampered, original)


def test_the_config_editor_writes_no_mod_keys(tmp_path):
    """`PUT /{key}/config` depends on `can_edit_sandbox`, not on `can_manage_mods`.

    `env_managed` alone covers only four PZ keys (Public/PublicName/Password/RCONPassword).
    Without a further block a config editor could empty `Mods`, `WorkshopItems` or `Map` past
    preflight, mandatory backup, world quarantine and boot proof — exactly the chain
    `services/mods/apply.py` exists for.

    The consequence: orphaned world cells without a map provider, no `reloadoptions`, so it only
    shows at the next restart — and then the server does not come up.

    The block sits in the DRIVER, not only in the router: `set_config` is reachable other ways too.
    """
    from app.models.server import GameServer, GameType
    from app.services import ini_editor as ini
    from app.services.drivers.pz import PZDriver

    srv = GameServer(key="pz-test", game_type=GameType.pz, data_dir=str(tmp_path),
                     extra=json.dumps({"servername": "servertest"}))
    ini_file = ini.ini_path(srv)
    os.makedirs(os.path.dirname(ini_file), exist_ok=True)
    with open(ini_file, "w", encoding="utf-8") as f:
        f.write("Mods=TownMod\nWorkshopItems=111\nMap=Newtown;Muldraugh, KY\nPVP=true\n")

    PZDriver().set_config(srv, {
        "Mods": "", "WorkshopItems": "", "Map": "Muldraugh, KY",   # blocked
        "PVP": "false",                                            # allowed
    })

    with open(ini_file, encoding="utf-8") as f:
        content = f.read()
    assert "Mods=TownMod" in content, "the mod list was emptied past the preflight"
    assert "WorkshopItems=111" in content
    assert "Map=Newtown;Muldraugh, KY" in content
    assert "PVP=false" in content, "normal sandbox keys must still get through"


# --- The raw editor writes the WHOLE file ----------------------------------------------------

def _quiet_raw_routes(monkeypatch):
    """Silence the three side effects of `put_raw`/`put_sandbox`: backup, RCON, audit."""
    async def _noop(*a, **k):
        return None

    monkeypatch.setattr(cfg, "_backup", _noop)
    monkeypatch.setattr(cfg, "_reload", _noop)
    monkeypatch.setattr(cfg, "record", lambda *a, **k: None)


def test_the_raw_editor_does_not_overwrite_an_intervening_change(tmp_path, monkeypatch):
    """The raw editor sends the COMPLETE file — without a check, whatever changed since loading
    would be gone.

    The GUI tab of the same page writes single keys into the same `.ini` via `PUT /config/ini`.
    The raw buffer in the browser stays at the revision of the page view, and saving it unchecked
    would silently drop the change just saved.

    The same gap hits a second browser tab, a second admin, `mods/apply` and the version change —
    so the check sits here and not in the frontend: only the server knows the real revision of
    the file.
    """
    import asyncio

    _quiet_raw_routes(monkeypatch)
    d = _create_files(tmp_path, ini_content="PVP=true\nMaxPlayers=16\n")
    srv = _srv(tmp_path)

    loaded = asyncio.run(cfg.get_raw(server=srv, _=None))
    assert loaded["revision"], "GET /config/raw must return the file revision"

    # Someone else (GUI tab, second admin, mods/apply) changes the file.
    ini.set_ini_keys_at(ini.ini_path(srv), {"PVP": "false"})

    # The old buffer wants to write the whole file back — with PVP=true.
    with pytest.raises(HTTPException) as e:
        asyncio.run(cfg.put_raw(
            cfg.RawIn(content=loaded["content"], revision=loaded["revision"]),
            server=srv, driver=None, session=None, user=None,
        ))
    assert e.value.status_code == 409
    assert "PVP=false" in (d / "servertest.ini").read_text(encoding="utf-8"), \
        "the foreign change was overwritten despite the 409"

    # With the current revision it goes through — the conflict does not lock permanently.
    fresh = asyncio.run(cfg.get_raw(server=srv, _=None))
    assert fresh["revision"] != loaded["revision"]
    asyncio.run(cfg.put_raw(
        cfg.RawIn(content=fresh["content"].replace("MaxPlayers=16", "MaxPlayers=24"),
                  revision=fresh["revision"]),
        server=srv, driver=None, session=None, user=None,
    ))
    text = (d / "servertest.ini").read_text(encoding="utf-8")
    assert "MaxPlayers=24" in text and "PVP=false" in text


def test_the_sandbox_raw_editor_checks_the_revision_too(tmp_path, monkeypatch):
    """SandboxVars has the same double access: quick settings and the raw text field sit on the
    same screen and write the same file."""
    import asyncio

    _quiet_raw_routes(monkeypatch)
    d = _create_files(tmp_path)
    srv = _srv(tmp_path)

    loaded = asyncio.run(cfg.get_sandbox(server=srv, _=None))
    sb.write_values({"Zombies": "1"}, srv)   # save the quick settings

    with pytest.raises(HTTPException) as e:
        asyncio.run(cfg.put_sandbox(
            cfg.RawIn(content=loaded["content"], revision=loaded["revision"]),
            server=srv, driver=None, session=None, user=None,
        ))
    assert e.value.status_code == 409
    assert "Zombies = 1" in (d / "servertest_SandboxVars.lua").read_text(encoding="utf-8")


def test_the_revision_depends_on_the_content_not_the_timestamp(tmp_path):
    """Cross-check: identical content → identical revision, one byte of difference → another.

    Without it there is no proof the check distinguishes anything — a stamp from the modification
    time would turn every save into a conflict, including one's own.
    """
    a = tmp_path / "a.ini"
    b = tmp_path / "b.ini"
    a.write_text("PVP=true\n", encoding="utf-8")
    b.write_text("PVP=true\n", encoding="utf-8")
    assert ini.file_revision(str(a)) == ini.file_revision(str(b))
    b.write_text("PVP=false\n", encoding="utf-8")
    assert ini.file_revision(str(a)) != ini.file_revision(str(b))


def test_reforger_admin_keys_require_the_moderation_permission(monkeypatch):
    """`game.admins` and `game.passwordAdmin` hand out game-master rights, not settings.

    Whoever is in `game.admins` opens the GM menu in a Game Master scenario and can spawn and
    control; whoever sets `game.passwordAdmin` becomes server admin via `#login`. As plain config
    fields behind `can_edit_sandbox` (the right to maintain game-world settings) a config editor
    could make themselves game master, past `can_moderate`.

    The seam sits at the DRIVER (`elevated_config_keys`), not as a list in the router: only the
    game knows which keys mean power.
    """
    from app.models import Role, User
    from app.models.server import GameServer, GameType
    from app.routers import server_ops
    from app.routers.server_ops import ConfigUpdate
    from app.services.drivers.reforger import ReforgerDriver

    srv = GameServer(key="rf-1", game_type=GameType.arma_reforger, data_dir="/tmp/rf")
    srv.id = 5
    drv = ReforgerDriver()
    written: list[dict] = []
    monkeypatch.setattr(ReforgerDriver, "set_config", lambda self, s, u: written.append(u))
    monkeypatch.setattr(ReforgerDriver, "config_fields", lambda self, s: [])
    monkeypatch.setattr(server_ops, "record", lambda *a, **k: None)

    def _user(**flags):
        u = User(display_name="x", username="x", pw_hash="x", role=Role.member)
        for f in ("can_edit_sandbox", "can_moderate"):
            setattr(u, f, flags.get(f, False))
        return u

    # Without the moderation permission: rejected, and NOTHING written.
    with pytest.raises(HTTPException) as ei:
        server_ops.put_config(
            ConfigUpdate(values={"game.admins": '["ME"]'}),
            server=srv, driver=drv, session=None, user=_user(can_edit_sandbox=True),
        )
    assert ei.value.status_code == 403
    assert ei.value.detail["code"] == "config.values_need_moderation"
    assert written == [], "written despite the 403"

    # The same for the #login password.
    with pytest.raises(HTTPException) as ei:
        server_ops.put_config(
            ConfigUpdate(values={"game.passwordAdmin": "secret"}),
            server=srv, driver=drv, session=None, user=_user(can_edit_sandbox=True),
        )
    assert ei.value.status_code == 403

    # Normal settings stay open for the config editor.
    server_ops.put_config(
        ConfigUpdate(values={"game.name": "New name"}),
        server=srv, driver=drv, session=None, user=_user(can_edit_sandbox=True),
    )
    assert written == [{"game.name": "New name"}]

    # And with the moderation permission it works.
    server_ops.put_config(
        ConfigUpdate(values={"game.admins": '["ME"]'}),
        server=srv, driver=drv, session=None, user=_user(can_edit_sandbox=True, can_moderate=True),
    )
    assert written[-1] == {"game.admins": '["ME"]'}


# --- The join password: visible, changeable, and honest about when it takes effect ----------

def test_an_expensive_field_raises_the_report(tmp_path, monkeypatch):
    """A single field can be more expensive than the rest of the editor.

    A Project Zomboid server re-reads its `.ini` while running (`live`) — but the join password
    sits in the container env and needs a RECREATE. Reporting the driver default here says "done"
    while the change has not arrived anywhere yet.
    """
    from app.routers.server_ops import _effective_from, _effective_from_fields
    from app.services.drivers.pz import PZDriver

    srv = _srv(tmp_path)
    d = PZDriver()
    monkeypatch.setattr(d, "config_fields", lambda s: [
        ConfigField(key="PVP", label="PVP"),
        ConfigField(key="Password", label="Join password", kind="join_password", reload="recreate"),
    ])

    # The driver default alone says "live" — PZ re-reads the .ini via `reloadoptions`.
    assert _effective_from(d)["config_reload"] == "live"
    # An ordinary field changes nothing about that...
    assert _effective_from_fields(d, srv, ["PVP"])["config_reload"] == "live"
    # ...the password does.
    result = _effective_from_fields(d, srv, ["PVP", "Password"])
    assert result["config_reload"] == "recreate" and result["restart_required"] is True
    # The page builds its own translated message from `config_reload`; no prose note travels.
    assert "note" not in result and "note" not in _effective_from(d)


def test_the_report_does_not_fail_on_an_unreadable_config(tmp_path, monkeypatch):
    """`put_config` has just written successfully — the response must not fall over here."""
    from app.routers.server_ops import _effective_from_fields
    from app.services.drivers.pz import PZDriver

    d = PZDriver()

    def broken(server):
        raise OSError("not readable")

    monkeypatch.setattr(d, "config_fields", broken)
    assert _effective_from_fields(d, _srv(tmp_path), ["Password"])["config_reload"] == "live"


def test_the_join_password_goes_to_the_registry_not_the_ini(tmp_path, monkeypatch, mem_engine):
    """The path is the point: in the `.ini` the value would have no effect.

    The PZ image writes the env-bound keys from the container env into the file on EVERY boot. A
    write there would look like success and be gone at the next start — exactly the kind of
    silent loss this project writes its guards against.
    """
    import json

    from sqlmodel import Session, select

    from app.models.server import GameServer as Row
    from app.services import ini_editor as ini
    from app.services.drivers.pz import PZDriver

    monkeypatch.setattr("app.db.engine", mem_engine)
    path = tmp_path / "servertest.ini"
    path.write_text("Password=old-from-the-image\nPVP=true\n", encoding="utf-8")
    monkeypatch.setattr(ini, "ini_path", lambda s: str(path))

    srv = _srv(tmp_path)
    with Session(mem_engine) as s:
        s.add(srv)
        s.commit()
        s.refresh(srv)

    PZDriver().set_config(srv, {"Password": "new-join", "PVP": "false"})

    with Session(mem_engine) as s:
        stored = s.exec(select(Row).where(Row.key == srv.key)).first()
    # Into the encrypted join slot (`services/secrets.py`), not as clear text into `extra`.
    from app.services import secrets
    assert "password" not in json.loads(stored.extra)
    assert secrets.resolve(stored, secrets.JOIN) == "new-join"
    # The .ini stays untouched for the password (it has no effect there anyway)...
    assert "old-from-the-image" in path.read_text(encoding="utf-8")
    # ...the normal keys are still written there.
    assert "PVP=false" in path.read_text(encoding="utf-8")


def test_an_emptied_password_opens_the_pz_server(tmp_path, monkeypatch):
    """Empty means empty — and then `container_env` deliberately sets no `PASSWORD` at all instead
    of writing an empty value."""
    import json

    from app.services.drivers.pz import PZDriver

    srv = _srv(tmp_path)
    srv.extra = json.dumps({"password": ""})
    assert "PASSWORD" not in PZDriver().container_env(srv, "rcon")


def test_the_curated_pz_editor_shows_the_join_password(tmp_path, monkeypatch, mem_engine):
    """PZ does not use the generic editor — without this path the field would never show there.

    `GET /config/ini` returns the curated `GUI_FIELDS` list; the password is deliberately appended
    instead of listed, so there is exactly ONE truth: the driver.
    """
    import asyncio
    import json as _json

    from sqlmodel import Session

    from app.routers import config as cfgr
    from app.services import ini_editor as ini

    monkeypatch.setattr("app.db.engine", mem_engine)
    path = tmp_path / "servertest.ini"
    path.write_text("Password=from-the-image\nPVP=true\n", encoding="utf-8")
    monkeypatch.setattr(ini, "ini_path", lambda s: str(path))

    srv = _srv(tmp_path)
    srv.extra = _json.dumps({"password": "join-from-the-registry"})
    with Session(mem_engine) as s:
        s.add(srv)
        s.commit()
        s.refresh(srv)
        srv.extra = _json.dumps({"password": "join-from-the-registry"})

    response = asyncio.run(cfgr.get_config(server=srv, _=None))
    fields = {f["key"]: f for f in response["fields"]}
    assert fields["Password"]["kind"] == "join_password"
    assert fields["Password"]["value"] == "join-from-the-registry"
    # And still: the RCON password does not appear in the curated list at all.
    assert "RCONPassword" not in fields


def test_get_config_reports_a_missing_ini_as_404_not_500(tmp_path):
    """A PZ server whose `.ini` has not been written yet (never started, not seeded) used to
    surface as an unhandled FileNotFoundError, i.e. a 500. The page can explain a 404."""
    import asyncio

    srv = _srv(tmp_path)  # no files below `data/Server`
    with pytest.raises(HTTPException) as exc:
        asyncio.run(cfg.get_config(server=srv, _=None))
    assert exc.value.status_code == 404
    assert exc.value.detail["code"] == "config.file_missing"
    assert exc.value.detail["params"] == {"file": "servertest.ini"}


@pytest.mark.parametrize("endpoint, file", [
    ("get_raw", "servertest.ini"),
    ("get_sandbox", "servertest_SandboxVars.lua"),
    ("get_sandbox_presets", "servertest_SandboxVars.lua"),
])
def test_the_other_readers_report_a_missing_file_as_404_not_500(tmp_path, endpoint, file):
    """Same state as above through the raw editor and the two SandboxVars readers: before, an
    unhandled FileNotFoundError (500)."""
    import asyncio

    srv = _srv(tmp_path)  # no files below `data/Server`
    with pytest.raises(HTTPException) as exc:
        asyncio.run(getattr(cfg, endpoint)(server=srv, _=None))
    assert exc.value.status_code == 404
    assert exc.value.detail["code"] == "config.file_missing"
    assert exc.value.detail["params"] == {"file": file}


def test_a_write_against_a_missing_file_is_404_not_500(tmp_path):
    """The revision check of the raw editors reads the file first — same 404 there."""
    with pytest.raises(HTTPException) as exc:
        cfg._check_revision(ini.sandbox_path(_srv(tmp_path)), "0" * 16)
    assert exc.value.status_code == 404
    assert exc.value.detail["code"] == "config.file_missing"
