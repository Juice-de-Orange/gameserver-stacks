"""Settings → Games: which games this installation runs (`services/game_settings.py`).

`GET` is open to every signed-in user: the frontend hides inactive games everywhere (account
linking, the first-run screen), not only on admin pages. `PUT` is admin-only and partial: the
body names only the games that change.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlmodel import Session

from ..auth.deps import get_current_user, require_admin
from ..db import get_session
from ..errors import err
from ..models import User
from ..models.server import GameType
from ..services import game_settings
from ..services.audit import record

router = APIRouter(prefix="/api/settings/games", tags=["settings"])


class GamesUpdate(BaseModel):
    games: dict[GameType, bool]


@router.get("")
def get_games(session: Session = Depends(get_session), _: User = Depends(get_current_user)) -> dict:
    return {"games": game_settings.listing(session)}


@router.put("")
def put_games(
    body: GamesUpdate,
    session: Session = Depends(get_session),
    user: User = Depends(require_admin),
) -> dict:
    # Check every deactivation BEFORE writing anything: a request that switches two games off,
    # one of them still in use, changes neither.
    for game, active in body.games.items():
        if active:
            continue
        keys = game_settings.enabled_servers_of(session, game)
        if keys:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                detail={
                    **err(
                        "games.has_servers",
                        "Game '{game}' still has enabled servers: {servers}. Disable them first.",
                        game=game.value, servers=", ".join(keys),
                    ),
                    "servers": keys,
                },
            )
    changed = [
        (game, active) for game, active in body.games.items()
        if game_settings.set_active(session, game, active)
    ]
    session.commit()
    for game, active in changed:
        record(session, user, "games.activate" if active else "games.deactivate", game.value)
    return {"games": game_settings.listing(session)}
