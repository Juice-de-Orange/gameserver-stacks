"""Mod configuration as a versioned object — draft, applied sets, history.

Without this the complete mod state would live only in the server's `.ini` on disk: no history,
no undo, no answer to "which combination was running back when it still worked?".

- **`ModSet`** — a snapshot of workshop items, load order, map list and spawn regions. Per server
  there is exactly ONE `draft` (the one being edited) and any number of `applied` sets
  (immutable records of what actually ran).
- **`ModChange`** — one apply run with a lifecycle, built like `models/version_change.py`:
  `pending → ok | rolled_back | failed`, including the mandatory backup and the quarantine
  reference.

Lists are stored as JSON strings (house style, cf. `game_servers.extra`/`game_ports`). New
tables → covered by `create_all`.
"""
from __future__ import annotations

import datetime as dt

from sqlmodel import Field, SQLModel

from .player import utcnow_naive

DRAFT = "draft"
APPLIED = "applied"


class ModSet(SQLModel, table=True):
    __tablename__ = "mod_sets"

    id: int | None = Field(default=None, primary_key=True)
    server_id: int = Field(index=True)
    kind: str = Field(default=DRAFT, index=True)   # "draft" | "applied"

    workshop_items: str = "[]"    # JSON list of workshop IDs
    mods: str = "[]"              # JSON list of mod IDs in LOAD ORDER
    maps: str = "[]"              # JSON list of map folders, base map last
    spawn_regions: str = "[]"     # JSON list of the selectable spawn regions

    label: str = ""               # e.g. "before map mod XY" — for orientation only
    created_at: dt.datetime = Field(default_factory=utcnow_naive)
    updated_at: dt.datetime = Field(default_factory=utcnow_naive)


class ModChange(SQLModel, table=True):
    __tablename__ = "mod_changes"

    id: int | None = Field(default=None, primary_key=True)
    server_id: int = Field(index=True)
    at: dt.datetime = Field(default_factory=utcnow_naive, index=True)

    status: str = "pending"       # "pending" | "ok" | "rolled_back" | "failed"
    from_set_id: int | None = None
    to_set_id: int | None = None

    backup_name: str = ""         # mandatory snapshot before the change
    quarantine_name: str = ""     # folder holding the world files set aside (if maps were removed)
    summary: str = ""             # "+2 mods, −1 map" — for the history
    detail: str = ""              # error text on failure
