"""
dispatcher/app/plugins/log_retention.py
─────────────────────────────────────────
LogRetentionPlugin — persist cell-job logs after the container is removed.

Background
----------
``execute_cell`` removes the container immediately after it exits so it
does not clutter ``docker ps``.  Without storing the logs first, any
subsequent call to ``GET /v1/logs/{container_id}`` would return 404
because the container is gone.

This plugin calls :meth:`~app.jobs.JobStore.store_logs` from ``post_run``
so the logs are safely written to the job record before the endpoint
handler returns.  The ``GET /v1/logs`` handler already knows to serve
from ``record.stored_logs`` when the container has been removed.

Community authors
-----------------
This is a worked example of a ``post_run`` hook that performs a
side-effect using the finished job record.  Fork it to:

- Ship logs to an external store (S3, Elasticsearch, Loki).
- Redact sensitive values (API keys, passwords) before storage.
- Add a TTL field so a sweep job can delete logs after N days.
"""
from __future__ import annotations

import typing as t

from app.core.plugin import CaasPlugin

if t.TYPE_CHECKING:
    from app.jobs import JobRecord


class LogRetentionPlugin(CaasPlugin):
    """Persist container logs into the job record after the container exits.

    Priority: 60
    """

    name = "log-retention"
    priority = 60

    def post_run(self, record: "JobRecord", result: dict) -> None:
        """Store ``result["logs"]`` in the job record.

        If *result* does not contain a ``"logs"`` key (e.g. the container
        exited via an error path that set no logs) this is a no-op.
        """
        logs = result.get("logs")
        if logs is not None:
            if self.services is not None:
                self.services.job_store.store_logs(record.job_id, logs)
            else:
                import app.main as _main  # pylint: disable=import-outside-toplevel
                _main.job_store.store_logs(record.job_id, logs)
