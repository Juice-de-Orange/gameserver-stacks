"""Which games an installation runs: seeding, the settings API and where the switch applies.

Someone who only wants Minecraft must never see, fetch or download anything of the other six
games — and an upgrade from before the switch existed must never hide a running server.
"""
import asyncio

import pytest
from fastapi import HTTPException
from sqlmodel import Session, select

from app.config import settings
from app.models import AuditLog, Role, User
from app.models.game_settings import GameSetting
from app.models.server import GameServer, GameType
from app.routers import servers as servers_router
from app.routers import settings_games
from app.services import game_settings

ADMIN = User(id=1, display_name="Admin", username="admin", pw_hash="x", role=Role.admin)


def _active(engine) -> set[str]:
    with Session(engine) as s:
        return {r.game_type for r in s.exec(select(GameSetting)).all() if r.active}


def _server(engine, key: str, game: GameType, enabled: bool = True) -> None:
    with Session(engine) as s:
        s.add(GameServer(key=key, game_type=game, enabled=enabled))
        s.commit()


# ------------------------------------------------------------------ GSS_GAMES and seeding

def test_gss_games_accepts_a_list_and_all():
    assert game_settings.parse_games(["minecraft", " Factorio ", ""]) == {GameType.minecraft, GameType.factorio}
    assert game_settings.parse_games(["all"]) == set(GameType)
    assert game_settings.parse_games([]) == set()


def test_a_typo_in_gss_games_stops_instead_of_running_nothing():
    with pytest.raises(ValueError, match="GSS_GAMES: 'minecarft'"):
        game_settings.parse_games(["minecarft"])


def test_the_setting_reads_a_comma_list(monkeypatch):
    from app.config import Settings

    monkeypatch.setenv("GSS_GAMES", "minecraft,factorio")
    assert Settings(_env_file=None).games == ["minecraft", "factorio"]


def test_a_fresh_install_without_gss_games_has_no_active_game(mem_engine, monkeypatch):
    monkeypatch.setattr(settings, "games", [])
    assert game_settings.seed(mem_engine) == []
    with Session(mem_engine) as s:
        assert len(s.exec(select(GameSetting)).all()) == len(GameType)   # one row per game
    assert _active(mem_engine) == set()


def test_a_fresh_install_activates_what_gss_games_names(mem_engine, monkeypatch):
    monkeypatch.setattr(settings, "games", ["minecraft"])
    game_settings.seed(mem_engine)
    assert _active(mem_engine) == {"minecraft"}


def test_an_upgrade_never_hides_a_server(mem_engine, monkeypatch):
    """Servers from before the switch existed: their games become active, enabled or not."""
    monkeypatch.setattr(settings, "games", [])
    _server(mem_engine, "pz-1", GameType.pz)
    _server(mem_engine, "vs-1", GameType.vintagestory, enabled=False)
    game_settings.seed(mem_engine)
    assert _active(mem_engine) == {"pz", "vintagestory"}


def test_after_the_first_seed_the_admin_decides_not_gss_games(mem_engine, monkeypatch):
    monkeypatch.setattr(settings, "games", ["minecraft"])
    game_settings.seed(mem_engine)
    with Session(mem_engine) as s:
        game_settings.set_active(s, GameType.minecraft, False)
        s.commit()
    monkeypatch.setattr(settings, "games", ["all"])
    game_settings.seed(mem_engine)                       # the next start
    assert _active(mem_engine) == set()


def test_a_game_added_later_gets_its_row_and_stays_off(mem_engine, monkeypatch):
    monkeypatch.setattr(settings, "games", ["all"])
    game_settings.seed(mem_engine)
    with Session(mem_engine) as s:
        s.delete(s.get(GameSetting, "avorion"))         # as if avorion were new in this release
        s.commit()
    game_settings.seed(mem_engine)
    with Session(mem_engine) as s:
        assert s.get(GameSetting, "avorion").active is False


# ------------------------------------------------------------------ the settings API

def test_everyone_signed_in_reads_the_list_in_enum_order(mem_engine, monkeypatch):
    monkeypatch.setattr(settings, "games", ["factorio"])
    game_settings.seed(mem_engine)
    with Session(mem_engine) as s:
        out = settings_games.get_games(session=s, _=ADMIN)
    assert [g["game_type"] for g in out["games"]] == [g.value for g in GameType]
    assert [g["game_type"] for g in out["games"] if g["active"]] == ["factorio"]


def test_put_is_partial_and_audited(mem_engine, monkeypatch):
    monkeypatch.setattr(settings, "games", ["minecraft"])
    game_settings.seed(mem_engine)
    body = settings_games.GamesUpdate(games={"factorio": True, "minecraft": True})
    with Session(mem_engine) as s:
        out = settings_games.put_games(body, session=s, user=ADMIN)
        actions = [(a.action, a.target) for a in s.exec(select(AuditLog)).all()]
    assert {g["game_type"] for g in out["games"] if g["active"]} == {"minecraft", "factorio"}
    assert actions == [("games.activate", "factorio")]   # minecraft did not change: no entry


def test_a_game_with_an_enabled_server_cannot_be_switched_off(mem_engine, monkeypatch):
    monkeypatch.setattr(settings, "games", ["minecraft", "factorio"])
    game_settings.seed(mem_engine)
    _server(mem_engine, "mc-1", GameType.minecraft)
    _server(mem_engine, "mc-old", GameType.minecraft, enabled=False)   # disabled: no blocker
    body = settings_games.GamesUpdate(games={"factorio": False, "minecraft": False})
    with Session(mem_engine) as s, pytest.raises(HTTPException) as ei:
        settings_games.put_games(body, session=s, user=ADMIN)
    assert ei.value.status_code == 409
    assert ei.value.detail["code"] == "games.has_servers"
    assert ei.value.detail["servers"] == ["mc-1"]
    assert ei.value.detail["params"] == {"game": "minecraft", "servers": "mc-1"}
    # All or nothing: factorio was free to go, but the request as a whole was refused.
    assert _active(mem_engine) == {"minecraft", "factorio"}


def test_an_unknown_game_is_refused_by_the_schema():
    with pytest.raises(ValueError):
        settings_games.GamesUpdate(games={"tetris": True})


def test_the_routes_need_a_sign_in_and_put_needs_an_admin(api_client):
    from app.auth.deps import get_current_user, require_admin
    from app.main import app
    from tests.route_walk import flat_routes

    assert api_client.get("/api/settings/games").status_code == 401
    assert api_client.put("/api/settings/games", json={"games": {}}).status_code == 401
    deps = {}
    for r in flat_routes(app):
        if r.path == "/api/settings/games":
            for m in r.methods:
                deps[m] = {d.call for d in r.dependant.dependencies}
    assert get_current_user in deps["GET"]
    assert require_admin in deps["PUT"]


# ------------------------------------------------------------------ where the switch applies

def test_presets_only_for_active_games(mem_engine, monkeypatch):
    monkeypatch.setattr(settings, "games", ["minecraft"])
    game_settings.seed(mem_engine)
    with Session(mem_engine) as s:
        out = servers_router.presets(ADMIN, session=s)
    assert [p["game_type"] for p in out["presets"]] == ["minecraft"]


def test_no_game_active_means_no_presets(mem_engine, monkeypatch):
    monkeypatch.setattr(settings, "games", [])
    game_settings.seed(mem_engine)
    with Session(mem_engine) as s:
        assert servers_router.presets(ADMIN, session=s)["presets"] == []


def test_a_server_of_an_inactive_game_is_not_created(mem_engine, monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "allowed_data_roots", [str(tmp_path)])
    monkeypatch.setattr(settings, "games", ["minecraft"])
    game_settings.seed(mem_engine)
    body = servers_router.ServerCreate(key="fx-1", game_type=GameType.factorio, container_name="fx-1",
                                       data_dir=str(tmp_path / "fx-1"))
    with Session(mem_engine) as s, pytest.raises(HTTPException) as ei:
        asyncio.run(servers_router.create_server(body, session=s, user=ADMIN))
    assert ei.value.status_code == 409
    assert ei.value.detail["code"] == "games.inactive"
    assert ei.value.detail["params"] == {"game": "factorio"}
    with Session(mem_engine) as s:
        assert s.exec(select(GameServer)).first() is None


def test_a_server_of_an_inactive_game_is_not_switched_on(mem_engine, monkeypatch):
    """Otherwise the rule "an inactive game has no enabled server" would have a back door."""
    monkeypatch.setattr(settings, "games", [])
    _server(mem_engine, "vs-1", GameType.vintagestory, enabled=False)
    with Session(mem_engine) as s:
        s.add(GameSetting(game_type="vintagestory", active=False))
        s.commit()
    with Session(mem_engine) as s, pytest.raises(HTTPException) as ei:
        asyncio.run(servers_router.update_server("vs-1", servers_router.ServerUpdate(enabled=True), session=s, user=ADMIN))
    assert ei.value.detail["code"] == "games.inactive"
    # Other edits of that row stay possible: it renders and can be renamed.
    with Session(mem_engine) as s:
        out = asyncio.run(servers_router.update_server(
            "vs-1", servers_router.ServerUpdate(display_name="Old world"), session=s, user=ADMIN
        ))
    assert out["display_name"] == "Old world"


def test_the_version_catalogue_skips_inactive_games(monkeypatch):
    from app.services import servers as servers_registry
    from app.services import version_catalog as vc

    rows = [
        GameServer(key="mc-1", game_type=GameType.minecraft, server_type="PAPER"),
        GameServer(key="fx-1", game_type=GameType.factorio),
    ]
    monkeypatch.setattr(servers_registry, "list_enabled", lambda: rows)
    monkeypatch.setattr(game_settings, "active_games", lambda session=None: {GameType.minecraft})
    assert [gt for gt, _ in vc._registered_targets()] == [GameType.minecraft]


def test_the_workshop_check_does_not_run_for_an_inactive_game(monkeypatch):
    from app.services import mod_check
    from app.services import servers as servers_registry

    reads = []
    monkeypatch.setattr(servers_registry, "list_enabled", lambda: reads.append(1) or [])
    monkeypatch.setattr(game_settings, "is_active", lambda game, session=None: False)
    asyncio.run(mod_check.scheduled_refresh())
    # Counted, not raised: the job swallows and logs its own errors.
    assert reads == []


def test_the_driver_of_an_inactive_game_still_resolves():
    """A row of an inactive game must render, not crash: activation filters lists only."""
    from app.services.drivers import get_driver

    for game in GameType:
        assert get_driver(GameServer(key="x", game_type=game)) is not None
