import os
import typing as t
import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException, Header, Depends, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field
import docker
from docker.errors import DockerException, NotFound, ContainerError
from dotenv import load_dotenv

load_dotenv()  # optional .env

logger = logging.getLogger("caas.dispatcher")

API_KEY = os.getenv("DISPATCHER_API_KEY")
# Explicit allow-list of host paths that may be bind-mounted into jobs.
# Empty string → no mounts allowed. Must be set deliberately.
ALLOWED_HOST_DIRS = [p for p in os.getenv("ALLOWED_HOST_DIRS", "").split(",") if p.strip()]


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
    yield


app = FastAPI(title="Compute Service Dispatcher", lifespan=lifespan)

client = docker.from_env()


class VolumeSpec(BaseModel):
    host_path: str
    container_path: str
    mode: str = Field("rw", description="Mount mode: rw or ro")


class GpuRequest(BaseModel):
    # Pass "all" to expose every GPU, or a list of device IDs e.g. ["0", "1"]
    device_ids: t.Union[t.List[str], t.Literal["all"]] = "all"
    # Driver capabilities forwarded to the NVIDIA container runtime
    capabilities: t.List[str] = Field(default_factory=lambda: ["gpu"])


class ExecuteRequest(BaseModel):
    image: str
    cmd: t.Union[str, t.List[str], None] = None
    env: t.Optional[t.Dict[str, str]] = None
    volumes: t.Optional[t.List[VolumeSpec]] = None
    detach: bool = True
    # When set, the job is given access to the specified GPUs via the
    # NVIDIA container runtime.  Requires nvidia-container-toolkit on the host.
    gpu: t.Optional[GpuRequest] = None


class CellRequest(BaseModel):
    """Request model for notebook cell execution via /v1/execute/cell."""
    code: str
    image: str
    env: t.Optional[t.Dict[str, str]] = None
    volumes: t.Optional[t.List[VolumeSpec]] = None
    # Cell execution is always synchronous; detach is not supported.
    gpu: t.Optional[GpuRequest] = None


def get_api_key(x_api_key: t.Optional[str] = Header(None)):
    if API_KEY is None:
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


@app.post("/v1/execute")
def execute(req: ExecuteRequest, authorized: bool = Depends(get_api_key)):
    try:
        volumes = None
        if req.volumes:
            volumes = _validate_volumes(req.volumes)

        # Build NVIDIA device_requests if GPU access is requested.
        device_requests = None
        if req.gpu is not None:
            device_requests = _build_device_requests(req.gpu)

        # ensure the image is available (pull if needed)
        try:
            client.images.get(req.image)
        except docker.errors.ImageNotFound:
            client.images.pull(req.image)

        run_kwargs = dict(
            command=req.cmd,
            environment=req.env,
            volumes=volumes,
            stdout=True,
            stderr=True,
            device_requests=device_requests,
        )

        if req.detach:
            container = client.containers.run(
                req.image,
                detach=True,
                **run_kwargs,
            )
            return JSONResponse({"container_id": container.id, "status": "running"})
        else:
            # Blocking run – wait for completion and return logs inline
            output = client.containers.run(
                req.image,
                detach=False,
                remove=True,
                **run_kwargs,
            )
            return JSONResponse({
                "container_id": None,
                "status": "exited",
                "logs": output.decode(errors="replace"),
            })
    except DockerException as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/v1/execute/cell")
def execute_cell(req: CellRequest, authorized: bool = Depends(get_api_key)):
    """Run a notebook cell as `python -c <code>` and return its output.

    Execution is always synchronous (detach=False, remove=True) — the response
    is returned only after the container exits.
    """
    try:
        volumes = None
        if req.volumes:
            volumes = _validate_volumes(req.volumes)

        device_requests = None
        if req.gpu is not None:
            device_requests = _build_device_requests(req.gpu)

        try:
            client.images.get(req.image)
        except docker.errors.ImageNotFound:
            client.images.pull(req.image)

        output = client.containers.run(
            req.image,
            command=["python", "-c", req.code],
            environment=req.env,
            volumes=volumes,
            stdout=True,
            stderr=True,
            device_requests=device_requests,
            detach=False,
            remove=True,
        )
        return JSONResponse({
            "status": "exited",
            "exit_code": 0,
            "logs": output.decode(errors="replace"),
        })
    except ContainerError as e:
        # User code exited non-zero — return logs rather than a 500 so the
        # client can display the traceback inline, just like a local cell.
        logs = e.stderr.decode(errors="replace") if isinstance(e.stderr, bytes) else (e.stderr or "")
        return JSONResponse({
            "status": "exited",
            "exit_code": e.exit_status,
            "logs": logs,
        })
    except DockerException as e:
        raise HTTPException(status_code=500, detail=str(e))


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
