"""resolve_secret precedence (env > literal). No database, via monkeypatch."""
from app.models.server import GameServer, GameType
from app.services.servers import resolve_secret


def _srv(**over) -> GameServer:
    base = dict(key="pz-1", game_type=GameType.pz, rcon_password_env="PZ_RCON_PW", rcon_password="literal")
    base.update(over)
    return GameServer(**base)


def test_env_var_wins(monkeypatch):
    monkeypatch.setenv("PZ_RCON_PW", "from-env")
    assert resolve_secret(_srv()) == "from-env"


def test_literal_fallback(monkeypatch):
    monkeypatch.delenv("PZ_RCON_PW", raising=False)
    assert resolve_secret(_srv()) == "literal"


def test_no_env_name_uses_literal(monkeypatch):
    monkeypatch.setenv("PZ_RCON_PW", "ignored")  # must not apply, the env name is empty
    assert resolve_secret(_srv(rcon_password_env="", rcon_password="lit")) == "lit"
