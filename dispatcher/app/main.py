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
import os
import typing as t
import logging
import threading
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from fastapi import FastAPI, HTTPException, Header, Depends
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
import docker
from docker.errors import DockerException, NotFound, ContainerError
from dotenv import load_dotenv
from app.jobs import JobStore, _fetch_resources
from app.core.plugin import registry
from app.plugins import register_default_plugins

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
    yield


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

# Register built-in plugins immediately so they are available even when the
# lifespan context manager is not entered (e.g. in tests that use TestClient
# without the context manager protocol).
register_default_plugins(job_store, client)


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

# ── Auth ──────────────────────────────────────────────────────────────────────
# Auth is intentionally kept here rather than extracted to a separate module.
# Tests monkey-patch ``app.main.API_KEY`` directly; moving the variable to
# another module would require every test that sets it to also patch that
# module, making the test setup more fragile for no runtime benefit.

def get_api_key(x_api_key: t.Optional[str] = Header(None)):
    """FastAPI dependency: validate the ``X-Api-Key`` header against ``API_KEY``."""
    if not API_KEY:
        return True
    if not x_api_key or x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API Key")
    return True


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
    if job.status != "running":
        return
    if not job.docker_backed:
        return
    try:
        container = client.containers.get(job.container_id)
        container.reload()
        docker_status = container.status
        if docker_status in _TERMINAL_STATES:
            exit_code = container.attrs.get("State", {}).get("ExitCode")
            job_store.mark_stopped(job.job_id, exit_code=exit_code)
            data["status"] = "stopped"
            data["exit_code"] = exit_code
        elif docker_status == "running":
            stats = _fetch_resources(container)
            data["resources"] = stats.model_dump() if stats else None
    except NotFound:
        job_store.mark_stopped(job.job_id)
        data["status"] = "stopped"
    except DockerException:
        pass


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
        raise HTTPException(status_code=500, detail=str(e))

    return JSONResponse({"job_id": job_id, "status": "stopped"})


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
