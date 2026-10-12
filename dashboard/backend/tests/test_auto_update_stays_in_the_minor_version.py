"""The automatic version change runs hotfixes, and nothing else.

**Where the rule comes from.** A server pinned to 42.20.0 could not be joined for days: Steam
kept updating the clients, and 42.20.3 shipped with multiplayer changes. Exactly this case, a
hotfix WITHIN the minor version, is to be caught up automatically.

**And where the boundary comes from.** The boot proof in `version_manager` shows "the server
starts". It does NOT show "the world is intact" or "the workshop mods still work". On a jump to a
new minor version both are the normal case, and the automatic rollback does not kick in there,
because from its point of view everything went fine. So 42.20 → 42.21 is manual work, and this
file pins that down.

**The second half is the fail-closed rule**, the same as in `idle_autostop`: an unreachable RCON
means "unknown", not "empty". Whoever mixes the two up stops a server under running players, at
04:45 at night when nobody is watching.
"""
import asyncio
import json

from app.models.server import GameServer, GameType, ManagementMode
from app.services import auto_update
from app.services.drivers import get_driver


def _pz() -> GameServer:
    s = GameServer(key="pz-1", game_type=GameType.pz,
                   management_mode=ManagementMode.dashboard, container_name="pz-1",
                   data_dir="/srv/gameservers/pz-1", enabled=True,
                   extra=json.dumps({"servername": "servertest"}))
    s.id = 1
    return s


def _e(version, ref, *, current=False, newer=False, channel="stable"):
    return {"version": version, "ref": ref, "current": current, "newer": newer, "channel": channel}


# --------------------------------------------------------------- the line rule

def test_the_same_hotfix_branch_counts_as_the_same_minor_version():
    assert auto_update.same_minor_version("42.20.0", "42.20.3") is True


def test_the_next_minor_version_does_not_count():
    assert auto_update.same_minor_version("42.20.0", "42.21.0") is False


def test_the_next_major_version_does_not_count():
    assert auto_update.same_minor_version("42.20.0", "43.0.0") is False


# --------------------------------------------------------------- the selection

def test_the_highest_hotfix_wins():
    """42.20.0 is running, 42.20.2 and 42.20.3 are available."""
    entries = [
        _e("42.20.0", "42.20.0-release", current=True),
        _e("42.20.2", "42.20.2-release", newer=True),
        _e("42.20.3", "42.20.3-release", newer=True),
    ]
    assert auto_update.hotfix_candidate(entries)["ref"] == "42.20.3-release"


def test_the_next_minor_version_is_not_selected():
    entries = [
        _e("42.20.0", "42.20.0-release", current=True),
        _e("42.21.0", "42.21.0-release", newer=True),
    ]
    assert auto_update.hotfix_candidate(entries) is None


def test_a_hotfix_is_selected_even_when_a_new_line_stands_next_to_it():
    entries = [
        _e("42.20.0", "42.20.0-release", current=True),
        _e("42.20.3", "42.20.3-release", newer=True),
        _e("43.0.0", "43.0.0-release", newer=True),
    ]
    assert auto_update.hotfix_candidate(entries)["ref"] == "42.20.3-release"


def test_another_channel_is_not_selected():
    """Whoever is on `stable` is not pushed to `unstable` overnight."""
    entries = [
        _e("42.20.0", "42.20.0-release", current=True, channel="stable"),
        _e("42.20.3", "42.20.3-unstable", newer=True, channel="unstable"),
    ]
    assert auto_update.hotfix_candidate(entries) is None


def test_without_a_known_current_version_nothing_is_selected():
    """No `current` entry means: we do not know what we are starting from."""
    entries = [_e("42.20.3", "42.20.3-release", newer=True)]
    assert auto_update.hotfix_candidate(entries) is None


def test_without_a_newer_entry_there_is_no_candidate():
    entries = [_e("42.20.3", "42.20.3-release", current=True)]
    assert auto_update.hotfix_candidate(entries) is None


# --------------------------------------------------------------- fail-closed

class _Driver:
    """Dummy with the FULL `players()` contract: `count` AND `names`.

    A dummy that only returns `names` proves nothing when `is_empty` also reads `names`: both
    fit together and the bug goes unseen. The contract in `base.GameDriver.players` names `count`
    as the reliable figure.
    """

    def __init__(self, names, count=None):
        self._names = names
        self._count = count

    async def players(self, server):
        if self._names is _BROKEN:
            raise RuntimeError("RCON dead")
        if self._count is _MISSING:
            return {"names": self._names}
        return {"count": len(self._names) if self._count is None else self._count,
                "names": self._names}


_BROKEN = object()
_MISSING = object()


def test_a_dead_rcon_means_unknown_not_empty(monkeypatch):
    monkeypatch.setattr(auto_update, "get_driver", lambda s: _Driver(_BROKEN))
    assert asyncio.run(auto_update.is_empty(_pz())) is None


def test_no_player_means_empty(monkeypatch):
    monkeypatch.setattr(auto_update, "get_driver", lambda s: _Driver([]))
    assert asyncio.run(auto_update.is_empty(_pz())) is True


def test_one_player_means_not_empty(monkeypatch):
    monkeypatch.setattr(auto_update, "get_driver", lambda s: _Driver(["BobBuilds"]))
    assert asyncio.run(auto_update.is_empty(_pz())) is False


def test_an_empty_name_list_with_count_above_zero_means_NOT_empty(monkeypatch):
    """The most expensive way to answer this wrongly.

    `parse_avorion_players` returns count=2 with an EMPTY name list for a bare header line
    ("2 players online"), and `parse_factorio_players` itself states that the name list can be
    truncated. Read via `names` that would mean "server is empty", and this job then stops,
    backs up, swaps the image and restarts, under two running players.
    """
    monkeypatch.setattr(auto_update, "get_driver", lambda s: _Driver([], count=2))
    assert asyncio.run(auto_update.is_empty(_pz())) is False


def test_a_missing_player_count_means_unknown(monkeypatch):
    """No `count` in the response is UNKNOWN, not zero; `names` as a fallback would be the
    same bug on a small scale."""
    monkeypatch.setattr(auto_update, "get_driver", lambda s: _Driver([], count=_MISSING))
    assert asyncio.run(auto_update.is_empty(_pz())) is None


def _run(monkeypatch, *, empty, entries=None, server=None):
    """`check_server` with the catalogue clamped off and the change recorded."""
    changed: list[str] = []

    async def _entries(srv):
        return {"supported": True, "current": "42.20.0-release",
                "entries": entries if entries is not None else [
                    _e("42.20.0", "42.20.0-release", current=True),
                    _e("42.20.3", "42.20.3-release", newer=True),
                ]}

    async def _change(key, **kw):
        # Record BOTH: depending on the version source `change_version` expects `image_tag` OR
        # `game_version`.
        changed.append(kw.get("image_tag") or kw.get("game_version") or "")
        return {"status": "ok"}

    monkeypatch.setattr(auto_update.version_catalog, "entries_for", _entries)

    # A stopped server is not checked at all. The cases here mean a RUNNING server; otherwise
    # they would end up only checking that stopped servers are skipped.
    async def _running(srv):
        return False
    monkeypatch.setattr(auto_update, "_safely_stopped", _running)
    monkeypatch.setattr(auto_update.version_manager, "change_version", _change)
    monkeypatch.setattr(auto_update, "_incident", lambda *a, **k: None)

    async def _empty(srv):
        return empty
    monkeypatch.setattr(auto_update, "is_empty", _empty)
    asyncio.run(auto_update.check_server(server if server is not None else _pz()))
    return changed


def test_with_players_online_no_update_happens(monkeypatch):
    assert _run(monkeypatch, empty=False) == []


def test_with_an_unknown_player_count_no_update_happens(monkeypatch):
    """The most dangerous case, and the only one in which doing nothing is the right answer."""
    assert _run(monkeypatch, empty=None) == []


def test_on_an_empty_server_the_hotfix_is_applied(monkeypatch):
    assert _run(monkeypatch, empty=True) == ["42.20.3-release"]


def test_without_a_candidate_nothing_happens(monkeypatch):
    entries = [_e("42.20.3", "42.20.3-release", current=True)]
    assert _run(monkeypatch, empty=True, entries=entries) == []


def test_the_job_is_off_without_the_switch(monkeypatch):
    """Default OFF: "no auto-update" stays the default until someone visibly lifts it."""
    called: list[str] = []
    monkeypatch.setattr(auto_update.settings, "auto_update_enabled", False)
    monkeypatch.setattr(auto_update.servers_registry, "list_enabled",
                        lambda: called.append("registry") or [])
    asyncio.run(auto_update.auto_update_once())
    assert called == []


# --------------------------------------------------------------- who takes part at all

def _srv(key, game, **over):
    s = GameServer(key=key, game_type=game, management_mode=ManagementMode.dashboard,
                   container_name=key, data_dir=f"/srv/gameservers/{key}", enabled=True)
    s.id = 2
    for k, v in over.items():
        setattr(s, k, v)
    return s


def test_the_boundary_sits_on_the_driver_and_not_in_the_job(monkeypatch):
    """Who takes part is decided by `auto_update_limit`, not by a list in the job.

    What this test checks is the SEAM: a game with `none` is skipped, without the job having to
    know the game type. Minecraft and Vintage Story take part (their own case is further down).
    """
    from app.services.drivers.base import AutoUpdate

    assert _run(monkeypatch, empty=True, server=_srv("rf-1", GameType.arma_reforger)) == []
    assert get_driver(_srv("rf-1", GameType.arma_reforger)).auto_update_limit is AutoUpdate.none
    assert get_driver(_srv("mc", GameType.minecraft)).auto_update_limit is AutoUpdate.patch


def test_valheim_does_not_take_part_because_the_image_does_it_itself(monkeypatch):
    assert _run(monkeypatch, empty=True, server=_srv("vh", GameType.valheim)) == []


def test_reforger_does_not_take_part(monkeypatch):
    assert _run(monkeypatch, empty=True, server=_srv("rf-1", GameType.arma_reforger)) == []


def test_factorio_catches_up_its_hotfixes(monkeypatch):
    """Here it is necessary, not convenient: Factorio compares the client version EXACTLY, so a
    pinned server would lock out every updated client at once."""
    entries = [
        _e("2.0.75", "2.0.75", current=True),
        _e("2.0.77", "2.0.77", newer=True),
    ]
    changed = _run(monkeypatch, empty=True, entries=entries,
                   server=_srv("fac", GameType.factorio))
    assert changed == ["2.0.77"]


def test_factorio_does_not_jump_into_the_next_line(monkeypatch):
    """2.0 → 2.1 stays manual work. A downgrade after such a jump can damage the world for good,
    and the boot proof sees none of it."""
    entries = [
        _e("2.0.77", "2.0.77", current=True),
        _e("2.1.14", "2.1.14", newer=True),
    ]
    assert _run(monkeypatch, empty=True, entries=entries,
                server=_srv("fac", GameType.factorio)) == []


def test_avorion_catches_up_within_its_line(monkeypatch):
    entries = [
        _e("2.5.13.44140", "2.5.13.44140", current=True),
        _e("2.5.13.44200", "2.5.13.44200", newer=True),
    ]
    changed = _run(monkeypatch, empty=True, entries=entries,
                   server=_srv("av", GameType.avorion))
    assert changed == ["2.5.13.44200"]


# --------------------------------------------------------------- stopped servers stay silent

def test_a_stopped_server_is_not_checked_at_all(monkeypatch):
    """Otherwise every night brings the same noise.

    A stopped server would record "42.20.3 available but not applied: player count unknown"
    every night. Correct in content (RCON dead = unknown = touch nothing), but the entry looks
    like a problem and repeats daily.

    Updating it would be wrong anyway: `change_version` starts the server at the end.
    """
    changed, incidents = [], []

    async def _entries(srv):
        raise AssertionError("a stopped server must not even query the catalogue")

    async def _change(key, **kw):
        changed.append(kw)
        return {}

    async def _stopped(srv):
        return True

    monkeypatch.setattr(auto_update.version_catalog, "entries_for", _entries)
    monkeypatch.setattr(auto_update.version_manager, "change_version", _change)
    monkeypatch.setattr(auto_update, "_safely_stopped", _stopped)
    monkeypatch.setattr(auto_update, "_incident", lambda *a, **k: incidents.append(a))

    assert asyncio.run(auto_update.check_server(_pz())) is None
    assert changed == [] and incidents == [], "no update, and above all: no incident"


def test_an_unclear_state_is_checked_normally(monkeypatch):
    """The cautious side: if the state cannot be determined, it takes the normal path.

    There `is_empty` decides, and that comes out fail-closed. The other way round, an unreachable
    Docker proxy would be a reason to silently skip EVERY server.
    """
    class _Broken:
        async def state(self, name):
            raise RuntimeError("proxy gone")

    monkeypatch.setattr("app.services.docker_client.docker", _Broken())
    assert asyncio.run(auto_update._safely_stopped(_pz())) is False


def test_a_running_server_does_not_count_as_stopped(monkeypatch):
    class _Running:
        async def state(self, name):
            return {"exists": True, "running": True, "restarting": False}

    monkeypatch.setattr("app.services.docker_client.docker", _Running())
    assert asyncio.run(auto_update._safely_stopped(_pz())) is False


def test_a_restarting_server_does_not_count_as_stopped_either(monkeypatch):
    """`restarting` means Docker has it in hand right now. That is no standstill."""
    class _Restart:
        async def state(self, name):
            return {"exists": True, "running": False, "restarting": True}

    monkeypatch.setattr("app.services.docker_client.docker", _Restart())
    assert asyncio.run(auto_update._safely_stopped(_pz())) is False


def test_minecraft_and_vintagestory_take_part_PROVIDED_they_are_pinned(monkeypatch):
    """Both may catch up hotfixes of their line (`auto_update_limit = patch`).

    That only takes effect where there is a current version: a server without a set
    `game_version` deliberately runs on its image's default, and the job does not pin it
    overnight (own case below).
    """
    entries = [
        _e("1.21.10", "1.21.10", current=True),
        _e("1.21.11", "1.21.11", newer=True),
    ]
    for key, game in (("mc", GameType.minecraft), ("vs", GameType.vintagestory)):
        srv = _srv(key, game)
        srv.game_version = "1.21.10"          # pinned: the case in which the boundary applies
        changed = _run(monkeypatch, empty=True, entries=entries, server=srv)
        assert changed == ["1.21.11"], f"{key} does not catch up its hotfix"


def test_the_jump_into_the_next_line_stays_manual_work(monkeypatch):
    """Also for Minecraft: the world migrates there, and the boot proof sees none of it."""
    entries = [
        _e("1.21.10", "1.21.10", current=True),
        _e("1.22.0", "1.22.0", newer=True),
    ]
    assert _run(monkeypatch, empty=True, entries=entries,
                server=_srv("mc", GameType.minecraft)) == []


def test_an_unpinned_server_is_not_pinned_overnight(monkeypatch):
    """Vintage Story may run deliberately WITHOUT a set VERSION; the image then takes its own
    default. The VS driver states the reason in `container_env`: an empty game_version must NOT
    act as a version change (it could migrate the world).

    The trap: the job gets the current version via detect_version() from the LOG, finds a
    candidate with it, and `change_version(game_version=…)` creates the new container with
    VERSION set. `_direction("", "1.21.7")` is "unknown", so the downgrade latch does not apply.
    The deliberately unpinned server would have become a pinned one overnight, with a possible
    world migration the boot proof does not see.
    """
    entries = [
        _e("1.21.6", "1.21.6", current=True),
        _e("1.21.7", "1.21.7", newer=True),
    ]
    unpinned = _srv("vs", GameType.vintagestory)
    unpinned.game_version = ""
    assert _run(monkeypatch, empty=True, entries=entries, server=unpinned) == []


def test_a_pinned_server_still_applies_its_hotfix(monkeypatch):
    """The cross-check: otherwise the condition above would be a switch-off instead of a rule.

    With `game_version='1.21.10'` there is a current version, and a hotfix is a change between
    two KNOWN versions. That is exactly the difference.
    """
    entries = [
        _e("1.21.10", "1.21.10", current=True),
        _e("1.21.11", "1.21.11", newer=True),
    ]
    pinned = _srv("mc", GameType.minecraft)
    pinned.game_version = "1.21.10"
    assert _run(monkeypatch, empty=True, entries=entries, server=pinned) == ["1.21.11"]


def test_image_tag_servers_are_not_affected_by_the_rule(monkeypatch):
    """For PZ, Factorio and Avorion the image tag is the version; an empty `game_version` says
    nothing there about "pinned or not"."""
    entries = [
        _e("2.0.75", "2.0.75", current=True),
        _e("2.0.77", "2.0.77", newer=True),
    ]
    fac = _srv("fac", GameType.factorio)
    fac.game_version = ""          # meaningless for image_tag servers
    assert _run(monkeypatch, empty=True, entries=entries, server=fac) == ["2.0.77"]
