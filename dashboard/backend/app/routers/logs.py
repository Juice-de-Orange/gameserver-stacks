"""Live tail of the server logs over WebSocket.

The REST side lives in `routers/server_ops.py` (`/api/servers/{key}/logs` and
`…/logs/download`). The WebSocket stays here because it follows the `/ws/*` convention rather
than the `/api/servers` prefix.

It requires `?server=<key>`, just like `/ws/console`. There is deliberately no fallback to a
default container: with a typo in the key that would silently deliver the logs of a DIFFERENT
server.
"""
from __future__ import annotations

import logging

import httpx
from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from sqlmodel import Session

from ..auth.deps import user_from_ws_ticket
from ..auth.perm import has_permission
from ..config import settings
from ..db import engine
from ..services import servers as servers_registry
from ..services.docker_client import LogDemuxer

log = logging.getLogger("uvicorn.error")

router = APIRouter()


@router.websocket("/ws/logs")
async def ws_logs(ws: WebSocket) -> None:
    await ws.accept()
    ticket = ws.query_params.get("ticket", "")
    with Session(engine) as session:
        user = user_from_ws_ticket(ticket, session)
    if user is None or not has_permission(user, "can_view_logs"):
        await ws.close(code=4401)
        return

    server_key = ws.query_params.get("server")
    if not server_key:
        await ws.close(code=4400)  # no silent fallback to some server
        return
    srv = servers_registry.get_by_key(server_key)
    if srv is None or not srv.enabled:
        await ws.close(code=4404)
        return

    url = (
        f"{settings.docker_proxy_url}/containers/{srv.container_name}"
        f"/logs?stdout=1&stderr=1&follow=1&tail=200"
    )
    try:
        async with httpx.AsyncClient(timeout=None) as client:
            async with client.stream("GET", url) as resp:
                # Check the status code BEFORE streaming. Otherwise the error body of the socket
                # proxy (404 "no such container", 403 for a missing grant) would run through the
                # demuxer and show up in the browser as an apparent log line — "container gone"
                # would be indistinguishable from "server is quiet".
                if resp.status_code != 200:
                    log.warning(
                        "ws/logs[%s]: Docker proxy answered with %s", srv.key, resp.status_code
                    )
                    await ws.close(code=4502)
                    return
                # Stateful, with a remainder buffer: TCP chunks do not align with Docker frame
                # boundaries. Demuxing each chunk on its own loses 8 bytes at every boundary
                # that falls inside a payload — they get read as a header.
                demux = LogDemuxer()
                async for chunk in resp.aiter_bytes():
                    text = demux.feed(chunk)
                    if text:
                        await ws.send_text(text)
    except WebSocketDisconnect:
        return
    except Exception:
        await ws.close()
