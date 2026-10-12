"""Catalogue of the SELECTABLE game versions per game type — feeds the version dropdown.

Refreshed daily from the upstream sources by `services/version_catalog.py` (PZ: Docker Hub
tags, MC: Paper/Mojang index, VS: official stable index), which makes it a **cache**, not a
source of truth: the rows may be rewritten at any time.

Persisted instead of in-memory only (cf. `mod_check._cache`) because the dropdown should be
populated right after an API restart and `first_seen_at` carries the "NEW" badge — that would be
lost with a pure process cache. New table → covered by create_all.
"""
from __future__ import annotations

import datetime as dt

from sqlmodel import Field, SQLModel

from .player import utcnow_naive


class VersionCatalogEntry(SQLModel, table=True):
    __tablename__ = "version_catalog"

    id: int | None = Field(default=None, primary_key=True)
    game_type: str = Field(index=True)     # "pz" | "minecraft" | "vintagestory"
    variant: str = ""                      # MC: server_type (PAPER/VANILLA/…); PZ/VS: ""
    version: str = ""                      # bare version number, e.g. "42.20.0" / "1.21.11"
    # What is written to the registry on a change: PZ = image tag ("42.20.0-release"),
    # MC/VS = the version itself (VERSION env). Unique together with (game_type, variant).
    ref: str = Field(default="", index=True)
    channel: str = ""                      # "stable" | "unstable" | "snapshot" | "legacy"
    released_at: dt.datetime | None = None

    # --- changes (not every source provides this; VS none at all) ---
    changelog_title: str = ""
    changelog_url: str = ""
    changelog_summary: str = ""

    # First sighting → drives "NEW since the last daily check" in the dropdown.
    first_seen_at: dt.datetime = Field(default_factory=utcnow_naive)
    fetched_at: dt.datetime = Field(default_factory=utcnow_naive)
