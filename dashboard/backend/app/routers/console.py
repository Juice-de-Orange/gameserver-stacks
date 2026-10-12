"""Live RCON console over WebSocket. Safe whitelist vs. full (RBAC). Every command is audited.

`?server=<key>` (REQUIRED) selects the target server; the frontend always appends it.
"""
from __future__ import annotations

from types import SimpleNamespace

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from sqlmodel import Session

from ..auth.deps import user_from_ws_ticket, ws_user_recheck
from ..auth.perm import has_permission
from ..db import engine
from ..errors import note
from ..services import servers as servers_registry
from ..services.audit import record
from ..services.drivers import get_driver
from ..services.rcon import RconError

router = APIRouter()

# Default safe set
SAFE_COMMANDS = {"players", "save", "servermsg"}


def _is_safe(cmd: str, allowed: frozenset[str]) -> bool:
    """Is this command on the safe list — as a whole command or as a prefix of it?

    A single-word entry (`/players`) still allows arguments (`/players online`); a multi-word
    entry (`/whitelist get`) allows exactly that subcommand and NOT its siblings
    (`/whitelist clear`).
    """
    norm = " ".join(cmd.strip().lower().split())
    for entry in allowed:
        e = " ".join(entry.strip().lower().split())
        if e and (norm == e or norm.startswith(e + " ")):
            return True
    return False


@router.websocket("/ws/console")
async def console(ws: WebSocket) -> None:
    await ws.accept()
    ticket = ws.query_params.get("ticket", "")
    with Session(engine) as session:
        user = user_from_ws_ticket(ticket, session)

    if user is None or not (
        has_permission(user, "can_console_safe") or has_permission(user, "can_console_full")
    ):
        await ws.close(code=4401)
        return

    # `?server=<key>` is required; there is no default server.
    server_key = ws.query_params.get("server", "")
    if not server_key:
        await ws.close(code=4404)
        return
    srv = servers_registry.get_by_key(server_key)
    if srv is None or not srv.enabled:
        await ws.close(code=4404)  # server unknown/disabled
        return
    try:
        drv = get_driver(srv)
    except NotImplementedError:
        await ws.close(code=4501)  # game type has no driver
        return
    # A reason the console cannot work on this server (Vintage Story without its RCON mod), sent
    # before anyone types a command into nothing.
    notice = getattr(drv, "console_notice", None)
    if callable(notice) and (reason := notice(srv)):
        await ws.send_json({"type": "error", "message": reason})

    full = has_permission(user, "can_console_full")
    uid, uname = user.id, user.username
    # Coded, so the page shows it in the reader's language (`ServerConsole.messageText`).
    connected = (
        note("console.connected_full", "Connected as {username} (full mode)", username=uname)
        if full else
        note("console.connected_safe", "Connected as {username} (safe mode)", username=uname)
    )
    await ws.send_json({"type": "info", "message": connected})

    try:
        while True:
            cmd = (await ws.receive_text()).strip()
            if not cmd:
                continue
            # Re-check auth for every command (like /ws/cross-chat) — otherwise a revoked
            # permission or a disabled/expired account would stay fully effective until
            # disconnect, although this socket issues RCON commands that write.
            with Session(engine) as session:
                cur = ws_user_recheck(user, session)
            if cur is None or not (
                has_permission(cur, "can_console_safe") or has_permission(cur, "can_console_full")
            ):
                await ws.close(code=4401)
                return
            full = has_permission(cur, "can_console_full")
            verb = cmd.split()[0].lower()
            # Safe verbs are per game (PZ players/save/servermsg · MC list/save-all/say ·
            # VS list/stats); the PZ vocabulary would wrongly block e.g. MC `list`/`save-all`.
            #
            # **The start of the command is checked, not just its first word.** For Factorio and
            # Avorion `/whitelist` and `/banlist` are not read verbs but administration verbs
            # with subcommands (`add|remove|get|clear`) — the very ones the moderation methods
            # send and that `routers/moderation.py` ties to the `can_moderate` permission. A gate
            # that only looks at `cmd.split()[0]` would let `/whitelist clear` through because
            # the first word is on the list. The safe list may therefore carry multi-word
            # entries (`/whitelist get`), and the whole prefix counts here.
            if not full and not _is_safe(cmd, drv.safe_console_commands()):
                await ws.send_json(
                    {"type": "error", "message": f"Command '{verb}' is not allowed in safe mode"}
                )
                continue
            try:
                out = await drv.console(srv, cmd)
            except RconError as e:
                # A coded reason where the driver has one (`detail`), otherwise the plain text.
                await ws.send_json({"type": "error", "message": getattr(e, "detail", None) or str(e)})
                continue
            with Session(engine) as session:
                # Through `record()` rather than the model directly, so the truncation rules
                # there cannot be bypassed. `record` needs a user object only for id/username —
                # both are at hand here, hence the lightweight carrier.
                record(
                    session, SimpleNamespace(id=uid, username=uname), "console",
                    srv.key, cmd, server_id=srv.id,
                )
            await ws.send_json({"type": "output", "command": cmd, "output": out})
    except WebSocketDisconnect:
        return
