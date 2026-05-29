"""caas_mcp.server — MCP server that wraps CaasClient and exposes stdio transport.

Configuration
─────────────
This server reads its target dispatcher URL, optional API key, and optional
remote-workspace path from environment variables at startup.

    CAAS_DISPATCHER_URL   (required)  Base URL, e.g. http://192.168.1.50:8000
    CAAS_API_KEY          (optional)  X-API-Key header forwarded to the dispatcher
    CAAS_REMOTE_WORKSPACE (optional)  Path on the **remote** dispatcher filesystem
                                      used for implicit /workspace volume mounts (PR 2)

Running independently (stdio)
─────────────────────────────
    CAAS_DISPATCHER_URL=http://10.0.0.1:8000 \\
    CAAS_API_KEY=secret \\
    CAAS_REMOTE_WORKSPACE=/mnt/staging \\
    python -m caas_mcp.server

Or via ``uv run`` / ``pip run``:
    CAAS_DISPATCHER_URL=... CAAS_API_KEY=... uv run -e . -m caas_mcp.server
"""
from __future__ import annotations

import logging
import sys
import typing as t

from caas.client import CaasClient, CaasError, CaasTimeoutError

from caas_mcp.config import Config

# FastMCP's @tool decorator inspects annotations at runtime via
# inspect.signature(..., eval_str=True), so Context must be available
# in the module namespace — not just under TYPE_CHECKING.
from mcp.server.fastmcp import Context, FastMCP  # noqa: E402

logger = logging.getLogger(__name__)


# ── lifecycle helpers ──────────────────────────────────────────────────────


def _build_config() -> Config:
    """Read configuration from environment (or raise for missing values)."""
    return Config()


def _build_client(cfg: Config) -> CaasClient:
    """Create a CaasClient configured from *cfg*."""
    kwargs: t.Dict = {"host": cfg.dispatcher_url, "api_key": cfg.api_key}
    if hasattr(cfg, "_mock_http"):
        kwargs["http_client"] = cfg._mock_http  # type: ignore[union-attr]
    return CaasClient(**kwargs)


# ── MCP server factory ────────────────────────────────────────────────────


def make_server(cfg: Config | None = None) -> FastMCP:
    """Construct and return the FastMCP server with resources and tools wired up."""
    if cfg is None:
        cfg = _build_config()

    logger.info("Creating MCP server targeting %s", cfg.dispatcher_url)

    server = FastMCP(
        name="caas-mcp",
        instructions=(
            "Compute-as-a-service MCP server.  Use tools to dispatch and manage "
            "containerised workloads on a remote node.  Resources expose status "
            "information; tools trigger side-effecting operations."
        ),
    )

    server._cfg = cfg  # type: ignore[attr-defined]

    # ── Resources ───────────────────────────────────────────────────────

    @server.resource("system://health")
    def health() -> str:
        """Return dispatcher health status as a JSON string."""
        client = _build_client(cfg)
        try:
            data = client.health()
        except CaasError as exc:
            return _to_json({"status": "error", "error": str(exc)})
        finally:
            client.close()
        return _to_json(data)

    # ── Tools (PR 1 — skeleton) ─────────────────────────────────────────

    @server.tool()
    def list_jobs(state: str | None = None) -> str:
        """List jobs dispatched through the compute-service dispatcher.

        Args:
            state:  Optional filter — ``"running"``, ``"stopped"``, or ``"*"`` for all.
                    Omit to use the legacy (unfiltered) listing.
        """
        client = _build_client(cfg)
        try:
            jobs = client.jobs(state=state)
        except CaasError as exc:
            return _to_json({"error": str(exc)})
        finally:
            client.close()
        return _to_json(jobs)

    # ── Tools (PR 2 — execute + workspace + timeout) ────────────────────

    @server.tool()
    async def execute_cell(
        code: str,
        image: str,
        env: str | None = None,
        ctx: Context | None = None,
    ) -> str:
        """Execute a Python code string in a container and return the output.

        The code runs synchronously — the tool waits for the container to finish.
        If you need to run long jobs, use ``execute`` with ``detach=True`` instead.

        Args:
            code:     Python code to execute.
            image:    Docker image to use (required — e.g. ``"python:3.11-slim"``).
            env:      Comma-separated ``KEY=VALUE`` pairs, e.g.  ``"FOO=1,BAR=baz"``.
        """
        parsed_env = _parse_env(env)
        workspace = server._cfg.remote_workspace  # type: ignore[attr-defined]

        volumes: list[dict] | None = None
        if workspace:
            volumes = [
                {"host_path": workspace, "container_path": "/workspace", "mode": "rw"}
            ]

        client = _build_client(cfg)
        try:
            try:
                output = client.execute_cell(
                    code=code,
                    image=image,
                    env=parsed_env or None,
                    volumes=volumes,
                )
            except CaasTimeoutError:
                return _to_json({
                    "status": "timeout",
                    "message": (
                        "Execution exceeded synchronous wait time (60s timeout). "
                        "The job is still running on the remote host."
                    ),
                    "guidance": (
                        "To monitor it:\n"
                        "  1. Use list_jobs to find the job_id\n"
                        "  2. Use get_logs with container_id to stream output\n"
                        "\n"
                        "To prevent this in the future, use execute(detach=True) and "
                        "monitor via list_jobs, or increase the timeout in CaasClient."
                    ),
                })
            except CaasError as exc:
                return _to_json({"error": str(exc)})
            return output
        finally:
            client.close()

    @server.tool()
    async def execute(
        image: str,
        cmd: str | None = None,
        env: str | None = None,
        gpu: str | None = None,
        detach: bool = True,
        ctx: Context | None = None,
    ) -> str:
        """Launch a job — detached (default) or synchronous.

        Args:
            image:  Docker image name (e.g. ``"nvcr.io/nvidia/pytorch:24.01-py3"``).
            cmd:    Command string to run in the container, e.g.
                    ``"python main.py --epochs 10"``.
            env:    Comma-separated ``KEY=VALUE`` pairs.
            gpu:    GPU request, e.g. ``"0,1"`` for GPU devices 0 and 1, or ``"all"``
                    for all available GPUs.  Omit entirely to run without GPU.
            detach: When *True* (default) returns immediately with job metadata.
                    When *False* blocks until the container exits.
        """
        parsed_env = _parse_env(env)
        workspace = server._cfg.remote_workspace  # type: ignore[attr-defined]

        volumes: list[dict] | None = None
        if workspace:
            volumes = [
                {"host_path": workspace, "container_path": "/workspace", "mode": "rw"}
            ]

        parsed_gpu = _parse_gpu(gpu) if gpu else None

        client = _build_client(cfg)
        try:
            try:
                result = client.execute(
                    image=image,
                    cmd=cmd,
                    env=parsed_env or None,
                    volumes=volumes or None,
                    gpu=parsed_gpu,
                    detach=detach,
                )
                result.pop("logs", None)
            except CaasTimeoutError as exc:
                return _to_json({
                    "status": "timeout",
                    "message": str(exc),
                    "guidance": (
                        "The dispatcher timed out waiting for a response. "
                        "The job may still be running or may have failed.  Use "
                        "list_jobs to check statuses."
                    ),
                })
            except CaasError as exc:
                return _to_json({"error": str(exc)})
            return _to_json(result)
        finally:
            client.close()

    # ── Tools (PR 3 — job management) ───────────────────────────────────

    @server.tool()
    async def stop_job(
        job_id: str,
        ctx: Context | None = None,
    ) -> str:
        """Stop and remove a running job.

        Args:
            job_id:  The full container ID (job ID) reported by ``list_jobs`` or ``execute``.
        """
        client = _build_client(cfg)
        try:
            try:
                result = client.stop(job_id)
            except CaasError as exc:
                return _to_json({"error": str(exc)})
            return _to_json(result)
        finally:
            client.close()

    @server.tool()
    async def get_logs(
        container_id: str,
        follow: bool = False,
        ctx: Context | None = None,
    ) -> str:
        """Retrieve logs for a container.

        Args:
            container_id:  The container ID from the job record.
            follow:        When *True*, blocks until the container exits, streaming
                           output.  When *False* (default) returns a snapshot.
        """
        client = _build_client(cfg)
        try:
            try:
                logs = client.logs(container_id, follow=follow)
            except CaasError as exc:
                return _to_json({"error": str(exc)})
            return logs
        finally:
            client.close()

    return server


# ── entry-point ────────────────────────────────────────────────────────────


def main() -> None:
    """Run the MCP server over stdio (the default transport)."""
    logging.basicConfig(
        level=logging.WARNING, stream=sys.stderr, format="%(name)s: %(message)s"
    )
    config = _build_config()
    _server = make_server(config)
    _server.run()


# ── small helpers ──────────────────────────────────────────────────────────


def _to_json(obj: t.Any) -> str:
    """Serialize *obj* to JSON (uses the stdlib json module)."""
    import json

    return json.dumps(obj, default=str, ensure_ascii=False)


def _parse_env(raw: str | None) -> dict[str, str] | None:
    """Parse ``KEY=VAL,KEY2=VAL2`` → ``{"KEY": "VAL", "KEY2": "VAL2"}``."""
    if not raw:
        return None
    result: dict[str, str] = {}
    for pair in raw.split(","):
        pair = pair.strip()
        if not pair or "=" not in pair:
            continue
        key, _, value = pair.partition("=")
        result[key.strip()] = value.strip()
    return result or None


def _parse_gpu(raw: str | None) -> dict | None:
    """Parse a GPU request string into Dispatcher GpuRequest shape.

    Produces ``{"device_ids": [...], "capabilities": ["gpu"]}`` to match
    the dispatcher's ``GpuRequest`` model.

    Args:
        raw:  GPU specification, e.g. ``"0,1"`` or ``"all"``.
              Accepts legacy ``"gpu:N"`` prefix (strips it).

    Returns:
        A ``GpuRequest``-compatible dict, or ``None`` if *raw* is empty.

    Examples::

        >>> _parse_gpu("0,1")
        {'device_ids': ['0', '1'], 'capabilities': ['gpu']}
        >>> _parse_gpu("all")
        {'device_ids': 'all', 'capabilities': ['gpu']}
        >>> _parse_gpu("gpu:2")
        {'device_ids': ['2'], 'capabilities': ['gpu']}
    """
    if not raw:
        return None

    gpu_raw = raw.strip()
    if not gpu_raw:
        return None

    # Strip legacy "gpu:N" prefix used in earlier iterations
    if gpu_raw.lower().startswith("gpu:"):
        gpu_raw = gpu_raw[4:]

    if gpu_raw.lower() == "all":
        return {"device_ids": "all", "capabilities": ["gpu"]}

    device_ids = [d.strip() for d in gpu_raw.split(",") if d.strip()]
    if not device_ids:
        return None

    return {"device_ids": device_ids, "capabilities": ["gpu"]}


if __name__ == "__main__":
    main()
