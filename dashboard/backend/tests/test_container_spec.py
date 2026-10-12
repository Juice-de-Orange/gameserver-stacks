"""build_create_payload — resource mapping, ports, guards (no DB, no Docker)."""
import pytest

from app.models.server import ConsoleKind, GameServer, GameType, ManagementMode
from app.services.container_spec import ContainerSpecError, build_create_payload
from app.services.docker_client import DockerClient


def _server(**over) -> GameServer:
    base = dict(
        key="pz-test",
        game_type=GameType.pz,
        management_mode=ManagementMode.dashboard,
        data_dir="/srv/gameservers/pz-test",
        image_repo="danixu86/project-zomboid-dedicated-server",
        image_tag="latest",
        console_kind=ConsoleKind.source_rcon,
        rcon_port=27016,
        game_ports='{"game":"16271/udp","direct":"16272/udp"}',
        ram_mb=4096,
        cpu_cores=2.0,
        heap_max_mb=4096,
        heap_min_mb=2048,
        presence_visible=False,
    )
    base.update(over)
    return GameServer(**base)


def test_resources_and_ports():
    p = build_create_payload(_server(), "s3cr3t")
    hc = p["HostConfig"]
    assert hc["Memory"] == 4096 * 1024 * 1024
    assert hc["MemorySwap"] == 4096 * 1024 * 1024  # == Memory → no swap (hard cap)
    assert hc["NanoCpus"] == 2_000_000_000
    # Game ports are published, RCON only exposed (internal).
    assert set(p["ExposedPorts"]) == {"16271/udp", "16272/udp", "27016/tcp"}
    assert set(hc["PortBindings"]) == {"16271/udp", "16272/udp"}
    assert hc["PortBindings"]["16271/udp"] == [{"HostPort": "16271"}]
    assert hc["RestartPolicy"] == {"Name": "unless-stopped"}  # survives a reboot
    assert hc["NetworkMode"] == "gameserver-net"
    assert "Labels" not in p
    assert p["Image"] == "danixu86/project-zomboid-dedicated-server:latest"
    assert hc["Binds"] == [
        "/srv/gameservers/pz-test/data:/home/steam/Zomboid",
        "/srv/gameservers/pz-test/workshop-mods:/home/steam/pz-dedicated/steamapps/workshop",
    ]
    env = dict(e.split("=", 1) for e in p["Env"])
    assert env["ADMINPASSWORD"] == "s3cr3t" and env["RCONPASSWORD"] == "s3cr3t"
    assert env["MAX_MEMORY"] == "4096m" and env["MIN_MEMORY"] == "2048m"


def test_created_containers_cannot_gain_privileges():
    """`no-new-privileges` on every game container; capabilities are left at Docker's default
    (nothing added, nothing dropped — see the comment in `build_create_payload`)."""
    hc = build_create_payload(_server(), "x")["HostConfig"]
    assert hc["SecurityOpt"] == ["no-new-privileges"]
    assert "Privileged" not in hc and "CapAdd" not in hc and "CapDrop" not in hc


def test_no_cap_keys_when_zero():
    p = build_create_payload(_server(ram_mb=0, cpu_cores=0.0), "x")
    assert "Memory" not in p["HostConfig"]
    assert "MemorySwap" not in p["HostConfig"]
    assert "NanoCpus" not in p["HostConfig"]


def test_image_without_tag():
    p = build_create_payload(_server(image_tag=""), "x")
    assert p["Image"] == "danixu86/project-zomboid-dedicated-server"


@pytest.mark.parametrize("bad", ["/etc", "/srv/gameserversx", "/srv/gameservers/../x", "", "relative/dir"])
def test_data_dir_whitelist_rejects(bad):
    with pytest.raises(ContainerSpecError):
        build_create_payload(_server(data_dir=bad), "x")


def test_compose_guard():
    with pytest.raises(ContainerSpecError):
        build_create_payload(_server(management_mode=ManagementMode.compose), "x")


def test_missing_image_repo():
    with pytest.raises(ContainerSpecError):
        build_create_payload(_server(image_repo=""), "x")


def test_allowlist_fails_closed(monkeypatch):
    """An empty or '/' root must NOT open the allowlist."""
    from app.config import settings
    from app.services.container_spec import _check_data_dir

    monkeypatch.setattr(settings, "allowed_data_roots", ["", "/", "/srv/gameservers"], raising=False)
    with pytest.raises(ContainerSpecError):
        _check_data_dir("/etc")           # rejected despite ""/"/" in the list
    _check_data_dir("/srv/gameservers/mc")     # a valid root is still allowed (no raise)


def test_split_image_does_not_mistake_a_registry_port_for_a_tag():
    """The pull splits "repo:tag" — a registry port ("host:5000/repo") must not pass as a tag,
    otherwise Docker would pull an image that does not exist."""
    split = DockerClient._split_image
    assert split("danixu86/project-zomboid-dedicated-server:42.20.0-release") == (
        "danixu86/project-zomboid-dedicated-server", "42.20.0-release"
    )
    assert split("registry.local:5000/pz") == ("registry.local:5000/pz", "latest")
    assert split("itzg/minecraft-server") == ("itzg/minecraft-server", "latest")


def test_stop_timeout_is_in_the_create_payload():
    """Without this field Docker gives only 10 seconds until SIGKILL — too little for a game
    server that writes its world on stop (the compose equivalent is `stop_grace_period: 120s`)."""
    payload = build_create_payload(_server(), "secret")
    assert payload["StopTimeout"] == 120          # PZ driver: the world needs time


def test_stop_grace_falls_back_when_the_driver_is_unavailable():
    """The helper must never prevent a stop through an error of its own."""
    from types import SimpleNamespace

    from app.services.drivers import stop_grace

    assert stop_grace(_server()) == 120
    assert stop_grace(SimpleNamespace(game_type="no-such-game")) == 60


def test_a_digest_is_joined_with_at_not_with_a_colon():
    """Docker separates a digest with `@`. Without it an image without movable tags could not be
    pinned at all — `ghcr.io/acemod/arma-reforger` carries only a few tags, and `latest` moves on
    upstream while an older image is still running here.
    """
    from app.services.container_spec import image_ref

    d = "sha256:9503881c9fe13f9b0859e8a3680b9235fe6057a191a2599d39428487b935807d"
    assert image_ref("ghcr.io/acemod/arma-reforger", d) == f"ghcr.io/acemod/arma-reforger@{d}"
    assert image_ref("repo", "1.2.3") == "repo:1.2.3"
    assert image_ref("repo", "") == "repo"


def test_the_digest_lands_in_the_payload_the_same_way():
    srv = _server()
    srv.image_tag = "sha256:deadbeef"
    payload = build_create_payload(srv, "s3cret")
    assert payload["Image"].endswith("@sha256:deadbeef")


def test_all_places_build_the_image_reference_the_same_way():
    """A place that assembles `repo:tag` by hand and knows no digests would build `repo:sha256:…`
    for a digest-pinned image (Arma Reforger): a reference Docker does not know.

    The Reforger save-swap helper would fail on it with "no such image" — exactly the save/scenario
    switch. This test pins that there is only ONE source.
    """
    import inspect

    from app.routers import server_ops
    from app.services import container_spec, version_manager

    assert version_manager._image_ref is container_spec.image_ref
    assert server_ops.image_ref is container_spec.image_ref

    # And nobody assembles it by hand next to it again.
    for module in (server_ops, version_manager):
        source = inspect.getsource(module)
        assert 'image_repo}:{' not in source, f"{module.__name__} assembles an image reference by hand"
