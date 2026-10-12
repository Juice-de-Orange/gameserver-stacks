"""Container create payload from a `game_servers` registry row.

For GAME SERVERS this is the only payload builder — `routers/server_ops.py` builds a separate one
for the Reforger helper container, which is not a game server and therefore does not belong here.

Hardening:
- `management_mode == compose` → never creatable through the API (taking over a compose-managed
  server is a deliberate step).
- `data_dir` MUST lie below an allowed root (`ALLOWED_DATA_ROOTS`, default `DATA_ROOT`): no
  traversal, no arbitrary host path.
- `no-new-privileges` on every created container (see `build_create_payload`).
These are LINUX host paths → `posixpath` (not `os.path`), so that the check is deterministic on
a Windows build as well.
"""
from __future__ import annotations

import json
import posixpath

from ..config import settings
from ..models.server import ConsoleKind, GameServer, ManagementMode
from .drivers import get_driver
from .ports import host_ip


class ContainerSpecError(ValueError):
    """Invalid or unsafe container specification (a guard fired)."""


def _check_data_dir(data_dir: str) -> None:
    """data_dir must lie below an allowed host root (settings.allowed_data_roots), without `..`.

    The default root is `DATA_ROOT`. It is an allowlist of absolute paths — no traversal, no
    arbitrary host path.
    """
    if not data_dir:
        raise ContainerSpecError("data_dir is empty")
    if ".." in data_dir.split("/"):
        raise ContainerSpecError("data_dir contains '..'")
    norm = posixpath.normpath(data_dir)
    # Defensive: drop empty and "/" roots (fail closed) so that the allowlist can never be open.
    roots = [r.rstrip("/") for r in settings.allowed_data_roots if r and r.startswith("/") and r.strip("/")]
    if not any(norm == r or norm.startswith(r + "/") for r in roots):
        raise ContainerSpecError(f"data_dir must be under {roots}: {data_dir!r}")


def image_ref(repo: str, tag: str) -> str:
    """`repo:tag` — or `repo@sha256:…` if a digest is pinned instead of a tag.

    Docker separates a digest with `@`, not `:`. Without this distinction an image that has no
    stable tags could not be pinned at all: `ghcr.io/acemod/arma-reforger` carries exactly three
    tags (`edge`, `latest`, `sha-<commit>`), and `latest` moves on upstream while an older image
    is still running locally. A digest is the only reference that cannot move under the server.
    """
    if not tag:
        return repo
    return f"{repo}@{tag}" if tag.startswith("sha256:") else f"{repo}:{tag}"


def build_create_payload(server: GameServer, secret: str) -> dict:
    """Build the Docker `/containers/create` payload for a dashboard-managed server.

    The driver supplies the game-specific environment and binds; ports, resources and limits
    come from the registry row, including `HostConfig.Memory` (from `ram_mb`) and
    `HostConfig.NanoCpus` (from `cpu_cores`).
    """
    # GUARD: compose-managed rows belong to Docker Compose — never (re)create them via the API.
    if server.management_mode == ManagementMode.compose:
        raise ContainerSpecError("compose-managed servers must not be created through the API")
    _check_data_dir(server.data_dir)
    if not server.image_repo:
        raise ContainerSpecError("image_repo is missing")  # nothing to create without an image

    driver = get_driver(server)
    env = driver.container_env(server, secret)
    binds = driver.container_binds(server)

    ports = json.loads(server.game_ports or "{}")  # {"game": "16271/udp", "direct": "16272/udp"}
    # 0.0.0.0 leaves `HostIp` out (Docker's default: every interface, IPv4 and IPv6).
    ip = host_ip(server.bind_address)
    exposed: dict = {}
    bindings: dict = {}
    for spec in ports.values():
        exposed[spec] = {}
        binding = {"HostPort": spec.split("/")[0]}  # host port == container port
        if ip:
            binding = {"HostIp": ip, **binding}
        bindings[spec] = [binding]

    # RCON is only EXPOSED, never published (reachable inside the Docker network only).
    if server.console_kind == ConsoleKind.source_rcon and server.rcon_port:
        exposed[f"{server.rcon_port}/tcp"] = {}

    host_config: dict = {
        "Binds": binds,
        "PortBindings": bindings,
        "NetworkMode": server.network or settings.game_network,
        # `unless-stopped`: survives a host reboot (a public server comes back by itself) but
        # does NOT start after an explicit `docker stop` (compatible with the idle auto-stop,
        # which stops on purpose).
        "RestartPolicy": {"Name": "unless-stopped"},
        # A process in the container cannot gain privileges through setuid binaries or file
        # capabilities. Dropping privileges still works, so entrypoints that start as root and
        # switch to a game user are not affected.
        #
        # `CapDrop` is deliberately NOT set globally: an image that starts as root needs
        # Docker's default capabilities in its entrypoint — the Valheim image does its set-up
        # (supervisor, SteamCMD) as root and only runs the game process as PUID/PGID — and which
        # capabilities an image needs is not recorded per driver. Nothing is added either: the
        # optional SYS_NICE of the Valheim compose block (thread priority; the server runs
        # without it) is not granted here.
        "SecurityOpt": ["no-new-privileges"],
    }
    if server.ram_mb and server.ram_mb > 0:
        mem_bytes = server.ram_mb * 1024 * 1024
        host_config["Memory"] = mem_bytes
        # MemorySwap == Memory → no swap beyond the cgroup limit (a hard, predictable cap). It
        # also avoids Docker's 409 "Memory limit should be smaller than already set memoryswap"
        # on a later /update.
        host_config["MemorySwap"] = mem_bytes
    if server.cpu_cores and server.cpu_cores > 0:
        host_config["NanoCpus"] = int(server.cpu_cores * 1_000_000_000)

    image = image_ref(server.image_repo, server.image_tag)
    payload: dict = {
        "Image": image,
        "Env": [f"{k}={v}" for k, v in env.items()],
        "ExposedPorts": exposed,
        # Grace period on stop. Without this field Docker's default of 10 seconds applies and a
        # game server is killed in the middle of saving its world. A compose file sets
        # `stop_grace_period` for this; dashboard-created containers do not inherit it because
        # they are not created through Compose.
        "StopTimeout": driver.stop_timeout(server),
        "HostConfig": host_config,
    }
    # Only set if the driver asks for it — otherwise the image default stays. It is needed where
    # the game server would run under a different uid than `console-api`, which writes to the
    # same files (see `GameDriver.container_user`).
    if (user := driver.container_user(server)):
        payload["User"] = user
    return payload
