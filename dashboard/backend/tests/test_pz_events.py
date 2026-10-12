"""The PZ event parser against REAL log lines.

`tests/fixtures/pz_user_log.txt` is an anonymised excerpt from real server logs (names and
SteamIDs replaced, format unchanged). Patterns that are never run against real lines can be
aimed at the wrong source altogether: these lines never appear in the Docker log.

Traps covered on purpose:
* **Player names with spaces** (`user Player Two died at …`) — a `\\S+` pattern would capture
  "Player" and swallow the rest.
* A player who dies and rejoins right away (two `fully connected` in quick succession).
* Anti-cheat warning lines that contain the same player name but are **not** an event.
"""
import datetime as dt
import pathlib

from app.models.server import GameServer, GameType
from app.services.drivers.pz import PZDriver

FIXTURE = pathlib.Path(__file__).parent / "fixtures" / "pz_user_log.txt"


def _srv():
    return GameServer(key="pz-1", game_type=GameType.pz, container_name="pz-1")


def _events(text=None):
    return PZDriver().parse_events(_srv(), text if text is not None else FIXTURE.read_text(encoding="utf-8"))


def test_deaths_are_detected_with_coordinates():
    deaths = [e for e in _events() if e.kind == "death"]
    assert len(deaths) == 5
    death = next(e for e in deaths if e.username == "Player Three")
    assert (death.x, death.y, death.z) == (6714, 5323, 1)
    assert death.detail == "no PvP"
    assert death.at == dt.datetime(2026, 6, 17, 19, 42, 49, 240000)


def test_names_with_spaces_stay_complete():
    """The death line carries the name unquoted."""
    names = {e.username for e in _events() if e.kind == "death"}
    assert "Player Two" in names
    assert "Player" not in names        # not cut off at the space
    assert "Player One" in names


def test_join_and_leave_carry_the_steamid():
    ev = _events()
    joins = [e for e in ev if e.kind == "join"]
    leaves = [e for e in ev if e.kind == "leave"]
    assert len(joins) == 12 and len(leaves) == 12
    assert all(e.game_id.isdigit() and len(e.game_id) == 17 for e in joins)
    first = joins[0]
    assert (first.x, first.y, first.z) == (11910, 6814, 0)


def test_anticheat_warnings_produce_no_event():
    line = ('[23-07-26 09:21:19.092] Warning: player="Player One" server-option=AntiCheatPlayer '
             "anti-cheat=PlayerUpdate reason=PlayerUpdate counter=1/4 action=Kick.")
    assert _events(line) == []


def test_lines_without_timestamp_are_discarded():
    """A `now()` fallback would smuggle old fragments in as fresh events."""
    assert _events('76561198000000000 "Someone" fully connected (1,2,3).') == []


def test_level_ups_come_from_the_difference_of_two_snapshots():
    a = "[23-07-26 09:00:00.000] [76561198000000000][Ada][10,20,0][Cooking=0, Woodwork=1][Hours Survived: 1]."
    b = "[23-07-26 10:00:00.000] [76561198000000000][Ada][10,20,0][Cooking=2, Woodwork=3][Hours Survived: 2]."
    ev = [e for e in _events(a + "\n" + b) if e.kind == "level_up"]
    assert len(ev) == 1
    assert ev[0].username == "Ada"
    assert ev[0].at == dt.datetime(2026, 7, 23, 10, 0)
    assert ev[0].detail == "Cooking 0→2, Woodwork 1→3"


def test_the_first_snapshot_is_not_a_level_up():
    """Otherwise every login would be a "level-up from 0" and the leaderboard nonsense."""
    a = "[23-07-26 09:00:00.000] [76561198000000000][Ada][10,20,0][Cooking=5][Hours Survived: 1]."
    assert [e for e in _events(a) if e.kind == "level_up"] == []


def test_a_skill_drop_after_death_is_not_a_level_up():
    a = "[23-07-26 09:00:00.000] [76561198000000000][Ada][10,20,0][Cooking=5, Axe=2][Hours Survived: 9]."
    b = "[23-07-26 10:00:00.000] [76561198000000000][Ada][99,99,0][Cooking=0, Axe=0][Hours Survived: 0]."
    assert [e for e in _events(a + "\n" + b) if e.kind == "level_up"] == []


def test_the_new_character_after_death_reports_no_level_up():
    """After a death the fresh character has different values, some higher, some lower.
    Without this rule the parser reported "Carving 0→3" for someone who had just died."""
    a = ("[23-07-26 09:24:12.622] [76561198000000000][Ada][11910,6815,0]"
         "[Doctor=6, Fitness=5, Carving=0][Hours Survived: 0].")
    b = ("[23-07-26 09:28:08.496] [76561198000000000][Ada][660,9657,0]"
         "[Doctor=0, Fitness=4, Carving=3][Hours Survived: 0].")
    assert [e for e in _events(a + "\n" + b) if e.kind == "level_up"] == []


def test_two_players_are_not_mixed_up():
    a = "[23-07-26 09:00:00.000] [76561198000000001][Ada][1,1,0][Cooking=0][Hours Survived: 1]."
    b = "[23-07-26 09:10:00.000] [76561198000000002][Bo][2,2,0][Cooking=9][Hours Survived: 1]."
    c = "[23-07-26 09:20:00.000] [76561198000000001][Ada][1,1,0][Cooking=1][Hours Survived: 1]."
    ev = [e for e in _events("\n".join([a, b, c])) if e.kind == "level_up"]
    assert len(ev) == 1 and ev[0].username == "Ada" and ev[0].detail == "Cooking 0→1"


def test_login_and_died_lines_of_the_perklog_produce_no_duplicate_events():
    """`PerkLog` records Login/Died too, but the death is already in `user.txt`, with the PvP flag."""
    line = "[23-07-26 09:26:44.478] [76561198000000000][Ada][11910,6951,0][Died][Hours Survived: 1]."
    assert _events(line) == []


def test_the_source_is_files_not_the_docker_log(tmp_path):
    """`event_log_paths` is the point: these lines never appear in the Docker log."""
    logs = tmp_path / "data" / "Logs"
    logs.mkdir(parents=True)
    (logs / "2026-07-23_05-00_user.txt").write_text("x", encoding="utf-8")
    (logs / "2026-07-23_05-00_PerkLog.txt").write_text("y", encoding="utf-8")
    (logs / "2026-07-23_05-00_chat.txt").write_text("z", encoding="utf-8")

    srv = GameServer(key="pz", game_type=GameType.pz, container_name="pz", data_dir=str(tmp_path))
    paths = PZDriver().event_log_paths(srv)

    assert len(paths) == 2
    assert any(p.endswith("_user.txt") for p in paths)
    assert any(p.endswith("_PerkLog.txt") for p in paths)
    assert not any(p.endswith("_chat.txt") for p in paths)  # chat goes through the bridge, not here


def test_without_log_files_the_list_stays_empty(tmp_path):
    srv = GameServer(key="pz", game_type=GameType.pz, container_name="pz", data_dir=str(tmp_path))
    assert PZDriver().event_log_paths(srv) == []
