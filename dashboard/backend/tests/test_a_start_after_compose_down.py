"""A stopped server after `docker compose down` + `up -d`: the start names the cause and the way out.

`down` removes the game network when no RUNNING container is attached to it. A stopped game
container keeps the old network's id, `up` creates the network anew with a new id, and
`docker start` then answers 404 "failed to set up container networking: network <id> not found"
— about the network, not the container. Found by the functional check of v0.1.0: the start
answered 502 with httpx's text, which names the socket proxy's URL and not the cause
("Client error '404 Not Found' for url 'http://console-dockerproxy:2375/containers/…/start'").

Reconnecting the container would need `/networks/…`, which the socket proxy does not grant
(`NETWORKS` is off in docker-compose.yml). The way back is a new container: Recreate.

The Docker side is faked at the HTTP level (`httpx.MockTransport`), so the real `DockerClient`,
the real `lifecycle.start_server` and the real `perform_action` run.
"""
from __future__ import annotations

import asyncio

import httpx
import pytest
from fastapi import HTTPException

from app.models import Role, User
from app.models.server import ConsoleKind, GameServer, GameType, ManagementMode
from app.routers import server as server_router
from app.services import docker_client
from app.services.docker_client import DockerClient

ADMIN = User(id=1, display_name="Admin", username="admin", pw_hash="x", role=Role.admin)
PROXY = "http://console-dockerproxy:2375"
GONE = "failed to set up container networking: network 6508f1c2d3e4 not found"


def _server(key: str) -> GameServer:
    return GameServer(
        key=key, container_name=key, game_type=GameType.minecraft,
        management_mode=ManagementMode.dashboard,
        data_dir=f"/srv/gameservers/{key}", image_repo="itzg/minecraft-server", image_tag="t",
        console_kind=ConsoleKind.source_rcon, rcon_port=25575,
        game_ports='{"game":"25565/tcp"}', game_version="1.21.4",
    )


def _docker(monkeypatch, start: httpx.Response) -> None:
    """Docker behind the proxy: no running containers, and `start` answers with `start`."""
    real = httpx.AsyncClient

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/containers/json":
            return httpx.Response(200, json=[])
        if request.method == "POST" and request.url.path.endswith("/start"):
            return start
        pytest.fail(f"unexpected request {request.method} {request.url}")

    monkeypatch.setattr(docker_client.httpx, "AsyncClient",
                        lambda **kw: real(transport=httpx.MockTransport(handler), **kw))

    async def _fits(server):
        return True, 0, []

    async def _no_blockers(server):
        return []

    monkeypatch.setattr(server_router.budget_svc, "can_start", _fits)
    monkeypatch.setattr(server_router.mod_state, "guard_boot", _no_blockers)


def _start(key: str) -> HTTPException:
    with pytest.raises(HTTPException) as ei:
        asyncio.run(server_router.perform_action(_server(key), "start", None, ADMIN))
    return ei.value


def test_dockers_refusal_carries_dockers_message_not_the_proxy_url(monkeypatch):
    _docker(monkeypatch, httpx.Response(404, json={"message": GONE}))
    with pytest.raises(httpx.HTTPStatusError) as ei:
        asyncio.run(DockerClient(PROXY).action("mc-net-1", "start"))
    assert ei.value.response.status_code == 404
    assert str(ei.value) == f"404: {GONE}"
    assert "console-dockerproxy" not in str(ei.value)
    assert docker_client.missing_network(ei.value) == "6508f1c2d3e4"


def test_a_start_on_a_removed_network_names_recreate(monkeypatch):
    _docker(monkeypatch, httpx.Response(404, json={"message": GONE}))
    e = _start("mc-net-2")
    assert e.status_code == 409 and e.detail["code"] == "server.network_missing"
    assert e.detail["params"] == {"key": "mc-net-2"}
    assert "Recreate" in e.detail["message"]
    assert "http://" not in e.detail["message"]


def test_a_missing_container_is_not_a_missing_network(monkeypatch):
    _docker(monkeypatch, httpx.Response(404, json={"message": "No such container: mc-net-3"}))
    e = _start("mc-net-3")
    assert e.status_code == 502 and e.detail["code"] == "server.docker_action_failed"
    assert e.detail["params"] == {"error": "404: No such container: mc-net-3"}
    assert "http://" not in e.detail["message"]


def test_dockers_port_refusal_reaches_the_port_answer(monkeypatch):
    # The same cause, one step further: `ports.docker_error` looks for Docker's text, and with
    # httpx's text it never found it — a held host port came back as a raw 502 as well.
    msg = ("driver failed programming external connectivity on endpoint mc-net-4: Bind for "
           "0.0.0.0:25565 failed: port is already allocated")
    _docker(monkeypatch, httpx.Response(500, json={"message": msg}))
    e = _start("mc-net-4")
    assert e.status_code == 409 and e.detail["code"] == "server.port_in_use_host"


def test_a_body_without_json_still_says_something(monkeypatch):
    _docker(monkeypatch, httpx.Response(502, text="Bad Gateway"))
    e = _start("mc-net-5")
    assert e.detail["code"] == "server.docker_action_failed"
    assert e.detail["params"] == {"error": "502: Bad Gateway"}
