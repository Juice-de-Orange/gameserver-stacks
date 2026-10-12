"""Version change history = rollback source.

Append-only. Every version change takes a mandatory snapshot FIRST (`backup_name`) and
remembers the old version (tag for PZ, VERSION env for MC/VS) so that a rollback is
unambiguous. New table → covered by create_all.
"""
from __future__ import annotations

import datetime as dt

from sqlmodel import Field, SQLModel

from .player import utcnow_naive


class VersionChange(SQLModel, table=True):
    __tablename__ = "version_change"

    id: int | None = Field(default=None, primary_key=True)
    server_id: int = Field(index=True)
    direction: str = ""              # "up" | "down" | "rollback"
    old_image_tag: str = ""
    new_image_tag: str = ""
    old_game_version: str = ""
    new_game_version: str = ""
    old_type: str = ""
    new_type: str = ""
    backup_name: str = ""            # pre-change snapshot (mandatory; rollback point)
    # "rolled_back_partial": the container is back but the WORLD is not — it is still in the
    # state the new version migrated it to. Reporting that as plain "rolled_back" would make the
    # operator stop looking.
    status: str = "pending"          # "pending"|"ok"|"failed"|"rolled_back"|"rolled_back_partial"
    detail: str = ""
    at: dt.datetime = Field(default_factory=utcnow_naive, index=True)
