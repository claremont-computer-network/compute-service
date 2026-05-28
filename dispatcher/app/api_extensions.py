"""
dispatcher/app/api_extensions.py
────────────────────────────────
Extension API endpoints for the compute-service dispatcher:

  /api/templates       - CRUD for job templates
  /api/files           - Browse files on mounted host directories
  /api/schedule        - Trigger jobs (with optional delay)
  /api/schedule/{id}   - Cancel a pending schedule
  /api/staging         - CRUD for staging areas (named mount configs)
  /api/jobs            - Filtered job list (state query param)
  /api/deployments/{id}/status - Check deployment success status

These routes are mounted as a sub-router under the ``app`` instance in
``main.py`` so they share authentication, plugin registry, and Docker
client access with the core API.

Design decisions
────────────────
- File browsing uses ``os.scandir`` on the host filesystem instead of
  shelling out to ``ls`` – safer for filenames with spaces.
- Scheduled jobs with ``delay_seconds > 0`` are triggered by a background
  asyncio scan task that runs every 10 seconds.
"""
from __future__ import annotations

import asyncio
import os
import typing as t
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse

from pydantic import BaseModel, ConfigDict, Field

# ── Router ────────────────────────────────────────────────────────────────────

router = APIRouter()

# ── Request models ────────────────────────────────────────────────────────────

class ScheduleTrigger(BaseModel):
    template_id: t.Optional[str] = None
    delay_seconds: int = Field(default=60, ge=0,
        description="Seconds to wait before triggering. Set to 0 for immediate execution.")
    image: t.Optional[str] = None
    cmd: t.Optional[t.Union[str, t.List[str]]] = None
    env: t.Optional[t.Dict[str, str]] = None
    volumes: t.Optional[t.List[t.Any]] = None
    gpu: t.Optional[t.Any] = None


class StagingCreate(BaseModel):
    name: str
    host_path: str
    dest_path: str = ""


class TemplateUpsert(BaseModel):
    model_config = ConfigDict(extra="allow")
    id: t.Optional[str] = None
    name: str = ""
    image: t.Optional[str] = ""
    cmd: t.Optional[t.Union[str, t.List[str]]] = None
    env: t.Optional[t.Dict[str, str]] = None
    volumes: t.Optional[t.List[t.Any]] = None
    gpu: t.Optional[t.Any] = None


# ── Helpers ───────────────────────────────────────────────────────────────────

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _import_main() -> t.Any:
    import app.main as _m
    return _m


def _fill_template_defaults(item: dict) -> dict:
    for k, v in {
        "id": "", "name": "", "image": "", "cmd": [], "env": {},
        "volumes": [], "gpu": None, "created_at": "", "modified_at": "",
    }.items():
        item.setdefault(k, list(v) if isinstance(v, list) else v)
    return item


def _fill_schedule_defaults(item: dict) -> dict:
    for k, v in {
        "id": "", "name": "unnamed", "status": "pending",
        "template_id": None, "delay_seconds": 300,
        "triggered_at": None, "jobs": [],
    }.items():
        item.setdefault(k, list(v) if isinstance(v, list) else v)
    return item


def _resolve_data_store():
    """Resolve data_store from app.main.

    Lazy import is needed because tests delete ``sys.modules["app.main"]``
    and reimport it with a patched client/job_store. A direct module-level
    import would retain the original (unpatched) reference.
    """
    return _import_main().data_store


def _resolve_job_store():
    """Resolve job_store from app.main (same lazy-import reasoning as above)."""
    return _import_main().job_store


# ── File browsing helpers ─────────────────────────────────────────────────────
# NOTE: `_is_allowed_path` duplicates the allowlist logic in
# `dispatcher/app/plugins/volumes.py`.  Both use string prefix comparison
# after `os.path.realpath`.  A future PR should consolidate them into a
# single shared utility (e.g. using `pathlib.Path.is_relative_to`).

def _is_allowed_path(path: str, allowed: list[str]) -> bool:
    """Return True if *path* is under any of the allowed directories.

    Fails closed when *allowed* is empty or None.
    """
    resolved = os.path.realpath(path)
    if not resolved.startswith("/"):
        return False
    if not allowed:
        return False
    allowed_real = [os.path.realpath(d) for d in allowed if d.strip()]
    for root in allowed_real:
        if resolved == root or resolved.startswith(root + "/"):
            return True
    return False


def _entries_from_path(path: str) -> list[dict]:
    """Return directory entries using os.scandir (no subprocess)."""
    entries: list[dict] = []
    try:
        for entry in os.scandir(path):
            try:
                stat_result = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            entries.append({
                "name": entry.name,
                "permissions": oct(stat_result.st_mode)[-3:],
                "size": stat_result.st_size,
                "modified": datetime.fromtimestamp(stat_result.st_mtime, tz=timezone.utc).isoformat(),
                "is_dir": entry.is_dir(follow_symlinks=False),
            })
    except PermissionError as exc:
        raise exc
    except FileNotFoundError as exc:
        raise exc
    return entries


# ── Background schedule scanner ───────────────────────────────────────────────

_schedule_scan_task: t.Optional[asyncio.Task] = None
_SCAN_INTERVAL = 10  # seconds


async def _scan_schedules() -> None:
    """Periodically check for schedules whose delay has elapsed."""
    while True:
        await asyncio.sleep(_SCAN_INTERVAL)
        m = _import_main()
        ds = m.data_store
        try:
            schedules = ds.read("schedules") or []
        except Exception:
            continue
        now = datetime.now(timezone.utc)
        for item in schedules:
            try:
                if item.get("status") != "pending":
                    continue
                created_str = item.get("created_at")
                delay = item.get("delay_seconds", 0)
                if created_str and delay > 0:
                    created_dt = datetime.fromisoformat(created_str.replace("Z", "+00:00"))
                    if now - created_dt >= timedelta(seconds=delay):
                        sched_id = item.get("id")
                        ds.update("schedules", sched_id, {
                            "triggered_at": _now_iso(),
                            "status": "active",
                        })
                        try:
                            _run_schedule(sched_id, item)
                        except Exception:
                            ds.update("schedules", sched_id, {
                                "status": "error",
                                "triggered_at": _now_iso(),
                            })
                        await asyncio.sleep(0)  # yield to event loop between runs
            except Exception:
                # Per-item errors should not kill the scanner task.
                continue


# ── Template endpoints ────────────────────────────────────────────────────────

@router.get("/api/templates")
def templates_list():
    """List all job templates."""
    ds = _resolve_data_store()
    items = ds.read("templates")
    return JSONResponse([_fill_template_defaults(dict(i)) for i in items])


@router.post("/api/templates")
def templates_upsert(req: TemplateUpsert):
    """Create or update a job template."""
    ds = _resolve_data_store()
    items = ds.read("templates")
    existing_id = req.id
    now = _now_iso()

    if existing_id:
        for i, item in enumerate(items):
            if item.get("id") == existing_id:
                values = req.model_dump(exclude_none=True)
                v = values.get("volumes")
                if v:
                    m = _import_main()
                    for vol in v:
                        hp = vol.get("host_path") if isinstance(vol, dict) else None
                        if hp and not _is_allowed_path(hp, m.ALLOWED_HOST_DIRS):
                            raise HTTPException(
                                status_code=400,
                                detail=f"Volume host_path {hp!r} is not under any allowed host directory: {m.ALLOWED_HOST_DIRS}",
                            )
                for key, val in values.items():
                    items[i][key] = val
                if "created_at" not in items[i]:
                    items[i]["created_at"] = now
                items[i]["modified_at"] = _now_iso()
                ds.write("templates", items)
                return JSONResponse(_fill_template_defaults(dict(items[i])))
        return JSONResponse(status_code=404, content={"detail": "Template not found"})

    new_id = f"tpl_{uuid.uuid4().hex[:12]}"
    m = _import_main()
    volumes = req.volumes or []
    for vol in volumes:
        hp = vol.get("host_path") if isinstance(vol, dict) else None
        if hp and not _is_allowed_path(hp, m.ALLOWED_HOST_DIRS):
            raise HTTPException(
                status_code=400,
                detail=f"Volume host_path {hp!r} is not under any allowed host directory: {m.ALLOWED_HOST_DIRS}",
            )
    item = {
        "id": new_id,
        "name": req.name or "",
        "image": req.image or "",
        "cmd": req.cmd or [],
        "env": req.env or {},
        "volumes": volumes,
        "gpu": req.gpu,
        "created_at": now,
        "modified_at": now,
    }
    items.append(item)
    ds.write("templates", items)
    return JSONResponse(dict(item), status_code=201)


@router.delete("/api/templates/{template_id}")
def templates_delete(template_id: str):
    """Delete a template by ID."""
    ds = _resolve_data_store()
    if ds.delete("templates", template_id):
        return JSONResponse({"deleted": template_id})
    return JSONResponse(status_code=404, content={"detail": "Template not found"})


# ── File browsing ─────────────────────────────────────────────────────────────

@router.get("/api/files")
def files_list(
    path: str = Query(default="/", description="Host path to list"),
):
    """List files in a mounted directory.

    Only directories under ``ALLOWED_HOST_DIRS`` may be browsed.
    """
    m = _import_main()
    allowed = m.ALLOWED_HOST_DIRS

    resolved = os.path.realpath(path)
    if not _is_allowed_path(path, allowed):
        raise HTTPException(
            status_code=400,
            detail=f"Path {path!r} is not under any allowed host directory: {allowed}",
        )

    try:
        entries = _entries_from_path(resolved)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"Path not found: {path}")
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=f"Permission denied: {path}")

    return JSONResponse({"path": resolved, "entries": entries})


# ── Scheduling endpoints ──────────────────────────────────────────────────────

@router.get("/api/schedule")
def schedules_list():
    """List all schedules."""
    ds = _resolve_data_store()
    return JSONResponse([_fill_schedule_defaults(dict(i)) for i in (ds.read("schedules") or [])])


@router.post("/api/schedule")
def schedule_create(
    req: ScheduleTrigger,
):
    """Create a schedule to trigger a job (optionally after a delay).

    If ``delay_seconds == 0`` the job is executed immediately.
    """
    ds = _resolve_data_store()
    items = ds.read("schedules") or []
    sched_id = f"sch_{uuid.uuid4().hex[:12]}"
    now = _now_iso()

    schedule_item = {
        "id": sched_id,
        "name": f"Schedule {sched_id}",
        "status": "pending" if req.delay_seconds > 0 else "active",
        "template_id": req.template_id,
        "delay_seconds": req.delay_seconds,
        "created_at": now,
        "triggered_at": None,
        "jobs": [],
        "inline": {
            "image": req.image, "cmd": req.cmd, "env": req.env,
            "volumes": req.volumes, "gpu": req.gpu,
        } if req.image or req.cmd else None,
    }

    if req.template_id:
        tpl = ds.fetch("templates", req.template_id)
        if not tpl:
            return JSONResponse(status_code=404, content={"detail": f"Template not found: {req.template_id}"})
        schedule_item["_template_name"] = tpl.get("name", tpl.get("image", ""))

    ds.create("schedules", schedule_item)
    result = _fill_schedule_defaults(dict(schedule_item))

    if req.delay_seconds == 0:
        try:
            _run_schedule(sched_id, schedule_item)
        except HTTPException:
            ds.update("schedules", sched_id, {
                "status": "error",
                "triggered_at": _now_iso(),
            })
            raise
        except Exception:
            ds.update("schedules", sched_id, {
                "status": "error",
                "triggered_at": _now_iso(),
            })
            _import_main().logger.error("Schedule %s immediate execution failed", sched_id, exc_info=True)

    return JSONResponse(result, status_code=201)


def _run_schedule(schedule_id: str, schedule_item: dict) -> None:
    """Execute a job from a schedule and record the job_id."""
    _m = _import_main()

    inline = schedule_item.get("inline")
    template_id = schedule_item.get("template_id")

    body: dict = {}

    if template_id and not inline:
        tpl = _m.data_store.fetch("templates", template_id)
        if not tpl:
            return
        body["image"] = tpl.get("image", "alpine:3.18")
        if tpl.get("cmd"):
            body["cmd"] = tpl["cmd"]
        if tpl.get("env"):
            body["env"] = tpl["env"]
        if tpl.get("volumes"):
            body["volumes"] = tpl["volumes"]
        if tpl.get("gpu"):
            body["gpu"] = tpl["gpu"]
    elif inline:
        body = {k: v for k, v in inline.items() if v is not None}

    if not body.get("image"):
        body["image"] = "alpine:3.18"

    request_model = _m.ExecuteRequest(**body, detach=True)
    resource = "gpu" if request_model.gpu is not None else "cpu"
    acquired = False
    try:
        _m._acquire_slot(resource)
        acquired = True

        run_kwargs = _m._prepare_run(request_model)
        run_kwargs["command"] = request_model.cmd

        _m.registry.pre_create(request_model, run_kwargs)
        _m._ensure_image(request_model.image)

        run_kwargs["detach"] = True
        container = _m.client.containers.run(request_model.image, **run_kwargs)
        record = _m.job_store.register(container, image=request_model.image,
                                        cmd=request_model.cmd)
        _m.registry.on_register(record)
        _m.resource_slots.release(resource)
        acquired = False

        _m.data_store.append_list("schedules", schedule_id, "jobs", {
            "job_id": container.id,
            "status": "running",
        })

    except _m.HTTPException:
        raise
    except _m.DockerException as e:
        _m.logger.error("Schedule %s failed to execute: %s", schedule_id, e)
        _m.data_store.update("schedules", schedule_id, {
            "status": "error",
            "triggered_at": _now_iso(),
        })
    finally:
        if acquired:
            _m.resource_slots.release(resource)


@router.delete("/api/schedule/{schedule_id}")
def schedule_cancel(
    schedule_id: str,
):
    """Cancel a pending schedule."""
    ds = _resolve_data_store()
    if ds.update("schedules", schedule_id, {"status": "cancelled"}):
        return JSONResponse({"cancelled": schedule_id})
    return JSONResponse(status_code=404, content={"detail": "Schedule not found"})


# ── Staging area endpoints ────────────────────────────────────────────────────

@router.get("/api/staging")
def staging_list():
    """List all staging areas."""
    ds = _resolve_data_store()
    return JSONResponse(ds.read("staging") or [])


@router.post("/api/staging")
def staging_create(
    req: StagingCreate,
):
    """Create a staging area – a named reference to a host path mount."""
    ds = _resolve_data_store()
    m = _import_main()

    if not _is_allowed_path(req.host_path, m.ALLOWED_HOST_DIRS):
        raise HTTPException(
            status_code=400,
            detail=f"host_path {req.host_path!r} is not under any allowed host directory: {m.ALLOWED_HOST_DIRS}",
        )

    items = ds.read("staging") or []
    staging_id = f"stg_{uuid.uuid4().hex[:12]}"
    now = _now_iso()
    item = {
        "id": staging_id,
        "name": req.name,
        "host_path": req.host_path,
        "dest_path": req.dest_path or req.host_path,
        "created_at": now,
        "description": "",
    }
    items.append(item)
    ds.write("staging", items)
    return JSONResponse(dict(item), status_code=201)


@router.delete("/api/staging/{staging_id}")
def staging_delete(
    staging_id: str,
):
    """Remove a staging area."""
    ds = _resolve_data_store()
    if ds.delete("staging", staging_id):
        return JSONResponse({"deleted": staging_id})
    return JSONResponse(status_code=404, content={"detail": "Staging area not found"})


# ── Filtered job list ────────────────────────────────────────────────────────
# NOTE: This endpoint duplicates `/v1/jobs` with only a `?state=` filter.
# A future PR may extend `/v1/jobs` with `?state=` and deprecate this route.

@router.get("/api/jobs")
def jobs_filter(
    state: str = Query(default="*", description="Filter by state: running, stopped, or * for all"),
):
    """List jobs filtered by status."""
    from app.main import _enrich_job_data as _enrich_job_data
    js = _resolve_job_store()
    records = js.get_by_state(state)
    result = []
    for job in records:
        data = job.model_dump(mode="json")
        _enrich_job_data(job, data)
        result.append(data)
    return JSONResponse(result)


# ── Deployment verification ──────────────────────────────────────────────────

@router.get("/api/deployments/{job_id}/status")
def deployment_status(
    job_id: str,
):
    """Check the outcome of a deployment (job)."""
    js = _resolve_job_store()
    job = js.get(job_id)
    m = _import_main()

    if job is None:
        try:
            container = m.client.containers.get(job_id)
            attrs = container.attrs.get("State", {})
            exit_code = attrs.get("ExitCode", -1)
            exit_code = int(exit_code) if exit_code is not None else -1
            if exit_code is not None:
                return JSONResponse({
                    "job_id": job_id,
                    "status": "stopped",
                    "exit_code": exit_code,
                    "success": exit_code == 0,
                    "message": "Container exited with success" if exit_code == 0 else f"Container exited with code {exit_code}",
                })
        except m.NotFound:
            pass
        return JSONResponse(status_code=404, content={"detail": f"No job found for {job_id}"})

    # Job record found — check live container state if marked running.
    if job.status == "running":
        container = None
        try:
            container = m.client.containers.get(job.container_id)
            container.reload()
        except m.NotFound:
            _ = m.job_store.mark_stopped(job.job_id)
        except m.DockerException:
            pass

        if container and container.status in ("exited", "dead"):
            ec = int(container.attrs.get("State", {}).get("ExitCode", -1))
            m.job_store.mark_stopped(job.job_id, exit_code=ec)
            enriched = m.job_store.get(job.job_id) or job
            m.registry.on_job_complete(enriched, ec)
            ec = int(ec) if ec is not None else -1
            return JSONResponse({
                "job_id": job_id,
                "status": "stopped",
                "exit_code": ec,
                "success": ec == 0,
                "message": "Job completed successfully" if ec == 0 else f"Job exited with code {ec}",
            })
        elif container:
            stats = m._fetch_resources(container)
            return JSONResponse({
                "job_id": job_id,
                "status": container.status,
                "exit_code": None,
                "success": False,
                "message": f"Container is {container.status}",
                "resources": stats.model_dump() if stats else None,
            })
        return JSONResponse({
            "job_id": job_id,
            "status": "running",
            "exit_code": None,
            "success": False,
            "message": "Job is currently running",
        })

    # Terminal job (stopped).
    ec = job.exit_code
    succeeded = ec == 0
    return JSONResponse({
        "job_id": job_id,
        "status": job.status,
        "exit_code": ec,
        "success": succeeded,
        "message": "Job completed successfully" if succeeded else f"Job exited with code {ec}",
    })
