"""Account linking + player sessions."""
from __future__ import annotations

import datetime as dt

from sqlmodel import Field, SQLModel


def utcnow_naive() -> dt.datetime:
    # Naive UTC -> consistent with Postgres TIMESTAMP (for time arithmetic in the poller).
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)


class PlayerSession(SQLModel, table=True):
    __tablename__ = "player_sessions"

    id: int | None = Field(default=None, primary_key=True)
    username: str = Field(index=True)
    steam_id: str = ""
    joined_at: dt.datetime = Field(default_factory=utcnow_naive, index=True)
    left_at: dt.datetime | None = Field(default=None, index=True)
    duration_seconds: int | None = None
    server_id: int | None = Field(default=None, index=True)  # multi-game: on which server


class PlayerLink(SQLModel, table=True):
    __tablename__ = "player_links"

    id: int | None = Field(default=None, primary_key=True)
    user_id: int = Field(index=True)
    steam_id: str = ""
    ingame_username: str = ""
    verified: bool = False
    # --- cross-game identity (multi-game): Steam (PZ/VS) | MC UUID/name (MC) ---
    game: str = "pz"                 # "pz" | "minecraft" | "vintagestory"
    external_id: str = ""            # game-specific ID (SteamID / MC UUID); complements steam_id


class PlayerEvent(SQLModel, table=True):
    """Events parsed from the server logs (join, leave, deaths, level-ups).

    Best effort: if a log format does not match, no events are produced instead of a crash.
    The coordinates are world coordinates of the respective game (for PZ the cell coordinates
    from the log) — they are what makes a map of death locations possible in the first place.
    """
    __tablename__ = "player_events"

    id: int | None = Field(default=None, primary_key=True)
    username: str = Field(index=True)
    kind: str = Field(index=True)        # "death" | "join" | "leave" | "level_up" | "kill"
    at: dt.datetime = Field(default_factory=utcnow_naive, index=True)
    detail: str = ""
    server_id: int | None = Field(default=None, index=True)  # multi-game: originating server
    # Game-specific account ID from the log line (PZ: SteamID64). The reliable key for account
    # linking — names can change and are ambiguous.
    game_id: str = Field(default="", index=True)
    x: int | None = None
    y: int | None = None
    z: int | None = None
