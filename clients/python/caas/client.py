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


class CaasClient:
    def __init__(
        self,
        host: str,
        api_key: t.Optional[str] = None,
        http_client: t.Optional[httpx.Client] = None,
    ):
        self._base = host.rstrip("/")
        self._api_key = api_key
        self._http = http_client or httpx.Client()
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

    # ── public API ────────────────────────────────────────────────────────────

    def health(self) -> dict:
        """Return the dispatcher health status."""
        resp = self._http.get(f"{self._base}/health", headers=self._headers())
        return self._check(resp).json()

    def execute(
        self,
        image: str,
        cmd: t.Union[str, t.List[str], None] = None,
        env: t.Optional[t.Dict[str, str]] = None,
        volumes: t.Optional[list] = None,
        gpu: t.Optional[dict] = None,
        detach: bool = True,
    ) -> dict:
        """Submit a job. Returns the raw response dict."""
        payload: dict = {"image": image, "detach": detach}
        if cmd is not None:
            payload["cmd"] = cmd
        if env:
            payload["env"] = env
        if volumes:
            payload["volumes"] = volumes
        if gpu is not None:
            payload["gpu"] = gpu
        resp = self._http.post(
            f"{self._base}/v1/execute",
            json=payload,
            headers=self._headers(),
        )
        return self._check(resp).json()

    def execute_cell(
        self,
        code: str,
        image: str,
        env: t.Optional[t.Dict[str, str]] = None,
        volumes: t.Optional[list] = None,
        gpu: t.Optional[dict] = None,
    ) -> str:
        """Send a Python code string to /v1/execute/cell. Returns the logs."""
        payload: dict = {"code": code, "image": image}
        if env:
            payload["env"] = env
        if volumes:
            payload["volumes"] = volumes
        if gpu is not None:
            payload["gpu"] = gpu
        resp = self._http.post(
            f"{self._base}/v1/execute/cell",
            json=payload,
            headers=self._headers(),
        )
        return self._check(resp).json()["logs"]

    def logs(self, container_id: str, follow: bool = False) -> str:
        """Fetch logs for a detached container.

        Pass follow=True to stream until the container exits (blocks until done).
        The dispatcher returns text/plain when streaming, JSON otherwise.
        """
        url = f"{self._base}/v1/logs/{container_id}"
        params = {"follow": "true"} if follow else {}
        if follow:
            with self._http.stream("GET", url, params=params, headers=self._headers()) as resp:
                self._check(resp)
                return "".join(resp.iter_text())
        resp = self._http.get(url, params=params, headers=self._headers())
        return self._check(resp).json()["logs"]
