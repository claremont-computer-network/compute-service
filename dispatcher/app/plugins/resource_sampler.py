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

Community authors
-----------------
This is a worked example of a *background-thread* ``on_register`` hook
paired with a cleanup ``post_run`` hook.  Fork it to:

- Emit metrics to Prometheus / StatsD in real time.
- Cap history at a different interval (change the ``3.0`` s sleep).
- Add GPU utilisation via ``nvidia-ml-py`` for richer profiling.
"""
from __future__ import annotations

import threading
import typing as t

from app.core.plugin import CaasPlugin

if t.TYPE_CHECKING:
    from app.jobs import JobRecord


class ResourceSamplerPlugin(CaasPlugin):
    """Sample container CPU/memory stats in a background thread.

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

    def on_register(self, record: "JobRecord") -> None:
        """Start a background sampling thread for *record*."""
        from app.jobs import _fetch_resources  # pylint: disable=import-outside-toplevel
        import app.main as _main              # pylint: disable=import-outside-toplevel

        stop_event = threading.Event()
        with self._lock:
            self._stop_events[record.job_id] = stop_event

        job_id = record.job_id

        def _sample() -> None:
            consecutive_misses = 0
            while not stop_event.wait(timeout=3.0):
                try:
                    container = _main.client.containers.get(job_id)
                    sample = _fetch_resources(container)
                except Exception:  # noqa: BLE001 — container may be gone
                    break
                if sample:
                    _main.job_store.append_resource_sample(job_id, sample)
                    consecutive_misses = 0
                else:
                    # _fetch_resources returns None when the container is no
                    # longer running or stats are unavailable.  Exit after two
                    # consecutive misses to avoid spinning forever on a stopped
                    # detached job where post_run is never called.
                    consecutive_misses += 1
                    if consecutive_misses >= 2:
                        break
            with self._lock:
                self._stop_events.pop(job_id, None)

        thread = threading.Thread(target=_sample, daemon=True)
        thread.start()

    def post_run(self, record: "JobRecord", result: dict) -> None:
        """Signal the sampler thread for *record* to stop."""
        with self._lock:
            stop_event = self._stop_events.pop(record.job_id, None)
        if stop_event is not None:
            stop_event.set()
