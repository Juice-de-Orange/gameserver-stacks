"""Reachability assistant: the checklist, the check from inside and the opt-in check from outside.

No test reaches the network: every HTTP call of `services/reachability/outside.py` goes through
`outside._transport`, which an autouse fixture sets to a transport that fails the test on any
request it was not told about. `1.1.1.1` stands for "some internet address" — the documentation
ranges (203.0.113.0/24 …) are not global, and the outside check refuses them on purpose.
"""
import asyncio
import json

import httpx
import pytest
from fastapi import HTTPException
from sqlmodel import Session, select

from app.config import Settings, settings
from app.models import AuditLog, Role, User
from app.models.server import ConsoleKind, GameServer, GameType
from app.routers import reachability as router
from app.services import a2s, install_settings
from app.services.reachability import checklist, inside, outside
from app.services.rcon import RconError

ADMIN = User(id=1, display_name="Admin", username="admin", pw_hash="x", role=Role.admin)
INTERNET_IP = "1.1.1.1"


def _srv(game=GameType.minecraft, ports=None, bind="0.0.0.0", public="", key="srv-1", **kw) -> GameServer:
    return GameServer(
        id=7, key=key, game_type=game, container_name=f"gss-{key}", bind_address=bind,
        game_ports=json.dumps(ports if ports is not None else {"game": "25565/tcp"}),
        extra=json.dumps({"public_host": public} if public else {}), **kw,
    )


class Recorder:
    """An `httpx.MockTransport` that answers per host and remembers every request."""

    def __init__(self, answers=None):
        self.requests: list[httpx.Request] = []
        self.answers = answers or {}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        answer = self.answers.get(request.url.host)
        if answer is None:
            pytest.fail(f"unexpected request to {request.url}")
        if isinstance(answer, Exception):
            raise answer
        return answer(request) if callable(answer) else httpx.Response(200, json=answer)


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    """No real network, a fresh cache, and the services on with their default URLs."""
    rec = Recorder()
    monkeypatch.setattr(outside, "_transport", httpx.MockTransport(rec))
    monkeypatch.setattr(outside, "docker", _Docker(running=True))
    outside.clear_cache()
    monkeypatch.setattr(settings, "public_host", "")
    monkeypatch.setattr(install_settings, "_cache", {})
    for name, value in (("reachability_probes", True), ("reachability_steam", True),
                        ("reachability_mcstatus", True), ("reachability_portcheck", True),
                        ("reachability_mcstatus_url", "https://api.mcstatus.io"),
                        ("reachability_portcheck_url", "https://portchecker.io")):
        monkeypatch.setattr(settings, name, value)
    yield
    outside.clear_cache()


def _use(monkeypatch, answers) -> Recorder:
    rec = Recorder(answers)
    monkeypatch.setattr(outside, "_transport", httpx.MockTransport(rec))
    return rec


# ------------------------------------------------------------------ checklist

def test_the_check_from_outside_is_off_unless_switched_on():
    fresh = Settings()
    assert fresh.reachability_probes is False
    assert (fresh.reachability_steam, fresh.reachability_mcstatus, fresh.reachability_portcheck) == (True, True, True)


def test_on_and_off_as_written_in_env_example(monkeypatch):
    monkeypatch.setenv("GSS_REACHABILITY_PROBES", "on")
    monkeypatch.setenv("GSS_REACHABILITY_STEAM", "off")
    loaded = Settings()
    assert loaded.reachability_probes is True and loaded.reachability_steam is False


def test_ports_come_from_the_row_in_port_order():
    s = _srv(GameType.valheim, {"crossplay": "2458/udp", "game": "2456/udp", "a2s": "2457/udp", "bad": "x"})
    assert [(p.name, p.port, p.proto) for p in checklist.ports_of(s)] == [
        ("game", 2456, "udp"), ("a2s", 2457, "udp"), ("crossplay", 2458, "udp"),
    ]


@pytest.mark.parametrize(("bind", "scope"), [
    ("0.0.0.0", "all"), ("", "all"), ("::", "all"), ("127.0.0.1", "loopback"), ("::1", "loopback"),
    ("192.168.1.20", "private"), ("10.0.0.5", "private"), (INTERNET_IP, "public"),
])
def test_bind_scope(bind, scope):
    assert checklist.bind_scope(bind) == scope


def test_the_public_address_says_where_it_comes_from(monkeypatch):
    assert checklist.public_address(_srv()) == {"host": "", "source": "none", "kind": "none"}
    monkeypatch.setattr(settings, "public_host", "play.example.com")
    assert checklist.public_address(_srv()) == {"host": "play.example.com", "source": "installation", "kind": "name"}
    assert checklist.public_address(_srv(public="192.168.1.20"))["kind"] == "private_ip"
    assert checklist.public_address(_srv(public=INTERNET_IP)) == {
        "host": INTERNET_IP, "source": "server", "kind": "public_ip"}


def test_commands_cover_ufw_docker_user_and_nftables_per_port():
    s = _srv(GameType.vintagestory, {"game": "42420/tcp", "game_udp": "42420/udp"})
    cmds = checklist.checklist(s)["commands"]
    assert cmds["ufw"] == [
        "sudo ufw route allow proto tcp from any to any port 42420",
        "sudo ufw route allow proto udp from any to any port 42420",
    ]
    assert "sudo iptables -I DOCKER-USER -p udp -m conntrack --ctorigdstport 42420 --ctdir ORIGINAL -j ACCEPT" in cmds["iptables"]
    assert "sudo ip6tables -I DOCKER-USER -p tcp -m conntrack --ctorigdstport 42420 --ctdir ORIGINAL -j ACCEPT" in cmds["iptables"]
    assert cmds["nftables"] == [
        "sudo nft insert rule inet filter forward tcp dport 42420 accept",
        "sudo nft insert rule inet filter forward udp dport 42420 accept",
    ]


def test_a_loopback_bind_gets_no_commands():
    out = checklist.checklist(_srv(bind="127.0.0.1"))
    assert out["bind"] == {"address": "127.0.0.1", "scope": "loopback"}
    assert out["commands"] == {"ufw": [], "iptables": [], "nftables": []}


# ------------------------------------------------------------------ which service checks what

def _plan(server):
    return {(r["name"], r["proto"]): (r["service"], r["state"]) for r in outside.plan(server)}


def test_the_plan_per_game():
    assert _plan(_srv()) == {("game", "tcp"): ("mcstatus", "on")}
    assert _plan(_srv(GameType.valheim, {"game": "2456/udp", "a2s": "2457/udp", "crossplay": "2458/udp"})) == {
        ("game", "udp"): ("steam", "on"), ("a2s", "udp"): ("steam", "on"), ("crossplay", "udp"): ("none", "none"),
    }
    assert _plan(_srv(GameType.factorio, {"game": "34197/udp"})) == {("game", "udp"): ("none", "none")}
    assert _plan(_srv(GameType.avorion, {"game": "27000/tcp", "game_udp": "27000/udp", "query": "27003/udp",
                                         "steam_query": "27020/udp"})) == {
        ("game", "tcp"): ("portcheck", "on"), ("game_udp", "udp"): ("steam", "on"),
        ("query", "udp"): ("none", "none"), ("steam_query", "udp"): ("steam", "on"),
    }
    assert _plan(_srv(GameType.vintagestory, {"game": "42420/tcp", "game_udp": "42420/udp"})) == {
        ("game", "tcp"): ("portcheck", "on"), ("game_udp", "udp"): ("none", "none"),
    }


def test_switches_change_the_plan(monkeypatch):
    monkeypatch.setattr(settings, "reachability_mcstatus", False)
    assert _plan(_srv()) == {("game", "tcp"): ("portcheck", "on")}, "Minecraft falls back to the TCP check"
    monkeypatch.setattr(settings, "reachability_portcheck", False)
    assert _plan(_srv()) == {("game", "tcp"): ("portcheck", "off")}
    monkeypatch.setattr(settings, "reachability_steam", False)
    assert _plan(_srv(GameType.pz, {"game": "16261/udp"})) == {("game", "udp"): ("steam", "off")}


def test_the_disclosure_names_every_service_and_its_address(monkeypatch):
    monkeypatch.setattr(settings, "reachability_portcheck_url", "https://ports.example.com/")
    d = outside.disclosure(_srv())
    by_id = {s["id"]: s for s in d["services"]}
    assert by_id["steam"]["url"] == "https://api.steampowered.com/ISteamApps/GetServersAtAddress/v1/"
    assert by_id["mcstatus"]["url"] == "https://api.mcstatus.io/v2/status/java/"
    assert by_id["portcheck"]["url"] == "https://ports.example.com/api/query"
    assert {s["sends"] for s in d["services"]} == {"ip", "ip_port", "ip_ports"}
    assert d["enabled"] is True and d["cache_s"] == 60


# ------------------------------------------------------------------ nothing is sent when …

@pytest.mark.parametrize(("server", "code"), [
    (_srv(bind="127.0.0.1", public=INTERNET_IP), "reachability.bound_to_loopback"),
    (_srv(), "reachability.no_public_address"),
    (_srv(public="192.168.1.20"), "reachability.not_on_internet"),
    (_srv(public="203.0.113.10"), "reachability.not_on_internet"),
])
def test_no_check_and_no_request_without_an_internet_address(server, code):
    out = asyncio.run(outside.check(server))   # the autouse transport fails on any request
    assert out["ran"] is False and out["reason"]["code"] == code and out["ports"] == []


def test_a_name_is_resolved_here_and_only_the_ip_is_sent(monkeypatch):
    async def resolve(host):
        assert host == "play.example.com"
        return ["192.168.1.20", INTERNET_IP]
    monkeypatch.setattr(outside, "_resolve_ipv4", resolve)
    rec = _use(monkeypatch, {"api.mcstatus.io": {"online": True}})
    out = asyncio.run(outside.check(_srv(public="play.example.com")))
    assert out["address"] == INTERNET_IP
    assert [str(r.url) for r in rec.requests] == [f"https://api.mcstatus.io/v2/status/java/{INTERNET_IP}:25565"]
    assert "play.example.com" not in str(rec.requests[0].url)


def test_a_stopped_server_is_not_checked(monkeypatch):
    monkeypatch.setattr(outside, "docker", _Docker(running=False))
    out = asyncio.run(outside.check(_srv(public=INTERNET_IP)))
    assert out["ran"] is False and out["reason"]["code"] == "reachability.not_running"
    monkeypatch.setattr(outside, "docker", _Docker(fail=True))
    out = asyncio.run(outside.check(_srv(public=INTERNET_IP)))
    assert out["reason"]["code"] == "reachability.state_unknown"


def test_an_unresolvable_name_sends_nothing(monkeypatch):
    async def resolve(host):
        raise OSError("Name or service not known")
    monkeypatch.setattr(outside, "_resolve_ipv4", resolve)
    out = asyncio.run(outside.check(_srv(public="nowhere.example.com")))
    assert out["reason"]["code"] == "reachability.unresolvable"


# ------------------------------------------------------------------ the three services

def test_minecraft_online_and_offline(monkeypatch):
    rec = _use(monkeypatch, {"api.mcstatus.io": {"online": True}})
    out = asyncio.run(outside.check(_srv(public=INTERNET_IP)))
    assert [(p["status"], p["detail"]["code"]) for p in out["ports"]] == [("reachable", "reachability.minecraft_online")]
    assert rec.requests[0].headers["user-agent"] == outside.USER_AGENT
    outside.clear_cache()
    _use(monkeypatch, {"api.mcstatus.io": {"online": False}})
    out = asyncio.run(outside.check(_srv(public=INTERNET_IP)))
    assert out["ports"][0]["status"] == "unreachable"


def test_steam_sends_only_the_address_and_matches_game_and_query_port(monkeypatch):
    listing = {"response": {"success": True, "servers": [
        {"addr": f"{INTERNET_IP}:2457", "gameport": 2456, "appid": 892970},
    ]}}
    rec = _use(monkeypatch, {"api.steampowered.com": listing})
    s = _srv(GameType.valheim, {"game": "2456/udp", "a2s": "2457/udp", "crossplay": "2458/udp"}, public=INTERNET_IP)
    out = asyncio.run(outside.check(s))
    assert len(rec.requests) == 1, "one listing serves every port"
    assert dict(rec.requests[0].url.params) == {"addr": INTERNET_IP}, "no key, no port, no name"
    assert [(p["name"], p["status"]) for p in out["ports"]] == [
        ("game", "listed"), ("a2s", "listed"), ("crossplay", "not_checkable"),
    ]


def test_steam_not_listed_names_the_other_ports_and_is_softer_for_reforger(monkeypatch):
    listing = {"response": {"success": True, "servers": [{"addr": f"{INTERNET_IP}:27016", "gameport": 27015}]}}
    _use(monkeypatch, {"api.steampowered.com": listing})
    out = asyncio.run(outside.check(_srv(GameType.pz, {"game": "16261/udp"}, public=INTERNET_IP)))
    assert out["ports"][0]["status"] == "not_listed"
    assert out["ports"][0]["detail"]["params"]["others"] == "27015"
    out = asyncio.run(outside.check(_srv(GameType.arma_reforger, {"game": "2001/udp"}, public=INTERNET_IP)))
    assert out["ports"][0]["detail"]["code"] == "reachability.steam_not_listed_reforger"


def test_portcheck_sends_address_and_ports_in_one_request(monkeypatch):
    def answer(request):
        assert json.loads(request.content) == {"host": INTERNET_IP, "ports": [27000]}
        return httpx.Response(200, json={"error": False, "msg": None, "host": INTERNET_IP,
                                         "check": [{"port": 27000, "status": False}]})
    rec = _use(monkeypatch, {"portchecker.io": answer, "api.steampowered.com": {"response": {"success": True}}})
    s = _srv(GameType.avorion, {"game": "27000/tcp", "game_udp": "27000/udp"}, public=INTERNET_IP)
    out = asyncio.run(outside.check(s))
    assert {r.url.host for r in rec.requests} == {"portchecker.io", "api.steampowered.com"}
    assert [(p["name"], p["status"]) for p in out["ports"]] == [("game", "unreachable"), ("game_udp", "not_listed")]


def test_a_failing_service_is_a_result_not_a_500(monkeypatch):
    _use(monkeypatch, {"portchecker.io": httpx.ConnectTimeout("slow"),
                       "api.steampowered.com": {"response": {"success": True, "servers": [{"gameport": 27000}]}}})
    s = _srv(GameType.avorion, {"game": "27000/tcp", "game_udp": "27000/udp"}, public=INTERNET_IP)
    out = asyncio.run(outside.check(s))
    rows = {p["name"]: p for p in out["ports"]}
    assert rows["game"]["status"] == "error"
    assert rows["game"]["detail"]["params"] == {"service": "portchecker.io", "reason": "timeout"}
    assert rows["game_udp"]["status"] == "listed", "the other service still answers"


def test_steam_success_false_is_an_error(monkeypatch):
    _use(monkeypatch, {"api.steampowered.com": {"response": {"success": False, "message": "bad addr"}}})
    out = asyncio.run(outside.check(_srv(GameType.pz, {"game": "16261/udp"}, public=INTERNET_IP)))
    assert out["ports"][0]["status"] == "error"
    assert out["ports"][0]["detail"]["params"]["reason"] == "bad addr"


def test_a_switched_off_service_is_not_called(monkeypatch):
    monkeypatch.setattr(settings, "reachability_steam", False)
    out = asyncio.run(outside.check(_srv(GameType.pz, {"game": "16261/udp", "direct": "16262/udp"}, public=INTERNET_IP)))
    assert [p["status"] for p in out["ports"]] == ["service_off", "not_checkable"]
    assert out["ports"][0]["detail"]["params"] == {"service": "Steam Web API"}


def test_answers_are_cached_briefly(monkeypatch):
    rec = _use(monkeypatch, {"api.mcstatus.io": {"online": True}})
    s = _srv(public=INTERNET_IP)
    asyncio.run(outside.check(s))
    asyncio.run(outside.check(s))
    assert len(rec.requests) == 1
    monkeypatch.setattr(outside, "CACHE_TTL_S", 0.0)
    asyncio.run(outside.check(s))
    assert len(rec.requests) == 2


def test_a_self_hosted_instance_replaces_the_public_one(monkeypatch):
    monkeypatch.setattr(settings, "reachability_mcstatus_url", "https://mc.example.com/")
    rec = _use(monkeypatch, {"mc.example.com": {"online": True}})
    asyncio.run(outside.check(_srv(public=INTERNET_IP)))
    assert str(rec.requests[0].url) == f"https://mc.example.com/v2/status/java/{INTERNET_IP}:25565"


# ------------------------------------------------------------------ the router

def test_the_outside_route_refuses_while_switched_off(monkeypatch, mem_engine):
    monkeypatch.setattr(settings, "reachability_probes", False)
    with Session(mem_engine) as s, pytest.raises(HTTPException) as ei:
        asyncio.run(router.check_outside(user=ADMIN, server=_srv(public=INTERNET_IP), session=s))
    assert ei.value.status_code == 409 and ei.value.detail["code"] == "reachability.probes_off"
    out = router.get_reachability(_=ADMIN, server=_srv())
    assert out["outside"]["enabled"] is False
    assert out["ports"] == [{"name": "game", "port": 25565, "proto": "tcp"}]


def test_a_check_that_ran_is_audited(monkeypatch, mem_engine):
    _use(monkeypatch, {"api.mcstatus.io": {"online": True}})
    with Session(mem_engine) as s:
        asyncio.run(router.check_outside(user=ADMIN, server=_srv(public=INTERNET_IP), session=s))
        asyncio.run(router.check_outside(user=ADMIN, server=_srv(), session=s))   # did not run
        rows = [(a.action, a.target, a.payload) for a in s.exec(select(AuditLog)).all()]
    assert rows == [("reachability.outside", "srv-1", "mcstatus")]


def test_every_route_needs_an_admin(api_client):
    from app.auth.deps import require_admin
    from app.main import app
    from tests.route_walk import flat_routes

    found = {}
    for r in flat_routes(app):
        if getattr(r, "path", "").startswith("/api/servers/{key}/reachability"):
            for m in r.methods:
                found[(m, r.path)] = {d.call for d in r.dependant.dependencies}
    assert set(found) == {
        ("GET", "/api/servers/{key}/reachability"),
        ("POST", "/api/servers/{key}/reachability/inside"),
        ("POST", "/api/servers/{key}/reachability/outside"),
    }
    assert all(require_admin in deps for deps in found.values())
    assert api_client.get("/api/servers/srv-1/reachability").status_code == 401
    assert api_client.post("/api/servers/srv-1/reachability/outside").status_code == 401


# ------------------------------------------------------------------ check from inside

class _Docker:
    def __init__(self, running=True, published=(), fail=False):
        self.running, self.published, self.fail = running, published, fail

    async def state(self, name):
        if self.fail:
            raise RuntimeError("proxy down")
        return {"running": self.running}

    async def running_containers(self):
        return [
            {"Names": ["/someone-else"], "Ports": [{"IP": "0.0.0.0", "PublicPort": 25565, "Type": "tcp"}]},
            {"Names": ["/gss-srv-1"], "Ports": [{"IP": ip, "PublicPort": port, "Type": proto}
                                                 for ip, port, proto in self.published]},
        ]


def _inside(monkeypatch, docker, driver, server=None):
    monkeypatch.setattr(inside, "docker", docker)
    monkeypatch.setattr(inside, "get_driver", lambda s: driver)
    return asyncio.run(inside.check(server or _srv(rcon_port=25575)))


def test_inside_reports_published_ports_and_the_rcon_answer(monkeypatch, fake_driver_cls):
    out = _inside(monkeypatch, _Docker(published=[("0.0.0.0", 25565, "tcp"), ("::", 25565, "tcp")]),
                  fake_driver_cls(names=["a"]))
    assert out["ran"] is True
    assert out["published"] == [{"name": "game", "port": 25565, "proto": "tcp", "ok": True, "detail": None}]
    assert out["answers"][0]["channel"] == "rcon" and out["answers"][0]["port"] == 25575
    assert out["answers"][0]["ok"] is True and out["answers"][0]["latency_ms"] is not None


def test_inside_finds_a_port_the_container_does_not_publish(monkeypatch, fake_driver_cls):
    out = _inside(monkeypatch, _Docker(published=[]), fake_driver_cls(fail_rcon=True))
    assert out["published"][0]["ok"] is False
    assert out["published"][0]["detail"]["code"] == "reachability.port_not_published"
    assert out["answers"][0] == {
        "channel": "rcon", "port": 25575, "ok": False, "latency_ms": None,
        "error": {"code": "reachability.probe_failed", "message": "down", "params": {"reason": "down"}},
    }


def test_inside_notices_a_container_on_another_address(monkeypatch, fake_driver_cls):
    """The row says loopback, the container was created before: it still listens everywhere."""
    out = _inside(monkeypatch, _Docker(published=[("0.0.0.0", 25565, "tcp"), ("::", 25565, "tcp")]),
                  fake_driver_cls(), _srv(bind="127.0.0.1", rcon_port=25575))
    assert out["published"][0]["ok"] is False
    assert out["published"][0]["detail"]["code"] == "reachability.port_other_address"
    assert out["published"][0]["detail"]["params"] == {"port": "25565/tcp", "actual": "0.0.0.0, ::", "wanted": "127.0.0.1"}
    out = _inside(monkeypatch, _Docker(published=[("127.0.0.1", 25565, "tcp")]),
                  fake_driver_cls(), _srv(bind="127.0.0.1", rcon_port=25575))
    assert out["published"][0]["ok"] is True


def test_inside_needs_a_running_container(monkeypatch, fake_driver_cls):
    assert _inside(monkeypatch, _Docker(running=False), fake_driver_cls())["reason"]["code"] == "reachability.not_running"
    assert _inside(monkeypatch, _Docker(fail=True), fake_driver_cls())["reason"]["code"] == "reachability.docker_unavailable"


def test_inside_asks_the_a2s_port_where_the_console_is_rcon(monkeypatch, fake_driver_cls):
    asked = []

    async def info(host, port, timeout=5.0):
        asked.append((host, port))
        raise RconError("A2S: no response")
    monkeypatch.setattr(a2s, "info", info)
    s = _srv(GameType.arma_reforger, {"game": "2001/udp", "a2s": "17777/udp"}, rcon_port=19999,
             console_kind=ConsoleKind.battleye_rcon)
    out = _inside(monkeypatch, _Docker(published=[("0.0.0.0", 2001, "udp"), ("0.0.0.0", 17777, "udp")]),
                  fake_driver_cls(), s)
    assert asked == [("gss-srv-1", 17777)]
    assert [(a["channel"], a["ok"]) for a in out["answers"]] == [("battleye", True), ("a2s", False)]
    # Valheim's `players()` IS the A2S query: no second one.
    asked.clear()
    v = _srv(GameType.valheim, {"game": "2456/udp", "a2s": "2457/udp"}, console_kind=ConsoleKind.none)
    out = _inside(monkeypatch, _Docker(), fake_driver_cls(), v)
    assert asked == [] and [a["channel"] for a in out["answers"]] == ["query"]
