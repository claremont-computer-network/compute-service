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
- File browsing uses ``subprocess.run`` / ``ls`` inside the dispatcher
  container (which has the host paths mounted).  No extra containers needed.
- Each schedule item stores a ``job_id`` when triggered so users can track
  which container their scheduled job created.
- Templates, schedules, and staging areas are persisted to JSON files under
  ``$CAAS_DATA_DIR`` (default ``/srv/caas-data``).
"""
from __future__ import annotations

import os
import re
import subprocess
import typing as t
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse

from pydantic import BaseModel, Field

import docker.errors

# Lazy imports to avoid circular imports at module level.
# These are resolved inside endpoint functions where main has already been loaded.

# ── Router ────────────────────────────────────────────────────────────────────

router = APIRouter()

# ── Request models ────────────────────────────────────────────────────────────

class ScheduleTrigger(BaseModel):
    template_id: t.Optional[str] = None
    delay_seconds: int = Field(default=60, ge=0,
        description="Seconds to wait before triggering. Set to 0 for immediate execution.")
    # Inline request fields (only used when template_id is not given)
    image: t.Optional[str] = None
    cmd: t.Optional[t.Union[str, t.List[str]]] = None
    env: t.Optional[t.Dict[str, str]] = None
    volumes: t.Optional[t.List[t.Any]] = None  # VolumeSpec dicts
    gpu: t.Optional[t.Any] = None  # GpuRequest dict


class StagingCreate(BaseModel):
    name: str
    host_path: str
    dest_path: str = ""


# ── Helpers ───────────────────────────────────────────────────────────────────

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _import_main() -> t.Any:
    """Import app.main lazily to avoid circular imports."""
    import app.main as _m  # type: ignore
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


def _parse_size(s: str) -> int:
    """Convert ``ls`` size strings like '4.0K', '1.2M' to bytes."""
    s = s.strip()
    if re.match(r'^[\d.]+$', s):
        return int(float(s))
    multipliers = {"K": 1024, "M": 1024**2, "G": 1024**3, "T": 1024**4}
    for suffix, mult in multipliers.items():
        if s.endswith(suffix):
            try:
                return int(float(s[:-1].strip()) * mult)
            except ValueError:
                return 0
    return 0




def _resolve_data_store():
    m = _import_main()
    return m.data_store


def _resolve_job_store():
    m = _import_main()
    return m.job_store


# ── Template endpoints ────────────────────────────────────────────────────────

@router.get("/api/templates")
def templates_list(
    
):
    """List all job templates."""
    ds = _resolve_data_store()
    items = ds.read("templates")
    return JSONResponse([_fill_template_defaults(dict(i)) for i in items])


@router.post("/api/templates")
def templates_upsert(
    req: dict,
    
):
    """Create or update a job template.

    If the request body contains an ``id`` key that matches an existing
    template, the template is updated in-place.  Otherwise a new template
    is created with a generated ``tpl_{uuid}`` ID.

    Request body fields:
        id (optional), name, image, cmd (list), env (dict),
        volumes (list[VolumeSpec]), gpu (GpuRequest dict).
    """
    ds = _resolve_data_store()
    items = ds.read("templates")
    existing_id = req.get("id")
    now = _now_iso()

    if existing_id:
        for i, item in enumerate(items):
            if item.get("id") == existing_id:
                items[i].update(req)
                if "created_at" not in items[i]:
                    items[i]["created_at"] = req.get("created_at", now)
                items[i]["modified_at"] = _now_iso()
                ds.write("templates", items)
                return JSONResponse(_fill_template_defaults(dict(items[i])))
        return JSONResponse(status_code=404, content={"detail": "Template not found"})

    # Create new.
    new_id = req.get("id", f"tpl_{uuid.uuid4().hex[:12]}")
    item = dict(req)
    item["id"] = new_id
    item["created_at"] = now
    item["modified_at"] = _now_iso()
    item = _fill_template_defaults(item)
    items.append(item)
    ds.write("templates", items)
    return JSONResponse(dict(item), status_code=201)


@router.delete("/api/templates/{template_id}")
def templates_delete(
    template_id: str,
    
):
    """Delete a template by ID."""
    ds = _resolve_data_store()
    if ds.delete("templates", template_id):
        return JSONResponse({"deleted": template_id})
    return JSONResponse(status_code=404, content={"detail": "Template not found"})


# ── File browsing ──────────────────────────────────────────────────────────────

@router.get("/api/files")
def files_list(
    path: str = Query(default="/", description="Host path to list (must be under ALLOWED_HOST_DIRS)"),
    
):
    """List files in a mounted directory using ``ls -lAh``.

    Only directories under ``ALLOWED_HOST_DIRS`` may be browsed.
    Returns a JSON object: ``{"path": "/real/path", "entries": [...]}``

    Each entry is ``{"name", "permissions", "size", "modified", "is_dir"}``.
    """
    m = _import_main()
    allowed = m.ALLOWED_HOST_DIRS

    resolved = os.path.realpath(path)
    if not resolved.startswith("/"):
        raise HTTPException(status_code=400, detail="Invalid path")

    allowed_root_paths = [os.path.realpath(d) for d in allowed if d.strip()]
    is_allowed = any(
        resolved == root or resolved.startswith(root + "/")
        for root in allowed_root_paths
    )
    if not is_allowed and allowed:
        raise HTTPException(
            status_code=400,
            detail=f"Path {path!r} is not under any allowed host directory: {allowed}",
        )

    # Use ls on the host filesystem directly (we run as root in the container).
    try:
        result = subprocess.run(
            ["ls", "-lAh", "--time-style=long-iso", path],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=504, detail="File browsing timed out")

    entries = []
    for line in result.stdout.strip().splitlines():
        if line.startswith("total "):
            continue
        parts = line.split(None, 8)
        if len(parts) < 8:
            continue
        name = parts[-1]
        if name in (".", ".."):
            continue
        try:
            size = _parse_size(parts[4])
        except (ValueError, IndexError):
            size = 0
        entries.append({
            "name": name,
            "permissions": parts[0],
            "size": size,
            "modified": parts[5] if len(parts) > 5 else "",
            "is_dir": name.endswith("/") or (len(parts) >= 1 and parts[0].startswith("d")),
        })
    return JSONResponse({"path": resolved, "entries": entries})


# ── Scheduling endpoints ──────────────────────────────────────────────────────

@router.get("/api/schedule")
def schedules_list(
    
):
    """List all schedules."""
    ds = _resolve_data_store()
    items = ds.read("schedules")
    return JSONResponse([_fill_schedule_defaults(dict(i)) for i in items])


@router.post("/api/schedule")
def schedule_create(
    req: ScheduleTrigger,
    
):
    """Create a schedule to trigger a job (optionally after a delay).

    If ``delay_seconds == 0`` the job is executed immediately.

    Provide either ``template_id`` (references a stored template) or
    inline fields (image, cmd, env, volumes, gpu).
    """
    ds = _resolve_data_store()
    items = ds.read("schedules")
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

    # If delay is 0, execute immediately.
    if req.delay_seconds == 0:
        _run_schedule(sched_id, schedule_item)

    return JSONResponse(result, status_code=201)


def _run_schedule(schedule_id: str, schedule_item: dict) -> None:
    """Execute a job from a schedule and record the job_id."""
    import app.main as _m  # type: ignore

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

    # Build pydantic request model from dict.
    request_model = _m.ExecuteRequest(**body, detach=True)

    resource = "gpu" if request_model.gpu is not None else "cpu"
    released = False
    try:
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
        released = True

        # Record job_id in the schedule.
        _m.data_store.append_list("schedules", schedule_id, "jobs", {
            "job_id": container.id,
            "status": "running",
        })

    except _m.HTTPException:
        raise
    except _m.DockerException as e:
        # Log but don't fail the schedule – the schedule remains active.
        _m.logger.error("Schedule %s failed to execute: %s", schedule_id, e)
        _m.data_store.update("schedules", schedule_id, {
            "status": "error",
            "triggered_at": _now_iso(),
        })
    finally:
        if not released:
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
def staging_list(
    
):
    """List all staging areas (named host-path mount configs)."""
    ds = _resolve_data_store()
    return JSONResponse(ds.read("staging"))


@router.post("/api/staging")
def staging_create(
    req: StagingCreate,
    
):
    """Create a staging area – a named reference to a host path mount.

    Staging areas help the caller track where output data is written during
    job execution, and enable file-browsing tools to surface the important
    directories.
    """
    ds = _resolve_data_store()
    items = ds.read("staging")
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

@router.get("/api/jobs")
def jobs_filter(
    state: str = Query(default="*", description="Filter by state: running, stopped, or * for all"),
    
):
    """List jobs filtered by status.

    ``/api/jobs?state=running`` — only running jobs
    ``/api/jobs?state=stopped`` — only stopped/jobs with exit code
    ``/api/jobs`` or ``/api/jobs?state=*`` — all jobs
    """
    js = _resolve_job_store()
    records = js.get_by_state(state)
    result = []
    for job in records:
        data = job.model_dump(mode="json")
        _enrich_job_data(job, data)
        result.append(data)
    return JSONResponse(result)


def _enrich_job_data(job, data: dict) -> None:
    """Mutate a job data dict in-place with live container state."""
    _import_main()  # ensure main is loaded (for client, job_store, registry)
    import app.main as _m  # type: ignore
    _TERMINAL_STATES = {"exited", "dead"}
    enriched_record = job
    if job.status == "running" and job.docker_backed:
        try:
            container = _m.client.containers.get(job.container_id)
            container.reload()
            docker_status = container.status
            if docker_status in _TERMINAL_STATES:
                exit_code = container.attrs.get("State", {}).get("ExitCode")
                _m.job_store.mark_stopped(job.job_id, exit_code=exit_code)
                data["status"] = "stopped"
                data["exit_code"] = exit_code
                refreshed = _m.job_store.get(job.job_id)
                enriched_record = refreshed or job
                _m.registry.on_job_complete(enriched_record, exit_code)
            elif docker_status == "running":
                stats = _m._fetch_resources(container)
                data["resources"] = stats.model_dump() if stats else None
        except _m.NotFound:
            _m.job_store.mark_stopped(job.job_id)
            data["status"] = "stopped"
            refreshed = _m.job_store.get(job.job_id)
            enriched_record = refreshed or job
            _m.registry.on_job_complete(enriched_record, None)
        except _m.DockerException:
            pass
    _m.registry.on_enrich(enriched_record, data)


# ── Deployment verification ──────────────────────────────────────────────────

@router.get("/api/deployments/{job_id}/status")
def deployment_status(
    job_id: str,
    
):
    """Check the outcome of a deployment (job).

    Returns the job status, exit code, and a human-readable success/failure
    label.  This endpoint is designed to be polled by the UI or a CI system
    to determine whether a training job completed successfully.

    Response:
        status_code: 200
            {
                "job_id": "...",
                "status": "running",
                "exit_code": 0,
                "success": true,                  // only when status=stopped+exit_code==0
                "message": "Job completed successfully"  // human readable
            }
    """
    js = _resolve_job_store()
    job = js.get(job_id)

    # Also try to resolve from Docker (container may have exited but record not updated).
    if job is None:
        try:
            container = _import_main().client.containers.get(job_id)
            attrs = container.attrs.get("State", {})
            exit_code = attrs.get("ExitCode", -1)
            # If the container has exited but no job record exists, create one.
            if exit_code:
                return JSONResponse({
                    "job_id": job_id,
                    "status": "stopped",
                    "exit_code": int(exit_code) if exit_code in (0, 1) else exit_code,
                    "success": int(exit_code) == 0,
                    "message": "Container exited with success" if int(exit_code) == 0 else f"Container exited with code {exit_code}",
                })
            return JSONResponse(status_code=404, content={"detail": f"No job found for {job_id}"})
        except _import_main().NotFound:
            return JSONResponse(status_code=404, content={"detail": f"No job found for {job_id}"})

    data = {"job_id": job_id}

    if job.status == "running":
        data.update({
            "status": "running",
            "exit_code": None,
            "success": False,
            "message": "Job is currently running",
        })
        # Try to get live container status.
        try:
            container = _import_main().client.containers.get(job_id)
            container.reload()
            if container.status in ("exited", "dead"):
                ec = container.attrs.get("State", {}).get("ExitCode")
                ec = int(ec) if ec not in (None, 0, 1) else ec
                _import_main().job_store.mark_stopped(job.job_id, exit_code=ec)
            data.update({
                "status": container.status,
                "exit_code": None,
                "success": False,
                "message": f"Container is {container.status}",
            })
        except _import_main().NotFound:
            _import_main().job_store.mark_stopped(job.job_id)
            data.update({
                "status": "stopped",
                "exit_code": None,
                "success": False,
                "message": "Container vanished — job may have failed",
            })
    else:
        ec = job.exit_code
        succeeded = ec == 0
        data.update({
            "status": job.status,
            "exit_code": ec,
            "success": succeeded,
            "message": "Job completed successfully" if succeeded else f"Job exited with code {ec}",
        })

    return JSONResponse(data)
