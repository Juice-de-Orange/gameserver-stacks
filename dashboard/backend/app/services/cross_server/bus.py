"""Chat bridge bus: settings and persistence of the cross-server chat.

The actual loop protection lives in `loop_guard`; this bus is the DB layer on top of it.
`record_inbound_chat` writes a row ONLY when the guard lets it through as new, genuine player
chat (not an echo of our own injection, not a duplicate).
"""
from __future__ import annotations

import datetime as dt
import logging

from sqlalchemy import delete as sa_delete
from sqlmodel import Session, select

from ...config import settings
from ...db import engine
from ...models.cross_server import ChatBridgeMessage, CrossServerSettings
from ...models.player import utcnow_naive
from ...models.server import GameServer
from . import loop_guard

log = logging.getLogger("uvicorn.error")


def get_settings(session: Session) -> CrossServerSettings:
    """Load the single settings row; create it with safe defaults on first access
    (`chat_bridge_enabled=False` is the model default)."""
    row = session.exec(select(CrossServerSettings).order_by(CrossServerSettings.id)).first()
    if row is None:
        row = CrossServerSettings()
        session.add(row)
        session.commit()
        session.refresh(row)
    return row


def record_inbound_chat(
    session: Session, server: GameServer, player: str, msg: str
) -> ChatBridgeMessage | None:
    """Write genuine player chat from `server` to the bus, filtered by the loop guard.

    Returns the persisted row, or `None` when the guard rejects it (injected echo or
    duplicate). `msg`/`player` are stripped; empty messages are ignored."""
    player = (player or "").strip()
    msg = (msg or "").strip()
    if not msg:
        return None
    if not loop_guard.should_ingest(server.key, player, msg):
        return None
    row = ChatBridgeMessage(
        server_id=server.id,
        server_key=server.key,
        kind="chat",
        player=player,
        message=msg,
        dedup_key=loop_guard.make_dedup_key(server.key, player, msg),
    )
    try:
        session.add(row)
        session.commit()
    except Exception:
        # Persisting failed: undo the dedup mark (otherwise the retry in the next poll would be
        # rejected as a duplicate) and surface the error to the caller.
        session.rollback()
        loop_guard.forget_dedup(server.key, player, msg)
        raise
    session.refresh(row)
    return row


def recent_chat(session: Session, limit: int = 50) -> list[ChatBridgeMessage]:
    """Most recent bridge messages (newest first), for the chat history in the UI/WS."""
    limit = max(1, min(limit, 200))
    rows = session.exec(
        select(ChatBridgeMessage).order_by(ChatBridgeMessage.id.desc()).limit(limit)
    ).all()
    return list(rows)


def prune_chat() -> None:
    """Delete old chat bridge rows; same pattern as `audit.prune_audit`.

    `chat_bridge_messages` grows with every chat line of every server (plus three indexes:
    `server_id`, `dedup_key`, `at`), so it needs a retention rule like the other
    high-frequency tables.
    """
    cutoff = utcnow_naive() - dt.timedelta(days=settings.chat_retention_days)
    try:
        with Session(engine) as session:
            session.execute(sa_delete(ChatBridgeMessage).where(ChatBridgeMessage.at < cutoff))
            session.commit()
    except Exception:
        log.warning("cross-server: chat prune failed", exc_info=False)
