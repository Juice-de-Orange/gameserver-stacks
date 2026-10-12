"""When a background job last actually ran (new table → `create_all`).

**Why it exists.** All jobs live in the `MemoryJobStore` and are re-registered at EVERY process
start; `add_job` computes the next run from that moment. For the time the process was down there
is no job that could misfire — `misfire_grace_time` (see `scheduler.py`) cannot help there.

A restart of the backend inside the window of the weekly backup would therefore skip that run
without a trace: no archive, no log line, no incident. The job simply never started and nothing
in the system knew it was missing. This table is the memory that makes the gap detectable.
"""
from __future__ import annotations

import datetime as dt

from sqlmodel import Field, SQLModel

from .player import utcnow_naive


class JobRun(SQLModel, table=True):
    __tablename__ = "job_runs"

    job_id: str = Field(primary_key=True)
    last_run_at: dt.datetime = Field(default_factory=utcnow_naive)
