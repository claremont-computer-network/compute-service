import os
import threading
import typing as t
import logging
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from fastapi import FastAPI, HTTPException, Header, Depends, Request
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
import docker
from docker.errors import DockerException, NotFound, ContainerError
from dotenv import load_dotenv
from app.jobs import JobStore, _fetch_resources

load_dotenv()  # optional .env

logger = logging.getLogger("caas.dispatcher")

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
    """
    Counting-semaphore pool keyed by resource name.

    Each key maps to the number of concurrent slots allowed for that resource.
    To configure additional slot pools for new resource names (for example,
    ``"tpu"``), add them to ``from_env()``. Callers that choose which
    resource name to acquire may also need corresponding updates.
    """
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
            return True   # unknown resource → always allow (forward compat)
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
    """Block until a slot is free, or raise 503 after QUEUE_TIMEOUT seconds.

    These endpoints are synchronous (``def``, not ``async def``), so FastAPI
    dispatches each request to a thread-pool worker.  The semaphore wait
    occupies that worker thread, not the event-loop thread, which is the
    correct behaviour for sync endpoints.  If these endpoints are ever
    converted to ``async def``, replace this with an ``asyncio.Semaphore``
    (or ``anyio.CapacityLimiter``) to avoid stalling the event loop.
    """
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
    job_store.hydrate_from_docker(client)
    yield


app = FastAPI(title="Compute Service Dispatcher", lifespan=lifespan)

# Serve the static web UI at /ui (no new API endpoints — uses existing job APIs,
# including reading /v1/jobs and stopping jobs via DELETE /v1/jobs/{id}).
def _resolve_ui_dir() -> t.Optional[str]:
    """Return the first existing ui/ directory, or None if none is found.

    Search order:
      1. DISPATCHER_UI_DIR env var (explicit override)
      2. Two levels above this file (repo-root ui/ — dev layout)
      3. Alongside this file (dispatcher/app/ui/ — container layout)
    """
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


class VolumeSpec(BaseModel):
    host_path: str
    container_path: str
    mode: str = Field("rw", description="Mount mode: rw or ro")


class GpuRequest(BaseModel):
    # Pass "all" to expose every GPU, or a list of device IDs e.g. ["0", "1"]
    device_ids: t.Union[t.List[str], t.Literal["all"]] = "all"
    # Driver capabilities forwarded to the NVIDIA container runtime
    capabilities: t.List[str] = Field(default_factory=lambda: ["gpu"])


class ContainerOptions(BaseModel):
    """Container runtime options shared by all execution endpoints.

    Adding a new runtime option (e.g. network_mode, cpus) means adding it
    here once.  Handlers receive it automatically via _prepare_run(); the
    client and magic each need a one-liner addition.
    """
    image: str
    env: t.Optional[t.Dict[str, str]] = None
    volumes: t.Optional[t.List[VolumeSpec]] = None
    # GPU access via the NVIDIA container runtime.
    # Requires nvidia-container-toolkit on the host.
    gpu: t.Optional[GpuRequest] = None
    # Shared-memory size passed to Docker (e.g. "1g", "512m").
    # NVIDIA recommends increasing this for PyTorch multi-GPU / DataLoader jobs.
    shm_size: t.Optional[str] = None
    # IPC namespace — set to "host" to share the host IPC namespace, which
    # gives PyTorch DataLoader workers unlimited shared memory.
    ipc_mode: t.Optional[str] = None


class ExecuteRequest(ContainerOptions):
    """Request model for /v1/execute."""
    cmd: t.Union[str, t.List[str], None] = None
    detach: bool = True


class CellRequest(ContainerOptions):
    """Request model for /v1/execute/cell.

    Cell execution is always synchronous (detach is not supported).
    """
    code: str


def get_api_key(x_api_key: t.Optional[str] = Header(None)):
    if not API_KEY:
        # allow running in dev if not set, but warn in logs
        return True
    if not x_api_key or x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API Key")
    return True


def _validate_volumes(volumes: t.List[VolumeSpec]):
    bindings = {}
    for v in volumes:
        hp = os.path.abspath(v.host_path)
        allowed = any(hp == p or hp.startswith(p + os.sep) for p in ALLOWED_HOST_DIRS if p)
        if not allowed:
            raise HTTPException(status_code=400, detail=f"Host path not allowed: {hp}")
        bindings[hp] = {"bind": v.container_path, "mode": v.mode}
    return bindings


_SHM_SUFFIXES: dict[str, int] = {"b": 1, "k": 1024, "m": 1024**2, "g": 1024**3}


def _validate_shm_ipc(shm_size: t.Optional[str], ipc_mode: t.Optional[str]) -> None:
    """Validate shm_size and ipc_mode against server-side policy.

    Raises HTTPException(400) when:
    - ipc_mode is requested but ALLOW_IPC_HOST env var is not set.
    - ipc_mode is a value other than "host".
    - shm_size exceeds the MAX_SHM_SIZE_MB limit.
    - shm_size cannot be parsed.
    """
    if ipc_mode is not None:
        if not ALLOW_IPC_HOST:
            raise HTTPException(
                status_code=400,
                detail="ipc_mode requires ALLOW_IPC_HOST=true on the dispatcher",
            )
        if ipc_mode != "host":
            raise HTTPException(
                status_code=400,
                detail=f"Unsupported ipc_mode: {ipc_mode!r}. Only 'host' is permitted.",
            )
    if shm_size is not None:
        raw = shm_size.strip().lower()
        if not raw:
            raise HTTPException(
                status_code=400,
                detail=f"Cannot parse shm_size: {shm_size!r}. Expected a value like '512m' or '2g'.",
            )
        suffix = raw[-1] if raw[-1] in _SHM_SUFFIXES else "b"
        number_part = raw[:-1] if raw[-1] in _SHM_SUFFIXES else raw
        try:
            size_bytes = float(number_part) * _SHM_SUFFIXES[suffix]
        except (ValueError, IndexError):
            raise HTTPException(
                status_code=400,
                detail=f"Cannot parse shm_size: {shm_size!r}. Expected a value like '512m' or '2g'.",
            )
        size_mb = size_bytes / (1024**2)
        if size_mb > MAX_SHM_SIZE_MB:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"shm_size {shm_size!r} exceeds the server limit of {MAX_SHM_SIZE_MB} MiB. "
                    f"Reduce the value or ask the administrator to raise MAX_SHM_SIZE_MB."
                ),
            )


def _build_device_requests(gpu: GpuRequest) -> list:
    """Translate a GpuRequest into a Docker SDK DeviceRequest list.

    count and device_ids are mutually exclusive per the Docker Engine API:
      - "all" GPUs → count=-1, no device_ids
      - specific IDs → device_ids only, no count
    """
    if gpu.device_ids == "all":
        return [
            docker.types.DeviceRequest(
                count=-1,
                capabilities=[gpu.capabilities],
            )
        ]
    if not gpu.device_ids:
        raise HTTPException(
            status_code=422,
            detail="gpu.device_ids must be a non-empty list or 'all'",
        )
    return [
        docker.types.DeviceRequest(
            device_ids=gpu.device_ids,
            capabilities=[gpu.capabilities],
        )
    ]


def _container_error_response(e: ContainerError, include_stdout: bool = False) -> dict:
    """Build the response body dict for a non-zero container exit.

    Both execution endpoints return HTTP 200 on non-zero exits so callers can
    inspect the output without catching exceptions.  The only difference is
    whether stdout is included alongside stderr — shell commands may write to
    either stream, while `python -c` tracebacks always go to stderr.

    Adding a new execution endpoint: call this instead of repeating the
    bytes-decoding logic.
    """
    stderr = e.stderr.decode(errors="replace") if isinstance(e.stderr, bytes) else (e.stderr or "")
    stdout = ""
    if include_stdout and hasattr(e, "stdout") and isinstance(e.stdout, bytes):
        stdout = e.stdout.decode(errors="replace")
    return {
        "status": "exited",
        "exit_code": e.exit_status,
        "logs": stdout + stderr,
    }


def _prepare_run(req: ContainerOptions) -> dict:
    """Validate a request and return Docker run kwargs ready for containers.run().

    Handles the pipeline that is identical for every execution endpoint:
      1. Validate and resolve volume bind-mounts
      2. Build NVIDIA DeviceRequest objects
      3. Enforce shm_size / ipc_mode server policy
      4. Pull the image if it is not present locally
      5. Assemble the kwargs dict

    The caller is responsible for setting `command` on the returned dict
    before passing it to containers.run().
    """
    volumes = _validate_volumes(req.volumes) if req.volumes else None

    device_requests = None
    if req.gpu is not None:
        device_requests = _build_device_requests(req.gpu)

    _validate_shm_ipc(req.shm_size, req.ipc_mode)

    try:
        client.images.get(req.image)
    except docker.errors.ImageNotFound:
        client.images.pull(req.image)

    kwargs: dict = dict(
        environment=req.env,
        volumes=volumes,
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


@app.post("/v1/execute")
def execute(req: ExecuteRequest, authorized: bool = Depends(get_api_key)):
    resource = "gpu" if req.gpu is not None else "cpu"
    _acquire_slot(resource)
    released = False
    try:
        run_kwargs = _prepare_run(req)
        run_kwargs["command"] = req.cmd

        if req.detach:
            run_kwargs["detach"] = True
            container = client.containers.run(req.image, **run_kwargs)
            record = job_store.register(container, image=req.image, cmd=req.cmd)
            # Detached jobs run in the background; release the slot now so other
            # submissions are not blocked by the container's full lifetime.
            resource_slots.release(resource)
            released = True
            return JSONResponse({"job_id": record.job_id, "container_id": container.id, "status": "running"})

        # Blocking run — hold the slot for the full duration, then release in finally.
        run_kwargs["detach"] = False
        run_kwargs["remove"] = True
        try:
            output = client.containers.run(req.image, **run_kwargs)
            return JSONResponse({
                "container_id": None,
                "status": "exited",
                "exit_code": 0,
                "logs": output.decode(errors="replace"),
            })
        except ContainerError as e:
            # Non-zero exit — return logs inline rather than a 500 so the
            # caller can inspect the output.
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
    """Run a notebook cell as `python -c <code>` and return its output.

    Execution is always synchronous (detach=False, remove=True) — the response
    is returned only after the container exits.  The job is registered in the
    job store so it appears in the UI, then immediately marked stopped.
    """
    resource = "gpu" if req.gpu is not None else "cpu"
    _acquire_slot(resource)
    try:
        # Validate and build run kwargs first — if this raises (bad shm_size,
        # ipc_mode blocked, invalid volumes, etc.) no job record is created.
        cmd = ["python", "-c", req.code]
        run_kwargs = _prepare_run(req)
        run_kwargs["command"] = cmd
        run_kwargs["detach"] = False
        run_kwargs["remove"] = True

        # Register only after validation passes so invalid requests don't
        # pollute the job store.
        job_id = uuid.uuid4().hex
        job_store.register_sync(job_id, image=req.image, cmd=cmd)
        try:
            output = client.containers.run(req.image, **run_kwargs)
            job_store.mark_stopped(job_id, exit_code=0)
            return JSONResponse({
                "status": "exited",
                "exit_code": 0,
                "logs": output.decode(errors="replace"),
            })
        except ContainerError as e:
            # User code exited non-zero — return logs rather than a 500 so the
            # client can display the traceback inline, just like a local cell.
            job_store.mark_stopped(job_id, exit_code=e.exit_status)
            return JSONResponse(_container_error_response(e))
        except DockerException as e:
            job_store.mark_stopped(job_id, exit_code=-1)
            raise HTTPException(status_code=500, detail=str(e))
    except HTTPException:
        raise
    except DockerException as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        resource_slots.release(resource)


# ── Job registry ──────────────────────────────────────────────────────────────

def _enrich_job_data(job, data: dict) -> None:
    """Mutate a job data dict in-place with live container state.

    For jobs recorded as "running", inspect the actual Docker container state:
    - Container gone (NotFound): mark stopped in the store and in data.
    - Container exited: update status + exit_code in both the store and data.
    - Container still running: attach live resource stats.
    - Docker unavailable: leave data unchanged (stale status is better than 500).

    Centralised here so both GET /v1/jobs and GET /v1/jobs/{job_id} stay in sync.
    Adding a new state transition only needs to happen in one place.
    """
    # Docker terminal states that are not "running" — treat all of them as stopped.
    # "exited" is the normal case; "dead" means Docker gave up; anything else
    # (e.g. "created", "paused", "removing") is transitional — we don't fetch
    # stats for those either, but we only mark_stopped for confirmed terminal states.
    _TERMINAL_STATES = {"exited", "dead"}

    if job.status != "running":
        return
    if not job.docker_backed:
        # Non-container-backed job (e.g. execute_cell tracked by UUID).
        # Status is managed directly by execute_cell — skip Docker enrichment.
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
        # else: transitional state ("created", "paused", "removing") —
        # leave data unchanged; the next poll will catch the final state.
    except NotFound:
        job_store.mark_stopped(job.job_id)
        data["status"] = "stopped"
    except DockerException:
        pass  # leave data as-is — stale "running" is safer than a 500


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
        pass  # already gone — mark it stopped below
    except DockerException as e:
        raise HTTPException(status_code=500, detail=str(e))

    # Mark stopped before remove so the registry is consistent even if
    # remove() raises (the container is already dead at this point).
    job_store.mark_stopped(job_id)

    try:
        container = client.containers.get(job.container_id)
        container.remove()
    except NotFound:
        pass  # already removed — not an error
    except DockerException as e:
        raise HTTPException(status_code=500, detail=str(e))

    return JSONResponse({"job_id": job_id, "status": "stopped"})


@app.get("/v1/logs/{container_id}")
def get_logs(container_id: str, follow: bool = False, authorized: bool = Depends(get_api_key)):
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
        return {"status": "ok"}
    except Exception:
        raise HTTPException(status_code=500, detail="Docker unreachable")
