"""Which games this installation runs: one row per game type. New table → covered by create_all.

`game_type` is the plain enum value (like `version_catalog.game_type`), not the native `gametype`
ENUM of `game_servers`: a game type added in a later release then needs no `ALTER TYPE` here, and
`services/game_settings.seed` tops up the missing row by itself.

`defaults` is reserved for per-game starting values an admin may set later (JSON string, house
style as in `game_servers.extra`); nothing reads it yet.
"""
from __future__ import annotations

import datetime as dt

from sqlmodel import Field, SQLModel

from .player import utcnow_naive


class GameSetting(SQLModel, table=True):
    __tablename__ = "game_settings"

    game_type: str = Field(primary_key=True)   # GameType value, e.g. "minecraft"
    active: bool = False
    defaults: str = "{}"
    created_at: dt.datetime = Field(default_factory=utcnow_naive)
    updated_at: dt.datetime = Field(default_factory=utcnow_naive)
