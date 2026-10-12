"""Global resource budget (singleton, id=1). New table → covered by create_all.

`total_ram_mb == 0` means: advisory ONLY (no enforcement at start) until an admin sets the
shared limit. The per-server allocation lives in `game_servers`.
"""
from __future__ import annotations

from sqlmodel import Field, SQLModel


class ResourceBudget(SQLModel, table=True):
    __tablename__ = "resource_budget"

    id: int | None = Field(default=None, primary_key=True)
    total_ram_mb: int = 0                       # shared limit; 0 = advisory only
    total_cpu_cores: float = 0.0                # shared CPU limit; 0 = advisory only
    mem_headroom_pct: int = 25                  # ram_mb must be >= heap_max_mb * (1 + pct/100) (JVM)
    default_idle_timeout_minutes: int = 28800   # ~20 days; per-server override via idle_timeout_minutes
