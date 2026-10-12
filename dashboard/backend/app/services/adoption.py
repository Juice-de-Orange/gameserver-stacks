"""Adoption preflight: read-only diff of the RUNNING Compose container against the payload the
dashboard would create on cutover (compose→dashboard).

Purpose: BEFORE the risky flip (`management_mode=dashboard` + create-then-swap) is executed,
prove that the recreate reproduces the container *faithfully*: same world binds, ports, network
and resources, and no dropped secrets (above all the join `PASSWORD` of a public server). This
module changes NOTHING: it reads the live inspect data and builds the intended payload on a
*non-persisted* dashboard copy of the registry row (so the compose guard in `container_spec`
stays intact).
"""
from __future__ import annotations

from ..models.server import GameServer, ManagementMode
from .container_spec import build_create_payload
from .docker_client import docker
from .servers import resolve_secret

# Env keys whose VALUE may safely be compared (no secrets).
_NONSECRET_ENV = {
    "STEAMAPPBRANCH",
    "SELF_MANAGED_MODS",
    "SERVERNAME",
    "DISPLAYNAME",
    "ADMINUSERNAME",
    "PUBLIC",
    "MIN_MEMORY",
    "MAX_MEMORY",
}
# Secret env keys: check presence ONLY (never compare or log the value).
_SECRET_ENV = {"ADMINPASSWORD", "RCONPASSWORD", "PASSWORD"}


def _env_list_to_dict(env: list[str] | None) -> dict[str, str]:
    out: dict[str, str] = {}
    for e in env or []:
        if "=" in e:
            k, v = e.split("=", 1)
            out[k] = v
    return out


def _binds_set(binds: list[str] | None) -> set[str]:
    """Normalise to `host:container` (strip mount flags such as :rw/:ro/:z) so that Compose
    (`:rw`) and our payload (no flag) are comparable."""
    out: set[str] = set()
    for b in binds or []:
        parts = b.split(":")
        if len(parts) >= 2:
            out.add(parts[0] + ":" + parts[1])
    return out


def _port_bindings(pb: dict | None) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for p, binds in (pb or {}).items():
        out[p] = sorted(b.get("HostPort") for b in (binds or []) if b.get("HostPort"))
    return out


def extract_live(inspect: dict) -> dict:
    cfg = inspect.get("Config", {}) or {}
    hc = inspect.get("HostConfig", {}) or {}
    return {
        "image": cfg.get("Image", ""),
        "network": hc.get("NetworkMode", ""),
        "binds": _binds_set(hc.get("Binds")),
        "port_bindings": _port_bindings(hc.get("PortBindings")),
        "exposed": set((cfg.get("ExposedPorts") or {}).keys()),
        "memory": hc.get("Memory") or 0,
        "nano_cpus": hc.get("NanoCpus") or 0,
        "env": _env_list_to_dict(cfg.get("Env")),
    }


def extract_intended(payload: dict) -> dict:
    hc = payload.get("HostConfig", {}) or {}
    return {
        "image": payload.get("Image", ""),
        "network": hc.get("NetworkMode", ""),
        "binds": _binds_set(hc.get("Binds")),
        "port_bindings": _port_bindings(hc.get("PortBindings")),
        "exposed": set((payload.get("ExposedPorts") or {}).keys()),
        "memory": hc.get("Memory") or 0,
        "nano_cpus": hc.get("NanoCpus") or 0,
        "env": _env_list_to_dict(payload.get("Env")),
    }


def diff_specs(live: dict, intended: dict) -> list[dict]:
    """List of findings `{field, severity, live, intended, note}`. Empty list = faithful recreate.

    `severity`: `critical` (the cutover would break or endanger something) | `warn` (deviation,
    review deliberately). Secret values are NEVER included, only "set"/"missing" markers."""
    findings: list[dict] = []

    def add(field: str, severity: str, live_v, int_v, note: str) -> None:
        findings.append(
            {"field": field, "severity": severity, "live": live_v, "intended": int_v, "note": note}
        )

    if live["image"] != intended["image"]:
        add("image", "critical", live["image"], intended["image"],
            "Image/tag differs — the wrong version would be started")
    if live["network"] != intended["network"]:
        add("network", "critical", live["network"], intended["network"],
            "Network differs — RCON/reachability affected")

    # Binds: the recreate MUST cover every live bind (otherwise world data would not be mounted).
    missing = live["binds"] - intended["binds"]
    extra_b = intended["binds"] - live["binds"]
    if missing:
        add("binds", "critical", sorted(live["binds"]), sorted(intended["binds"]),
            f"Live bind(s) missing in the recreate: {sorted(missing)} — WORLD/mod data would not be mounted")
    if extra_b:
        add("binds", "warn", sorted(live["binds"]), sorted(intended["binds"]),
            f"Additional bind(s) in the recreate: {sorted(extra_b)}")

    if live["port_bindings"] != intended["port_bindings"]:
        add("port_bindings", "critical", live["port_bindings"], intended["port_bindings"],
            "Published ports differ")
    exp_missing = live["exposed"] - intended["exposed"]
    if exp_missing:
        add("exposed", "warn", sorted(live["exposed"]), sorted(intended["exposed"]),
            f"Ports exposed by the live container are missing in the recreate: {sorted(exp_missing)}")

    if live["memory"] != intended["memory"]:
        add("memory", "warn", live["memory"], intended["memory"],
            "cgroup memory differs (0 = no cap)")
    if live["nano_cpus"] != intended["nano_cpus"]:
        add("nano_cpus", "warn", live["nano_cpus"], intended["nano_cpus"],
            "cgroup CPU differs (0 = no limit)")

    le, ie = live["env"], intended["env"]
    for k in _SECRET_ENV:
        in_live = bool(le.get(k))
        in_int = bool(ie.get(k))
        if in_live and not in_int:
            sev = "critical" if k == "PASSWORD" else "warn"
            note = (
                "a public server would be recreated WITHOUT A PASSWORD"
                if k == "PASSWORD"
                else f"{k} was set live, missing in the recreate — login change"
            )
            add(f"env:{k}", sev, "set", "MISSING", note)
        elif in_int and not in_live:
            add(f"env:{k}", "warn", "MISSING", "set", f"{k} is newly set (was not present live)")
    for k in _NONSECRET_ENV:
        lv, iv = le.get(k), ie.get(k)
        if lv != iv:
            add(f"env:{k}", "warn", lv, iv, f"{k} differs")
    return findings


def intended_payload(server: GameServer) -> dict:
    """Intended recreate payload, built on a NON-persisted dashboard copy of the row so that the
    compose guard in `build_create_payload` stays intact (it blocks compose rows)."""
    secret = resolve_secret(server)
    as_dashboard = server.model_copy(update={"management_mode": ManagementMode.dashboard})
    return build_create_payload(as_dashboard, secret)


async def adoption_preflight(server: GameServer) -> dict:
    """Read-only preflight for a server's compose→dashboard cutover.

    Returns `{ok, container, live_running, findings, counts}`. `ok=True` ⇔ no `critical` finding,
    i.e. the recreate would reproduce the running container faithfully. Performs NO change."""
    name = server.container_name or server.key
    inspect = await docker.inspect(name)
    if inspect is None:
        return {
            "ok": False,
            "container": name,
            "live_running": False,
            "findings": [{
                "field": "container", "severity": "critical", "live": "missing", "intended": name,
                "note": "Live container not found — nothing to adopt (is the compose stack running?)",
            }],
            "counts": {"critical": 1, "warn": 0},
        }
    live = extract_live(inspect)
    intended = extract_intended(intended_payload(server))
    findings = diff_specs(live, intended)
    crit = sum(1 for f in findings if f["severity"] == "critical")
    warn = sum(1 for f in findings if f["severity"] == "warn")
    return {
        "ok": crit == 0,
        "container": name,
        "live_running": bool((inspect.get("State") or {}).get("Running")),
        "findings": findings,
        "counts": {"critical": crit, "warn": warn},
    }
