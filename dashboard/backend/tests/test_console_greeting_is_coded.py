"""The console's greeting is a coded note, so the page shows it in the reader's language.

Found in the German UI: "» Connected as admin (full mode)" stayed English, because the socket
sent a ready-made English sentence. `ServerConsole.messageText` translates `{code, message,
params}` by its code (`console.connected_full` / `console.connected_safe`).
"""
import asyncio
from types import SimpleNamespace

import pytest
from fastapi import WebSocketDisconnect

from app.models import Role, User
from app.routers import console as console_router


class _Ws:
    def __init__(self):
        self.query_params = {"ticket": "t", "server": "mc-1"}
        self.sent: list[dict] = []

    async def accept(self):
        return None

    async def send_json(self, data):
        self.sent.append(data)

    async def receive_text(self):
        raise WebSocketDisconnect()

    async def close(self, code=1000):
        self.sent.append({"closed": code})


@pytest.mark.parametrize("role, code", [(Role.admin, "console.connected_full"),
                                        (Role.member, "console.connected_safe")])
def test_the_greeting_is_coded(monkeypatch, role, code):
    # A member gets the safe console through the per-user flag.
    user = User(id=7, display_name="A", username="alice", pw_hash="x", role=role, can_console_safe=True)
    monkeypatch.setattr(console_router, "user_from_ws_ticket", lambda ticket, s: user)
    monkeypatch.setattr(console_router.servers_registry, "get_by_key",
                        lambda key: SimpleNamespace(key=key, enabled=True))
    monkeypatch.setattr(console_router, "get_driver", lambda srv: SimpleNamespace())
    ws = _Ws()
    asyncio.run(console_router.console(ws))
    greeting = ws.sent[0]
    assert greeting["type"] == "info"
    assert greeting["message"]["code"] == code
    assert greeting["message"]["params"] == {"username": "alice"}
