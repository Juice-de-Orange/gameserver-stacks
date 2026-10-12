"""The host layout comes from settings, not from literals in the code.

`DATA_ROOT` is the one directory that is bound into the API container, and `GAME_NETWORK` the
one Docker network the compose file creates. Everything that needs a default derives it from
those two.
"""
from app.config import Settings, settings
from app.models.server import GameServer, GameType
from app.routers.servers import ServerCreate
from app.services import container_spec


def _settings(**kw) -> Settings:
    return Settings(_env_file=None, **kw)


def test_allowed_roots_default_to_the_data_root():
    assert _settings().allowed_data_roots == [_settings().data_root]
    assert _settings(data_root="/data/games").allowed_data_roots == ["/data/games"]


def test_an_explicit_allowlist_wins():
    s = _settings(data_root="/data/games", allowed_data_roots=["/a", "/b/"])
    assert s.allowed_data_roots == ["/a", "/b/"]


def test_an_unusable_allowlist_falls_back_to_the_data_root_not_to_everything():
    """Fail-closed: an empty or "/" entry must never open the allowlist."""
    s = _settings(data_root="/data/games", allowed_data_roots=["", "/", "relative"])
    assert s.allowed_data_roots == ["/data/games"]


def test_new_servers_join_the_configured_network(monkeypatch):
    monkeypatch.setattr(settings, "game_network", "my-games")
    assert GameServer(key="x1", game_type=GameType.pz).network == "my-games"
    assert ServerCreate(key="x1", game_type=GameType.pz).network == "my-games"


def test_a_row_without_network_is_created_on_the_configured_network(monkeypatch):
    monkeypatch.setattr(settings, "game_network", "my-games")
    monkeypatch.setattr(settings, "allowed_data_roots", ["/srv/gameservers"])
    srv = GameServer(
        key="pz-1", game_type=GameType.pz, container_name="pz-1",
        data_dir="/srv/gameservers/pz-1", image_repo="repo", image_tag="1", network="",
    )
    payload = container_spec.build_create_payload(srv, "secret")
    assert payload["HostConfig"]["NetworkMode"] == "my-games"


def test_list_settings_accept_json_and_comma_separated_values(monkeypatch):
    """`ALLOWED_DATA_ROOTS=/a,/b` used to abort the start with a JSON decoding traceback."""
    monkeypatch.setenv("ALLOWED_DATA_ROOTS", "/srv/gameservers, /mnt/games")
    monkeypatch.setenv("TRUSTED_PROXIES", "console-web,10.0.0.0/8")
    s = _settings()
    assert s.allowed_data_roots == ["/srv/gameservers", "/mnt/games"]
    assert s.trusted_proxies == ["console-web", "10.0.0.0/8"]

    monkeypatch.setenv("ALLOWED_DATA_ROOTS", '["/srv/gameservers","/mnt/games"]')
    monkeypatch.setenv("TRUSTED_PROXIES", '["192.0.2.5"]')
    s = _settings()
    assert s.allowed_data_roots == ["/srv/gameservers", "/mnt/games"]
    assert s.trusted_proxies == ["192.0.2.5"]

    monkeypatch.setenv("ALLOWED_DATA_ROOTS", "")
    monkeypatch.setenv("TRUSTED_PROXIES", "")
    s = _settings()
    assert s.allowed_data_roots == [s.data_root]
    assert s.trusted_proxies == []


def test_a_broken_list_setting_names_the_variable(monkeypatch):
    import pytest

    monkeypatch.setenv("ALLOWED_DATA_ROOTS", '["/srv/gameservers"')
    with pytest.raises(ValueError, match="ALLOWED_DATA_ROOTS"):
        _settings()


def test_trusted_proxies_default_to_nobody_and_reject_garbage():
    import pytest

    assert _settings().trusted_proxies == []
    for bad in (["10.0.0.0/33"], ["10.0.0.300"], ["not a host"], ["http://proxy"]):
        with pytest.raises(ValueError, match="TRUSTED_PROXIES"):
            _settings(trusted_proxies=bad)
