"""Long-running operations — visible, traceable, independent of the browser window.

Applying mods and changing versions can take up to 15 minutes (boot verification), an image
download several more. A reverse proxy or CDN in front of the dashboard typically cuts an HTTP
request off long before that (often after ~100 seconds): the browser would see an error while
the server keeps working correctly — and reloading the page would lose track of the operation.

Therefore the call only **starts** the operation and returns an ID immediately; progress is
recorded here and polled. That survives dropped connections and page reloads.

Stored in the database rather than only in memory so that a backend restart does not swallow
the outcome of an operation (the operation itself is dead then, but its state stays readable —
better than vanishing without a trace).
"""
from __future__ import annotations

import datetime as dt

from sqlmodel import Field, SQLModel

from .player import utcnow_naive

RUNNING = "running"
OK = "ok"
FAILED = "failed"


class Operation(SQLModel, table=True):
    __tablename__ = "operations"

    id: int | None = Field(default=None, primary_key=True)
    server_id: int = Field(index=True)
    kind: str = Field(default="", index=True)   # "mods.apply" | "version.change" | "version.pull"
    status: str = Field(default=RUNNING, index=True)

    step: str = ""              # plain text of what is happening right now
    current: int = 0            # progress (e.g. bytes) — 0 if not measurable
    total: int = 0
    detail: str = ""            # error text or result summary
    result: str = "{}"          # JSON result of the operation

    started_at: dt.datetime = Field(default_factory=utcnow_naive, index=True)
    finished_at: dt.datetime | None = None

    @property
    def percent(self) -> float | None:
        return round(self.current / self.total * 100, 1) if self.total else None
