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

    @server.resource("system://gpu")
    def gpu_info() -> str:
        """Return GPU hardware info (memory, temperature, utilization) as JSON.

        Queries the remote node's nvidia-smi via the dispatcher.
        """
        client = _build_client(cfg)
        try:
            return _to_json(client.gpu_info())
        except CaasError as exc:
            return _to_json({"error": str(exc)})
        finally:
            client.close()

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

    @server.tool()
    def list_images() -> str:
        """List Docker images pre-downloaded on the dispatcher node."""
        client = _build_client(cfg)
        try:
            images = client.images_list()
        except CaasError as exc:
            return _to_json({"error": str(exc)})
        finally:
            client.close()
        return _to_json(images)

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

    # ── Tools: workspace file I/O (via execute_cell wrapper) ─────────────

    @server.tool()
    async def write_file(
        path: str,
        content: str,
        ctx: Context | None = None,
    ) -> str:
        """Write content to a file in the remote workspace (/workspace/<path>).

        Uses execute_cell to write — the Docker volume mount persists files
        across tool calls. Content is safely escaped via json.dumps to prevent
        SyntaxError from special characters.
        """
        import json as _json
        import os as _os

        safe_path = _os.path.basename(path)
        safe_content = _json.dumps(content)
        bytes_written = len(content.encode("utf-8"))

        code = (
            "import os\n"
            f"content = {safe_content}\n"
            f"with open('/workspace/{safe_path}', 'w') as f:\n"
            "    f.write(content)\n"
            f"print('Wrote {bytes_written} bytes to {safe_path}')"
        )

        return await _execute_cell_inline(code)

    @server.tool()
    async def read_file(
        path: str,
        ctx: Context | None = None,
    ) -> str:
        """Read content from a file in the remote workspace (/workspace/<path>).

        Uses execute_cell to read — hard-limit at 8000 chars to protect the
        agent's context window.
        """
        import os as _os

        safe_path = _os.path.basename(path)

        code = (
            "try:\n"
            f"    with open('/workspace/{safe_path}', 'r') as f:\n"
            "        data = f.read(8000)\n"
            "        print(data)\n"
            "        if f.read(1):\n"
            "            print('\\n\\n[System: File truncated to 8000 chars]')\n"
            "except FileNotFoundError:\n"
            '    print("Error: File not found in workspace.")'
        )

        return await _execute_cell_inline(code)

    async def _execute_cell_inline(code: str) -> str:
        """Helper: run inline Python via execute_cell with workspace mount."""
        import json
        from mcp.server.fastmcp import Context as _Ctx

        workspace = server._cfg.remote_workspace  # type: ignore[attr-defined]
        volumes: list[dict] | None = None
        if workspace:
            volumes = [
                {"host_path": workspace, "container_path": "/workspace", "mode": "rw"}
            ]

        client = _build_client(cfg)
        try:
            try:
                result = client.execute_cell(
                    code=code,
                    image="python:3.11-slim",
                    volumes=volumes,
                    suppress_entrypoint=True,
                )
                # Result may be raw stdout or JSON-serialized with timeout guidance
                try:
                    return _to_json(json.loads(result))
                except (json.JSONDecodeError, TypeError):
                    return _to_json({"output": result.strip()})
            except CaasTimeoutError:
                return _to_json({
                    "status": "timeout",
                    "message": "Execution timed out. The job may still be running.",
                })
            except CaasError as exc:
                return _to_json({"error": str(exc)})
        finally:
            client.close()

    # ── Tools: template management ──────────────────────────────────────

    @server.tool()
    async def list_templates(
        ctx: Context | None = None,
    ) -> str:
        """List all saved job templates."""
        client = _build_client(cfg)
        try:
            return _to_json(client.templates_list())
        except CaasError as exc:
            return _to_json({"error": str(exc)})
        finally:
            client.close()

    @server.tool()
    async def upsert_template(
        name: str,
        image: str | None = None,
        cmd: str | None = None,
        env: str | None = None,
        volumes: str | None = None,
        gpu: str | None = None,
        ctx: Context | None = None,
    ) -> str:
        """Create or update a job template.

        Args:
            name:  Template name.
            image: Docker image.
            cmd:   Command to run in the container.
            env:   Comma-separated KEY=VALUE pairs.
            volumes: JSON array of volume configs (e.g. ``[{"host_path":"/data","container_path":"/data","mode":"rw"}]``).
            gpu:   GPU spec string (e.g. ``"0,1"`` or ``"all"``).

        Returns the saved template dict with ``id``, ``created_at``, ``modified_at``.
        """
        client = _build_client(cfg)
        try:
            parsed_env = _parse_env(env)
            parsed_gpu = _parse_gpu(gpu) if gpu else None
            parsed_volumes = None
            if volumes:
                import json as _json
                try:
                    parsed_volumes = _json.loads(volumes)
                except (_json.JSONDecodeError, ValueError):
                    return _to_json({"error": f"Invalid volumes JSON: {volumes!r}"})

            result = client.templates_upsert(
                name=name,
                image=image,
                cmd=cmd,
                env=parsed_env,
                volumes=parsed_volumes,
                gpu=parsed_gpu,
            )
            return _to_json(result)
        except CaasError as exc:
            return _to_json({"error": str(exc)})
        finally:
            client.close()

    # ── Tools: schedule management ──────────────────────────────────────

    @server.tool()
    async def list_schedules(
        ctx: Context | None = None,
    ) -> str:
        """List all schedules (pending, active, cancelled)."""
        client = _build_client(cfg)
        try:
            return _to_json(client.schedules_list())
        except CaasError as exc:
            return _to_json({"error": str(exc)})
        finally:
            client.close()

    @server.tool()
    async def create_schedule(
        template_id: str | None = None,
        delay_seconds: int = 60,
        image: str | None = None,
        cmd: str | None = None,
        env: str | None = None,
        volumes: str | None = None,
        gpu: str | None = None,
        ctx: Context | None = None,
    ) -> str:
        """Create a schedule to trigger a job.

        Args:
            template_id:  ID of a saved template to use.
            delay_seconds: Seconds to wait (0 = immediate).
            image:        Docker image (if not using template_id).
            cmd:          Command to run (if not using template_id).
            env:          Comma-separated KEY=VALUE pairs.
            volumes:      JSON array of volume configs.
            gpu:          GPU spec string.

        Either ``template_id`` or inline fields (image, cmd, etc.) must be provided.
        """
        client = _build_client(cfg)
        try:
            parsed_env = _parse_env(env)
            parsed_gpu = _parse_gpu(gpu) if gpu else None
            parsed_volumes = None
            if volumes:
                import json as _json
                try:
                    parsed_volumes = _json.loads(volumes)
                except (_json.JSONDecodeError, ValueError):
                    return _to_json({"error": f"Invalid volumes JSON: {volumes!r}"})

            result = client.schedules_upsert(
                template_id=template_id,
                delay_seconds=delay_seconds,
                image=image,
                cmd=cmd,
                env=parsed_env,
                volumes=parsed_volumes,
                gpu=parsed_gpu,
            )
            return _to_json(result)
        except CaasError as exc:
            return _to_json({"error": str(exc)})
        finally:
            client.close()

    @server.tool()
    async def cancel_schedule(
        schedule_id: str,
        ctx: Context | None = None,
    ) -> str:
        """Cancel a pending schedule by ID."""
        client = _build_client(cfg)
        try:
            result = client.schedule_cancel(schedule_id)
            return _to_json(result)
        except CaasError as exc:
            return _to_json({"error": str(exc)})
        finally:
            client.close()

    # ── Tools: job management (existing endpoints, missing MCP tools) ─────

    @server.tool()
    async def browse_files(
        path: str = "/",
        ctx: Context | None = None,
    ) -> str:
        """List files in a mounted directory.

        Uses dispatcher extension endpoint ``GET /api/files``.

        Returns ``{"path": "/real/path", "entries": [...]}`` where each entry
        is ``{"name", "permissions", "size", "modified", "is_dir"}``.

        Only directories under enabled host directories may be browsed.

        Args:
            path: Host path to list (e.g. ``"/workspace/code"``).
        """
        client = _build_client(cfg)
        try:
            result = client.files_list(path=path)
            return _to_json(result)
        except CaasError as exc:
            return _to_json({"error": str(exc)})
        finally:
            client.close()

    @server.tool()
    async def get_job_by_id(
        job_id: str,
        ctx: Context | None = None,
    ) -> str:
        """Return a single job record by job_id (the full container ID).

        Args:
            job_id: The full 64-char Docker container ID.
        """
        client = _build_client(cfg)
        try:
            result = client.job(job_id)
            return _to_json(result)
        except CaasError as exc:
            return _to_json({"error": str(exc)})
        finally:
            client.close()

    @server.tool()
    async def delete_template(
        template_id: str,
        ctx: Context | None = None,
    ) -> str:
        """Delete a template by ID.

        Args:
            template_id: The template ID returned by ``upsert_template``.
        """
        client = _build_client(cfg)
        try:
            result = client.templates_delete(template_id)
            return _to_json(result)
        except CaasError as exc:
            return _to_json({"error": str(exc)})
        finally:
            client.close()

    @server.tool()
    async def check_image(
        image: str,
        ctx: Context | None = None,
    ) -> str:
        """Check if a specific Docker image is available on the node.

        Uses dispatcher extension endpoint ``POST /api/images/check``.

        Returns ``{"found": true, "image": {...}}`` or ``{"found": false}``.

        Args:
            image: Docker image reference (e.g. ``"python:3.11-slim"``).
        """
        client = _build_client(cfg)
        try:
            result = client.images_check(image)
            return _to_json(result)
        except CaasError as exc:
            return _to_json({"error": str(exc)})
        finally:
            client.close()

    # ── Tools: staging area management ────────────────────────────────────

    @server.tool()
    async def staging_list(
        ctx: Context | None = None,
    ) -> str:
        """List all staging areas.

        Staging areas are named references to host paths that can be mounted
        into containers. Uses ``GET /api/staging``.
        """
        client = _build_client(cfg)
        try:
            result = client.staging_list()
            return _to_json(result)
        except CaasError as exc:
            return _to_json({"error": str(exc)})
        finally:
            client.close()

    @server.tool()
    async def staging_create(
        name: str,
        host_path: str,
        dest_path: str | None = None,
        ctx: Context | None = None,
    ) -> str:
        """Create a staging area — a named reference to a host path mount.

        Staging areas can be used as volume sources when creating jobs or
        sandboxes. Uses ``POST /api/staging``.

        Args:
            name: A human-readable name for this staging area.
            host_path: The host filesystem path to bind-mount. Must be under
                       an allowed directory.
            dest_path: Optional container destination path. Defaults to the
                       host path value.
        """
        client = _build_client(cfg)
        try:
            result = client.staging_create(name=name, host_path=host_path, dest_path=dest_path)
            return _to_json(result)
        except CaasError as exc:
            return _to_json({"error": str(exc)})
        finally:
            client.close()

    @server.tool()
    async def staging_delete(
        staging_id: str,
        ctx: Context | None = None,
    ) -> str:
        """Remove a staging area.

        Uses dispatcher extension endpoint ``DELETE /api/staging/{staging_id}``.

        Args:
            staging_id: The staging area ID returned by ``staging_list``.
        """
        client = _build_client(cfg)
        try:
            result = client.staging_delete(staging_id)
            return _to_json(result)
        except CaasError as exc:
            return _to_json({"error": str(exc)})
        finally:
            client.close()

    # ── Sandbox MCP integration ──────────────────────────────────────────

    @server.resource("sandbox://{sandbox_id}/status")
    def sandbox_status(sandbox_id: str) -> str:
        """Return the current status of a specific sandbox."""
        client = _build_client(cfg)
        try:
            job = client.job(sandbox_id)
            if not job:
                return _to_json({"error": "Sandbox not found"})
            return _to_json({
                "status": job.get("status"),
                "job_type": job.get("job_type"),
                "image": job.get("image"),
                "last_accessed": job.get("submitted_at"),
            })
        except CaasError as exc:
            return _to_json({"error": str(exc)})
        finally:
            client.close()

    @server.tool()
    async def create_sandbox(
        image: str,
        env: str | None = None,
        gpu: str | None = None,
        shm_size: str | None = None,
        ctx: Context | None = None,
    ) -> str:
        """Create a persistent sandbox container for interactive execution.

        The sandbox starts with ``sleep infinity`` and holds a resource slot
        until it is stopped (via ``sandbox_stop``) or reaped by the idle
        reaper (default TTL: 30 min).

        Args:
            image:   Docker image to use.
            env:     Comma-separated ``KEY=VALUE`` pairs.
            gpu:     GPU request, e.g. ``"0,1"`` or ``"all"``.
            shm_size: Shared memory size (e.g. ``"2g"``).
        """
        parsed_env = _parse_env(env)
        parsed_gpu = _parse_gpu(gpu) if gpu else None
        workspace = server._cfg.remote_workspace  # type: ignore[attr-defined]

        volumes: list[dict] | None = None
        if workspace:
            volumes = [
                {"host_path": workspace, "container_path": "/workspace", "mode": "rw"}
            ]

        client = _build_client(cfg)
        try:
            res = client.sandbox_create(
                image=image,
                env=parsed_env or None,
                volumes=volumes,
                gpu=parsed_gpu,
                shm_size=shm_size,
            )
            return _to_json(res)
        except CaasError as exc:
            return _to_json({"error": str(exc)})
        finally:
            client.close()

    @server.tool()
    async def sandbox_exec(
        sandbox_id: str,
        cmd: str,
        ctx: Context | None = None,
    ) -> str:
        """Run a command inside an existing sandbox.

        Args:
            sandbox_id: ID of the running sandbox.
            cmd:        Command string to execute (e.g. ``'pip install pandas'``).
        """
        client = _build_client(cfg)
        try:
            res = client.sandbox_exec(sandbox_id, cmd)

            # Enforce truncation limit to protect context window
            MAX_CHARS = 8000
            for key in ["stdout", "stderr"]:
                if key in res and isinstance(res[key], str) and len(res[key]) > MAX_CHARS:
                    res[key] = res[key][-MAX_CHARS:] + f"\n\n[System Note: {key} truncated.]"

            return _to_json(res)
        except CaasError as exc:
            return _to_json({"error": str(exc)})
        finally:
            client.close()

    @server.tool()
    async def sandbox_stop(
        sandbox_id: str,
        ctx: Context | None = None,
    ) -> str:
        """Stop and remove a sandbox."""
        client = _build_client(cfg)
        try:
            client.stop(sandbox_id)
            return _to_json({"status": "stopped", "sandbox_id": sandbox_id})
        except CaasError as exc:
            return _to_json({"error": str(exc)})
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
