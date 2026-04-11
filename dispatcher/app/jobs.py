"""
jobs.py
───────
In-memory job registry for the compute-service dispatcher.

JobRecord is a plain Pydantic model so it serialises to JSON for free
via FastAPI's JSONResponse — no manual dict-building needed.

JobStore is an in-memory dict keyed by job_id (= container short ID).
State is intentionally ephemeral: the store is rebuilt from docker ps
on each startup via hydrate_from_docker(), so a service restart does
not lose visibility of already-running containers.
"""
from __future__ import annotations

import logging
import typing as t
from datetime import datetime, timezone

from pydantic import BaseModel

logger = logging.getLogger("caas.jobs")


class ResourceStats(BaseModel):
    """Live resource usage snapshot fetched from the Docker stats API."""
    cpu_percent: float
    mem_usage_mb: float
    mem_limit_mb: float
    mem_percent: float


class JobRecord(BaseModel):
    """Immutable identity fields plus mutable status for one dispatched job."""
    job_id: str                                    # = container short ID
    container_id: str                              # full 64-char ID
    image: str
    cmd: t.Union[str, t.List[str], None] = None
    submitted_at: datetime
    status: str = "running"                        # running | exited | stopped
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
    mem_usage_mb = usage / (1024 ** 2)
    mem_limit_mb = limit / (1024 ** 2)
    mem_percent = (usage / limit * 100.0) if limit > 0 else 0.0

    return ResourceStats(
        cpu_percent=round(cpu_percent, 2),
        mem_usage_mb=round(mem_usage_mb, 2),
        mem_limit_mb=round(mem_limit_mb, 2),
        mem_percent=round(mem_percent, 2),
    )


class JobStore:
    """Thread-unsafe in-memory store — acceptable for a single-process dispatcher."""

    def __init__(self) -> None:
        self._jobs: dict[str, JobRecord] = {}

    # ── write ─────────────────────────────────────────────────────────────────

    def register(self, container, image: str,
                 cmd: t.Union[str, t.List[str], None] = None) -> JobRecord:
        """Record a newly started detached container."""
        record = JobRecord(
            job_id=container.short_id,
            container_id=container.id,
            image=image,
            cmd=cmd,
            submitted_at=datetime.now(timezone.utc),
        )
        self._jobs[record.job_id] = record
        return record

    def mark_stopped(self, job_id: str, exit_code: int = 0) -> None:
        if job_id in self._jobs:
            self._jobs[job_id].status = "stopped"
            self._jobs[job_id].exit_code = exit_code

    # ── read ──────────────────────────────────────────────────────────────────

    def get(self, job_id: str) -> t.Optional[JobRecord]:
        return self._jobs.get(job_id)

    def list_all(self) -> list[JobRecord]:
        return list(self._jobs.values())

    # ── startup recovery ──────────────────────────────────────────────────────

    def hydrate_from_docker(self, docker_client) -> None:
        """Populate the store from running containers on startup.

        Containers started before the dispatcher process (or after a restart)
        are registered with submitted_at=unknown (epoch) so they appear in
        /v1/jobs immediately rather than being invisible until a new job runs.
        """
        try:
            containers = docker_client.containers.list()
        except Exception as exc:
            logger.warning("Could not list containers on startup: %s", exc)
            return

        epoch = datetime.fromtimestamp(0, tz=timezone.utc)
        for c in containers:
            if c.short_id not in self._jobs:
                record = JobRecord(
                    job_id=c.short_id,
                    container_id=c.id,
                    image=c.image.tags[0] if c.image.tags else c.image.short_id,
                    cmd=c.attrs.get("Config", {}).get("Cmd"),
                    submitted_at=epoch,
                )
                self._jobs[record.job_id] = record
                logger.debug("Hydrated job %s from docker ps", record.job_id)

        logger.info("Job store hydrated: %d job(s) recovered", len(self._jobs))
