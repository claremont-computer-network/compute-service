"""
caas.client
───────────
Thin httpx wrapper around the compute-service dispatcher API.
Can be used standalone (no IPython required).
"""
from __future__ import annotations

import typing as t
import httpx


class CaasError(Exception):
    """Raised when the dispatcher returns a non-2xx response."""


class CaasTimeoutError(CaasError):
    """Raised when the dispatcher does not respond within the configured timeout.

    The job may still be running on the remote — use detach=True and poll
    logs() for long-running workloads, or increase the timeout.
    """


# Default read timeout for synchronous (blocking) requests.
# Synchronous jobs block until the container exits, so this needs to be
# longer than the longest expected job.  Override per-client via the
# timeout= constructor argument.
DEFAULT_TIMEOUT = 60.0  # seconds


class CaasClient:
    def __init__(
        self,
        host: str,
        api_key: t.Optional[str] = None,
        http_client: t.Optional[httpx.Client] = None,
        timeout: float = DEFAULT_TIMEOUT,
    ):
        """Create a CaasClient.

        Args:
            host:        Dispatcher base URL, e.g. "http://192.168.1.101:8000".
            api_key:     Value for the X-API-Key header (optional).
            http_client: Inject a custom httpx.Client (e.g. for tests).
                         When provided, the caller is responsible for closing it.
            timeout:     Read timeout in seconds applied to blocking requests.
                         Connect / write / pool timeouts are left at httpx
                         defaults (5 s).  Ignored when http_client is supplied.
                         Default: 60 s.  Increase for long-running jobs.
        """
        self._base = host.rstrip("/")
        self._api_key = api_key
        # Only the read phase needs a long timeout — jobs block until the
        # container exits.  Connect/write/pool keep httpx's 5 s defaults.
        self._http = http_client or httpx.Client(
            timeout=httpx.Timeout(5.0, read=timeout)
        )
        # track whether we own the client so we know whether to close it
        self._owns_http = http_client is None

    def close(self) -> None:
        """Close the underlying HTTP connection pool."""
        if self._owns_http:
            self._http.close()

    def __enter__(self) -> "CaasClient":
        return self

    def __exit__(self, *_) -> None:
        self.close()

    # ── internal ─────────────────────────────────────────────────────────────

    def _headers(self) -> dict:
        if self._api_key:
            return {"X-API-Key": self._api_key}
        return {}

    @staticmethod
    def _compact(**kwargs) -> dict:
        """Build a request payload, dropping keys whose value is None.

        Adding a new optional field to execute() or execute_cell() only
        requires adding it to the method signature and to the _compact() call
        here — no extra ``if x is not None`` block needed.
        """
        return {k: v for k, v in kwargs.items() if v is not None}

    def _check(self, resp: httpx.Response) -> httpx.Response:
        if resp.is_error:
            detail = resp.text
            if resp.content:
                try:
                    payload = resp.json()
                except ValueError:
                    pass
                else:
                    if isinstance(payload, dict):
                        detail = payload.get("detail", resp.text)
            raise CaasError(detail)
        return resp

    def _call(self, method: str, url: str, **kwargs) -> httpx.Response:
        """Make an HTTP request, converting ReadTimeout into CaasTimeoutError."""
        try:
            resp = self._http.request(method, url, **kwargs)
        except httpx.ReadTimeout as exc:
            timeout_val = (
                self._http.timeout.read
                if hasattr(self._http, "timeout")
                else "unknown"
            )
            raise CaasTimeoutError(
                f"The dispatcher did not respond within {timeout_val}s.\n"
                f"The job may still be running on the remote.\n"
                f"• For long-running workloads, use detach=True and poll logs()\n"
                f"• Or increase the timeout: CaasClient(host=..., timeout=300)"
            ) from exc
        return resp

    # ── public API ────────────────────────────────────────────────────────────

    def health(self) -> dict:
        """Return the dispatcher health status."""
        resp = self._call("GET", f"{self._base}/health", headers=self._headers())
        return self._check(resp).json()

    def execute(
        self,
        image: str,
        cmd: t.Union[str, t.List[str], None] = None,
        env: t.Optional[t.Dict[str, str]] = None,
        volumes: t.Optional[list] = None,
        gpu: t.Optional[dict] = None,
        detach: bool = True,
        shm_size: t.Optional[str] = None,
        ipc_mode: t.Optional[str] = None,
    ) -> dict:
        """Submit a job. Returns the raw response dict."""
        payload = self._compact(
            image=image, cmd=cmd, env=env or None, volumes=volumes or None,
            gpu=gpu, detach=detach, shm_size=shm_size, ipc_mode=ipc_mode,
        )
        resp = self._call("POST", f"{self._base}/v1/execute",
                          json=payload, headers=self._headers())
        return self._check(resp).json()

    def execute_cell(
        self,
        code: str,
        image: str,
        env: t.Optional[t.Dict[str, str]] = None,
        volumes: t.Optional[list] = None,
        gpu: t.Optional[dict] = None,
        shm_size: t.Optional[str] = None,
        ipc_mode: t.Optional[str] = None,
        verbose: bool = False,
        suppress_entrypoint: t.Optional[bool] = None,
    ) -> str:
        """Send a Python code string to /v1/execute/cell. Returns the output.

        By default only stdout is returned so container-entrypoint banner noise
        (NVIDIA copyright, pip deprecation warnings, etc.) is suppressed — those
        all go to stderr and are not part of the user's code output.

        Pass verbose=True to get stdout + stderr merged, which is useful when
        debugging or when the job exits non-zero.

        suppress_entrypoint controls whether the container's ENTRYPOINT script
        is bypassed entirely (entrypoint="").  If left as None (the default),
        it is auto-enabled for nvcr.io/* images, which print a large banner to
        stdout before exec-ing the user command.  Pass False explicitly to
        disable the auto-detection (e.g. if you rely on the NGC entrypoint for
        env-var setup).
        """
        if suppress_entrypoint is None:
            suppress_entrypoint = image.startswith("nvcr.io/")
        payload = self._compact(
            code=code, image=image, env=env or None, volumes=volumes or None,
            gpu=gpu, shm_size=shm_size, ipc_mode=ipc_mode,
            suppress_entrypoint=suppress_entrypoint or None,
        )
        resp = self._call("POST", f"{self._base}/v1/execute/cell",
                          json=payload, headers=self._headers())
        body = self._check(resp).json()
        if verbose or body.get("exit_code", 0) != 0:
            # On failure always include stderr so tracebacks are visible.
            return body.get("logs", "")
        return body.get("stdout", body.get("logs", ""))

    def logs(self, container_id: str, follow: bool = False) -> str:
        """Fetch logs for a detached container.

        Pass follow=True to stream until the container exits (blocks until done).
        The dispatcher returns text/plain when streaming, JSON otherwise.
        """
        url = f"{self._base}/v1/logs/{container_id}"
        params = {"follow": "true"} if follow else {}
        if follow:
            try:
                with self._http.stream("GET", url, params=params, headers=self._headers()) as resp:
                    self._check(resp)
                    return "".join(resp.iter_text())
            except httpx.ReadTimeout as exc:
                raise CaasTimeoutError(
                    "Timed out while streaming logs from the dispatcher.\n"
                    "• Increase the timeout: CaasClient(host=..., timeout=300)\n"
                    "• Or retry the request if appropriate for the operation"
                ) from exc
        resp = self._call("GET", url, params=params, headers=self._headers())
        return self._check(resp).json()["logs"]

    # ── job registry ──────────────────────────────────────────────────────────

    def jobs(self, state: t.Optional[str] = None) -> list:
        """Return all known jobs (with live resource stats for running ones).

        Args:
            state: Optional filter — "running", "stopped", or "*" for all.
                   When omitted, falls back to the legacy /v1/jobs endpoint
                   (no filtering).  Pass a value to use the extension API.
        """
        if state is not None:
            params = {"state": state}
            resp = self._call("GET", f"{self._base}/api/jobs", params=params, headers=self._headers())
        else:
            resp = self._call("GET", f"{self._base}/v1/jobs", headers=self._headers())
        return self._check(resp).json()

    def job(self, job_id: str) -> dict:
        """Return a single job record by job_id (the full container ID)."""
        resp = self._call("GET", f"{self._base}/v1/jobs/{job_id}", headers=self._headers())
        return self._check(resp).json()

    def stop(self, job_id: str) -> dict:
        """Stop and remove a running job. Returns {"job_id": ..., "status": "stopped"}."""
        resp = self._call("DELETE", f"{self._base}/v1/jobs/{job_id}", headers=self._headers())
        return self._check(resp).json()

    def deployment_status(self, job_id: str) -> dict:
        """Check the outcome of a deployment (job).

        Returns the job status, exit code, and a human-readable success/failure
        label.  Useful for polling CI systems or the UI.
        """
        resp = self._call("GET", f"{self._base}/api/deployments/{job_id}/status", headers=self._headers())
        return self._check(resp).json()

    def gpu_info(self) -> list:
        """Return GPU hardware info (index, memory, temperature, utilization) from nvidia-smi."""
        resp = self._call("GET", f"{self._base}/api/gpu", headers=self._headers())
        return self._check(resp).json()

    # ── templates ───────────────────────────────────────────────────────────────

    def templates_list(self) -> list:
        """List all job templates."""
        resp = self._call("GET", f"{self._base}/api/templates", headers=self._headers())
        return self._check(resp).json()

    def templates_upsert(self, name: t.Optional[str] = None, image: t.Optional[str] = None,
                         cmd: t.Optional[t.Union[str, t.List[str]]] = None,
                         env: t.Optional[t.Dict[str, str]] = None,
                         volumes: t.Optional[list] = None,
                         gpu: t.Optional[dict] = None,
                         id: t.Optional[str] = None) -> dict:
        """Create or update a job template.

        If *id* is provided and matches an existing template, it is updated
        (only the provided fields are sent to the server).
        Otherwise a new template is created.

        Returns:
            The template dict with ``id``, ``created_at``, and ``modified_at``.
        """
        payload = self._compact(
            id=id, name=name, image=image, cmd=cmd, env=env,
            volumes=volumes, gpu=gpu,
        )
        resp = self._call("POST", f"{self._base}/api/templates",
                          json=payload, headers=self._headers())
        return self._check(resp).json()

    def templates_delete(self, template_id: str) -> dict:
        """Delete a template by ID."""
        resp = self._call("DELETE", f"{self._base}/api/templates/{template_id}", headers=self._headers())
        return self._check(resp).json()

    # ── files ───────────────────────────────────────────────────────────────────

    def files_list(self, path: str = "/") -> dict:
        """List files in a mounted directory.

        Returns ``{"path": "/real/path", "entries": [...]}`` where each entry
        is ``{"name", "permissions", "size", "modified", "is_dir"}``.
        """
        params = {"path": path}
        resp = self._call("GET", f"{self._base}/api/files", params=params, headers=self._headers())
        return self._check(resp).json()

    # ── schedules ───────────────────────────────────────────────────────────────

    def schedules_list(self) -> list:
        """List all schedules."""
        resp = self._call("GET", f"{self._base}/api/schedule", headers=self._headers())
        return self._check(resp).json()

    def schedules_upsert(self, template_id: t.Optional[str] = None,
                         delay_seconds: int = 60,
                         image: t.Optional[str] = None,
                         cmd: t.Optional[t.Union[str, t.List[str]]] = None,
                         env: t.Optional[t.Dict[str, str]] = None,
                         volumes: t.Optional[list] = None,
                         gpu: t.Optional[dict] = None) -> dict:
        """Create a schedule to trigger a job (optionally after a delay).

        If ``delay_seconds == 0`` the job executes immediately.

        Provide either ``template_id`` (references a stored template) or
        inline fields (image, cmd, env, volumes, gpu).
        """
        payload = self._compact(
            template_id=template_id, delay_seconds=delay_seconds,
            image=image, cmd=cmd, env=env, volumes=volumes, gpu=gpu,
        )
        resp = self._call("POST", f"{self._base}/api/schedule",
                          json=payload, headers=self._headers())
        return self._check(resp).json()

    def schedule_cancel(self, schedule_id: str) -> dict:
        """Cancel a pending schedule."""
        resp = self._call("DELETE", f"{self._base}/api/schedule/{schedule_id}", headers=self._headers())
        return self._check(resp).json()

    # ── staging ─────────────────────────────────────────────────────────────────

    def staging_list(self) -> list:
        """List all staging areas."""
        resp = self._call("GET", f"{self._base}/api/staging", headers=self._headers())
        return self._check(resp).json()

    def staging_create(self, name: str, host_path: str,
                       dest_path: t.Optional[str] = None) -> dict:
        """Create a staging area — a named reference to a host path mount."""
        payload = self._compact(name=name, host_path=host_path, dest_path=dest_path)
        resp = self._call("POST", f"{self._base}/api/staging",
                          json=payload, headers=self._headers())
        return self._check(resp).json()

    def staging_delete(self, staging_id: str) -> dict:
        """Remove a staging area."""
        resp = self._call("DELETE", f"{self._base}/api/staging/{staging_id}", headers=self._headers())
        return self._check(resp).json()

    # ── images ──────────────────────────────────────────────────────────────────

    def images_list(self) -> list:
        """Return all Docker images cached on the dispatcher node."""
        resp = self._call("GET", f"{self._base}/api/images", headers=self._headers())
        return self._check(resp).json()

    def images_check(self, image: str) -> dict:
        """Check if a specific image is available on the dispatcher.

        Returns ``{"found": true, "image": {...}}`` or ``{"found": false}``.
        """
        resp = self._call("POST", f"{self._base}/api/images/check",
                          json={"image": image}, headers=self._headers())
        return self._check(resp).json()

    # ── sandbox ─────────────────────────────────────────────────────────────────

    def sandbox_create(
        self,
        image: str,
        env: t.Optional[t.Dict[str, str]] = None,
        volumes: t.Optional[t.List[t.Dict[str, str]]] = None,
        gpu: t.Optional[t.Dict[str, t.Any]] = None,
        shm_size: t.Optional[str] = None,
    ) -> dict:
        """Create a persistent sandbox container for interactive execution."""
        # Omit empty dict/list values — _compact() only removes None, not empty containers
        payload: t.Dict[str, t.Any] = {"image": image}
        if env:
            payload["env"] = env
        if volumes:
            payload["volumes"] = volumes
        if gpu is not None:
            payload["gpu"] = gpu
        if shm_size is not None:
            payload["shm_size"] = shm_size

        resp = self._call("POST", f"{self._base}/v1/sandbox",
                          json=payload, headers=self._headers())
        return self._check(resp).json()

    def sandbox_exec(
        self, sandbox_id: str, cmd: str
    ) -> dict:
        """Execute a command interactively inside a running sandbox."""
        payload = self._compact(cmd=cmd)
        resp = self._call("POST",
                          f"{self._base}/v1/jobs/{sandbox_id}/exec",
                          json=payload, headers=self._headers())
        return self._check(resp).json()
