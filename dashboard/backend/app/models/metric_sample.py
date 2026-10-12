"""Periodic metric samples for the history graphs (new table → create_all)."""
from __future__ import annotations

import datetime as dt

from sqlalchemy import BigInteger
from sqlmodel import Field, SQLModel

from .player import utcnow_naive


class MetricSample(SQLModel, table=True):
    __tablename__ = "metric_samples"

    id: int | None = Field(default=None, primary_key=True)
    at: dt.datetime = Field(default_factory=utcnow_naive, index=True)
    running: bool = False
    players: int = 0
    cpu_percent: float = 0.0
    # BIGINT: container RSS regularly exceeds 2^31 B (~2.1 GB), e.g. a JVM with -Xmx8g; a plain
    # INTEGER would fail the INSERT with "integer out of range" (lost metrics).
    mem_bytes: int = Field(default=0, sa_type=BigInteger)
    mem_pct: float | None = None  # relative to the -Xmx heap ceiling
    server_id: int | None = Field(default=None, index=True)  # multi-game: originating server
