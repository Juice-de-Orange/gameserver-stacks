import datetime as dt
from types import SimpleNamespace

from app.routers.server import _bucket
from app.services.player_parse import parse_players as _parse_players


def test_parse_players():
    out = "Players connected (2):\n-Alice\n-Bob"
    p = _parse_players(out)
    assert p["count"] == 2
    assert p["names"] == ["Alice", "Bob"]


def test_parse_players_empty():
    p = _parse_players("Players connected (0):")
    assert p["count"] == 0
    assert p["names"] == []


def _sample(i: int):
    return SimpleNamespace(
        at=dt.datetime(2026, 6, 1) + dt.timedelta(minutes=i),
        players=i,
        cpu_percent=1.0,
        mem_pct=50.0,
        running=True,
    )


def test_bucket_downsamples():
    rows = [_sample(i) for i in range(1000)]
    out = _bucket(rows, 240)
    assert 0 < len(out) <= 240
    assert set(out[0].keys()) == {"t", "players", "cpu", "mem_pct", "running"}


def test_bucket_passthrough_and_empty():
    rows = [_sample(i) for i in range(10)]
    assert len(_bucket(rows, 240)) == 10
    assert _bucket([], 240) == []
