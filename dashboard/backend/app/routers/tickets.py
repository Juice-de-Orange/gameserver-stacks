"""Mod-request tickets: CRUD + comments + status workflow + adding the mod."""
from __future__ import annotations

import datetime as dt
import re

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlmodel import Session, select

from ..auth.deps import get_current_user, require_flag
from ..auth.perm import has_permission
from ..db import get_session
from ..errors import err
from ..models import Ticket, TicketCategory, TicketComment, TicketStatus, User
from ..models.server import GameType
from ..schemas import CommentRead, TicketDetail, TicketRead
from ..services import servers as servers_registry
from ..services.audit import record
from ..services.mods import state as mod_state

router = APIRouter(prefix="/api/tickets", tags=["tickets"])


class TicketIn(BaseModel):
    title: str
    workshop_url: str = ""
    body: str = ""
    category: TicketCategory = TicketCategory.general
    workshop_id: str = ""
    mod_id: str = ""
    # WHICH server this is about. Without it every request would end up in the draft of the
    # first PZ server, including one from a Minecraft player.
    server_key: str = ""


class CommentIn(BaseModel):
    body: str


class StatusIn(BaseModel):
    status: TicketStatus


def _can_manage(user: User) -> bool:
    return has_permission(user, "can_manage_tickets")


def _parse_workshop_id(url: str) -> str:
    """Extract the numeric workshop ID from a Steam Workshop link (…?id=12345)."""
    m = re.search(r"[?&]id=(\d+)", url or "")
    return m.group(1) if m else ""


def _check_weblink(url: str) -> str:
    """Only let http(s) through — the value is rendered as an `href` later.

    `workshop_url` is typed by the requester and read by the admin who decides on the request.
    A `javascript:` scheme would run on click in THE ADMIN'S session, and the access token sits
    in the browser's `localStorage`. That would turn "may write tickets" into a path to "is
    admin".

    The frontend checks it as well (`lib/url.safeUrl`) — both sides, because neither is
    enough alone: the browser protects the reader, the server prevents such a value from being
    stored at all and tells the requester what is wrong.
    """
    value = (url or "").strip()
    if not value:
        return ""
    if not re.match(r"^https?://", value, re.IGNORECASE):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail=err("ticket.link_scheme", "The link must start with http:// or https://."),
        )
    return value


@router.get("", response_model=list[TicketRead])
def list_tickets(session: Session = Depends(get_session), user: User = Depends(get_current_user)):
    query = select(Ticket).order_by(Ticket.id.desc())
    if not _can_manage(user):
        query = query.where(Ticket.author_id == user.id)
    return session.exec(query).all()


@router.post("", status_code=status.HTTP_201_CREATED, response_model=TicketRead)
def create_ticket(body: TicketIn, session: Session = Depends(get_session), user: User = Depends(get_current_user)):
    # For mod requests derive the workshop ID from the link on the server if it is empty.
    workshop_url = _check_weblink(body.workshop_url)
    workshop_id = body.workshop_id.strip()
    if body.category == TicketCategory.mod_request and not workshop_id:
        workshop_id = _parse_workshop_id(workshop_url)
    target_server = servers_registry.get_by_key(body.server_key.strip()) if body.server_key.strip() else None
    if body.server_key.strip() and target_server is None:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail=err("ticket.server_unknown", "Server '{key}' does not exist.", key=body.server_key),
        )
    ticket = Ticket(
        author_id=user.id,
        author_name=user.display_name,
        server_id=target_server.id if target_server else None,
        title=body.title,
        workshop_url=workshop_url,
        body=body.body,
        category=body.category,
        workshop_id=workshop_id,
        mod_id=body.mod_id.strip(),
    )
    session.add(ticket)
    session.commit()
    session.refresh(ticket)
    record(session, user, "ticket.create", f"ticket:{ticket.id}", f"{body.category.value}:{body.title}")
    return ticket


@router.get("/{ticket_id}", response_model=TicketDetail)
def get_ticket(ticket_id: int, session: Session = Depends(get_session), user: User = Depends(get_current_user)):
    ticket = session.get(Ticket, ticket_id)
    if not ticket:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=err("ticket.not_found", "Ticket not found"))
    if not _can_manage(user) and ticket.author_id != user.id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail=err("ticket.no_access", "No access"))
    comments = session.exec(
        select(TicketComment).where(TicketComment.ticket_id == ticket_id).order_by(TicketComment.id)
    ).all()
    return {"ticket": ticket, "comments": comments}


@router.post("/{ticket_id}/comment", status_code=status.HTTP_201_CREATED, response_model=CommentRead)
def add_comment(ticket_id: int, body: CommentIn, session: Session = Depends(get_session), user: User = Depends(get_current_user)):
    ticket = session.get(Ticket, ticket_id)
    if not ticket:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=err("ticket.not_found", "Ticket not found"))
    if not _can_manage(user) and ticket.author_id != user.id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail=err("ticket.no_access", "No access"))
    comment = TicketComment(
        ticket_id=ticket_id, author_id=user.id, author_name=user.display_name, body=body.body
    )
    session.add(comment)
    session.commit()
    session.refresh(comment)
    return comment


@router.put("/{ticket_id}/status", response_model=TicketRead)
def set_status(ticket_id: int, body: StatusIn, session: Session = Depends(get_session), user: User = Depends(require_flag("can_manage_tickets"))):
    ticket = session.get(Ticket, ticket_id)
    if not ticket:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=err("ticket.not_found", "Ticket not found"))
    ticket.status = body.status
    ticket.updated_at = dt.datetime.now(dt.timezone.utc)
    session.add(ticket)
    session.commit()
    session.refresh(ticket)
    record(session, user, "ticket.status", f"ticket:{ticket_id}", body.status.value)
    return ticket


@router.post("/{ticket_id}/apply-mod", response_model=TicketRead)
def apply_mod(
    ticket_id: int,
    session: Session = Depends(get_session),
    user: User = Depends(require_flag("can_manage_mods")),
):
    """Add an approved mod request to the **draft** (the server stays untouched).

    It only goes live through `POST /api/servers/<key>/mods/apply` — that is where preflight,
    mandatory backup, boot verification and the automatic fallback run. Writing directly to the
    server's `.ini` here would make the change live at the next arbitrary restart without anyone
    having pressed "apply".
    """
    ticket = session.get(Ticket, ticket_id)
    if not ticket:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=err("ticket.not_found", "Ticket not found"))
    if ticket.category != TicketCategory.mod_request:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=err("ticket.not_mod_request", "Not a mod request"))
    if ticket.status != TicketStatus.approved:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, detail=err("ticket.not_approved", "Ticket is not approved")
        )
    if not ticket.workshop_id:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, detail=err("ticket.workshop_id_missing", "Workshop ID is missing")
        )

    target = servers_registry.resolve_server(ticket.server_id)
    if target is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            detail=err("ticket.target_server_not_found", "Target server not found"),
        )
    # `mod_state.add_item` writes to the PROJECT ZOMBOID draft — workshop IDs only exist there.
    # For Minecraft and Vintage Story mods are managed through uploaded files, for Reforger
    # through GUIDs. Without this check a Minecraft request would silently land in a PZ
    # server's draft and be shipped with the next "apply" there.
    if target.game_type != GameType.pz:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail=err(
                "ticket.no_workshop_mods",
                "“{server}” does not manage its mods through workshop IDs — "
                "this request has to be carried out there by hand.",
                server=target.display_name,
            ),
        )
    mod_state.add_item(target, ticket.workshop_id)

    now = dt.datetime.now(dt.timezone.utc)
    ticket.status = TicketStatus.done
    ticket.applied_at = now
    ticket.updated_at = now
    session.add(ticket)
    session.commit()
    session.refresh(ticket)
    record(session, user, "ticket.apply_mod", f"ticket:{ticket_id}", ticket.workshop_id)
    return ticket
