# `%%dispatch` Magic

`%%dispatch` is an IPython cell magic that sends a notebook cell to the remote dispatcher and prints its output inline — as if the cell ran locally.

---

## Setup

Run once per notebook session (typically in the first cell):

```python
import os
os.environ["CAAS_HOST"]          = "http://192.168.1.50:8000"
os.environ["DISPATCHER_API_KEY"] = "your-secret-key"
os.environ["CAAS_DEFAULT_IMAGE"] = "python:3.12-slim"

from caas import register_magic
register_magic()
```

`register_magic()` reads the three environment variables above. If `DISPATCHER_API_KEY` is not set (dispatcher running without auth), simply omit it.

!!! tip "Persisting settings"
    Set the environment variables in your shell profile or a `.env` file and load them with
    `python-dotenv` so you never have to re-enter them:
    ```python
    from dotenv import load_dotenv
    load_dotenv()  # reads .env from the current directory
    from caas import register_magic
    register_magic()
    ```

---

## Basic usage

```python
%%dispatch
import platform
print("running on", platform.node())
```

Output:

```
running on compute-node-1
```

---

## Flags

### `--image`

Override the image for a single cell:

```python
%%dispatch --image python:3.11-slim
import sys
print(sys.version)
```

### `--gpu`

Request GPU access. Accepts `all` or a comma-separated list of device IDs:

```python
%%dispatch --image pytorch/pytorch:2.3.0-cuda12.1-cudnn8-runtime --gpu all
import torch
print(torch.cuda.is_available())
print(torch.cuda.get_device_name(0))
```

```python
%%dispatch --image pytorch/pytorch:2.3.0-cuda12.1-cudnn8-runtime --gpu 0,1
import torch
print([torch.cuda.get_device_name(i) for i in range(torch.cuda.device_count())])
```

### `--volume`

Bind-mount a host directory into the container. Format: `HOST_PATH:CONTAINER_PATH[:MODE]`
where `MODE` is `rw` (default) or `ro`. Repeat the flag for multiple mounts.

The host path must be within `ALLOWED_HOST_DIRS` on the dispatcher — see [Configuration](../configuration.md).

```python
%%dispatch --image python:3.12-slim --volume /mnt/nas_data:/inputs:ro
import os
print(os.listdir("/inputs"))
```

```python
# Mount an input read-only and an output directory read-write
%%dispatch --image python:3.12-slim \
    --volume /mnt/nas_data:/inputs:ro \
    --volume /home/erik/results:/outputs
import json, pathlib
data = json.loads(pathlib.Path("/inputs/data.json").read_text())
pathlib.Path("/outputs/result.json").write_text(json.dumps({"count": len(data)}))
```

!!! warning "Host path must be allowed"
    If the dispatcher returns HTTP 400 with `"Host path not allowed"`, the path is not listed
    in `ALLOWED_HOST_DIRS`. Ask the operator to add it, or use a path that is already allowed.

### `--shm-size`

Set the size of `/dev/shm` inside the container. Useful for PyTorch `DataLoader` with multiple
workers, which uses shared memory for inter-process communication.

```python
%%dispatch --image pytorch/pytorch:2.3.0-cuda12.1-cudnn8-runtime --gpu all --shm-size 1g
import torch
loader = torch.utils.data.DataLoader(dataset, batch_size=64, num_workers=4)
```

### `--ipc`

Set the IPC namespace. `--ipc host` shares the host IPC namespace, giving workers unlimited
shared memory — an alternative to `--shm-size` for large PyTorch multi-GPU jobs.

```python
%%dispatch --image pytorch/pytorch:2.3.0-cuda12.1-cudnn8-runtime --gpu all --ipc host
```

### `--timeout`

Override the HTTP read timeout (seconds) for this cell. Increase it for very long-running
synchronous workloads. Default: 120 s.

```python
%%dispatch --image my-long-job:latest --timeout 3600
# ... job that takes up to an hour
```

### `--suppress-entrypoint` / `--no-suppress-entrypoint`

Override the container's `ENTRYPOINT` with an empty string so the image's startup script is
skipped entirely.  Useful for **NVIDIA NGC images** (`nvcr.io/*`), whose entrypoint prints a
multi-page banner to stdout before exec-ing the user command.

**This flag is auto-enabled for any image whose name starts with `nvcr.io/`**, so you
normally don't need to type it:

```python
# Banner is automatically suppressed — no flag needed
%%dispatch --image nvcr.io/nvidia/pytorch:25.03-py3 --gpu all
import torch
print(torch.cuda.get_device_name(0))
```

If you need to explicitly disable the auto-suppression (e.g. the entrypoint sets up env vars
your code depends on), pass `--no-suppress-entrypoint`:

```python
%%dispatch --image nvcr.io/nvidia/pytorch:25.03-py3 --gpu all --no-suppress-entrypoint
import torch
print(torch.cuda.get_device_name(0))
```

!!! warning "Stale client"
    Auto-suppression lives in the Python client, not the server.  If you see the NVIDIA banner
    in cell output, your `caas` package is out of date.  Reinstall it:
    ```python
    import subprocess, sys
    subprocess.check_call([sys.executable, "-m", "pip", "install",
                           "--upgrade", "--force-reinstall",
                           "git+https://github.com/claremont-computer-network/compute-service.git"
                           "#subdirectory=clients/python"])
    ```
    Then restart the notebook kernel.

### `--verbose`

Include stderr (container banner, pip deprecation warnings, etc.) in the cell output.
Stderr is always shown when the job exits non-zero so tracebacks are never hidden.

```python
%%dispatch --image python:3.11-slim --verbose
import subprocess
subprocess.run(["pip", "install", "numpy"])  # pip output visible
import numpy as np
print(np.__version__)
```

### `--template`

Use a stored job template instead of specifying `--image` and other fields inline.
The template id is looked up on the dispatcher and its configuration (image, cmd, env,
volumes, gpu) is used as the base for the container.

Individual fields from the template can still be overridden using other flags:

```python
%%dispatch --image python:3.11-slim --template tpl_training
import torch
print(torch.cuda.is_available())
```

When both `--image` and `--template` are provided, the image is used as a fallback only
if the template does not specify one.

---

### `--staging`

Mount a pre-configured staging area instead of specifying `--volume` flags. Staging areas
are named references to host path mounts created via the dispatcher's `/api/staging`
endpoint.

```python
# Create a staging area first (run once):
#   client.staging_create(name="outputs", host_path="/mnt/datasets", dest_path="/data")

# Then use it in dispatch:
%%dispatch --image python:3.12-slim --staging outputs
import os
print(os.listdir("/data"))
```

When a staging area has no explicit `dest_path`, the container mount point defaults to the
host path value.

---

## Flags reference

| Flag | Description |
|------|-------------|
| `--template ID` | Use a stored job template |
| `--staging ID` | Mount a staging area by name |
| `--image IMAGE` | Docker image to use for this cell (mutually exclusive with `--template`) |
| `--gpu all\|ID,...` | Request GPU access |
| `--volume HOST:CONTAINER[:MODE]` | Bind-mount a host path (repeatable) |
| `--shm-size SIZE` | `/dev/shm` size, e.g. `1g` |
| `--ipc host` | Share host IPC namespace (unlimited shm) |
| `--timeout SECS` | HTTP read timeout override |
| `--suppress-entrypoint` | Skip the container ENTRYPOINT (auto-set for `nvcr.io/*`) |
| `--no-suppress-entrypoint` | Disable auto-suppression for `nvcr.io/*` images |
| `--verbose` | Show stderr in output (always shown on non-zero exit) |

---

## Behaviour

| Aspect | Detail |
|--------|--------|
| **State** | Each cell runs in a **fresh container** — no variables, imports, or side effects carry over between cells. |
| **Execution** | Synchronous — the cell blocks until the remote container exits, then output appears inline. |
| **Exceptions** | A Python exception in the remote code prints the traceback inline. No error is raised in the notebook. |
| **Exit code** | A non-zero exit code is printed as a warning below the output. |
| **Image** | The image must have `python` available. Use a custom image for pre-installed packages. |
| **Queue** | If all resource slots on the dispatcher are occupied, the magic will wait up to `QUEUE_TIMEOUT_SECS` (default 300 s). A `503` response means the queue was full for the entire timeout period. |

---

## Environment variables read by `register_magic()`

| Variable | Description |
|----------|-------------|
| `CAAS_HOST` | Full base URL of the dispatcher |
| `DISPATCHER_API_KEY` | API key. Omit or leave empty to disable. |
| `CAAS_DEFAULT_IMAGE` | Image used when `--image` is not specified |

---

## Errors

`%%dispatch` raises `CaasMagicError` (a subclass of `Exception`) for configuration problems:

| Error | Cause |
|-------|-------|
| `CAAS_HOST is not configured` | `register_magic()` was not called, or `CAAS_HOST` is unset |
| `No image specified` | `CAAS_DEFAULT_IMAGE` is unset and no `--image` flag was given |
| `Invalid --gpu value` | `--gpu` was given a non-empty value that is neither `all` nor a valid ID list |
| HTTP 400 `Host path not allowed` | A `--volume` host path is not in `ALLOWED_HOST_DIRS` on the dispatcher |
| HTTP 503 `No GPU/CPU slots available` | All resource slots were busy for `QUEUE_TIMEOUT_SECS` — wait for a running job to finish |

All of these are surfaced as a red error cell in the notebook so the problem is visible immediately.
