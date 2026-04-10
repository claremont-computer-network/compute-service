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

---

## Behaviour

| Aspect | Detail |
|--------|--------|
| **State** | Each cell runs in a **fresh container** — no variables, imports, or side effects carry over between cells. |
| **Execution** | Synchronous — the cell blocks until the remote container exits, then output appears inline. |
| **Exceptions** | A Python exception in the remote code prints the traceback inline. No error is raised in the notebook. |
| **Exit code** | A non-zero exit code is printed as a warning below the output. |
| **Image** | The image must have `python` available. Use a custom image for pre-installed packages. |

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

All of these are surfaced as a red error cell in the notebook so the problem is visible immediately.
