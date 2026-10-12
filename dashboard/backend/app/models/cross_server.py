"""Cross-server features: settings (single row) + chat-bridge bus.

Each of the four features can be switched individually. Every cross-server message names its
originating server (format templates with {server}). New tables → create_all.
The chat bridge is OFF by default (risk of message loops).
"""
from __future__ import annotations

import datetime as dt

from sqlmodel import Field, SQLModel

from .player import utcnow_naive


class CrossServerSettings(SQLModel, table=True):
    __tablename__ = "cross_server_settings"

    id: int | None = Field(default=None, primary_key=True)
    # --- four individually switchable features ---
    chat_bridge_enabled: bool = False    # OFF by default
    presence_enabled: bool = True
    leaderboard_enabled: bool = True
    announcements_enabled: bool = True

    # --- format templates ({server}=origin, {player}, {msg}) ---
    chat_format: str = "[{server}] {player}: {msg}"
    event_format: str = "[{server}] {msg}"
    come_over_format: str = "{player} is playing on {server} right now – come over!"

    # --- protection ---
    rate_limit_per_min: int = 30         # token bucket against spam/loops
    come_over_cooldown_sec: int = 300


class ChatBridgeMessage(SQLModel, table=True):
    __tablename__ = "chat_bridge_messages"

    id: int | None = Field(default=None, primary_key=True)
    server_id: int | None = Field(default=None, index=True)   # originating server (no FK)
    server_key: str = ""
    kind: str = "chat"               # "chat" | "join" | "leave" | "event" | "announce" | "come_over"
    player: str = ""
    message: str = ""
    dedup_key: str = Field(default="", index=True)  # loop/duplicate protection
    at: dt.datetime = Field(default_factory=utcnow_naive, index=True)
