# MCP Server

The **caas-mcp** server exposes compute-as-a-service as discoverable tools via the [Model Context Protocol (MCP)](https://modelcontextprotocol.io/). It lets LLM agents dispatch Python code, run containerised workloads, monitor jobs, and retrieve output — all through a standard tool interface.

---

## Architecture

The MCP server runs as a **standalone stdio process** on the agent's machine. It wraps `CaasClient` over HTTP — zero changes to the dispatcher are required.

```
LLM agent ───MCP stdio───▶ caas-mcp server ───HTTP───▶ Dispatcher (remote machine)
                                          │
                                          └──▶ CaasClient (in-process HTTP client)
```

---

## Installation

### Install the Python client (required)

The MCP server depends on `caas-client`. Install it first:

```bash
uv pip install -e clients/python
# or
pip install -e clients/python
```

### Install the MCP server

```bash
uv pip install -e clients/python/mcp
# or
pip install -e clients/python/mcp
```

Install dev dependencies and run the tests:

```bash
uv pip install -e "clients/python/mcp[dev]"
uv run pytest clients/python/mcp/tests/ -v
```

### Verify

```python
from caas_mcp.config import Config
cfg = Config(dispatcher_url="http://192.168.1.50:8000")
print(cfg)
```

---

## Configuration

The MCP server reads its configuration from environment variables at startup. All values are passed by the **MCP host** (e.g. Cursor, Claude Desktop, opencode) — no runtime prompts or config files.

| Env Var | Required | Description | Example |
|---|---|---|---|
| `CAAS_DISPATCHER_URL` | **yes** | Base URL of the dispatcher HTTP API | `http://192.168.1.50:8000` |
| `CAAS_API_KEY` | no | API key sent as `X-API-Key` | `my-secret-key` |
| `CAAS_REMOTE_WORKSPACE` | no | Path on the **remote** dispatcher filesystem for implicit `/workspace` volume mounts | `/mnt/data/agent_staging` |

!!! note "Remote workspace paths"
    `CAAS_REMOTE_WORKSPACE` is a **REMOTE path** on the dispatcher's filesystem.
    Docker bind mounts operate relative to the Docker daemon host (the remote
    compute node). The path must exist on the remote host and be within
    `ALLOWED_HOST_DIRS`. No local-path resolution or manipulation is performed
    — the value is passed through verbatim as `host_path`.

### Example

```bash
export CAAS_DISPATCHER_URL=http://192.168.1.50:8000
export CAAS_API_KEY=your-key
export CAAS_REMOTE_WORKSPACE=/mnt/data/agent_staging
```

---

## Running the server

### Standalone (stdio)

```bash
caas-mcp
# or
python -m caas_mcp.server
# or
uv run -m caas_mcp.server
```

### With environment variables (inline)

```bash
CAAS_DISPATCHER_URL=http://192.168.1.50:8000 \
CAAS_API_KEY=secret \
uv run -m caas_mcp.server
```

---

## Available tools

| Tool | Description |
|---|---|
| `list_jobs` | List all jobs. Optional `state` filter: `"running"`, `"stopped"`, `"*"`. |
| `execute_cell` | Execute a Python code string in a container. Synchronous — returns output. |
| `execute` | Launch a job — detached (default) or synchronous. Accepts `image`, `cmd`, `env`, `gpu`. |
| `stop_job` | Stop and remove a running job. |
| `get_logs` | Retrieve container stdout/stderr logs. |

### `list_jobs(state: str | None) → str`

List jobs dispatched through the dispatcher.

```json
// Tool call
{
    "name": "list_jobs",
    "arguments": { "state": "*" }
}

// Returns
[]
```

### `execute_cell(code: str, image: str, env: str | None) → str`

Execute Python code in a container. Returns captured stdout as a string.

```json
// Tool call
{
    "name": "execute_cell",
    "arguments": {
        "code": "print(42)",
        "image": "python:3.11-slim"
    }
}

// Returns
"42\n"
```

`env` accepts comma-separated `KEY=VAL` pairs, e.g. `"FOO=1,BAR=baz"`.

Implicit workspace mount: when `CAAS_REMOTE_WORKSPACE` is set, every
`execute_cell` call automatically receives a volume mount at
`/workspace` so data persists across calls.

### `execute(image: str, cmd: str | None, env: str | None, gpu: str | None, detach: bool) → str`

Launch a detached or synchronous job.

```json
// Tool call — detached job with GPU
{
    "name": "execute",
    "arguments": {
        "image": "nvcr.io/nvidia/pytorch:24.01-py3",
        "cmd": "python train.py",
        "gpu": "all",
        "detach": true
    }
}

// Returns
{"job_id": "abc123", "container_id": "a3f8d...", "status": "running"}
```

`gpu` accepts `"0,1"` for specific devices, `"all"` for all GPUs, or `"gpu:2"` legacy prefix.

### `stop_job(job_id: str) → str`

Stop a running container.

```json
{
    "name": "stop_job",
    "arguments": { "job_id": "a3f8d0e12b9c" }
}

// Returns
{"job_id": "a3f8d0e12b9c", "status": "stopped"}
```

### `get_logs(container_id: str, follow: bool) → str`

Retrieve logs for a container.

```json
{
    "name": "get_logs",
    "arguments": { "container_id": "a3f8d0e12b9c" }
}

// Returns captured stdout + stderr
"output from container\n"
```

Set `follow=true` to stream until the container exits.

---

## Resources

| URI | Description |
|---|---|
| `system://health` | Dispatcher health status + active plugin list. |

```json
// Tool call
{
    "name": "resources/read",
    "arguments": { "uri": "system://health" }
}

// Returns
{"status": "ok", "plugins": ["nvidia-entrypoint", "volume-policy", ...]}
```

---

## Integrating with opencode

Add this to `~/.config/opencode/opencode.json`:

```json
{
  "mcp": {
    "caas": {
      "type": "local",
      "command": [
        "/path/to/.venv/bin/python3",
        "-m",
        "caas_mcp.server"
      ],
      "environment": {
        "CAAS_DISPATCHER_URL": "http://192.168.1.50:8000",
        "CAAS_API_KEY": "your-secret-key",
        "CAAS_REMOTE_WORKSPACE": "/mnt/data/agent_staging"
      },
      "enabled": true,
      "timeout": 30000
    }
  }
}
```

The `command` array should point to a Python virtualenv that has both `caas-client` and `caas-mcp` installed. Restart opencode to load the new MCP server.

---

## Integrating with opencode (GitHub Codespaces or dev machine)

For GitHub Codespaces, VS Code Dev Containers, or any environment where you have the repo cloned locally:

```json
{
  "mcp": {
    "caas": {
      "type": "local",
      "command": [
        "uv", "run", "-C", "/workspace/clients/python/mcp",
        "-m", "caas_mcp.server"
      ],
      "environment": {
        "CAAS_DISPATCHER_URL": "http://192.168.1.50:8000",
        "CAAS_API_KEY": "your-secret-key"
      },
      "enabled": true,
      "timeout": 30000
    }
  }
}
```

Replace `/workspace` with your actual repo path.

---

## Integrating with Cursor

Add the server to Cursor's MCP config (`~/.cursor/mcp.json`):

```json
{
  "mcpServers": {
    "caas-mcp": {
      "command": "/path/to/venv/bin/python3",
      "args": ["-m", "caas_mcp.server"],
      "env": {
        "CAAS_DISPATCHER_URL": "http://192.168.1.50:8000",
        "CAAS_API_KEY": "your-secret-key"
      }
    }
  }
}
```

---

## Integrating with Claude Desktop

Add the server to `claude_desktop_config.json` (typically at
`~/Library/Support/com.anthropic Claude/claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "caas-mcp": {
      "command": "/path/to/venv/bin/python3",
      "args": ["-m", "caas_mcp.server"],
      "env": {
        "CAAS_DISPATCHER_URL": "http://192.168.1.50:8000",
        "CAAS_API_KEY": "your-secret-key"
      }
    }
  }
}
```

---

## Troubleshooting

### MCP host can't connect to the server

Verify the command runs independently first:

```bash
CAAS_DISPATCHER_URL=http://192.168.1.50:8000 python -m caas_mcp.server
```

If you see a `ConfigError` about missing `CAAS_DISPATCHER_URL`, ensure your
MCP host passes the environment variables correctly.

### "GPU functionality will not be available" inside the container

The MCP server passes GPU requests to the dispatcher, which then tells Docker
which GPUs are visible. Ensure the remote machine has GPU drivers and NVIDIA
Container Toolkit installed.

### Workspace mount fails with permission denied

`CAAS_REMOTE_WORKSPACE` must be a path that exists on the **remote** host and
is listed in the dispatcher's `ALLOWED_HOST_DIRS` config. The MCP server does
not check this — the dispatcher returns an HTTP error if the path is out of
bounds.
