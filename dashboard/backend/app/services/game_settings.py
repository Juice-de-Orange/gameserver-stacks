"""Which games this installation runs (`game_settings`, one row per game type).

Only active games offer a preset, fetch version catalogues, run workshop jobs and cause
downloads. The switch filters where lists are BUILT, not where a server is resolved: the driver
of an inactive game stays importable (`drivers.get_driver`), so a server row of that game still
renders instead of crashing. Deactivating is refused while the game has an enabled server
(`routers/settings_games.py`), and enabling a server of an inactive game is refused
(`routers/servers.py`), so every enabled server belongs to an active game.

Seeding (`seed`, every start, idempotent) writes a row for every game type that has none:

* active if the registry already holds a server of that game — an installation from before this
  table, upgraded, must never hide a running server;
* on the very first seeding also active if `GSS_GAMES` names it;
* inactive otherwise. Without `GSS_GAMES` no game is active and the first-run screen asks.

Once a row exists, the admin's choice in the UI is the truth and `GSS_GAMES` changes nothing
about it (it is still parsed at every start, so a typo is never silent).
"""
from __future__ import annotations

import logging
from collections.abc import Iterable

from sqlmodel import Session, select

from ..config import settings
from ..db import engine
from ..models.game_settings import GameSetting
from ..models.player import utcnow_naive
from ..models.server import GameServer, GameType

log = logging.getLogger("uvicorn.error")


def parse_games(names: Iterable[str]) -> set[GameType]:
    """`GSS_GAMES` entries → game types. `all` means every game; an unknown name raises.

    Raising instead of skipping: a typo (`minecarft`) would otherwise leave the operator with an
    installation that silently runs nothing they asked for.
    """
    known = {g.value: g for g in GameType}
    out: set[GameType] = set()
    for raw in names:
        name = raw.strip().lower()
        if not name:
            continue
        if name == "all":
            out.update(GameType)
        elif name in known:
            out.add(known[name])
        else:
            raise ValueError(
                f"GSS_GAMES: {raw!r} is not a game type. Known: all, {', '.join(sorted(known))}"
            )
    return out


def seed(eng=None) -> list[str]:
    """Write the missing rows (see the module docstring). Returns the game types it activated."""
    wanted = parse_games(settings.games)
    with Session(eng or engine) as s:
        rows = {r.game_type for r in s.exec(select(GameSetting)).all()}
        first = not rows
        with_servers = {GameType(g).value for g in s.exec(select(GameServer.game_type)).all()}
        activated: list[str] = []
        for game in GameType:
            if game.value in rows:
                continue
            active = game.value in with_servers or (first and game in wanted)
            s.add(GameSetting(game_type=game.value, active=active))
            if active:
                activated.append(game.value)
        s.commit()
    if activated:
        log.info("game_settings: activated %s", ", ".join(activated))
    return activated


def active_games(session: Session | None = None) -> set[GameType]:
    """The active game types. A game type without a row (not seeded yet) is inactive."""
    def _read(s: Session) -> set[GameType]:
        known = {g.value: g for g in GameType}
        return {
            known[r.game_type]
            for r in s.exec(select(GameSetting).where(GameSetting.active == True)).all()  # noqa: E712
            if r.game_type in known
        }

    if session is not None:
        return _read(session)
    with Session(engine) as s:
        return _read(s)


def is_active(game_type: GameType | str, session: Session | None = None) -> bool:
    try:
        game = GameType(game_type)
    except ValueError:
        return False
    return game in active_games(session)


def listing(session: Session) -> list[dict]:
    """Every game type in enum order with its switch — the response of `GET /api/settings/games`."""
    active = active_games(session)
    return [{"game_type": g.value, "active": g in active} for g in GameType]


def enabled_servers_of(session: Session, game: GameType) -> list[str]:
    """Keys of the enabled servers of one game — what blocks deactivating it."""
    return list(
        srv.key
        for srv in session.exec(
            select(GameServer)
            .where(GameServer.game_type == game, GameServer.enabled == True)  # noqa: E712
            .order_by(GameServer.sort_order, GameServer.id)
        ).all()
    )


def set_active(session: Session, game: GameType, active: bool) -> bool:
    """Write one switch. Returns whether it changed. The caller commits and audits."""
    row = session.get(GameSetting, game.value)
    if bool(row and row.active) == active:
        return False
    if row is None:
        row = GameSetting(game_type=game.value)
    row.active = active
    row.updated_at = utcnow_naive()
    session.add(row)
    return True
