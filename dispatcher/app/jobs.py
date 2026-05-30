"""
jobs.py
───────
In-memory job registry for the compute-service dispatcher.

JobRecord is a plain Pydantic model so it serialises to JSON for free
via FastAPI's JSONResponse — no manual dict-building needed.

JobStore is a thread-safe in-memory dict keyed by full container ID
(container.id).  The full ID has no collision risk unlike the 12-char
short_id prefix.  State is intentionally ephemeral: the store is rebuilt
from docker ps on each startup via hydrate_from_docker(), so a service
restart does not lose visibility of already-running containers.
"""
from __future__ import annotations

import logging
import threading
import typing as t
from datetime import datetime, timezone

from pydantic import BaseModel, Field

logger = logging.getLogger("caas.jobs")


class GpuStats(BaseModel):
    """GPU usage snapshot from nvidia-smi query output."""
    device_id: int
    gpu_name: str
    temperature: int       # ℃
    memory_used_mib: float
    memory_total_mib: float
    memory_percent: float
    utilization_percent: float


class ResourceStats(BaseModel):
    """Live resource usage snapshot fetched from the Docker stats API."""
    cpu_percent: float
    mem_usage_mib: float   # mebibytes (÷ 1024²) — matches Docker's own display
    mem_limit_mib: float
    mem_percent: float
    gpu: t.Optional[list[GpuStats]] = None  # GPU stats, populated only on GPU containers


class JobRecord(BaseModel):
    """Immutable identity fields plus mutable status for one dispatched job."""
    job_id: str          # full 64-char Docker container ID
    container_id: str    # same as job_id; kept separate for historical compat
    docker_backed: bool = True  # always True; kept for forward-compatibility
    image: str
    cmd: t.Union[str, t.List[str], None] = None
    # Execution mode: "cell" for synchronous execute_cell jobs, "detached" for
    # background /v1/execute jobs, "sandbox" for persistent interactive containers.
    # Used by ResourceSamplerPlugin to decide whether to start a background
    # sampling thread, and by resource management code for slot tracking.
    job_type: str = "detached"
    submitted_at: datetime
    status: str = "running"                        # running | stopped
    exit_code: t.Optional[int] = None
    resources: t.Optional[ResourceStats] = None   # populated on GET, not at submit
    # Sampled resource history collected by a background thread while the
    # container is running.  Populated for execute_cell jobs (which complete
    # before the UI can poll for live stats) but also useful for any job that
    # finishes quickly.  Capped at 200 samples (~10 min at 3 s intervals).
    resource_history: t.List[ResourceStats] = Field(default_factory=list)
    # Logs captured at container exit (execute_cell jobs only).  The container
    # is removed immediately after the cell runs, so this is the only way to
    # serve logs after the fact.  Capped at LOG_MAX_BYTES before storing.
    # Excluded from model_dump() so job-list/detail responses never embed the
    # full log payload; logs are served exclusively via GET /v1/logs/{id}.
    stored_logs: t.Optional[str] = Field(default=None, exclude=True)
    # Which resource slot this job consumed (e.g. "gpu" or "cpu"). Tracked
    # in the record so slot release is deterministic even if the container
    # has been obliterated from the Docker daemon before cleanup runs.
    resource_type: str = "cpu"


def _parse_gpu_stats() -> t.Optional[list[GpuStats]]:
    """Parse nvidia-smi output into a list of GpuStats.

    Returns None if nvidia-smi is unavailable or fails.
    """
    import subprocess

    try:
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=index,name,temperature.gpu,memory.used,memory.total,utilization.gpu",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode != 0:
            return None
        gpus = []
        for line in result.stdout.strip().split("\n"):
            if not line.strip():
                continue
            parts = [p.strip() for p in line.split(",")]
            if len(parts) < 6:
                continue
            memory_used_mib = round(float(parts[3]), 2)
            memory_total_mib = round(float(parts[4]), 2)
            memory_percent = round((memory_used_mib / memory_total_mib * 100.0), 2) if memory_total_mib > 0 else 0.0
            gpus.append(GpuStats(
                device_id=int(parts[0]),
                gpu_name=parts[1],
                temperature=int(parts[2]),
                memory_used_mib=memory_used_mib,
                memory_total_mib=memory_total_mib,
                memory_percent=memory_percent,
                utilization_percent=round(float(parts[5]), 1),
            ))
        return gpus or None
    except (FileNotFoundError, subprocess.TimeoutExpired, ValueError, ZeroDivisionError):
        return None


def _fetch_resources(container) -> t.Optional[ResourceStats]:
    """Call the Docker stats API (single-shot) and compute derived metrics.

    Also queries nvidia-smi for GPU stats when the host has NVIDIA GPUs.
    Returns None if the container is no longer running or stats are
    unavailable — callers must handle this gracefully.
    """
    try:
        raw = container.stats(stream=False)
    except Exception:
        return None

    try:
        # CPU: delta between two consecutive readings that Docker gives us in
        # one single-shot call.
        cpu_delta = (
            raw["cpu_stats"]["cpu_usage"]["total_usage"]
            - raw["precpu_stats"]["cpu_usage"]["total_usage"]
        )
        system_delta = (
            raw["cpu_stats"]["system_cpu_usage"]
            - raw["precpu_stats"]["system_cpu_usage"]
        )
        num_cpus = raw["cpu_stats"].get("online_cpus") or len(
            raw["cpu_stats"]["cpu_usage"].get("percpu_usage", [1])
        )
        cpu_percent = (cpu_delta / system_delta * num_cpus * 100.0) if system_delta > 0 else 0.0

        mem = raw.get("memory_stats", {})
        usage = mem.get("usage", 0)
        limit = mem.get("limit", 1)
        mem_usage_mib = usage / (1024 ** 2)
        mem_limit_mib = limit / (1024 ** 2)
        mem_percent = (usage / limit * 100.0) if limit > 0 else 0.0
        gpu_stats = _parse_gpu_stats()

        return ResourceStats(
            cpu_percent=round(cpu_percent, 2),
            mem_usage_mib=round(mem_usage_mib, 2),
            mem_limit_mib=round(mem_limit_mib, 2),
            mem_percent=round(mem_percent, 2),
            gpu=gpu_stats,
        )
    except (KeyError, TypeError, ZeroDivisionError) as exc:
        logger.debug("Ignoring malformed Docker stats payload: %s", exc)
        return None


class JobStore:
    """Thread-safe in-memory store for dispatched jobs.

    FastAPI runs sync endpoints in a threadpool, so concurrent requests can
    call register()/mark_stopped()/list_all() simultaneously.  A single lock
    around all mutations and the list snapshot is sufficient here.

    Key: full container ID (container.id).  The full ID has no collision risk
    unlike the 12-char short_id prefix.

    Memory bounds
    ─────────────
    MAX_JOBS        Best-effort bound on the number of job records kept.
                    When exceeded, the oldest stopped jobs are evicted first.
                    Running jobs are never evicted, so the store may remain
                    above MAX_JOBS while many jobs are still active.
    LOG_MAX_BYTES   Stored logs are truncated to strictly within this many
                    bytes (UTF-8, including any truncation marker) before
                    being written into the record.  Prevents a single chatty
                    cell job from consuming large amounts of memory.
    """

    MAX_JOBS: int = 500
    LOG_MAX_BYTES: int = 256 * 1024   # 256 KiB

    def __init__(self) -> None:
        self._jobs: dict[str, JobRecord] = {}
        self._lock = threading.Lock()

    # ── write ─────────────────────────────────────────────────────────────────

    def register(self, container, image: str,
                 cmd: t.Union[str, t.List[str], None] = None,
                 job_type: str = "detached",
                 resource_type: str = "cpu") -> JobRecord:
        """Record a newly started detached container (docker_backed=True)."""
        record = JobRecord(
            job_id=container.id,
            container_id=container.id,
            docker_backed=True,
            image=image,
            cmd=cmd,
            job_type=job_type,
            submitted_at=datetime.now(timezone.utc),
            resource_type=resource_type,
        )
        with self._lock:
            self._jobs[record.job_id] = record
            self._evict_oldest_stopped()
        return record

    def register_sync(self, job_id: str, image: str,
                      cmd: t.Union[str, t.List[str], None] = None) -> JobRecord:
        """Record a synchronous execute_cell job by explicit UUID (docker_backed=False).

        job_id and container_id are a UUID, not a Docker container ID.
        _enrich_job_data skips Docker enrichment for these records so it will
        not attempt containers.get() with a non-container ID.
        """
        record = JobRecord(
            job_id=job_id,
            container_id=job_id,
            docker_backed=False,
            image=image,
            cmd=cmd,
            submitted_at=datetime.now(timezone.utc),
        )
        with self._lock:
            self._jobs[record.job_id] = record
            self._evict_oldest_stopped()
        return record

    def mark_stopped(self, job_id: str, exit_code: t.Optional[int] = None) -> bool:
        """Mark a job as stopped.

        Returns True if the job status was actually transitioned from
        ``"running"`` to ``"stopped"`` by this call.  Returns False if the
        job was already stopped or does not exist.  Callers that need to
        fire completion hooks exactly once should gate on this return value.
        """
        with self._lock:
            if job_id in self._jobs:
                was_running = self._jobs[job_id].status == "running"
                self._jobs[job_id].status = "stopped"
                if exit_code is not None:
                    self._jobs[job_id].exit_code = exit_code
                return was_running
        return False

    def append_resource_sample(self, job_id: str, sample: ResourceStats) -> None:
        """Append one resource-stats sample collected during a running job.

        Silently drops the sample if the job already has 200 entries (~10 min
        at the default 3-second polling interval) to prevent unbounded growth.
        """
        with self._lock:
            if job_id in self._jobs:
                history = self._jobs[job_id].resource_history
                if len(history) < 200:
                    history.append(sample)

    def store_logs(self, job_id: str, logs: str) -> None:
        """Persist captured logs for a completed job (execute_cell path).

        The stored value is guaranteed to be at most LOG_MAX_BYTES when
        re-encoded as UTF-8 (including the truncation marker when cut).
        """
        encoded = logs.encode("utf-8")
        if len(encoded) > self.LOG_MAX_BYTES:
            if self.LOG_MAX_BYTES % 1024 == 0:
                limit_display = f"{self.LOG_MAX_BYTES // 1024} KiB"
            else:
                limit_display = f"{self.LOG_MAX_BYTES} bytes"
            marker = f"\n[... truncated: output exceeded {limit_display} ...]"
            marker_bytes = marker.encode("utf-8")
            if len(marker_bytes) >= self.LOG_MAX_BYTES:
                logs = marker_bytes[:self.LOG_MAX_BYTES].decode("utf-8", errors="ignore")
            else:
                payload_max = self.LOG_MAX_BYTES - len(marker_bytes)
                truncated = encoded[:payload_max].decode("utf-8", errors="ignore")
                logs = truncated + marker
        with self._lock:
            if job_id in self._jobs:
                self._jobs[job_id].stored_logs = logs

    def _evict_oldest_stopped(self) -> None:
        """Remove the oldest stopped jobs when the store exceeds MAX_JOBS.

        Must be called with self._lock held.
        Running jobs are never evicted — only stopped ones are candidates.
        """
        if len(self._jobs) <= self.MAX_JOBS:
            return
        stopped = sorted(
            (r for r in self._jobs.values() if r.status == "stopped"),
            key=lambda r: r.submitted_at,
        )
        to_remove = len(self._jobs) - self.MAX_JOBS
        for record in stopped[:to_remove]:
            del self._jobs[record.job_id]
            logger.debug("Evicted old job record %s", record.job_id)

    # ── read ──────────────────────────────────────────────────────────────────

    def get(self, job_id: str) -> t.Optional[JobRecord]:
        with self._lock:
            record = self._jobs.get(job_id)
            return record.model_copy(deep=True) if record is not None else None

    def list_all(self) -> list[JobRecord]:
        with self._lock:
            return [r.model_copy(deep=True) for r in self._jobs.values()]

    def get_by_state(self, state: str) -> list[JobRecord]:
        """Return jobs matching *state* ('*' for all, 'running', 'stopped')."""
        with self._lock:
            if state == "*":
                return [r.model_copy(deep=True) for r in self._jobs.values()]
            return [r.model_copy(deep=True) for r in self._jobs.values() if r.status == state]

    # ── startup recovery ──────────────────────────────────────────────────────

    def hydrate_from_docker(self, docker_client) -> None:
        """Populate the store from running containers on startup.

        Containers started before the dispatcher process (or after a restart)
        are registered with submitted_at=unknown (epoch) so they appear in
        /v1/jobs immediately rather than being invisible until a new job runs.
        """
        try:
            containers = docker_client.containers.list(
                filters={"label": "caas.managed=true"}
            )
        except Exception as exc:
            logger.warning("Could not list containers on startup: %s", exc)
            return

        epoch = datetime.fromtimestamp(0, tz=timezone.utc)
        with self._lock:
            for c in containers:
                if c.id not in self._jobs:
                    record = JobRecord(
                        job_id=c.id,
                        container_id=c.id,
                        image=c.image.tags[0] if c.image.tags else c.image.short_id,
                        cmd=c.attrs.get("Config", {}).get("Cmd"),
                        submitted_at=epoch,
                    )
                    self._jobs[record.job_id] = record
                    logger.debug("Hydrated job %s from docker ps", record.job_id)

        logger.info("Job store hydrated: %d job(s) recovered", len(self._jobs))
