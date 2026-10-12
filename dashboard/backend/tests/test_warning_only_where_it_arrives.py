"""A restart warning that nobody can receive is not scheduled either.

`driver.announce()` is a deliberate no-op for Valheim and Arma Reforger — these games have no way
to tell all players something (Valheim has no console at all). Scheduling warning jobs for them
anyway means `_broadcast` calls `announce`, which does nothing, night after night, while the
schedule page offers the lead-up time as if it took effect. Whoever relies on it pulls the server
out from under the players without notice — and no error ever shows it.

The answer comes from `capabilities()`, not from a list of game names — an eighth game answers the
question by itself.

**Cross-check:** without the query in `reload_jobs`, warning jobs appear for Valheim again.
"""
from __future__ import annotations

import pytest
from sqlmodel import Session

from app.models import RestartSchedule
from app.models.server import GameServer, GameType
from app.services import restarts


def _srv(game_type: GameType) -> GameServer:
    s = GameServer(key=game_type.value, game_type=game_type, container_name="c", data_dir="/x",
                   display_name="x", server_type="PAPER")
    s.id = 1
    return s


# Who can broadcast and who cannot — both come from the driver, not from this file.
#
# Avorion moved from the right list to the left one as soon as `/say` was measured and `announce`
# filled in, and got its restart warning back by itself, without anyone having to remember it.
# That is the proof this guard hangs at the right end: a rule tied to a list of game names would
# have stayed behind.
CAN = [GameType.pz, GameType.minecraft, GameType.vintagestory, GameType.factorio, GameType.avorion]
CANNOT = [GameType.valheim, GameType.arma_reforger]


@pytest.mark.parametrize("game_type", CAN)
def test_whoever_can_broadcast_gets_their_warning(game_type):
    possible, reason = restarts.can_announce(_srv(game_type))
    assert possible is True and reason == ""


@pytest.mark.parametrize("game_type", CANNOT)
def test_whoever_cannot_broadcast_says_so(game_type):
    possible, reason = restarts.can_announce(_srv(game_type))
    assert possible is False
    assert reason, "without a reason the UI would show only a disabled field"
    assert "restart itself" in reason, "the operator must know that the restart still runs"


def _jobs_for(monkeypatch, mem_engine, game_type) -> list[dict]:
    jobs: list[dict] = []
    monkeypatch.setattr(restarts.scheduler, "add_job", lambda func, trigger, **kw: jobs.append(kw))
    monkeypatch.setattr(restarts, "_clear_jobs", lambda: None)
    monkeypatch.setattr(restarts, "engine", mem_engine)
    monkeypatch.setattr(restarts.servers_registry, "resolve_server", lambda sid: _srv(game_type))
    with Session(mem_engine) as s:
        s.add(RestartSchedule(time_hhmm="05:00", days="daily", warn_minutes="5,1", enabled=True))
        s.commit()
    restarts.reload_jobs()
    return jobs


def test_without_broadcast_no_warning_job_is_created(monkeypatch, mem_engine):
    jobs = _jobs_for(monkeypatch, mem_engine, GameType.valheim)
    assert [j for j in jobs if "_warn_" not in j["id"]], "the restart itself must still run"
    assert not [j for j in jobs if "_warn_" in j["id"]], (
        "a warning job is created that cannot do anything"
    )


def test_with_broadcast_the_warning_jobs_are_created_as_before(monkeypatch, mem_engine):
    """Cross-check: the normal case must not be switched off along with it."""
    jobs = _jobs_for(monkeypatch, mem_engine, GameType.pz)
    assert len(([j for j in jobs if "_warn_" in j["id"]])) == 2  # "5,1"


def test_a_surviving_warning_job_does_not_end_silently(monkeypatch, caplog):
    """Second safety net: a job from an older registration says why it does nothing."""
    import asyncio

    monkeypatch.setattr(restarts.servers_registry, "resolve_server", lambda sid: _srv(GameType.valheim))
    with caplog.at_level("INFO"):
        asyncio.run(restarts._broadcast(5, sid=None))
    assert any("warning for" in r.getMessage() for r in caplog.records), (
        "the skipped broadcast leaves no trace"
    )
