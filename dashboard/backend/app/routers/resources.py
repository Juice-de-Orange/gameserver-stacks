"""Resource management: shared budget + per-server RAM/CPU/idle.

- Budget (`resource_budget` singleton): shared limit; `total_ram_mb=0` = advisory.
- Per server: `ram_mb`/`cpu_cores` are cgroup limits that are applied **live** via
  `docker.update_resources` while the container runs (no recreate). Heap changes (JVM `-Xmx`)
  need a recreate → they are only persisted here and reported as `recreate_required` (the
  version manager/recreate performs the swap).
- Safety: never lower memory below the **live RSS**, and enforce `validate_allocation`
  (ram ≥ heap×1.25) — the first cap on a previously unlimited running server is the most
  dangerous operation.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlmodel import Session, select

from ..auth.deps import get_server, require_flag
from ..db import get_session
from ..errors import err
from ..models import ResourceBudget, User
from ..models.player import utcnow_naive
from ..models.server import GameServer
from ..services import resource_budget as budget_svc
from ..services.docker_client import docker

router = APIRouter(prefix="/api", tags=["resources"])

_view = require_flag("can_view_status")
_manage = require_flag("can_manage_resources")


class BudgetUpdate(BaseModel):
    total_ram_mb: int | None = None
    total_cpu_cores: float | None = None
    mem_headroom_pct: int | None = None
    default_idle_timeout_minutes: int | None = None


class ResourceUpdate(BaseModel):
    ram_mb: int | None = None
    cpu_cores: float | None = None
    heap_max_mb: int | None = None
    heap_min_mb: int | None = None


class IdleUpdate(BaseModel):
    idle_autostop_enabled: bool | None = None
    idle_timeout_minutes: int | None = None


# ------------------------------------------------------------------ budget (global)

def _budget_row(session: Session) -> ResourceBudget:
    row = session.exec(select(ResourceBudget)).first()
    if row is None:  # seed_servers normally creates it; add defensively
        row = ResourceBudget()
        session.add(row)
        session.commit()
        session.refresh(row)
    return row


def _budget_public(b: ResourceBudget) -> dict:
    return {
        "total_ram_mb": b.total_ram_mb,
        "total_cpu_cores": b.total_cpu_cores,
        "mem_headroom_pct": b.mem_headroom_pct,
        "default_idle_timeout_minutes": b.default_idle_timeout_minutes,
    }


@router.get("/budget")
def get_budget(session: Session = Depends(get_session), _: User = Depends(_view)) -> dict:
    return _budget_public(_budget_row(session))


@router.put("/budget")
def put_budget(
    body: BudgetUpdate, session: Session = Depends(get_session), _: User = Depends(_manage)
) -> dict:
    b = _budget_row(session)
    # Enforce value ranges: a negative mem_headroom_pct would defeat the OOM guard
    # (need = heap×(1+pct/100) < heap → a memory limit below the JVM heap ceiling passes the check).
    _ranges = {
        "total_ram_mb": (0, None), "total_cpu_cores": (0, None),
        "mem_headroom_pct": (0, 100), "default_idle_timeout_minutes": (1, None),
    }
    for k, v in body.model_dump(exclude_unset=True).items():
        if v is None:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                detail=err("resources.value_null", "{field} must not be null", field=k),
            )
        lo, hi = _ranges.get(k, (None, None))
        if (lo is not None and v < lo) or (hi is not None and v > hi):
            bound = f"≥ {lo}" if hi is None else f"{lo}–{hi}"
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                detail=err("resources.value_out_of_range", "{field} must be {bound}", field=k, bound=bound),
            )
        setattr(b, k, v)
    session.add(b)
    session.commit()
    session.refresh(b)
    return _budget_public(b)


@router.get("/budget/usage")
async def get_usage(_: User = Depends(_view)) -> dict:
    return await budget_svc.usage()


# ------------------------------------------------------------------ per server

async def _resource_state(server: GameServer) -> dict:
    """Registry reservation + LIVE cgroup limit/RSS (for the resources page)."""
    live: dict = {}
    try:
        st = await docker.state(server.container_name)
        live["running"] = bool(st.get("running"))
        live["cgroup_memory"] = st.get("memory")      # 0 = unlimited
        live["cgroup_nano_cpus"] = st.get("nano_cpus")
        if st.get("running"):
            stats = await docker.stats(server.container_name)
            live["rss_bytes"] = stats.get("mem_bytes")
    except Exception:
        live = {"running": None}
    return {
        "key": server.key,
        "ram_mb": server.ram_mb,
        "cpu_cores": server.cpu_cores,
        "heap_max_mb": server.heap_max_mb,
        "heap_min_mb": server.heap_min_mb,
        # The idle state belongs HERE, not in the browser's copy of the registry: that copy is
        # not refreshed after a save, so the page would show the old value next to a green
        # "saved".
        "idle_autostop_enabled": server.idle_autostop_enabled,
        "idle_timeout_minutes": server.idle_timeout_minutes,
        "live": live,
    }


@router.get("/servers/{key}/resources")
async def get_resources(server: GameServer = Depends(get_server), _: User = Depends(_view)) -> dict:
    return await _resource_state(server)


@router.put("/servers/{key}/resources")
async def put_resources(
    key: str,
    body: ResourceUpdate,
    session: Session = Depends(get_session),
    _: User = Depends(_manage),
) -> dict:
    srv = session.exec(select(GameServer).where(GameServer.key == key)).first()
    if srv is None or not srv.enabled:
        # Like get_server (GET returns 404 for disabled servers) — otherwise PUT would allow an
        # operational action (live cgroup update) on a server the same API reports as non-existent.
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, detail=err("server.not_found", "Server '{key}' not found", key=key)
        )
    fields = body.model_dump(exclude_unset=True)

    new_ram = fields.get("ram_mb", srv.ram_mb)
    new_heap = fields.get("heap_max_mb", srv.heap_max_mb)
    budget = budget_svc.get_budget()
    ok, msg = budget_svc.validate_allocation(new_ram or 0, new_heap, budget.mem_headroom_pct)
    if not ok:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=msg)

    heap_changed = ("heap_max_mb" in fields and fields["heap_max_mb"] != srv.heap_max_mb) or (
        "heap_min_mb" in fields and fields["heap_min_mb"] != srv.heap_min_mb
    )
    ram_changed = "ram_mb" in fields and fields["ram_mb"] != srv.ram_mb
    cpu_changed = "cpu_cores" in fields and fields["cpu_cores"] != srv.cpu_cores
    cgroup_changed = ram_changed or cpu_changed
    # Apply memory LIVE when ram changes AND either the limit is REMOVED (new_ram==0 → always
    # safe, more memory never OOMs) OR no heap change is pending at the same time. With heap+RAM
    # together a limit validated against the new (smaller) heap ceiling could lie below the old
    # JVM ceiling → OOM; the recreate takes care of that. cpu_cores==0 = remove the limit
    # (can always be applied live).
    apply_mem_live = ram_changed and (new_ram == 0 or not heap_changed)

    # Is the container running? → read the live state for the RSS guard + live apply.
    running, rss = False, None
    try:
        st = await docker.state(srv.container_name)
        running = bool(st.get("running"))
        if running:
            rss = (await docker.stats(srv.container_name)).get("mem_bytes")
    except Exception:
        running = False

    # NEVER lower memory below the current live RSS (it would be OOM-killed immediately) — only
    # relevant when lowering to a POSITIVE value; removing the limit (new_ram==0) is always safe.
    if running and apply_mem_live and new_ram > 0 and rss and new_ram * 1024 * 1024 < rss:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail=err(
                "resources.ram_below_live_rss",
                "ram_mb={ram_mb} MB < live RSS {rss_mb} MB — the container would be OOM-killed",
                ram_mb=new_ram, rss_mb=round(rss / 1024 / 1024),
            ),
        )

    for k, v in fields.items():
        setattr(srv, k, v)
    srv.updated_at = utcnow_naive()
    session.add(srv)
    session.commit()
    session.refresh(srv)

    applied = "persisted"
    if running and cgroup_changed:
        # On a heap change (positive RAM): memory only at recreate; 0 (remove the limit) is sent live.
        mem = srv.ram_mb * 1024 * 1024 if (ram_changed and apply_mem_live) else None
        # cpu_cores==0 is DELIBERATELY sent as NanoCpus=0 (Docker removes the CPU limit live with that).
        cpu = int(srv.cpu_cores * 1_000_000_000) if cpu_changed else None
        if mem is not None or cpu is not None:
            try:
                await docker.update_resources(srv.container_name, memory=mem, nano_cpus=cpu)
                applied = "live"
            except Exception as e:
                raise HTTPException(
                    status.HTTP_502_BAD_GATEWAY,
                    detail=err("resources.cgroup_update_failed", "cgroup update failed: {error}", error=str(e)),
                )
    # recreate_required: heap change OR a positive RAM change that could not go live because of the heap.
    recreate_required = heap_changed or (ram_changed and new_ram > 0 and not apply_mem_live)
    return {"applied": applied, "recreate_required": recreate_required, **(await _resource_state(srv))}


@router.post("/servers/{key}/start/preflight")
async def start_preflight(server: GameServer = Depends(get_server), _: User = Depends(_view)) -> dict:
    """Dry check before the start: does the server fit into the shared budget?"""
    ok, deficit, candidates = await budget_svc.can_start(server)
    return {"ok": ok, "deficit_mb": deficit, "stop_candidates": candidates}


@router.put("/servers/{key}/idle")
def put_idle(
    key: str,
    body: IdleUpdate,
    session: Session = Depends(get_session),
    _: User = Depends(_manage),
) -> dict:
    srv = session.exec(select(GameServer).where(GameServer.key == key)).first()
    if srv is None or not srv.enabled:  # consistent with get_server (GET returns 404 for disabled ones)
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, detail=err("server.not_found", "Server '{key}' not found", key=key)
        )
    fields = body.model_dump(exclude_unset=True)
    if "idle_autostop_enabled" in fields:
        srv.idle_autostop_enabled = bool(fields["idle_autostop_enabled"])
    if "idle_timeout_minutes" in fields:
        srv.idle_timeout_minutes = fields["idle_timeout_minutes"]  # None = global default
    srv.updated_at = utcnow_naive()
    session.add(srv)
    session.commit()
    session.refresh(srv)
    return {
        "key": srv.key,
        "idle_autostop_enabled": srv.idle_autostop_enabled,
        "idle_timeout_minutes": srv.idle_timeout_minutes,
    }
