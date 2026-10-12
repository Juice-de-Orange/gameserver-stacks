"""Registry helpers for the `game_servers` table.

Synchronous SQLModel sessions like the rest of the backend (`with Session(engine)`), NO DB access
at import time (functions open their session lazily, so importing the module needs no database).

`list_enabled_with_driver` is the canonical fan-out source; `scope_clause`/`resolve_server`
encapsulate the server_id convention: every per-server row belongs to exactly one registry row.
"""
from __future__ import annotations

import logging

from sqlmodel import Session, select

from ..db import engine
from ..models.server import GameServer
from .drivers import GameDriver, get_driver

log = logging.getLogger("uvicorn.error")


def list_enabled() -> list[GameServer]:
    """All enabled servers in stable order (sort_order, id)."""
    with Session(engine) as s:
        return list(
            s.exec(
                select(GameServer)
                .where(GameServer.enabled == True)  # noqa: E712 — SQLModel comparison
                .order_by(GameServer.sort_order, GameServer.id)
            ).all()
        )


def list_all() -> list[GameServer]:
    """**All** registry rows, including disabled ones — same order.

    Needed for exactly one question: "who is using RAM on this host right now?" Switching a
    server off stops its container (`routers/servers._stop_for_disable`), but a row switched off
    by an older version, or one whose container someone started by hand, may still run and use
    memory. Computing usage from `list_enabled()` would book "no longer managed" as "uses
    nothing". For everything else (jobs, fan-out, display) `list_enabled()` stays right: a
    disabled server is deliberately no longer served.
    """
    with Session(engine) as s:
        return list(
            s.exec(select(GameServer).order_by(GameServer.sort_order, GameServer.id)).all()
        )


def get_by_key(key: str) -> GameServer | None:
    """Get a server by its URL slug (`key`)."""
    with Session(engine) as s:
        return s.exec(select(GameServer).where(GameServer.key == key)).first()


def get_by_id(server_id: int) -> GameServer | None:
    """Get a server by primary key (for the server_id columns of the per-feature tables)."""
    with Session(engine) as s:
        return s.get(GameServer, server_id)


def resolve_secret(server: GameServer) -> str:
    """Resolve the console (RCON/admin) secret. Precedence: env variable → stored value.

    `rcon_password_env` names an environment variable from the `.env`; if it is set, it wins.
    Otherwise the value entered in the UI, stored encrypted (`services/secrets.py`).
    """
    from . import secrets

    return secrets.resolve(server)


def resolve_server(server_id: int | None) -> GameServer | None:
    """server_id → row. `None` if there is no `server_id` or it points to a missing row (the
    caller then skips the operation)."""
    if server_id is None:
        return None
    return get_by_id(server_id)


def scope_clause(col, server: GameServer):
    """WHERE clause for a `server_id` column: each server sees only its own rows. Prevents
    cross-server contamination (e.g. the stats poller of server A closing the open sessions of
    server B).
    """
    return col == server.id


_skipped_drivers: set[str] = set()  # process-local (single worker): one log message per key


def list_enabled_with_driver() -> list[tuple[GameServer, GameDriver]]:
    """Canonical fan-out source: enabled servers plus their resolved driver.

    - Rows whose game has no driver (`NotImplementedError`) are skipped and logged exactly once
      per `key` — such a row must not crash a job loop.
    - No rows or only disabled rows → empty list.
    """
    rows = list_enabled()
    out: list[tuple[GameServer, GameDriver]] = []
    for row in rows:
        try:
            drv = get_driver(row)
        except NotImplementedError:
            if row.key not in _skipped_drivers:
                log.info(
                    "No driver for server '%s' (game_type=%s) — skipped.",
                    row.key, row.game_type,
                )
                _skipped_drivers.add(row.key)
            continue
        out.append((row, drv))
    return out
