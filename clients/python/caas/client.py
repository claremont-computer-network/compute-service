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
    ) -> str:
        """Send a Python code string to /v1/execute/cell. Returns the logs."""
        payload = self._compact(
            code=code, image=image, env=env or None, volumes=volumes or None,
            gpu=gpu, shm_size=shm_size, ipc_mode=ipc_mode,
        )
        resp = self._call("POST", f"{self._base}/v1/execute/cell",
                          json=payload, headers=self._headers())
        return self._check(resp).json()["logs"]

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
