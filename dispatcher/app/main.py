"""
dispatcher/app/main.py
───────────────────────
FastAPI application — HTTP endpoints and application lifecycle.

Architecture
────────────
Feature logic lives in ``dispatcher/app/plugins/`` as CaasPlugin subclasses.
``main.py`` is responsible only for:

- Parsing environment-variable configuration.
- Owning the Docker client and job store.
- Defining the HTTP endpoints.
- Invoking the plugin registry at the right points in each request lifecycle.

Plugin extension points
────────────────────────
``registry.pre_create(req, create_kwargs)``
    Called after ``_prepare_run()`` but before ``containers.create()`` /
    ``containers.run()``.  Plugins mutate *create_kwargs* in-place.

``registry.on_register(record)``
    Called immediately after ``job_store.register()``.

``registry.post_run(record, result)``
    Called after the container exits and logs are captured.
"""
import asyncio
import os
import typing as t
import logging
import threading
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from fastapi import FastAPI, HTTPException, Header, Depends
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
import docker
from docker.errors import DockerException, NotFound, ContainerError
from dotenv import load_dotenv
from app.jobs import JobStore, JobRecord, _fetch_resources
from app.core.plugin import registry
from app.core.data_store import DataStore, DEFAULT_DATA_DIR
from app.plugins import register_default_plugins
from app.image_registry import populate as populate_image_registry

load_dotenv()  # optional .env

logger = logging.getLogger("caas.dispatcher")

# ── Module-level configuration ────────────────────────────────────────────────
# These variables are read by plugin modules at call-time (via ``import app.main``)
# so that tests can override them without reloading the entire module.

API_KEY = os.getenv("DISPATCHER_API_KEY")
# Explicit allow-list of host paths that may be bind-mounted into jobs.
# Empty string → no mounts allowed. Must be set deliberately.
ALLOWED_HOST_DIRS = [p for p in os.getenv("ALLOWED_HOST_DIRS", "").split(",") if p.strip()]
# Controls whether ipc_mode=host is permitted (Docker shares host IPC namespace).
ALLOW_IPC_HOST = os.getenv("ALLOW_IPC_HOST", "false").lower() == "true"
# Maximum shared-memory segment size (in MiB) that callers may request.
_raw_max_shm = os.getenv("MAX_SHM_SIZE_MB", "8192")
try:
    MAX_SHM_SIZE_MB = int(_raw_max_shm)
except ValueError:
    logger.warning(
        "MAX_SHM_SIZE_MB=%r is not a valid integer – falling back to 8192 MiB.",
        _raw_max_shm,
    )
    MAX_SHM_SIZE_MB = 8192

# ── Resource queue ────────────────────────────────────────────────────────────

@dataclass
class ResourceSlots:
    """Counting-semaphore pool keyed by resource name."""
    _slots: dict[str, threading.Semaphore] = field(default_factory=dict)

    @staticmethod
    def _parse_slot_count(env_var: str, default: int) -> int:
        raw = os.getenv(env_var, str(default))
        try:
            value = int(raw)
        except ValueError:
            logger.warning(
                "%s=%r is not a valid integer – falling back to %d.",
                env_var, raw, default,
            )
            return default
        if value < 0:
            logger.warning(
                "%s=%r is negative – using 0 to avoid an invalid semaphore value.",
                env_var, raw,
            )
            return 0
        return value

    @classmethod
    def from_env(cls) -> "ResourceSlots":
        return cls(_slots={
            "gpu": threading.Semaphore(cls._parse_slot_count("MAX_CONCURRENT_GPU_JOBS", 1)),
            "cpu": threading.Semaphore(cls._parse_slot_count("MAX_CONCURRENT_CPU_JOBS", 4)),
        })

    def acquire(self, resource: str, timeout: int) -> bool:
        """Attempt to acquire one slot for *resource*.  Returns True on success."""
        sem = self._slots.get(resource)
        if sem is None:
            return True
        return sem.acquire(timeout=timeout)

    def release(self, resource: str) -> None:
        sem = self._slots.get(resource)
        if sem is not None:
            sem.release()


def _parse_queue_timeout() -> int:
    raw = os.getenv("QUEUE_TIMEOUT_SECS", "300")
    try:
        value = int(raw)
    except ValueError:
        logger.warning(
            "QUEUE_TIMEOUT_SECS=%r is not a valid integer – falling back to 300s.",
            raw,
        )
        return 300
    if value < 0:
        logger.warning(
            "QUEUE_TIMEOUT_SECS=%r is negative – using 0 (fail-fast mode).",
            raw,
        )
        return 0
    return value


QUEUE_TIMEOUT = _parse_queue_timeout()
resource_slots = ResourceSlots.from_env()


# ── Sandbox TTL (background reaper) ───────────────────────────────────────────

def _parse_sandbox_ttl() -> int:
    raw = os.getenv("SANDBOX_TTL_SECS", "1800")  # 30 minutes default
    try:
        value = int(raw)
    except ValueError:
        logger.warning(
            "SANDBOX_TTL_SECS=%r is not a valid integer – falling back to 1800s.",
            raw,
        )
        return 1800
    if value < 0:
        logger.warning(
            "SANDBOX_TTL_SECS=%r is negative – using 0 (no sandbox reaping).",
            raw,
        )
        return 0
    return value


def _acquire_slot(resource: str) -> None:
    """Block until a slot is free, or raise 503 after QUEUE_TIMEOUT seconds."""
    if not resource_slots.acquire(resource, timeout=QUEUE_TIMEOUT):
        raise HTTPException(
            status_code=503,
            detail=(
                f"No {resource.upper()} slots available — all slots were busy for "
                f"{QUEUE_TIMEOUT}s. Try again later."
            ),
        )


@asynccontextmanager
async def lifespan(app: FastAPI):
    if not API_KEY:
        logger.warning(
            "DISPATCHER_API_KEY is not set – all requests are accepted without "
            "authentication. Set this variable before exposing the service."
        )
    if not ALLOWED_HOST_DIRS:
        logger.warning(
            "ALLOWED_HOST_DIRS is not set – volume mounts will be rejected for all "
            "requests. Set this variable to permit specific host paths."
        )
    logger.info("Loaded plugins: %s", registry.names())
    job_store.hydrate_from_docker(client)
    populate_image_registry(client)

    # Recover sandbox containers: scan for containers with caas.sandbox=true and
    # sync their resource slots so the dispatcher accurately reflects occupied
    # hardware after a restart.
    try:
        sandbox_containers = client.containers.list(
            filters={"label": "caas.sandbox=true"}
        )
        for sc in sandbox_containers:
            record = job_store.get(sc.id)
            if record is not None:
                record.job_type = "sandbox"
                record.resource_type = _determine_resource_type(sc)
                record.container_id = sc.id
                job_store._jobs[sc.id] = record  # pylint: disable=protected-access
                sandbox_hydrate_container(sc, record)
                logger.info("Restored sandbox %s (%s slot)", record.job_id[:12], record.resource_type)
            else:
                # Container exists but record may have been evicted – still need to
                # acquire the slot so the next job assignment is accurate.
                record = JobRecord(
                    job_id=sc.id,
                    container_id=sc.id,
                    image=sc.image.tags[0] if sc.image.tags else sc.image.short_id,
                    cmd=sc.attrs.get("Config", {}).get("Cmd"),
                    job_type="sandbox",
                    submitted_at=datetime.now(timezone.utc),
                    resource_type=_determine_resource_type(sc),
                )
                job_store._jobs[sc.id] = record  # pylint: disable=protected-access
                sandbox_hydrate_container(sc, record)
                logger.info("Recovered orphan sandbox %s (%s slot)", sc.id[:12], record.resource_type)
    except Exception as exc:
        logger.warning("Failed to recover sandbox containers: %s", exc)

    # Start the sandbox reaper that cleans up idle sandboxes to prevent GPU
    # slot deadlock (a zombie sandbox holding a GPU indefinitely is the only
    # way the slot pool can exhaust with no running jobs).
    import app.api_extensions as _ext
    schedule_task = asyncio.ensure_future(_ext._scan_schedules())
    reaper_task = asyncio.ensure_future(_reap_sandbox(idle_seconds=_parse_sandbox_ttl()))

    yield
    schedule_task.cancel()
    reaper_task.cancel()


app = FastAPI(title="Compute Service Dispatcher", lifespan=lifespan)


def _resolve_ui_dir() -> t.Optional[str]:
    """Return the first existing ui/ directory, or None if none is found."""
    candidates: list[str] = []
    configured = os.getenv("DISPATCHER_UI_DIR")
    if configured:
        candidates.append(configured)
    candidates.append(os.path.join(os.path.dirname(__file__), "..", "..", "ui"))
    candidates.append(os.path.join(os.path.dirname(__file__), "ui"))
    for candidate in candidates:
        if os.path.isdir(candidate):
            return candidate
    if configured:
        logger.warning(
            "DISPATCHER_UI_DIR=%r does not point to an existing directory; "
            "the /ui route will not be mounted.",
            configured,
        )
    return None


_ui_dir = _resolve_ui_dir()
if _ui_dir is not None:
    app.mount("/ui", StaticFiles(directory=_ui_dir, html=True), name="ui")

client = docker.from_env()
job_store = JobStore()

# ── Sandbox helpers ───────────────────────────────────────────────────────────
# These are populated after hydrate_from_docker() runs in the lifespan.


def _determine_resource_type(container) -> str:
    """Inspect a container's Docker attrs and return its resource type ("gpu" or "cpu").

    Docker stores device requests under ``HostConfig.DeviceRequests``.  Each
    request has a ``Capabilities`` key (capital C) with value
    ``[["gpu"], ...]`` (list-of-lists).
    """
    reqs = container.attrs.get("HostConfig", {}).get("DeviceRequests") or []
    for req in reqs:
        caps = req.get("Capabilities") or req.get("capabilities")
        if caps:
            for level in caps:
                if isinstance(level, list):
                    for item in level:
                        if item == "gpu":
                            return "gpu"
                elif level == "gpu":
                    return "gpu"
    return "cpu"


def sandbox_hydrate_container(container, record) -> None:
    """Sync ResourceSlots state for a recovered sandbox container.

    Scans the container's Docker attributes to determine whether it holds a
    GPU or CPU slot, then decrements the appropriate semaphore. Runs at
    dispatcher startup after JobStore.hydrate_from_docker() so the process
    accurately reflects hardware that other containers are occupying.

    Uses timeout=0 to fail-fast if a slot is somehow already held (safety
    against double-acquisition).  Raises RuntimeError on failure so the
    caller can abort recovery gracefully rather than silently corrupting
    slot accounting.

    Sets ``sandbox_last_access`` to *now* (not ``record.submitted_at``) so
    the recovered sandbox is not immediately flagged as idle by the reaper.
    The container record is not modified.
    """
    resource = _determine_resource_type(container)
    if not resource_slots.acquire(resource, timeout=0):
        raise RuntimeError(
            f"Slot {resource!r} already held for recovered sandbox "
            f"{record.job_id[:12]} — possible slot corruption"
        )
    sandbox_last_access[record.job_id] = datetime.now(timezone.utc)


def _release_sandbox_slots(job) -> None:
    """Release resource slot and clean tracker for an exited sandbox.

    Idempotent: guard with ``sandbox_last_access`` so repeated cleanup
    (e.g. enrich detects exit then DELETE runs later) only releases once.

    Uses ``job.resource_type`` (recorded at creation) so the call is
    deterministic even if the Docker container has been obliterated.
    """
    last_access = sandbox_last_access.pop(job.job_id, None)
    if last_access is None:
        return
    resource_slots.release(job.resource_type)

# ── Persistent data store ────────────────────────────────────────────────────
# Mounted at /srv/caas-data in the container so data survives restarts.
# Eagerly initialised at import time so that _get_data_store() is always
# available for lazy consumers (extension endpoints) without None guards.

_DATA_DIR = os.getenv("CAAS_DATA_DIR") or DEFAULT_DATA_DIR
_data_store: t.Optional[DataStore] = None


def _get_data_store() -> DataStore:
    global _data_store
    if _data_store is None:
        _data_store = DataStore(_DATA_DIR)
    return _data_store


# For module-level access (extensions import via _get_data_store).
data_store = _get_data_store()

# Register built-in plugins immediately so they are available even when the
# lifespan context manager is not entered (e.g. in tests that use TestClient
# without the context manager protocol).
register_default_plugins(job_store, client)

# ── Auth (needs to be before the router import so FastAPI can resolve it) ──────
# Tests monkey-patch ``app.main.API_KEY`` directly.

def get_api_key(x_api_key: t.Optional[str] = Header(None)):
    """FastAPI dependency: validate the ``X-Api-Key`` header against ``API_KEY``."""
    if not API_KEY:
        return True
    if not x_api_key or x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API Key")
    return True


# ── Sandbox tracking (persistent interactive containers) ─────────────────────

sandbox_last_access: dict[str, datetime] = {}


# ── Extension API router ─────────────────────────────────────────────────────
# Must be imported after client, job_store, and data_store are set up.
from app.api_extensions import router as api_router  # noqa: E402
app.include_router(api_router, dependencies=[Depends(get_api_key)])


# ── Request / response models ─────────────────────────────────────────────────

class VolumeSpec(BaseModel):
    host_path: str
    container_path: str
    mode: str = Field("rw", description="Mount mode: rw or ro")


class GpuRequest(BaseModel):
    device_ids: t.Union[t.List[str], t.Literal["all"]] = "all"
    capabilities: t.List[str] = Field(default_factory=lambda: ["gpu"])


class ContainerOptions(BaseModel):
    """Container runtime options shared by all execution endpoints."""
    image: str
    env: t.Optional[t.Dict[str, str]] = None
    volumes: t.Optional[t.List[VolumeSpec]] = None
    gpu: t.Optional[GpuRequest] = None
    shm_size: t.Optional[str] = None
    ipc_mode: t.Optional[str] = None


class ExecuteRequest(ContainerOptions):
    """Request model for /v1/execute."""
    cmd: t.Union[str, t.List[str], None] = None
    detach: bool = True


class CellRequest(ContainerOptions):
    """Request model for /v1/execute/cell."""
    code: str
    suppress_entrypoint: bool = Field(
        default=False,
        description=(
            "When True the container's entrypoint is overridden with an empty "
            "string so the image's ENTRYPOINT script is skipped entirely.  "
            "Useful for NVIDIA NGC images (nvcr.io/*) whose entrypoint prints "
            "a multi-page banner to stdout before exec-ing the user command."
        ),
    )


class SandboxRequest(ContainerOptions):
    """Request model for /v1/sandbox."""
    pass


class ExecRequest(BaseModel):
    """Request model for /v1/jobs/{job_id}/exec."""
    cmd: str

# ── Docker helpers ────────────────────────────────────────────────────────────

def _build_device_requests(gpu: GpuRequest) -> list:
    """Translate a GpuRequest into a Docker SDK DeviceRequest list."""
    if gpu.device_ids == "all":
        return [docker.types.DeviceRequest(count=-1, capabilities=[gpu.capabilities])]
    if not gpu.device_ids:
        raise HTTPException(status_code=422, detail="gpu.device_ids must be a non-empty list or 'all'")
    return [docker.types.DeviceRequest(device_ids=gpu.device_ids, capabilities=[gpu.capabilities])]


def _container_error_response(e: ContainerError, include_stdout: bool = False) -> dict:
    """Build the response body dict for a non-zero container exit."""
    stderr = e.stderr.decode(errors="replace") if isinstance(e.stderr, bytes) else (e.stderr or "")
    stdout = ""
    if include_stdout and hasattr(e, "stdout") and isinstance(e.stdout, bytes):
        stdout = e.stdout.decode(errors="replace")
    return {"status": "exited", "exit_code": e.exit_status, "logs": stdout + stderr}


def _ensure_image(image: str) -> None:
    """Pull *image* if it is not already present locally."""
    try:
        client.images.get(image)
    except docker.errors.ImageNotFound:
        client.images.pull(image)


def _prepare_run(req: ContainerOptions) -> dict:
    """Build Docker run kwargs.

    Volume validation and shm/ipc policy enforcement are delegated to
    VolumePolicyPlugin and ShmIpcPolicyPlugin via registry.pre_create().
    The ``volumes`` key is intentionally None here; VolumePolicyPlugin
    populates it with resolved bindings.

    Note: image pulling happens *after* this returns, once plugin validation
    has passed, so that invalid requests don't trigger unnecessary pulls.
    """
    device_requests = None
    if req.gpu is not None:
        device_requests = _build_device_requests(req.gpu)

    kwargs: dict = dict(
        environment=req.env,
        volumes=None,
        stdout=True,
        stderr=True,
        device_requests=device_requests,
        labels={"caas.managed": "true"},
    )
    if req.shm_size:
        kwargs["shm_size"] = req.shm_size
    if req.ipc_mode:
        kwargs["ipc_mode"] = req.ipc_mode
    return kwargs


# ── Endpoints ─────────────────────────────────────────────────────────────────

@app.post("/v1/execute")
def execute(req: ExecuteRequest, authorized: bool = Depends(get_api_key)):
    resource = "gpu" if req.gpu is not None else "cpu"
    _acquire_slot(resource)
    released = False
    try:
        run_kwargs = _prepare_run(req)
        run_kwargs["command"] = req.cmd

        # Plugins validate first (shm policy, volume policy, etc.) — before pull.
        registry.pre_create(req, run_kwargs)
        _ensure_image(req.image)

        if req.detach:
            run_kwargs["detach"] = True
            container = client.containers.run(req.image, **run_kwargs)
            record = job_store.register(container, image=req.image, cmd=req.cmd)
            registry.on_register(record)
            resource_slots.release(resource)
            released = True
            return JSONResponse({"job_id": record.job_id, "container_id": container.id, "status": "running"})

        run_kwargs["detach"] = False
        run_kwargs["remove"] = True
        try:
            output = client.containers.run(req.image, **run_kwargs)
            return JSONResponse({"container_id": None, "status": "exited", "exit_code": 0,
                                 "logs": output.decode(errors="replace")})
        except ContainerError as e:
            body = _container_error_response(e, include_stdout=True)
            body["container_id"] = None
            return JSONResponse(body)
    except HTTPException:
        raise
    except DockerException as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        if not released:
            resource_slots.release(resource)


@app.post("/v1/execute/cell")
def execute_cell(req: CellRequest, authorized: bool = Depends(get_api_key)):
    """Run a notebook cell as `python -c <code>` and return its output."""
    resource = "gpu" if req.gpu is not None else "cpu"
    _acquire_slot(resource)
    container = None
    record = None
    fired_complete_hook = False
    try:
        cmd = ["python", "-c", req.code]
        run_kwargs = _prepare_run(req)
        run_kwargs["command"] = cmd
        create_kwargs = {k: v for k, v in run_kwargs.items() if k not in ("stdout", "stderr")}

        # Plugins validate first (shm policy, volume policy, etc.) — before pull.
        registry.pre_create(req, create_kwargs)
        _ensure_image(req.image)

        container = client.containers.create(req.image, **create_kwargs)
        record = job_store.register(container, image=req.image, cmd=cmd, job_type="cell")

        # Plugins react to registration (e.g. ResourceSamplerPlugin starts thread).
        registry.on_register(record)

        container.start()
        result = container.wait()

        exit_code = result.get("StatusCode", -1)
        try:
            stdout = container.logs(stdout=True, stderr=False).decode(errors="replace")
            stderr = container.logs(stdout=False, stderr=True).decode(errors="replace")
            logs   = container.logs(stdout=True,  stderr=True ).decode(errors="replace")
        except (NotFound, DockerException):
            stdout = stderr = logs = ""

        job_store.mark_stopped(record.job_id, exit_code=exit_code)

        response = {
            "status": "exited",
            "exit_code": exit_code,
            "stdout": stdout,
            "stderr": stderr,
            "logs": logs,
        }

        # Plugins react to completion (log retention, sampler stop, etc.).
        registry.post_run(record, response)
        completed_record = job_store.get(record.job_id)
        registry.on_job_complete(completed_record or record, exit_code)
        fired_complete_hook = True

        try:
            container.remove()
        except (NotFound, DockerException):
            pass

        return JSONResponse(response)
    except HTTPException:
        raise
    except (NotFound, DockerException) as e:
        if record is not None:
            existing = job_store.get(record.job_id)
            already_stopped = existing is not None and existing.status == "stopped"
            if not already_stopped:
                job_store.mark_stopped(record.job_id, exit_code=-1)
            if not fired_complete_hook:
                stopped_record = job_store.get(record.job_id)
                registry.on_job_complete(stopped_record or record, -1)
                fired_complete_hook = True  # noqa: F841 — guards re-entry
            if isinstance(e, NotFound) and already_stopped:
                return JSONResponse({"status": "stopped", "exit_code": existing.exit_code,
                                     "stdout": "", "stderr": "", "logs": ""})
        if container is not None:
            try:
                container.remove(force=True)
            except DockerException:
                pass
        if isinstance(e, NotFound):
            return JSONResponse({"status": "stopped", "exit_code": -1, "stdout": "", "stderr": "", "logs": ""})
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        resource_slots.release(resource)


# ── Job registry ──────────────────────────────────────────────────────────────

def _enrich_job_data(job, data: dict) -> None:
    """Mutate a job data dict in-place with live container state."""
    _TERMINAL_STATES = {"exited", "dead"}
    enriched_record = job  # may be replaced with a refreshed record below
    if job.status == "running" and job.docker_backed:
        try:
            container = client.containers.get(job.container_id)
            container.reload()
            docker_status = container.status
            if docker_status in _TERMINAL_STATES:
                exit_code = container.attrs.get("State", {}).get("ExitCode")
                job_store.mark_stopped(job.job_id, exit_code=exit_code)
                # Release resource slot if this is a sandbox (unexpected exit).
                if job.job_type == "sandbox":
                    _release_sandbox_slots(job)
                data["status"] = "stopped"
                data["exit_code"] = exit_code
                refreshed = job_store.get(job.job_id)
                enriched_record = refreshed or job
                registry.on_job_complete(enriched_record, exit_code)
            elif docker_status == "running":
                stats = _fetch_resources(container)
                data["resources"] = stats.model_dump() if stats else None
        except NotFound:
            # Container was obliterated out-of-band (docker rm -f, prune, etc.).
            # Use the stored resource_type for deterministic slot release.
            job_store.mark_stopped(job.job_id)
            data["status"] = "stopped"
            if job.job_type == "sandbox":
                _release_sandbox_slots(job)
            refreshed = job_store.get(job.job_id)
            enriched_record = refreshed or job
            registry.on_job_complete(enriched_record, None)
        except DockerException:
            pass
    registry.on_enrich(enriched_record, data)


@app.get("/v1/jobs")
def list_jobs(authorized: bool = Depends(get_api_key)):
    """Return all known jobs with live resource stats for running containers."""
    records = []
    for job in job_store.list_all():
        data = job.model_dump(mode="json")
        _enrich_job_data(job, data)
        records.append(data)
    return JSONResponse(records)


@app.get("/v1/jobs/{job_id}")
def get_job(job_id: str, authorized: bool = Depends(get_api_key)):
    """Return a single job record with live resource stats."""
    job = job_store.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Job not found: {job_id}")
    data = job.model_dump(mode="json")
    _enrich_job_data(job, data)
    return JSONResponse(data)


@app.delete("/v1/jobs/{job_id}")
def stop_job(job_id: str, authorized: bool = Depends(get_api_key)):
    """Stop and remove a running container, then mark the job stopped."""
    job = job_store.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Job not found: {job_id}")
    try:
        container = client.containers.get(job.container_id)
        container.stop()
    except NotFound:
        pass
    except DockerException as e:
        raise HTTPException(status_code=500, detail=str(e))

    job_store.mark_stopped(job_id)

    try:
        container = client.containers.get(job.container_id)
        container.remove()
    except NotFound:
        pass
    except DockerException as e:
        logger.warning("Failed to remove container %s for job %s: %s", job.container_id, job_id, e)

    # Release resource slot if this was a sandbox (always runs, even if container remove failed).
    if job.job_type == "sandbox":
        _release_sandbox_slots(job)

    # Only fire the completion hook if the job was actually running before
    # this request — prevents double-firing if DELETE is retried on an
    # already-stopped job.
    if job.status == "running":
        stopped_record = job_store.get(job_id)
        registry.on_job_complete(stopped_record or job, None)

    return JSONResponse({"job_id": job_id, "status": "stopped"})


# ── Sandbox endpoints ─────────────────────────────────────────────────────────

@app.post("/v1/sandbox")
def create_sandbox(req: SandboxRequest, authorized: bool = Depends(get_api_key)):
    """Create a persistent interactive container (sandbox).

    The sandbox runs ``sleep infinity`` and stays alive for incremental
    command execution via ``POST /v1/jobs/{job_id}/exec``.  Sandboxes hold
    their resource slot (GPU or CPU) indefinitely — until explicitly stopped
    or reclaimed by the background reaper task.

    Resource acquisition:
    ────────────────────
    The resource slot is acquired *before* container creation so that the
    dispatcher accurately reflects hardware occupancy.  If the container
    creation fails the slot is released (``finally`` block).
    """
    resource = "gpu" if req.gpu is not None else "cpu"
    _acquire_slot(resource)
    released = False
    try:
        run_kwargs = _prepare_run(req)
        run_kwargs["command"] = ["sleep", "infinity"]
        run_kwargs["labels"] = {**run_kwargs.get("labels", {}), "caas.sandbox": "true"}

        # Plugins validate first (shm policy, volume policy, etc.) — before pull.
        registry.pre_create(req, run_kwargs)
        _ensure_image(req.image)

        run_kwargs["detach"] = True
        container = client.containers.run(req.image, **run_kwargs)

        record = job_store.register(
            container, image=req.image, cmd=["sleep", "infinity"],
            job_type="sandbox", resource_type=resource,
        )
        sandbox_last_access[record.job_id] = datetime.now(timezone.utc)
        registry.on_register(record)

        released = True
        return JSONResponse({
            "sandbox_id": record.job_id,
            "status": "running",
            "resource_type": resource,
        })
    except HTTPException:
        raise
    except DockerException as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        if not released:
            resource_slots.release(resource)


@app.post("/v1/jobs/{job_id}/exec")
def exec_in_sandbox(job_id: str, req: ExecRequest, authorized: bool = Depends(get_api_key)):
    """Execute a command inside a sandbox container.

    The container must have ``job_type="sandbox"`` or the request is rejected
    with 400.  On success the sandbox's ``sandbox_last_access`` timestamp is
    refreshed so the reaper considers the sandbox active.
    """
    job = job_store.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Job not found: {job_id}")
    if job.job_type != "sandbox":
        raise HTTPException(
            status_code=400,
            detail=f"Job {job_id} is not a sandbox (job_type={job.job_type!r})",
        )

    try:
        container = client.containers.get(job.container_id)
    except NotFound:
        job_store.mark_stopped(job_id)
        _release_sandbox_slots(job)
        stopped_record = job_store.get(job_id)
        registry.on_job_complete(stopped_record or job, None)
        raise HTTPException(status_code=404, detail=f"Sandbox container not found: {job_id}")

    try:
        result = container.exec_run(req.cmd)
        exit_code = result.exit_code
        stdout = result.output.decode(errors="replace") if result.output else ""
    except ContainerError as e:
        stdout = ""
        stderr = e.stderr.decode(errors="replace") if isinstance(e.stderr, bytes) else (e.stderr or "")
        return JSONResponse({
            "stdout": stdout,
            "stderr": stderr,
            "exit_code": e.exit_status,
        })
    except DockerException as e:
        raise HTTPException(status_code=500, detail=str(e))

    sandbox_last_access[job_id] = datetime.now(timezone.utc)
    return JSONResponse({
        "stdout": stdout,
        "exit_code": exit_code,
    })


# ── Sandbox reaper ───────────────────────────────────────────────────────────

async def _reap_sandbox(idle_seconds: int, check_interval: int = 60) -> None:
    """Background task that kills sandboxes idle beyond the configured TTL.

    Runs every ``check_interval`` seconds.  Each cycle collects sandbox IDs
    whose ``sandbox_last_access`` is older than ``idle_seconds``, stops their
    containers, and releases their resource slots — matching the cleanup
    performed by the explicit ``DELETE /v1/jobs/{id}`` endpoint.
    """
    if idle_seconds <= 0:
        return  # reaper disabled
    while True:
        await asyncio.sleep(check_interval)
        now = datetime.now(timezone.utc)
        to_reap: list[str] = []
        for sid, last_access in list(sandbox_last_access.items()):
            if (now - last_access).total_seconds() > idle_seconds:
                to_reap.append(sid)
        if not to_reap:
            continue
        for sid in to_reap:
            job = job_store.get(sid)
            if job is None:
                sandbox_last_access.pop(sid, None)
                continue
            container_stopped = False
            try:
                try:
                    container = client.containers.get(sid)
                    container.stop()
                    container.remove()
                    container_stopped = True
                except NotFound:
                    # Container already gone — nothing to stop/remove.
                    pass
            except DockerException as e:
                logger.warning("Sandbox reaper: Docker error stopping sandbox %s: %s", sid[:12], e)

            # Always release slot and mark stopped, even when container operations fail.
            # The container is no longer running from the system's perspective — we must
            # still clean up the dispatcher's accounting so the slot becomes available.
            _release_sandbox_slots(job)
            job_store.mark_stopped(sid)
            registry.on_job_complete(job_store.get(sid) or job, None)
            logger.info("Reaped idle sandbox %s (idle > %ds)", sid[:12], idle_seconds)


# ── Job registry ──────────────────────────────────────────────────────────────

@app.get("/v1/logs/{container_id}")
def get_logs(container_id: str, follow: bool = False, authorized: bool = Depends(get_api_key)):
    record = job_store.get(container_id)
    if record is not None and record.stored_logs is not None:
        if follow:
            return StreamingResponse(iter([record.stored_logs.encode()]), media_type="text/plain")
        return JSONResponse({"container_id": container_id, "logs": record.stored_logs})

    try:
        cont = client.containers.get(container_id)
    except NotFound:
        raise HTTPException(status_code=404, detail="Container not found")

    if follow:
        def stream_logs():
            for chunk in cont.logs(stream=True, stdout=True, stderr=True, follow=True):
                yield chunk
        return StreamingResponse(stream_logs(), media_type="text/plain")

    logs = cont.logs(stdout=True, stderr=True, tail=1000)
    return JSONResponse({"container_id": container_id, "logs": logs.decode(errors='replace')})


@app.get("/health")
def health():
    try:
        client.ping()
        return {"status": "ok", "plugins": registry.names()}
    except Exception:
        raise HTTPException(status_code=500, detail="Docker unreachable")
