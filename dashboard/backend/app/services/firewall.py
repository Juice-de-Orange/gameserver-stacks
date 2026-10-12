"""The API's side of the firewall helper (`console-firewall`, compose profile `firewall`).

The helper (`dashboard/firewall/`) runs in the host's network namespace with NET_ADMIN and keeps
one chain, `GSS-ALLOW`, hooked first into Docker's `DOCKER-USER`, with ACCEPT rules for the
published game ports. This module only tells it what should be open:

* **Desired state** (`desired`): every published port (`services/ports.bindings_of`) of every
  ENABLED server whose game is active, except ports on a loopback address (those cannot be
  reached from outside anyway), plus the source networks of GSS_FIREWALL_SOURCES.
* **Opening needs an admin.** `routers/firewall.py` shows the helper's plan (what would be added
  and removed) and applies exactly that plan: the helper refuses an apply whose plan id no
  longer matches the rules and the desired state.
* **Closing does not wait** (`close_stale`, every minute): a port of a server that was switched
  off, removed, moved to another port or whose game was deactivated is taken out of the last
  applied state and applied right away. That only ever removes ACCEPT rules.

Transport: HTTP on a Unix socket in a volume that only the API and the helper mount (a container
in the host's network namespace cannot join the internal Docker network), plus the shared token
GSS_FIREWALL_TOKEN. Trust boundary: `docs/security.md`.
"""
from __future__ import annotations

import ipaddress
import json
import logging

import httpx
from fastapi import HTTPException, status
from sqlmodel import Session, select

from ..config import settings
from ..db import engine
from ..errors import err
from ..models.server import GameServer
from . import game_settings, ports
from .audit import record

log = logging.getLogger("uvicorn.error")

TOKEN_MIN_LENGTH = 32  # the helper's own minimum (dashboard/firewall/gss_firewall/service.py)


def enabled() -> bool:
    return bool(settings.firewall_token)


def _reachable_address(ip: str) -> str | None:
    """The bind address the way the helper stores it, so that `close_stale` can compare the two:
    "" for every interface, a specific address in canonical form. `None`: not reachable from
    outside (loopback) or not an address at all — nothing to open."""
    if ip in ("", ports.ANY, "::"):
        return ""
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return None
    return None if addr.is_loopback else str(addr)


def desired(session: Session) -> dict:
    """What should be open: `{"allows": [{server, port, proto, address}], "sources": [...]}`."""
    active = game_settings.active_games(session)
    rows = session.exec(select(GameServer).where(GameServer.enabled == True)).all()  # noqa: E712
    allows = []
    for srv in sorted(rows, key=lambda s: s.key):
        if srv.game_type not in active:
            continue
        for b in ports.bindings_of(srv):
            address = _reachable_address(b.ip)
            if address is not None:
                allows.append({"server": srv.key, "port": b.port, "proto": b.proto, "address": address})
    return {"allows": allows, "sources": list(settings.firewall_sources)}


def _allow_key(a: dict) -> tuple:
    return (a.get("server", ""), int(a.get("port") or 0), a.get("proto", ""), a.get("address", ""))


async def _call(method: str, path: str, body: dict | None = None) -> dict:
    """One request to the helper. Every failure becomes an HTTPException with a code."""
    transport = httpx.AsyncHTTPTransport(uds=settings.firewall_socket)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://console-firewall",
                                     timeout=30.0) as client:
            res = await client.request(method, path, json=body,
                                       headers={"Authorization": f"Bearer {settings.firewall_token}"})
    except (httpx.HTTPError, OSError) as e:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=err(
                "firewall.unreachable",
                "The firewall helper does not answer on {socket} ({reason}). Is the compose profile "
                "`firewall` switched on, and does `docker compose logs console-firewall` show it "
                "listening?",
                socket=settings.firewall_socket, reason=str(e)[:200] or type(e).__name__,
            ),
        ) from None
    try:
        data = res.json()
    except ValueError:
        data = {}
    if not isinstance(data, dict):
        data = {}
    if res.status_code == 200:
        return data
    reason = str(data.get("message") or res.text)[:500]
    kind = data.get("error")
    if res.status_code == 401:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=err("firewall.token_refused",
                       "The firewall helper refused the API's token: GSS_FIREWALL_TOKEN must be the "
                       "same for console-api and console-firewall."),
        )
    if kind == "refused":
        problems = [str(p)[:200] for p in (data.get("problems") or [])][:20]
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail={**err("firewall.refused",
                          "The firewall helper refuses this state: {reason}", reason=reason),
                    "problems": problems},
        )
    if kind == "plan_changed":
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail=err("firewall.plan_changed",
                       "The rules or the servers changed since the preview. Check the new preview "
                       "and apply again."),
        )
    if kind == "backend":
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=err("firewall.backend",
                       "The firewall helper cannot use iptables on this host: {reason}", reason=reason),
        )
    raise HTTPException(
        status.HTTP_502_BAD_GATEWAY,
        detail=err("firewall.helper_failed", "The firewall helper failed ({reason}).", reason=reason),
    )


def _check_enabled() -> None:
    if not enabled():
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail=err("firewall.off",
                       "The firewall helper is off. Set GSS_FIREWALL_TOKEN and add `firewall` to "
                       "COMPOSE_PROFILES in the .env, then `docker compose up -d`."),
        )
    if len(settings.firewall_token) < TOKEN_MIN_LENGTH:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail=err("firewall.token_too_short",
                       "GSS_FIREWALL_TOKEN is shorter than {minimum} characters; the helper does "
                       "not start with it.", minimum=TOKEN_MIN_LENGTH),
        )


async def preview(session: Session) -> dict:
    """The helper's plan for the current desired state (changes nothing)."""
    _check_enabled()
    return await _call("POST", "/v1/plan", {"desired": desired(session)})


async def apply(session: Session, plan_id: str) -> dict:
    """Apply the current desired state, if it is still exactly the plan with this id."""
    _check_enabled()
    return await _call("POST", "/v1/apply", {"desired": desired(session), "plan_id": plan_id})


async def close_stale() -> list[dict]:
    """Close the ports that are open but no longer wanted (the scheduler, every minute).

    Takes the state the helper last applied and keeps only the allows that are still wanted;
    nothing is added, the source networks stay as they were applied. Returns what was closed.
    """
    if not enabled() or len(settings.firewall_token) < TOKEN_MIN_LENGTH:
        return []
    try:
        state = await _call("GET", "/v1/state")
    except HTTPException as e:
        log.debug("firewall: close check skipped (%s)", e.detail)
        return []
    stored = state.get("stored")
    if not stored or not stored.get("allows"):
        return []
    with Session(engine) as session:
        wanted = {_allow_key(a) for a in desired(session)["allows"]}
    keep = [a for a in stored["allows"] if _allow_key(a) in wanted]
    closed = [a for a in stored["allows"] if _allow_key(a) not in wanted]
    if not closed:
        return []
    narrowed = {"allows": keep, "sources": stored.get("sources") or []}
    try:
        plan = await _call("POST", "/v1/plan", {"desired": narrowed})
        await _call("POST", "/v1/apply", {"desired": narrowed, "plan_id": plan.get("plan_id", "")})
    except HTTPException as e:
        log.warning("firewall: closing %d port(s) failed (%s)", len(closed), e.detail)
        return []
    labels = ", ".join(f"{a['server']} {a['port']}/{a['proto']}" for a in closed)
    log.info("firewall: closed %s", labels)
    with Session(engine) as session:
        record(session, None, "firewall.close", labels, json.dumps(closed))
    return closed
