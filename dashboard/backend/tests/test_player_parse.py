"""parse_players as a shared util. The PZ driver uses the same implementation (no duplicate);
the routers resolve players through `driver.players()`, not directly."""
from app.services.drivers.pz import _parse_players as driver_pp
from app.services.player_parse import parse_players


def test_parse_players_basic():
    p = parse_players("Players connected (2):\n-Alice\n-Bob")
    assert p == {"count": 2, "names": ["Alice", "Bob"]}


def test_parse_players_empty():
    assert parse_players("Players connected (0):") == {"count": 0, "names": []}


def test_parse_players_no_match():
    # No counter, no names → robust zero values (no crash).
    assert parse_players("anything") == {"count": 0, "names": []}


def test_driver_shares_one_impl():
    # The PZ driver points to THE SAME function via an alias import (no duplicate).
    assert driver_pp is parse_players
