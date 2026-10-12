"""Approving an account link is the only authorisation check in the whole flow.

`POST /api/links/claim` accepts any typed name — there is no check whether that name is already
verified on ANOTHER account. So the whole access decision rests on the admin's click on "Verify",
and `POST /api/links/{id}/verify` must do more than set a flag.

What a wrong approval opens: `GET /api/me/stats` filters only by the verified names and returns
the complete session history, the activity heatmap (weekday × hour), achievements and the latest
events with timestamps. Visible under `/my-stats`, which every signed-in user reaches without any
flag.

And the admin needs more than a raw foreign-key number ("User #7") and the claimed name: a display
name, the game, and above all a note that the same name is already verified elsewhere.
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException
from sqlmodel import Session

from app.models import PlayerLink, Role, User
from app.routers import stats as stats_router


@pytest.fixture()
def session(mem_engine):
    # `mem_engine` from conftest.py instead of an own engine: an own engine that is never
    # `dispose()`d leaves one sqlite3.Connection open per test function — ResourceWarnings that
    # the GC reports at some point somewhere else, even in tests without any database.
    with Session(mem_engine) as s:
        yield s


def _user(s: Session, name: str) -> User:
    u = User(display_name=name.capitalize(), username=name, pw_hash="x", role=Role.member)
    s.add(u)
    s.commit()
    s.refresh(u)
    return u


def _claim(s: Session, user: User, name: str, *, game="pz", verified=False) -> PlayerLink:
    link = PlayerLink(user_id=user.id, ingame_username=name, game=game, verified=verified)
    s.add(link)
    s.commit()
    s.refresh(link)
    return link


def test_an_already_taken_name_is_not_verified_a_second_time(session):
    """The core: two verified claims on the same name are always one too many."""
    steve, eve = _user(session, "steve"), _user(session, "eve")
    _claim(session, steve, "steve", verified=True)
    foreign = _claim(session, eve, "steve")

    with pytest.raises(HTTPException) as e:
        stats_router.verify_link(foreign.id, session=session, _=None)
    assert e.value.status_code == 409
    assert str(steve.id) in str(e.value.detail)

    session.refresh(foreign)
    assert foreign.verified is False, "verified despite 409"


def test_the_same_name_in_ANOTHER_game_is_no_conflict(session):
    """Cross-check: "steve" in Project Zomboid and "steve" in Minecraft can be two people.

    Without this distinction the rule would be too strict and the admin would have to work around
    it — after which nobody takes it seriously any more.
    """
    steve, eve = _user(session, "steve"), _user(session, "eve")
    _claim(session, steve, "steve", game="pz", verified=True)
    other_game = _claim(session, eve, "steve", game="minecraft")

    stats_router.verify_link(other_game.id, session=session, _=None)
    session.refresh(other_game)
    assert other_game.verified is True


def test_a_second_claim_of_the_same_account_is_no_conflict(session):
    """Claiming one's own name again (e.g. after a rejection) does not block oneself."""
    steve = _user(session, "steve")
    _claim(session, steve, "steve", verified=True)
    again = _claim(session, steve, "steve")

    stats_router.verify_link(again.id, session=session, _=None)
    session.refresh(again)
    assert again.verified is True


def test_the_list_names_the_account_and_the_conflict(session):
    """Without this information the system's only control is a shot in the dark."""
    steve, eve = _user(session, "steve"), _user(session, "eve")
    _claim(session, steve, "steve", verified=True)
    _claim(session, eve, "steve")

    lines = stats_router.all_links(session=session, _=None)
    open_ = [row for row in lines if not row["verified"]]
    assert len(open_) == 1
    assert open_[0]["user"]["username"] == "eve", "the claimant must be shown by name"
    assert open_[0]["conflict"] is not None, "the existing claim must be visible"
    assert open_[0]["conflict"]["user"]["username"] == "steve"

    # An undisputed claim carries no conflict — otherwise the marker would be worthless.
    bob = _user(session, "bob")
    _claim(session, bob, "bobby")
    lines = stats_router.all_links(session=session, _=None)
    assert [row for row in lines if row["ingame_username"] == "bobby"][0]["conflict"] is None


def test_a_claim_can_be_deleted(session):
    """Without this path the conflict check would be a dead end.

    It also covers legacy entries: claims made before the game was sent along are stored as
    `game="pz"` in the database.
    """
    steve = _user(session, "steve")
    link = _claim(session, steve, "steve", verified=True)

    stats_router.delete_link(link.id, session=session, _=None)
    assert session.get(PlayerLink, link.id) is None

    with pytest.raises(HTTPException) as e:
        stats_router.delete_link(link.id, session=session, _=None)
    assert e.value.status_code == 404
