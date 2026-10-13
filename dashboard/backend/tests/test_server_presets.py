"""The "Add server" presets, the setup state of a new server and the env-name check.

A preset is only worth something if the row it produces works with the driver it is meant for.
These tests build the row the way the form does (`frontend/src/pages/AddServer.tsx`: ports into
`game_ports`, a port's `env` and the `env` values into `extra.env`, the `extra` values into
`extra`) and ask the driver what it makes of it.
"""
import asyncio
import json
import re
from pathlib import Path

import pytest
from fastapi import HTTPException
from sqlmodel import Session

from app.config import settings
from app.models import Role, User
from app.models.server import ConsoleKind, GameServer, GameType, ManagementMode
from app.routers import servers as servers_router
from app.services import server_presets
from app.services.drivers import get_driver
from app.services.drivers.reforger import ReforgerDriver

ADMIN = User(id=1, display_name="Admin", username="admin", pw_hash="x", role=Role.admin)


def _preset(game: GameType) -> server_presets.Preset:
    p = server_presets.preset_for(game)
    assert p is not None
    return p


def _body(p: server_presets.Preset, key: str, data_root: str, ports: dict[str, int] | None = None,
          secret: str = "") -> dict:
    """The request body the form sends for a preset, with optional port changes."""
    ports = ports or {}
    env: dict[str, str] = {}
    extra: dict = {}
    game_ports = {}
    for port in p.ports:
        number = ports.get(port.name, port.port)
        game_ports[port.name] = f"{number}/{port.proto}"
        if port.env:
            env[port.env] = str(number)
    for v in p.values:
        value = v.value or ("hunter22" if v.required else "")
        if v.target == "env":
            env[v.name] = value
        else:
            extra[v.name] = value
    if env:
        extra["env"] = env
    return {
        "key": key,
        "game_type": p.game_type,
        "display_name": key,
        "container_name": key,
        "data_dir": f"{data_root}/{key}",
        "image_repo": p.image_repo,
        "image_tag": p.image_tag,
        "console_kind": p.console_kind,
        "rcon_host": key if p.console_kind != ConsoleKind.none else "",
        "rcon_port": p.rcon_port,
        "rcon_password_env": p.secret_env,
        "rcon_password": secret,
        "game_ports": game_ports,
        "game_version": p.game_version,
        "server_type": p.server_type,
        "ram_mb": p.ram_mb,
        "heap_max_mb": p.heap_max_mb,
        "heap_min_mb": p.heap_min_mb,
        "extra": extra,
        # The form sends the accepted EULA (`Acknowledgement` "eula"); create refuses without it.
        "eula_accepted": any(a.id == "eula" for a in p.acknowledgements),
    }


def _activate(engine, *games: GameType) -> None:
    """Switch games on (`services/game_settings.py`): presets and create only serve active games."""
    from app.models.game_settings import GameSetting

    with Session(engine) as s:
        for g in games:
            s.add(GameSetting(game_type=g.value, active=True))
        s.commit()


def _row(body: dict) -> GameServer:
    data = dict(body)
    data["game_ports"] = json.dumps(data["game_ports"])
    data["extra"] = json.dumps(data["extra"])
    return GameServer(**data)


# ------------------------------------------------------------------ the presets themselves

def test_there_is_one_preset_per_game_type():
    assert sorted(p.game_type.value for p in server_presets.PRESETS) == sorted(g.value for g in GameType)


def test_the_endpoint_sends_the_validation_values_of_create(mem_engine):
    _activate(mem_engine, *GameType)
    with Session(mem_engine) as s:
        out = servers_router.presets(ADMIN, session=s)
    assert out["key_pattern"] == servers_router._KEY_RE.pattern
    assert out["data_root"] == settings.data_root
    assert out["network"] == settings.game_network
    mc = next(p for p in out["presets"] if p["game_type"] == "minecraft")
    assert mc["console_kind"] == "source_rcon"                       # plain JSON, no enums
    assert mc["acknowledgements"][0]["id"] == "eula"
    json.dumps(out)                                                  # serialisable as it is


@pytest.mark.parametrize("game", list(GameType))
def test_every_preset_is_accepted_by_create(game, mem_engine, tmp_path, monkeypatch):
    """The unchanged preset goes through `create_server`, including the driver's seeding."""
    monkeypatch.setattr(settings, "allowed_data_roots", [str(tmp_path)])
    _activate(mem_engine, game)
    p = _preset(game)
    body = servers_router.ServerCreate(**_body(p, "new-1", str(tmp_path)))
    with Session(mem_engine) as s:
        out = asyncio.run(servers_router.create_server(body, session=s, user=ADMIN))
    assert out["enabled"] is False                                   # created switched off
    assert out["management_mode"] == ManagementMode.dashboard
    assert out["image_repo"] == p.image_repo


@pytest.mark.parametrize("game", list(GameType))
def test_version_and_console_match_the_driver(game):
    p = _preset(game)
    driver = get_driver(GameServer(key="x", game_type=game))
    assert p.version_required == driver.version_required
    if p.version_required:
        assert driver.version_valid(p.game_version)
    caps = driver.capabilities()
    # A game without a console has no console port and no password to ask for.
    if p.console_kind == ConsoleKind.none:
        assert "console" not in caps and p.rcon_port == 0 and not p.secret_env
    else:
        assert p.rcon_port > 0 and p.secret_env


def test_secret_names_are_the_ones_in_the_env_template():
    template = (Path(__file__).resolve().parents[2] / ".env.example").read_text(encoding="utf-8")
    names = {p.secret_env for p in server_presets.PRESETS if p.secret_env}
    names |= {v.value for p in server_presets.PRESETS for v in p.values if v.target == "extra" and v.name.endswith("_env")}
    for name in names:
        assert re.search(rf"^{name}=", template, re.M), f"{name} is not in dashboard/.env.example"


# ------------------------------------------------------------------ ports: editable means the game listens there

def test_the_pz_password_help_names_the_dashboard_store():
    """Since A3 both PZ passwords can be stored in the dashboard, encrypted; the env variable wins
    while it is set. A help that only offers the variable sends the admin to the .env needlessly,
    and "Empty: no join password" is no longer true once one is stored."""
    values = {v.name: v.help for v in _preset(GameType.pz).values}
    for name in ("password_env", "admin_password_env"):
        help_text = values[name]
        assert "encrypted" in help_text and "wins" in help_text, name
    assert "Configuration" in values["password_env"]
    assert "edit page" in values["admin_password_env"]


def test_factorio_listens_on_a_changed_port():
    p = _preset(GameType.factorio)
    row = _row(_body(p, "fac", settings.data_root, ports={"game": 24010}))
    assert get_driver(row).container_env(row, "")["PORT"] == "24010"


def test_minecraft_gets_the_changed_port_as_server_port():
    p = _preset(GameType.minecraft)
    row = _row(_body(p, "mc", settings.data_root, ports={"game": 24011}))
    env = get_driver(row).container_env(row, "pw")
    assert env["SERVER_PORT"] == "24011"
    assert env["RCON_PORT"] == "25575" and env["TYPE"] == "PAPER" and env["VERSION"] == "26.2"
    assert env["MAX_MEMORY"] == "4096M" and env["EULA"] == "TRUE"


def test_valheim_listens_on_a_changed_port():
    p = _preset(GameType.valheim)
    row = _row(_body(p, "vh", settings.data_root, ports={"game": 24020, "a2s": 24021}))
    driver = get_driver(row)
    assert driver.container_env(row, "")["SERVER_PORT"] == "24020"
    assert driver._query_port(row) == 24021


def test_reforger_writes_changed_ports_into_its_config():
    p = _preset(GameType.arma_reforger)
    row = _row(_body(p, "rf", settings.data_root, ports={"game": 24030, "a2s": 24031}))
    cfg = ReforgerDriver().initial_config(row, "pw")
    assert cfg["bindPort"] == 24030 and cfg["publicPort"] == 24030 and cfg["a2s"]["port"] == 24031
    assert cfg["rcon"]["port"] == 19999


def test_project_zomboid_listens_on_changed_ports():
    """Both PZ ports were fixed although the image takes PORT and UDPPORT (the block passes
    them): a second installation on the same host could not run Project Zomboid at all."""
    p = _preset(GameType.pz)
    assert all(port.editable for port in p.ports)
    row = _row(_body(p, "pz", settings.data_root, ports={"game": 24040, "direct": 24041}))
    env = get_driver(row).container_env(row, "pw")
    assert env["PORT"] == "24040" and env["UDPPORT"] == "24041"


@pytest.mark.parametrize("game, name", [(GameType.vintagestory, "Vintage Story"), (GameType.avorion, "Avorion")])
def test_a_fixed_port_says_why_and_what_it_means(game, name):
    """The hint under a fixed port names the reason and the consequence: one such server per
    host, which a second installation on the same host runs into."""
    for port in _preset(game).ports:
        assert f"Only one {name} server per host" in port.help, port.name


@pytest.mark.parametrize("game", [GameType.vintagestory, GameType.avorion])
def test_fixed_ports_are_not_passed_to_the_container(game):
    """`editable=False` because no variable carries the port into the game — check that."""
    p = _preset(game)
    assert all(not port.editable and not port.env for port in p.ports)
    row = _row(_body(p, "x1", settings.data_root))
    env = get_driver(row).container_env(row, "pw")
    numbers = {str(port.port) for port in p.ports}
    assert not numbers & set(env.values())


def test_factorio_preset_is_the_rootless_image(monkeypatch):
    # The driver answers with the API process's own ids (`APP_UID`, 1000 in the image); pin them
    # so the answer does not depend on who runs pytest (root in the CI container).
    monkeypatch.setattr("os.getuid", lambda: 1000)
    monkeypatch.setattr("os.getgid", lambda: 1000)
    p = _preset(GameType.factorio)
    row = _row(_body(p, "fac", settings.data_root))
    assert get_driver(row).container_user(row) == "1000:1000"


# ------------------------------------------------------------------ env variable names

@pytest.mark.parametrize("name", ["MC_RCON_PW", "_X", "a1_b2"])
def test_valid_env_names_pass(name):
    servers_router._validate_env_names(name, {"admin_password_env": name})


@pytest.mark.parametrize(
    ("rcon_env", "extra"),
    [("hunter2 pw!", None), ("1ABC", None), ("MC-PW", None), ("", {"password_env": "s3cr3t pass"})],
)
def test_a_password_in_a_name_field_is_refused_without_being_echoed(rcon_env, extra):
    with pytest.raises(HTTPException) as ei:
        servers_router._validate_env_names(rcon_env, extra)
    assert ei.value.status_code == 400
    assert ei.value.detail["code"] == "server.secret_env_name_invalid"
    assert ei.value.detail["params"] == {}
    for value in (rcon_env, *(extra or {}).values()):
        if value:
            assert value not in ei.value.detail["message"]


def test_create_refuses_a_password_in_the_name_field(mem_engine, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "allowed_data_roots", [str(tmp_path)])
    body = _body(_preset(GameType.minecraft), "mc-1", str(tmp_path))
    body["rcon_password_env"] = "correct horse battery"
    with Session(mem_engine) as s, pytest.raises(HTTPException) as ei:
        asyncio.run(servers_router.create_server(servers_router.ServerCreate(**body), session=s, user=ADMIN))
    assert ei.value.detail["code"] == "server.secret_env_name_invalid"


# ------------------------------------------------------------------ setup state of a new server

class _Docker:
    def __init__(self, state=None, fail=False):
        self._state = state or {"exists": False, "running": False, "status": "missing"}
        self.fail = fail

    async def state(self, name):
        if self.fail:
            raise RuntimeError("proxy down")
        return self._state


def _stored(mem_engine, **over) -> None:
    base = dict(key="mc-1", game_type=GameType.minecraft, enabled=False, container_name="mc-1",
                rcon_password_env="MC_RCON_PW")
    base.update(over)
    with Session(mem_engine) as s:
        s.add(GameServer(**base))
        s.commit()


def _setup(mem_engine, key="mc-1") -> dict:
    with Session(mem_engine) as s:
        return asyncio.run(servers_router.setup_state(key, session=s, _=ADMIN))


def test_setup_of_a_disabled_row_without_container(mem_engine, monkeypatch):
    """The state the next-steps panel is for: created, switched off, no container yet."""
    monkeypatch.setattr(servers_router, "docker", _Docker())
    monkeypatch.delenv("MC_RCON_PW", raising=False)
    _stored(mem_engine)
    out = _setup(mem_engine)
    assert out["enabled"] is False
    assert out["container"] == {"exists": False, "running": False, "status": "missing"}
    assert out["create_command"] == "docker compose exec console-api python -m app.cli create-container mc-1"
    assert out["secret"] == {"source": "none", "env_name": "MC_RCON_PW", "env_set": False,
                             "stored": False, "unreadable": False}


def test_setup_reports_an_existing_container_and_a_set_variable(mem_engine, monkeypatch):
    monkeypatch.setattr(servers_router, "docker", _Docker({"exists": True, "running": False, "status": "created"}))
    monkeypatch.setenv("MC_RCON_PW", "value-never-sent")
    _stored(mem_engine)
    out = _setup(mem_engine)
    assert out["container"]["exists"] is True and out["container"]["status"] == "created"
    assert out["secret"]["env_set"] is True and out["secret"]["source"] == "env"
    assert "value-never-sent" not in json.dumps(out)


def test_setup_says_unknown_when_docker_cannot_be_asked(mem_engine, monkeypatch):
    monkeypatch.setattr(servers_router, "docker", _Docker(fail=True))
    _stored(mem_engine)
    out = _setup(mem_engine)
    assert out["container"]["exists"] is None
    assert "proxy down" in out["container"]["error"]


def test_setup_of_a_compose_row_offers_no_create_command(mem_engine, monkeypatch):
    monkeypatch.setattr(servers_router, "docker", _Docker())
    _stored(mem_engine, management_mode=ManagementMode.compose)
    assert _setup(mem_engine)["create_command"] == ""


def test_setup_of_an_unknown_key_is_404(mem_engine, monkeypatch):
    monkeypatch.setattr(servers_router, "docker", _Docker())
    with pytest.raises(HTTPException) as ei:
        _setup(mem_engine, "nope")
    assert ei.value.status_code == 404


def test_the_create_command_names_the_real_service_and_module():
    """`console-api` is the API service of the compose file, and `app.cli` knows the command."""
    compose = (Path(__file__).resolve().parents[2] / "docker-compose.yml").read_text(encoding="utf-8")
    assert re.search(r"^  console-api:$", compose, re.M)
    cli = (Path(__file__).resolve().parents[1] / "app" / "cli.py").read_text(encoding="utf-8")
    assert 'cmd == "create-container"' in cli
    assert servers_router.CREATE_CONTAINER_COMMAND.startswith("docker compose exec console-api python -m app.cli ")


def test_presets_and_setup_answer_before_the_key_route():
    """`/presets` must not be answered by `GET /{key}` (a key named `presets` → 404)."""
    from starlette.routing import Match

    from app.main import app
    from tests.route_walk import flat_routes

    def winner(path):
        scope = {"type": "http", "method": "GET", "path": path, "headers": [], "query_string": b"",
                 "root_path": "", "path_params": {}}
        return next(getattr(r, "name", "?") for r in flat_routes(app) if r.matches(scope)[0] == Match.FULL)

    assert winner("/api/servers/presets") == "presets"
    assert winner("/api/servers/mc-1/setup") == "setup_state"


# ------------------------------------------------------------------ Vintage Story runner image

def test_the_vs_runner_comes_from_ghcr_and_its_tag_from_a_setting():
    """The runner used to be a local-only name (`stacks-console/vintagestory-runner:1`) that every
    operator had to build by hand before the first Vintage Story server could start. It is
    published on GHCR now; the setting picks the tag (or a self-built name)."""
    from app.config import settings

    from app.services.server_presets import preset_for

    vs = preset_for(GameType.vintagestory)
    assert settings.vs_runner_image == "ghcr.io/juice-de-orange/stacks-console-vintagestory-runner:1"
    assert (vs.image_repo, vs.image_tag) == ("ghcr.io/juice-de-orange/stacks-console-vintagestory-runner", "1")
    assert "docker build" in vs.image_help, "building it yourself stays documented"


@pytest.mark.parametrize("ref, expected", [
    ("ghcr.io/o/runner:1", ("ghcr.io/o/runner", "1")),
    ("local/runner", ("local/runner", "")),
    ("registry.example.com:5000/runner", ("registry.example.com:5000/runner", "")),
    ("registry.example.com:5000/runner:2", ("registry.example.com:5000/runner", "2")),
    ("ghcr.io/o/runner@sha256:abc", ("ghcr.io/o/runner", "sha256:abc")),
])
def test_an_image_setting_is_split_like_image_ref_joins_it(ref, expected):
    from app.services.container_spec import image_ref
    from app.services.server_presets import _split_ref

    assert _split_ref(ref) == expected
    assert image_ref(*expected) == ref
