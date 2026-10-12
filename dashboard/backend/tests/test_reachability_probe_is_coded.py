"""The check from inside sends why a game does not answer as a coded text, like the panel's
other texts. Before, it sent the raw exception string, so Valheim's "query not open yet"
explanation reached the German UI in English."""
import asyncio

from app.models.server import ConsoleKind, GameServer, GameType
from app.services.a2s import A2sError
from app.services.drivers.valheim import ValheimDriver
from app.services.reachability import inside


def _srv():
    s = GameServer(key="vh-1", game_type=GameType.valheim, container_name="gs-vh-1",
                   console_kind=ConsoleKind.none, game_ports='{"game": "2456/udp"}')
    s.id = 1
    return s


class _Docker:
    async def state(self, name):
        return {"running": True}

    async def running_containers(self):
        return []


def _answer(monkeypatch, players):
    class _Drv:
        async def players(self, server):
            return await players()

    monkeypatch.setattr(inside, "docker", _Docker())
    monkeypatch.setattr(inside, "get_driver", lambda s: _Drv())
    return asyncio.run(inside.check(_srv()))["answers"][0]


def test_the_valheim_explanation_arrives_coded(monkeypatch):
    """The real driver: the container is up, the log has "Game server connected" but not yet
    "Opened Steam server", and the Steam query does not answer."""
    async def no_answer(host, port, timeout=5.0):
        raise A2sError(f"A2S: no response from {host}:{port} within {timeout:g} s")

    async def state(name):
        return {"running": True, "started_at": "2026-10-12T12:03:41.749766336Z"}

    async def logs(name, tail=200, timestamps=False, since=None):
        return "12:05:57: Game server connected\n"

    monkeypatch.setattr("app.services.a2s.info", no_answer)
    monkeypatch.setattr("app.services.docker_client.docker.state", state)
    monkeypatch.setattr("app.services.docker_client.docker.logs", logs)
    monkeypatch.setattr(inside, "docker", _Docker())
    monkeypatch.setattr(inside, "get_driver", lambda s: ValheimDriver())

    error = asyncio.run(inside.check(_srv()))["answers"][0]["error"]
    assert error["code"] == "server.valheim_query_not_open_yet"
    assert error["message"].startswith("Valheim has not opened its Steam query yet.")


def test_a_timeout_is_coded_with_its_number(monkeypatch):
    monkeypatch.setattr(inside, "TIMEOUT_S", 0.01)

    async def silent():
        await asyncio.sleep(1)

    error = _answer(monkeypatch, silent)["error"]
    assert error["code"] == "reachability.probe_timeout"
    assert error["params"] == {"seconds": "0.01"}


def test_foreign_text_travels_as_a_parameter(monkeypatch):
    async def refused():
        raise OSError("[Errno 111] Connection refused")

    error = _answer(monkeypatch, refused)["error"]
    assert error == {"code": "reachability.probe_failed", "message": "[Errno 111] Connection refused",
                     "params": {"reason": "[Errno 111] Connection refused"}}


def test_an_answer_carries_no_error(monkeypatch):
    async def fine():
        return {"count": 0, "names": []}

    answer = _answer(monkeypatch, fine)
    assert answer["ok"] is True and answer["error"] is None
