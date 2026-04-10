# compute-service

A self-hosted, machine-agnostic system for running containerised workloads on a remote machine via an HTTP API.

---

## Motivation

Most compute jobs — model training, data processing, batch scripts — end up tied to a specific machine by implicit assumptions: hardcoded paths, manual SSH sessions, or ad-hoc scripts that only one person knows how to run.

This project solves that by treating a remote machine as a generic compute node. You provision it once with Ansible, then trigger any containerised workload from anywhere by sending a single HTTP request. The machine does not need to know what the job does. The job does not need to know what machine it runs on.

The result is a repo you can clone onto any machine, fill in a local config file, and have running in under an hour.

---

## How it works

There are three layers:

**Provisioner (Ansible)**
Prepares the host machine: installs Docker, creates the service user, and mounts any external storage to a consistent local path. Run once to prime a new machine; safe to re-run at any time.

**Dispatcher (FastAPI)**
A lightweight HTTP API that runs on the compute node. Accepts a job description — a Docker image, a command, environment variables, and optional volume mappings — and executes it via the Docker daemon. Returns logs either inline (for short jobs) or via a separate logs endpoint.

**Jobs (Docker)**
The dispatcher treats every job as a black box. Whether the container runs a Python script, a Rust binary, or anything else, the dispatcher only needs to know the image, the command, and the data path.

---

## Repository layout

```
ansible/                Ansible playbook and example configuration
clients/
  python/               Installable Python client and IPython %%dispatch magic
    caas/               Package source (CaasClient, register_magic)
    tests/              Client unit tests
dispatcher/             FastAPI application, Dockerfile, and Compose file
  app/                  Application source
scripts/                bootstrap.sh to provision a node; smoke_test.py for end-to-end testing
.env.example            Template for required environment variables
```

Actual secrets and machine-specific config (inventory, group_vars, .env) are gitignored. The repo only contains the templates.

---

## Quickstart

### 1. Clone and configure

```bash
git clone https://github.com/claremont-computer-network/compute-service
cd compute-service
```

Copy the example files and fill them in:

```bash
cp ansible/inventory.example.yml inventory.yml
cp ansible/vars.example.yml ansible/group_vars/all.yml
cp .env.example .env
```

Edit `inventory.yml` to point at your remote machine. Edit `group_vars/all.yml` to set the data mount path and service user. Edit `.env` to set a strong `DISPATCHER_API_KEY`.

### 2. Provision the remote machine

Requires Ansible installed locally and SSH access to the remote machine.

```bash
./scripts/bootstrap.sh
```

This installs Docker on the remote host, creates the service user, and mounts the configured storage path. It is safe to run again if anything changes.

### 3. Start the dispatcher

On the remote machine, from the `dispatcher/` directory:

```bash
docker compose up -d --build
```

Or install it as a persistent service using `dispatcher/compute-service.service.example` as a template.

### 4. Submit a job

From any machine with network access to the node:

```bash
curl -X POST http://<host>:8000/v1/execute \
  -H "Content-Type: application/json" \
  -H "X-API-Key: <your-key>" \
  -d '{
    "image": "alpine:3.18",
    "cmd": ["sh", "-c", "echo hello"],
    "detach": false
  }'
```

The response contains the job status and logs.

---

## API reference

| Method | Path | Description |
|--------|------|-------------|
| GET | `/health` | Returns `{"status": "ok"}` if the dispatcher and Docker daemon are reachable |
| POST | `/v1/execute` | Submit a job |
| POST | `/v1/execute/cell` | Run a Python code string synchronously and return its output (used by the notebook client) |
| GET | `/v1/logs/{container_id}` | Fetch logs for a detached job; append `?follow=true` to stream |

**Execute request body** (`/v1/execute`)

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `image` | string | yes | Docker image to run |
| `cmd` | string or list | no | Command to pass to the container |
| `env` | object | no | Environment variables |
| `volumes` | list | no | Host-to-container path mappings (see below) |
| `detach` | boolean | no | If false, block until the container exits and return logs inline. Default: true |
| `gpu` | object | no | GPU access request (see below) |

**Cell execute request body** (`/v1/execute/cell`)

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `code` | string | yes | Python source code to run (`python -c <code>`) |
| `image` | string | yes | Docker image to run (must have Python available) |
| `env` | object | no | Environment variables |
| `volumes` | list | no | Host-to-container path mappings |
| `gpu` | object | no | GPU access request |

Cell execution is always synchronous. The response always contains `status`, `exit_code`, and `logs`. A non-zero exit code (e.g. a Python exception) returns HTTP 200 with the traceback in `logs` rather than a 500, so the caller can display it inline.

**Volume entry**

| Field | Type | Description |
|-------|------|-------------|
| `host_path` | string | Absolute path on the host (must be within `ALLOWED_HOST_DIRS`) |
| `container_path` | string | Mount point inside the container |
| `mode` | string | `rw` or `ro`. Default: `rw` |

**GPU request**

| Field | Type | Description |
|-------|------|-------------|
| `device_ids` | `"all"` or list of strings | Which GPUs to expose. `"all"` exposes every GPU; a list such as `["0", "1"]` exposes specific devices |
| `capabilities` | list of strings | Driver capabilities forwarded to the NVIDIA runtime. Default: `["gpu"]` |

Requires `nvidia-container-toolkit` on the host. See the GPU support section below.

---

## Python client

A thin installable client lives in `clients/python/`. It can be used standalone or inside a Jupyter notebook via the `%%dispatch` cell magic.

### Installation

```bash
pip install "git+https://github.com/claremont-computer-network/compute-service.git#subdirectory=clients/python"
```

For notebook use, IPython must also be available (it is included automatically in JupyterHub/JupyterLab environments):

```bash
pip install "git+https://github.com/claremont-computer-network/compute-service.git#subdirectory=clients/python[notebook]"
```

### CaasClient

`CaasClient` is a synchronous httpx wrapper around the dispatcher API.

```python
from caas import CaasClient

client = CaasClient(
    host="http://192.168.1.50:8000",
    api_key="your-secret-key",          # omit if the dispatcher has no key set
)

# Check the dispatcher is reachable
client.health()                         # {"status": "ok"}

# Submit a detached job (returns immediately)
job = client.execute(
    image="alpine:3.18",
    cmd=["sh", "-c", "sleep 5 && echo done"],
)
print(job["container_id"])

# Fetch logs for a detached job
print(client.logs(job["container_id"]))

# Run a job synchronously and get logs inline
result = client.execute(
    image="python:3.12-slim",
    cmd=["python", "-c", "print(1 + 1)"],
    detach=False,
)
print(result["logs"])                   # "2\n"

# Send a Python code string to /v1/execute/cell
logs = client.execute_cell(
    code="import platform; print(platform.node())",
    image="python:3.12-slim",
)
print(logs)
```

`CaasClient` implements the context manager protocol. Use it with `with` when managing the connection pool explicitly:

```python
with CaasClient(host="http://192.168.1.50:8000", api_key="key") as client:
    print(client.health())
```

All error responses raise `caas.CaasError` with the detail message from the dispatcher.

### GPU jobs

Pass a `gpu` dict to request GPU access:

```python
# All available GPUs
client.execute(image="pytorch/pytorch:latest", cmd=["python", "train.py"],
               gpu={"device_ids": "all"})

# Specific devices
client.execute(image="pytorch/pytorch:latest", cmd=["python", "train.py"],
               gpu={"device_ids": ["0", "1"]})
```

### Volume mounts

Paths must be within `ALLOWED_HOST_DIRS` configured on the dispatcher node.

```python
client.execute(
    image="python:3.12-slim",
    cmd=["python", "/data/process.py"],
    volumes=[{"host_path": "/mnt/datasets", "container_path": "/data", "mode": "ro"}],
)
```

### IPython `%%dispatch` magic

`%%dispatch` sends a notebook cell to the remote dispatcher and prints its output inline, as if the cell ran locally.

**Setup (run once per notebook session)**

```python
import os
os.environ["CAAS_HOST"]          = "http://192.168.1.50:8000"
os.environ["DISPATCHER_API_KEY"] = "your-secret-key"
os.environ["CAAS_DEFAULT_IMAGE"] = "python:3.12-slim"   # used when no --image flag is given

from caas import register_magic
register_magic()
```

**Basic usage**

```python
%%dispatch
import platform
print("running on", platform.node())
```

**Override the image for a single cell**

```python
%%dispatch --image pytorch/pytorch:2.3.0-cuda12.1-cudnn8-runtime
import torch
print(torch.cuda.get_device_name(0))
```

**Request GPU access**

```python
%%dispatch --image pytorch/pytorch:2.3.0-cuda12.1-cudnn8-runtime --gpu all
import torch
print(torch.cuda.is_available())
```

```python
%%dispatch --image pytorch/pytorch:2.3.0-cuda12.1-cudnn8-runtime --gpu 0,1
import torch
print([torch.cuda.get_device_name(i) for i in range(torch.cuda.device_count())])
```

**How it behaves**

- Each cell runs in a **fresh container** with no shared state between cells.
- Execution is synchronous — the cell blocks until the remote container exits, then output appears inline.
- Python exceptions in the remote code print the traceback inline (exit code is non-zero but no error is raised in the notebook).
- The image must have Python available. Use a custom image if you need specific packages.

---

## Configuration

All configuration is passed via environment variables, typically via a local `.env` file (see `.env.example`).

| Variable | Description |
|----------|-------------|
| `DISPATCHER_API_KEY` | Secret key required in the `X-API-Key` header. If unset, authentication is disabled — only acceptable for local development. |
| `ALLOWED_HOST_DIRS` | Comma-separated list of host paths that jobs may bind-mount. Requests referencing paths outside this list are rejected with 400. |

**Client-side environment variables** (used by `register_magic()` and readable by `CaasClient` callers)

| Variable | Description |
|----------|-------------|
| `CAAS_HOST` | Full base URL of the dispatcher, e.g. `http://192.168.1.50:8000` |
| `DISPATCHER_API_KEY` | API key sent in `X-API-Key`. Must match the value set on the dispatcher. |
| `CAAS_DEFAULT_IMAGE` | Docker image used by `%%dispatch` when no `--image` flag is given |

---

## GPU support

The dispatcher can forward GPU access to containers via the NVIDIA container runtime.

To enable it, set `caas_gpu_enabled: true` in `ansible/group_vars/all.yml` before running the provisioner. The Ansible playbook will install the NVIDIA drivers and `nvidia-container-toolkit` on the host.

Once provisioned, pass a `gpu` field in any execute request, or use `--gpu` in the `%%dispatch` magic. See the GPU request table in the API reference above.

---

## Security notes

The dispatcher has access to the Docker socket, which gives it root-equivalent access to the host. Keep this in mind when deciding what network to expose it on. Tailscale or a similar overlay network is a straightforward way to limit access to trusted machines without opening ports to the internet.

Never commit `.env`, `inventory.yml`, or `group_vars/` to version control. The `.gitignore` is configured to prevent this, but the responsibility ultimately lies with the operator.

---

## Development

Install dependencies (dispatcher + dev tools):

```bash
uv pip install -r dispatcher/requirements-dev.txt
```

Install the Python client in editable mode:

```bash
uv pip install -e clients/python
```

Run the full test suite (dispatcher + client + magic):

```bash
uv run pytest -v
```

Run the local smoke test (no Docker daemon required):

```bash
uv run python scripts/smoke_test.py
```
