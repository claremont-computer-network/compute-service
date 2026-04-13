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

from pydantic import BaseModel

logger = logging.getLogger("caas.jobs")


class ResourceStats(BaseModel):
    """Live resource usage snapshot fetched from the Docker stats API."""
    cpu_percent: float
    mem_usage_mib: float   # mebibytes (÷ 1024²) — matches Docker's own display
    mem_limit_mib: float
    mem_percent: float


class JobRecord(BaseModel):
    """Immutable identity fields plus mutable status for one dispatched job."""
    job_id: str                                    # = full container ID
    container_id: str                              # full 64-char ID (same value, kept for API compat)
    image: str
    cmd: t.Union[str, t.List[str], None] = None
    submitted_at: datetime
    status: str = "running"                        # running | stopped
    exit_code: t.Optional[int] = None
    resources: t.Optional[ResourceStats] = None   # populated on GET, not at submit


def _fetch_resources(container) -> t.Optional[ResourceStats]:
    """Call the Docker stats API (single-shot) and compute derived metrics.

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

        return ResourceStats(
            cpu_percent=round(cpu_percent, 2),
            mem_usage_mib=round(mem_usage_mib, 2),
            mem_limit_mib=round(mem_limit_mib, 2),
            mem_percent=round(mem_percent, 2),
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
    """

    def __init__(self) -> None:
        self._jobs: dict[str, JobRecord] = {}
        self._lock = threading.Lock()

    # ── write ─────────────────────────────────────────────────────────────────

    def register(self, container, image: str,
                 cmd: t.Union[str, t.List[str], None] = None) -> JobRecord:
        """Record a newly started detached container."""
        record = JobRecord(
            job_id=container.id,
            container_id=container.id,
            image=image,
            cmd=cmd,
            submitted_at=datetime.now(timezone.utc),
        )
        with self._lock:
            self._jobs[record.job_id] = record
        return record

    def register_sync(self, job_id: str, image: str,
                      cmd: t.Union[str, t.List[str], None] = None) -> JobRecord:
        """Record a synchronous (non-detached) job by ID without a container object.

        Used for execute_cell jobs where the container is gone by the time we
        want to register it, so we track them for history in the UI.
        """
        record = JobRecord(
            job_id=job_id,
            container_id=job_id,
            image=image,
            cmd=cmd,
            submitted_at=datetime.now(timezone.utc),
        )
        with self._lock:
            self._jobs[record.job_id] = record
        return record

    def mark_stopped(self, job_id: str, exit_code: t.Optional[int] = None) -> None:
        with self._lock:
            if job_id in self._jobs:
                self._jobs[job_id].status = "stopped"
                if exit_code is not None:
                    self._jobs[job_id].exit_code = exit_code

    # ── read ──────────────────────────────────────────────────────────────────

    def get(self, job_id: str) -> t.Optional[JobRecord]:
        with self._lock:
            record = self._jobs.get(job_id)
            return record.model_copy(deep=True) if record is not None else None

    def list_all(self) -> list[JobRecord]:
        with self._lock:
            return [r.model_copy(deep=True) for r in self._jobs.values()]

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
