"""Container control and status ONLY through the Docker socket proxy (never docker.sock directly)."""
from __future__ import annotations

import datetime as dt
import json
import logging
import re

import httpx

from ..config import settings

log = logging.getLogger("uvicorn.error")


def _parse_mem(val: str) -> int | None:
    """JVM/Docker memory string → bytes. '8192m'/'8g'/'2048k'/'1073741824' → int."""
    if not val:
        return None
    m = re.match(r"^(\d+)\s*([kmg])?b?$", val.strip().lower())
    if not m:
        return None
    mult = {None: 1, "k": 1024, "m": 1024 ** 2, "g": 1024 ** 3}[m.group(2)]
    return int(m.group(1)) * mult


def _cpu_percent(d: dict) -> dict:
    """CPU % usage from a single stats sample (contains cpu_stats + precpu_stats)."""
    cpu = d.get("cpu_stats", {})
    pre = d.get("precpu_stats", {})
    cpu_delta = cpu.get("cpu_usage", {}).get("total_usage", 0) - pre.get("cpu_usage", {}).get("total_usage", 0)
    sys_delta = cpu.get("system_cpu_usage", 0) - pre.get("system_cpu_usage", 0)
    ncpu = cpu.get("online_cpus") or len(cpu.get("cpu_usage", {}).get("percpu_usage") or []) or 1
    pct = round(cpu_delta / sys_delta * ncpu * 100, 1) if sys_delta > 0 and cpu_delta > 0 else 0.0
    return {"cpu_percent": pct, "cpu_count": ncpu}


class LogDemuxer:
    """Stateful demuxer for the Docker log stream.

    Docker logs of non-TTY containers are multiplexed: an 8-byte header per frame (stream id +
    size as big-endian). All our containers run without `Tty`
    (`container_spec.build_create_payload` does not set it, the Docker default is `false`), so
    the stream really is framed.

    **Why this needs state:** `_demux` below is stateless and assumes the buffer starts at a
    frame boundary. That holds for a complete response body. In the live tail
    (`routers/logs.py`) data arrives per `aiter_bytes()` chunk, and TCP chunks do not fall on
    frame boundaries. If a boundary lands inside the payload, a stateless call reads 8 payload
    bytes as a header: they vanish, and the phantom size read from them (ASCII as a BE number,
    e.g. `"stea"` → ~1.9 GB) shifts everything after it. The output still looks plausible but
    the text is wrong.
    """

    # Docker caps a frame at 16 KiB. The cap here is more generous and only protects against a
    # phantom size from a broken stream filling up memory.
    MAX_REST = 1 << 20

    def __init__(self) -> None:
        self._rest = b""

    def feed(self, data: bytes) -> str:
        """Feed new bytes; returns the text that can be read completely from them."""
        buffer = self._rest + data
        out: list[str] = []
        i = 0
        while i + 8 <= len(buffer):
            size = int.from_bytes(buffer[i + 4 : i + 8], "big")
            if i + 8 + size > len(buffer):
                break  # frame still incomplete: keep the rest, continue with the next chunk
            i += 8
            out.append(buffer[i : i + size].decode("utf-8", errors="replace"))
            i += size
        self._rest = buffer[i:]
        if len(self._rest) > self.MAX_REST:
            # No valid frame in sight: better to reset than to buffer without bound.
            log.warning("logs: frame buffer exceeds %d bytes — restarting the stream", self.MAX_REST)
            self._rest = b""
        return "".join(out)


def _demux(data: bytes) -> str:
    """Unframe a COMPLETE log body (not for streams, use `LogDemuxer` for those).

    The trailing rest is deliberately included here: for a complete body it is either empty or
    comes from a container without framing (TTY), and then raw text is correct.
    """
    d = LogDemuxer()
    text = d.feed(data)
    return text + d._rest.decode("utf-8", errors="replace")


class DockerError(httpx.HTTPStatusError):
    """Docker refused a request: its status and Docker's OWN message (`{"message": …}`).

    `raise_for_status` names the proxy's URL instead ("Client error '404 Not Found' for url
    'http://console-dockerproxy:2375/containers/…/start'"): no cause, and that is what a failed
    start showed the user. A subclass of `HTTPStatusError`, so whoever catches that keeps working.
    """

    def __init__(self, response: httpx.Response) -> None:
        try:
            body = response.json()
        except ValueError:
            body = None
        message = body.get("message") if isinstance(body, dict) else None
        self.docker_message = str(message or response.text[:300] or response.reason_phrase)
        super().__init__(f"{response.status_code}: {self.docker_message}",
                         request=response.request, response=response)


_NETWORK_GONE = re.compile(r"\bnetwork (\S+) not found")


def missing_network(error: BaseException) -> str | None:
    """The id of the network a container could not start on because it no longer exists.

    `docker compose down` removes the game network when no RUNNING container is attached. A
    stopped game container keeps the old network's id, `up` creates the network with a new one,
    and `docker start` answers 404 "failed to set up container networking: network <id> not
    found" — the container itself exists. Reconnecting it would need `/networks/…`, which the
    socket proxy does not grant (`NETWORKS` is off in docker-compose.yml); the way back is a new
    container. `None` for any other error, a missing container included.
    """
    if isinstance(error, DockerError) and error.response.status_code == 404:
        if m := _NETWORK_GONE.search(error.docker_message):
            return m.group(1)
    return None


class DockerClient:
    def __init__(self, base_url: str) -> None:
        self.base = base_url.rstrip("/")

    async def state(self, name: str) -> dict:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(f"{self.base}/containers/{name}/json")
            if r.status_code == 404:
                return {"exists": False, "running": False, "status": "missing"}
            r.raise_for_status()
            data = r.json()
            s = data.get("State", {})
            # JVM heap ceiling (-Xmx) from the container env, so the RAM display is computed
            # against the real game server limit instead of the host's total RAM.
            max_heap = None
            # The full env is returned: it is the only information about what the RUNNING
            # container actually has set. For Valheim it holds the entire configuration, and
            # that can differ arbitrarily from the registry: `set_config` only writes the
            # registry, which takes effect on the next rebuild.
            container_env: dict[str, str] = {}
            for e in data.get("Config", {}).get("Env", []) or []:
                if "=" in e:
                    k, v = e.split("=", 1)
                    container_env[k] = v
                if e.startswith("MAX_MEMORY="):
                    max_heap = _parse_mem(e.split("=", 1)[1])
            # cgroup limits (multi-game: ram_mb/cpu_cores). 0 == unlimited.
            hc = data.get("HostConfig", {}) or {}
            return {
                "exists": True,
                # **The id, not just the name.** A `create-then-swap` (version change, rebuild)
                # creates a NEW container under the same name; anything that tracks
                # per-container state over time would otherwise confuse it with the removed
                # one. The watchdog counts its boot loop by it.
                "id": data.get("Id") or "",
                "running": bool(s.get("Running")),
                "restarting": bool(s.get("Restarting")),
                "restarts": int(data.get("RestartCount") or 0),   # restarts by `unless-stopped`
                "status": s.get("Status"),
                "started_at": s.get("StartedAt"),
                # When it last stopped. The stats poller uses it to close the sessions still
                # open at stop time at the right moment instead of "now"; otherwise playtime
                # keeps growing over the whole downtime.
                "finished_at": s.get("FinishedAt"),
                "health": (s.get("Health") or {}).get("Status"),
                "max_heap_bytes": max_heap,
                "env": container_env,
                "memory": hc.get("Memory") or 0,
                "nano_cpus": hc.get("NanoCpus") or 0,
            }

    async def container_names(self, name_filter: str) -> list[str]:
        """Names (without the leading `/`) of all containers, running or not, whose name
        contains `name_filter` — Docker's `name` filter is a substring match, so callers check
        the exact pattern themselves. Read-only through the proxy (CONTAINERS+GET)."""
        filters = json.dumps({"name": [name_filter]})
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.get(f"{self.base}/containers/json", params={"all": "1", "filters": filters})
            r.raise_for_status()
            return [n.lstrip("/") for item in r.json() for n in (item.get("Names") or [])]

    async def running_containers(self) -> list[dict]:
        """`GET /containers/json`: the RUNNING containers with their names and published ports
        (`Ports[].IP/PublicPort/Type`). Read-only through the proxy (CONTAINERS+GET); the port
        check (`services/ports.py`) reads who holds a host port."""
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.get(f"{self.base}/containers/json")
            r.raise_for_status()
            return list(r.json() or [])

    async def inspect(self, name: str) -> dict | None:
        """Full `/containers/{name}/json` (or None on 404). Read-only.

        Used by the adoption preflight: the running Compose container is diffed against the
        intended recreate payload before the cutover (compose→dashboard) is executed.
        Read-only through the proxy (CONTAINERS+GET)."""
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.get(f"{self.base}/containers/{name}/json")
            if r.status_code == 404:
                return None
            r.raise_for_status()
            return r.json()

    async def stats(self, name: str) -> dict:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.get(f"{self.base}/containers/{name}/stats?stream=false")
            r.raise_for_status()
            d = r.json()
        mem = d.get("memory_stats", {})
        usage = mem.get("usage", 0) - (mem.get("stats", {}) or {}).get("inactive_file", 0)
        limit = mem.get("limit", 0) or 0
        usage = max(usage, 0)
        # Network throughput since container start; Docker returns it in the same response.
        # These are CUMULATIVE counters, not a rate: for a rate, take the difference of two
        # samples.
        rx = tx = 0
        for iface in (d.get("networks") or {}).values():
            rx += int(iface.get("rx_bytes") or 0)
            tx += int(iface.get("tx_bytes") or 0)
        return {
            "mem_bytes": usage,
            "mem_limit": limit,
            "mem_mib": round(usage / 1048576, 1),
            "mem_percent": round(usage / limit * 100, 1) if limit else None,
            "net_rx": rx,
            "net_tx": tx,
            **_cpu_percent(d),
        }

    async def host_info(self) -> dict:
        """Cores and total memory of the HOST, or an empty result if the proxy does not provide it.

        `GET /info` depends on the socket proxy's `INFO` grant (set in `docker-compose.yml`).
        Without it there is **no guessed result** here: the caller must be able to tell
        "measured" from "not available", otherwise the fleet page labels a budget as host
        load.
        """
        try:
            async with httpx.AsyncClient(timeout=8) as c:
                r = await c.get(f"{self.base}/info")
                r.raise_for_status()
                d = r.json()
        except Exception:
            return {}
        return {
            "cpu_count": int(d.get("NCPU") or 0) or None,
            "mem_total_bytes": int(d.get("MemTotal") or 0) or None,
            "docker_version": d.get("ServerVersion") or "",
        }

    async def action(self, name: str, action: str, *, stop_timeout: int | None = None) -> None:
        """action: start | stop | restart

        `stop_timeout` (seconds) is passed as `?t=` for `stop`/`restart`: the grace period
        between SIGTERM and SIGKILL. It takes effect **immediately on already running
        containers**, unlike `StopTimeout` in the create payload, which only applies after the
        next rebuild. Without either, Docker's default of 10 s applies and a game server is
        killed in the middle of saving the world.
        """
        if action not in {"start", "stop", "restart"}:
            raise ValueError("invalid action")
        query = ""
        if stop_timeout is not None and action in {"stop", "restart"}:
            query = f"?t={int(stop_timeout)}"
        # The HTTP timeout must outlast the grace period, otherwise the client aborts before Docker.
        http_timeout = max(140, int(stop_timeout or 0) + 30)
        async with httpx.AsyncClient(timeout=http_timeout) as c:
            r = await c.post(f"{self.base}/containers/{name}/{action}{query}")
            if r.status_code not in (204, 304):
                # Docker's reason, not the proxy URL: the callers show it (`server.docker_action_failed`,
                # `start-recorded`) and read it (`ports.docker_error`, `missing_network`).
                raise DockerError(r)

    async def logs(self, name: str, tail: int = 200, timestamps: bool = False, since: int | None = None) -> str:
        params = f"stdout=1&stderr=1&tail={tail}"
        if timestamps:
            params += "&timestamps=1"
        if since:
            params += f"&since={int(since)}"
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.get(f"{self.base}/containers/{name}/logs?{params}")
            r.raise_for_status()
            return _demux(r.content)

    # --- Lifecycle primitives for dashboard-managed containers. In active use by
    #     version_manager (create-then-swap + rollback), resources (live cgroup/recreate),
    #     adoption and the Reforger root helper. Do NOT remove. ---

    async def create(self, name: str, payload: dict) -> dict:
        """Create a container (stopped). payload via services/container_spec.build_create_payload."""
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.post(f"{self.base}/containers/create?name={name}", json=payload)
            if r.status_code != 201:
                raise RuntimeError(f"create {name} → {r.status_code}: {r.text[:300]}")
            return r.json()  # {"Id": "...", "Warnings": [...]}

    async def remove(self, name: str, force: bool = False, volumes: bool = False) -> None:
        """Remove a container. 404 (already gone) is treated as success.

        `force=True` (`?force=1`) stops a still-running container and removes it, needed in the
        version rollback (the failed new container may still be running).

        `volumes=True` (`?v=1`) also removes the container's ANONYMOUS volumes, the ones Docker
        creates for a `VOLUME` of the image that no bind covers. Docker never removes a named
        volume or a bind this way, so bind data stays untouched either way. Only for a container
        that is gone for good (removing a server, a helper): a recreate leaves the default."""
        query = "&".join(q for q, on in (("force=1", force), ("v=1", volumes)) if on)
        async with httpx.AsyncClient(timeout=60) as c:
            r = await c.delete(f"{self.base}/containers/{name}{'?' + query if query else ''}")
            if r.status_code not in (204, 404):
                r.raise_for_status()

    async def rename(self, name: str, new_name: str) -> None:
        """Rename a container (for create-then-swap: set the old container aside until the new
        one is verified). POST /containers/{name}/rename?name=... (through the proxy: CONTAINERS+POST)."""
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.post(f"{self.base}/containers/{name}/rename?name={new_name}")
            if r.status_code not in (204,):
                raise RuntimeError(f"rename {name}→{new_name} → {r.status_code}: {r.text[:200]}")

    async def update_resources(
        self, name: str, memory: int | None = None, nano_cpus: int | None = None
    ) -> None:
        """Set cgroup limits LIVE (no recreate): HostConfig.Memory / MemorySwap / NanoCpus."""
        body: dict = {}
        if memory is not None:
            body["Memory"] = int(memory)
            # MemorySwap == Memory (no swap): satisfies the Docker invariant MemorySwap >= Memory
            # in ONE call (otherwise 409 "Memory limit should be smaller than already set
            # memoryswap").
            body["MemorySwap"] = int(memory)
        if nano_cpus is not None:
            body["NanoCpus"] = int(nano_cpus)
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.post(f"{self.base}/containers/{name}/update", json=body)
            r.raise_for_status()

    # --- Images (need IMAGES=1 on the socket proxy) --------------------------------------
    @staticmethod
    def _split_image(image: str) -> tuple[str, str]:
        """"repo:tag" → (repo, tag). Splits only at the LAST ':' if no '/' follows it;
        otherwise a registry port ("host:5000/repo") would be misread as a tag."""
        head, sep, tail = image.rpartition(":")
        if sep and "/" not in tail:
            return head, tail
        return image, "latest"

    async def image_exists(self, image: str) -> bool:
        """Is the image present locally? (A container create with a missing image 404s otherwise.)"""
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.get(f"{self.base}/images/{image}/json")
            if r.status_code == 404:
                return False
            r.raise_for_status()
            return True

    async def pull(self, image: str, on_progress=None) -> None:
        """Pull an image (POST /images/create). `on_progress` receives every JSON frame of the stream.

        CAUTION: Docker answers with **HTTP 200 even if the pull fails**; the error is an
        `{"error": …}` line INSIDE the progress stream. Without this check a failed pull would
        pass as success and only the following container create would trip (in the middle of
        the swap). Hence: read and evaluate the stream completely.

        Generous timeout: the PZ server image is ~10 GB.
        """
        repo, tag = self._split_image(image)
        timeout = httpx.Timeout(1800.0, connect=30.0)
        async with httpx.AsyncClient(timeout=timeout) as c:
            async with c.stream(
                "POST", f"{self.base}/images/create", params={"fromImage": repo, "tag": tag}
            ) as r:
                if r.status_code != 200:
                    body = (await r.aread()).decode("utf-8", errors="replace")[:300]
                    raise RuntimeError(f"pull {image} → {r.status_code}: {body}")
                error = ""
                async for line in r.aiter_lines():
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        msg = json.loads(line)
                    except ValueError:
                        continue  # not a JSON frame → irrelevant for the error check
                    if not isinstance(msg, dict):
                        continue
                    if msg.get("error"):
                        error = str(msg.get("error"))
                    elif on_progress is not None:
                        on_progress(msg)
                if error:
                    raise RuntimeError(f"pull {image} failed: {error[:300]}")

    async def remove_image(self, image: str, force: bool = False) -> bool:
        """Remove an image (`DELETE /images/{ref}`).

        The socket proxy permits this (`IMAGES=1` + `POST=1`, see docker-compose.yml). Without
        it EVERY old image stays on disk after a version change, at roughly 10 GB each for PZ.

        404 (already gone) and 409 (still referenced by a container) do not raise: both mean
        "nothing to clean up here", and forcing a referenced image would be the opposite of
        what this function is for.
        """
        query = "?force=1" if force else ""
        async with httpx.AsyncClient(timeout=120) as c:
            r = await c.delete(f"{self.base}/images/{image}{query}")
            if r.status_code not in (200, 404, 409):
                r.raise_for_status()
            # 409 = still referenced by a container. Not an error (forcing would be wrong), but
            # NOT a removal either, so it must not be reported as removed.
            return r.status_code in (200, 404)

    async def image_sizes(self, repo: str) -> dict[str, int]:
        """Local images of ONE repo with their size in bytes: {tag: size}. Best-effort."""
        try:
            async with httpx.AsyncClient(timeout=20) as c:
                r = await c.get(f"{self.base}/images/json")
                r.raise_for_status()
                images = r.json()
        except Exception:
            return {}
        out: dict[str, int] = {}
        for entry in images or []:
            size = int(entry.get("Size") or 0)
            for ref in entry.get("RepoTags") or []:
                if ref.startswith(f"{repo}:"):
                    out[ref.split(":", 1)[1]] = size
        return out

    async def image_tags(self, repo: str) -> set[str]:
        """Locally present tags of ONE repo, for the "already downloaded" marker in the dropdown
        (one call instead of a query per catalogue entry). Best-effort: empty on error."""
        try:
            async with httpx.AsyncClient(timeout=20) as c:
                r = await c.get(f"{self.base}/images/json")
                r.raise_for_status()
                images = r.json()
        except Exception:
            return set()
        tags: set[str] = set()
        for entry in images or []:
            for ref in entry.get("RepoTags") or []:
                name, tag = self._split_image(str(ref))
                if name == repo:
                    tags.add(tag)
        return tags

    async def update_restart_policy(self, name: str, policy: str = "unless-stopped") -> None:
        """Set the RestartPolicy of an EXISTING container live (no recreate), for reboot
        resilience. POST /containers/{name}/update with {RestartPolicy:{Name:…}}."""
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.post(
                f"{self.base}/containers/{name}/update",
                json={"RestartPolicy": {"Name": policy}},
            )
            r.raise_for_status()


docker = DockerClient(settings.docker_proxy_url)


def start_time(raw: str | None) -> dt.datetime | None:
    """Translate Docker's `StartedAt` into a naive UTC time, or None if it is not one.

    Docker returns RFC3339 with **nine** fractional digits (`2026-08-01T18:52:03.263540726Z`).
    `fromisoformat` understands that since Python 3.11: it truncates to microseconds and reads
    the `Z` as UTC. A timestamp with an offset is **converted**, not ignored. A container that
    never started reports the zero date `0001-01-01T00:00:00Z`, which is not a start time and
    is discarded.

    Anything unreadable falls back to `None`, and `None` here means **"don't know"**: each
    caller decides what that means for it (idle stop: no grace period; watchdog: no boot
    window).

    Lives here rather than in a consumer because `idle_autostop`, `fleet` and the watchdog all
    need it, and the question "when did this container start?" belongs to the module that
    provides the field.
    """
    if not raw:
        return None
    try:
        stamp = dt.datetime.fromisoformat(raw.strip())
    except (TypeError, ValueError):
        return None
    if stamp.tzinfo is not None:
        stamp = stamp.astimezone(dt.timezone.utc).replace(tzinfo=None)
    return None if stamp.year < 2000 else stamp
