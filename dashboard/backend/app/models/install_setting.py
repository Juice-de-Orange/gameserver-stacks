"""Installation-wide settings an admin changes in the UI. New table → covered by create_all.

One row per key; `value` is plain text. What lives here overrides the matching environment
variable, which stays the starting value (`services/install_settings.py`). No secrets: the
table is readable for anybody with database access.
"""
from __future__ import annotations

import datetime as dt

from sqlmodel import Field, SQLModel

from .player import utcnow_naive


class InstallSetting(SQLModel, table=True):
    __tablename__ = "install_settings"

    key: str = Field(primary_key=True)   # e.g. "public_host"
    value: str = ""
    updated_at: dt.datetime = Field(default_factory=utcnow_naive)
