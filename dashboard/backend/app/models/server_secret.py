"""Secrets of a game server that were entered in the UI, encrypted. New table → covered by create_all.

One row per server and slot (`rcon` = the console password, `admin` = Project Zomboid's
in-game admin password, `join` = the join password). `ciphertext` is a Fernet token made
with a key derived from `GSS_SECRET_KEY` (`services/secrets.py`); the clear text is never stored
and never sent by the API. A row is deleted together with its server (`provisioning.TELEMETRY`).
"""
from __future__ import annotations

import datetime as dt

from sqlalchemy import UniqueConstraint
from sqlmodel import Field, SQLModel

from .player import utcnow_naive


class ServerSecret(SQLModel, table=True):
    __tablename__ = "server_secrets"
    __table_args__ = (UniqueConstraint("server_id", "slot", name="uq_server_secrets_server_slot"),)

    id: int | None = Field(default=None, primary_key=True)
    server_id: int = Field(index=True)
    slot: str = "rcon"
    ciphertext: str = ""
    updated_at: dt.datetime = Field(default_factory=utcnow_naive)
