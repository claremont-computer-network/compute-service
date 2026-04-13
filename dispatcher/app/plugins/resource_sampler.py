"""
dispatcher/app/plugins/resource_sampler.py
────────────────────────────────────────────
ResourceSamplerPlugin — background CPU/memory sampling for cell jobs.

Background
----------
Cell execution is synchronous: the response is only returned after the
container exits.  This means the UI cannot poll ``GET /v1/jobs/{id}``
for live stats while the cell is running — by the time the client asks,
the container is already gone.

This plugin compensates by starting a background thread the moment a
cell job is registered (``on_register``).  The thread samples the
Docker stats API every three seconds and appends each
:class:`~app.jobs.ResourceStats` snapshot to
:attr:`~app.jobs.JobRecord.resource_history`.  When the container exits
(``post_run``) the thread is signalled to stop and the history stays
attached to the job record so the UI can display a post-hoc graph.

Scope
-----
Sampling is intentionally limited to **cell jobs** — i.e. jobs whose
record carries ``cmd[0] == "python"`` and that were created by
``containers.create()`` rather than ``containers.run(detach=True)``.
Detached ``/v1/execute`` jobs can be long-running and the caller can
poll ``GET /v1/jobs/{id}`` for live stats directly, so the background
overhead is unnecessary.

Community authors
-----------------
This is a worked example of a *background-thread* ``on_register`` hook
paired with a cleanup ``post_run`` hook.  Fork it to:

- Emit metrics to Prometheus / StatsD in real time.
- Cap history at a different interval (change the ``3.0`` s sleep).
- Add GPU utilisation via ``nvidia-ml-py`` for richer profiling.
"""
from __future__ import annotations

import logging
import threading
import typing as t

import docker.errors

from app.core.plugin import CaasPlugin

if t.TYPE_CHECKING:
    from app.jobs import JobRecord

logger = logging.getLogger("caas.dispatcher")

# Maximum consecutive transient Docker errors before the sampler gives up.
_MAX_TRANSIENT_ERRORS = 3


class ResourceSamplerPlugin(CaasPlugin):
    """Sample container CPU/memory stats in a background thread.

    Sampling is only started for **cell jobs** (where ``record.cmd`` starts
    with ``["python", "-c", ...]``).  Detached jobs are excluded — the caller
    can poll ``GET /v1/jobs/{id}`` for live stats instead.

    One :class:`threading.Event` is kept per ``job_id``; ``post_run``
    sets it to stop the thread after the container exits.

    Priority: 50
    """

    name = "resource-sampler"
    priority = 50

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # job_id → stop Event for running sampler threads
        self._stop_events: dict[str, threading.Event] = {}

    @staticmethod
    def _is_cell_job(record: "JobRecord") -> bool:
        """Return True when *record* represents a cell execution job."""
        cmd = record.cmd
        if isinstance(cmd, list) and len(cmd) >= 2:
            return cmd[0] == "python" and cmd[1] == "-c"
        return False

    def on_register(self, record: "JobRecord") -> None:
        """Start a background sampling thread for cell jobs only."""
        if not self._is_cell_job(record):
            return

        from app.jobs import _fetch_resources  # pylint: disable=import-outside-toplevel
        import app.main as _main              # pylint: disable=import-outside-toplevel

        stop_event = threading.Event()
        with self._lock:
            self._stop_events[record.job_id] = stop_event

        job_id = record.job_id

        def _sample() -> None:
            consecutive_misses = 0
            transient_errors = 0
            while not stop_event.wait(timeout=3.0):
                try:
                    container = _main.client.containers.get(job_id)
                    sample = _fetch_resources(container)
                    transient_errors = 0  # successful Docker call — reset error count
                except docker.errors.NotFound:
                    # Container is definitively gone — stop sampling.
                    break
                except docker.errors.DockerException as exc:
                    # Transient error (socket hiccup, etc.) — retry up to the limit.
                    transient_errors += 1
                    if transient_errors >= _MAX_TRANSIENT_ERRORS:
                        logger.warning(
                            "resource-sampler: stopping for job %s after %d consecutive "
                            "Docker errors: %s",
                            job_id, transient_errors, exc,
                        )
                        break
                    continue
                if sample:
                    _main.job_store.append_resource_sample(job_id, sample)
                    consecutive_misses = 0
                else:
                    # _fetch_resources returns None when the container is no
                    # longer running or stats are unavailable.  Exit after two
                    # consecutive misses so we don't spin on a stopped container.
                    consecutive_misses += 1
                    if consecutive_misses >= 2:
                        break
            with self._lock:
                self._stop_events.pop(job_id, None)

        thread = threading.Thread(target=_sample, daemon=True, name=f"sampler-{job_id[:12]}")
        thread.start()

    def post_run(self, record: "JobRecord", result: dict) -> None:
        """Signal the sampler thread for *record* to stop."""
        with self._lock:
            stop_event = self._stop_events.pop(record.job_id, None)
        if stop_event is not None:
            stop_event.set()
