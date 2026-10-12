"""Cross-server router: settings, presence, leaderboard, come-over, announce, chat.

Read views (presence/leaderboard/chat history) only need `can_view_status`. Write actions
(settings, come-over, announce) need `can_manage_cross_server`. The chat bridge itself stays
gated by `settings.chat_bridge_enabled` (model default OFF) — this router does NOT enable it
automatically; that takes a deliberate PUT on the settings by an admin. "Default OFF" therefore
says nothing about a running installation: check the stored setting.
"""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, WebSocket
from pydantic import BaseModel
from sqlmodel import Session, select

from ..auth.deps import require_flag, user_from_ws_ticket, ws_user_recheck
from ..auth.perm import has_permission
from ..db import engine, get_session
from ..models import User
from ..models.cross_server import ChatBridgeMessage
from ..services.cross_server import bus, features

router = APIRouter(prefix="/api/cross", tags=["cross-server"])
# WS deliberately WITHOUT the /api prefix (like /ws/console) — one consistent proxy route for upgrades.
ws_router = APIRouter()

_view = require_flag("can_view_status")
_manage = require_flag("can_manage_cross_server")

_SETTINGS_FIELDS = (
    "chat_bridge_enabled", "presence_enabled", "leaderboard_enabled", "announcements_enabled",
    "chat_format", "event_format", "come_over_format", "rate_limit_per_min", "come_over_cooldown_sec",
)


def _settings_dict(s) -> dict:
    return {f: getattr(s, f) for f in _SETTINGS_FIELDS}


class SettingsIn(BaseModel):
    chat_bridge_enabled: bool | None = None
    presence_enabled: bool | None = None
    leaderboard_enabled: bool | None = None
    announcements_enabled: bool | None = None
    chat_format: str | None = None
    event_format: str | None = None
    come_over_format: str | None = None
    rate_limit_per_min: int | None = None
    come_over_cooldown_sec: int | None = None


class ComeOverIn(BaseModel):
    server_key: str
    player: str


class AnnounceIn(BaseModel):
    message: str


@router.get("/settings")
def get_settings(session: Session = Depends(get_session), _: User = Depends(_view)) -> dict:
    return _settings_dict(bus.get_settings(session))


@router.put("/settings")
def put_settings(
    body: SettingsIn, session: Session = Depends(get_session), _: User = Depends(_manage)
) -> dict:
    s = bus.get_settings(session)
    for field, value in body.model_dump(exclude_none=True).items():
        setattr(s, field, value)
    session.add(s)
    session.commit()
    session.refresh(s)
    return _settings_dict(s)


@router.get("/presence")
def get_presence(session: Session = Depends(get_session), _: User = Depends(_view)) -> list[dict]:
    return features.presence(session)


@router.get("/leaderboard")
def get_leaderboard(
    limit: int = 20, session: Session = Depends(get_session), _: User = Depends(_view)
) -> list[dict]:
    return features.leaderboard(session, limit=limit)


@router.get("/chat")
def get_chat(
    limit: int = 50, session: Session = Depends(get_session), _: User = Depends(_view)
) -> list[dict]:
    rows = bus.recent_chat(session, limit=limit)
    return [
        {"id": r.id, "server_key": r.server_key, "player": r.player, "message": r.message,
         "kind": r.kind, "at": r.at}
        for r in rows
    ]


@router.post("/come-over")
async def post_come_over(
    body: ComeOverIn, session: Session = Depends(get_session), _: User = Depends(_manage)
) -> dict:
    return await features.come_over(session, body.server_key, body.player)


@router.post("/announce")
async def post_announce(
    body: AnnounceIn, session: Session = Depends(get_session), _: User = Depends(_manage)
) -> dict:
    return await features.announce_all(session, body.message)


@ws_router.websocket("/ws/cross-chat")
async def ws_cross_chat(ws: WebSocket) -> None:
    """Live feed: new bridge chats + presence. Any signed-in user with can_view_status.

    Poll-based (single worker, no pub/sub needed): sends history + presence on connect, then
    new chat lines every ~3 s. Also works with the bridge switched off (no new chats arrive
    then, but presence/history are visible)."""
    await ws.accept()
    ticket = ws.query_params.get("ticket", "")
    with Session(engine) as session:
        user = user_from_ws_ticket(ticket, session)
        if user is None or not has_permission(user, "can_view_status"):
            await ws.close(code=4401)
            return
        history = bus.recent_chat(session, limit=50)
        last_id = history[0].id if history else 0
        await ws.send_json({
            "type": "init",
            "chat": [{"id": r.id, "server_key": r.server_key, "player": r.player,
                      "message": r.message, "at": r.at.isoformat()} for r in reversed(history)],
            "presence": features.presence(session),
        })
    try:
        while True:
            await asyncio.sleep(3)
            with Session(engine) as session:
                # Re-check auth on every tick — like the HTTP endpoints (get_current_user
                # re-validates per request). A revocation (lockout/permission removal/token
                # expiry) takes effect immediately instead of streaming on until disconnect.
                u = ws_user_recheck(user, session)
                if u is None or not has_permission(u, "can_view_status"):
                    await ws.close(code=4401)
                    return
                rows = session.exec(
                    select(ChatBridgeMessage)
                    .where(ChatBridgeMessage.id > last_id)
                    .order_by(ChatBridgeMessage.id)
                    .limit(100)
                ).all()
                if rows:
                    last_id = rows[-1].id
                    await ws.send_json({
                        "type": "chat",
                        "chat": [{"id": r.id, "server_key": r.server_key, "player": r.player,
                                  "message": r.message, "at": r.at.isoformat()} for r in rows],
                    })
    except Exception:
        return
