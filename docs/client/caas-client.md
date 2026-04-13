# CaasClient

`CaasClient` is a synchronous [httpx](https://www.python-httpx.org/) wrapper around the dispatcher API. It can be used standalone in scripts, in notebooks, or as a building block for higher-level tooling.

---

## Construction

```python
from caas import CaasClient

client = CaasClient(
    host="http://192.168.1.50:8000",
    api_key="your-secret-key",   # omit if the dispatcher has no key set
)
```

**Parameters**

| Parameter | Type | Description |
|-----------|------|-------------|
| `host` | `str` | Base URL of the dispatcher. Trailing slashes are stripped. |
| `api_key` | `str \| None` | API key sent as `X-API-Key`. Leave `None` to disable authentication. |
| `http_client` | `httpx.Client \| None` | Inject an existing httpx client (useful in tests). If omitted, a new client is created and owned by `CaasClient`. |

---

## Context manager

`CaasClient` implements the context manager protocol. Use it with `with` when you want the connection pool closed automatically:

```python
with CaasClient(host="http://192.168.1.50:8000", api_key="key") as client:
    print(client.health())
```

If you create the client manually, close it yourself when done:

```python
client = CaasClient(host="http://192.168.1.50:8000")
try:
    ...
finally:
    client.close()
```

!!! note "Injected clients"
    If you pass an `http_client` argument, `CaasClient` will **not** close it on `close()` or
    `__exit__`. Lifetime management of injected clients is the caller's responsibility.

---

## Methods

### `health() → dict`

Check that the dispatcher and Docker daemon are reachable.

```python
client.health()
# {"status": "ok"}
```

Raises `CaasError` if the dispatcher returns a non-2xx response.

---

### `execute(...) → dict`

Submit a container job.

```python
client.execute(
    image="alpine:3.18",
    cmd=["sh", "-c", "echo hello"],
    detach=False,
)
# {"container_id": null, "status": "exited", "exit_code": 0, "logs": "hello\n"}
```

**Parameters**

| Parameter | Type | Description |
|-----------|------|-------------|
| `image` | `str` | Docker image to run |
| `cmd` | `str \| list \| None` | Command to pass to the container |
| `env` | `dict \| None` | Environment variables |
| `volumes` | `list \| None` | Volume specs — see [API Reference](../api-reference.md#volumespec) |
| `gpu` | `dict \| None` | GPU request — see [API Reference](../api-reference.md#gpurequest) |
| `detach` | `bool` | `True` (default) returns immediately; `False` blocks and returns logs |

**Synchronous job (inline logs):**

```python
result = client.execute(
    image="python:3.12-slim",
    cmd=["python", "-c", "print(2 ** 10)"],
    detach=False,
)
print(result["logs"])       # "1024\n"
print(result["exit_code"])  # 0
```

**Detached job (fetch logs later):**

```python
job = client.execute(
    image="python:3.12-slim",
    cmd=["python", "-c", "import time; time.sleep(5); print('done')"],
)
job_id = job["job_id"]
container_id = job["container_id"]
...
print(client.logs(container_id))
```

**Job with a bind-mount:**

```python
result = client.execute(
    image="python:3.12-slim",
    cmd=["python", "/inputs/process.py"],
    volumes=[{"host_path": "/mnt/datasets", "container_path": "/inputs", "mode": "ro"}],
    detach=False,
)
```

---

### `jobs() → list`

Return all jobs currently tracked by the dispatcher (running and recently stopped).

```python
all_jobs = client.jobs()
for j in all_jobs:
    print(j["job_id"], j["status"], j.get("resources"))
```

---

### `job(job_id) → dict`

Get a single job by ID.

```python
j = client.job("a3f8d0e12b9c")
print(j["status"])      # "running" or "stopped"
print(j["exit_code"])   # None while running, integer once stopped
print(j["resources"])   # {"cpu_percent": ..., "mem_usage_mib": ...} or None
```

Raises `CaasError` with HTTP 404 if the job ID is unknown.

---

### `stop(job_id) → dict`

Stop a running job by ID.

```python
client.stop("a3f8d0e12b9c")
# {"job_id": "a3f8d0e12b9c", "status": "stopped"}
```

Raises `CaasError` with HTTP 404 if the job ID is unknown, or HTTP 409 if the job has already stopped.

---

### `execute_cell(code, image, ...) → str`

Send a Python code string to `/v1/execute/cell`. Returns the captured stdout as a string.

```python
logs = client.execute_cell(
    code="import platform; print(platform.node())",
    image="python:3.12-slim",
)
print(logs)
```

**Parameters**

| Parameter | Type | Description |
|-----------|------|-------------|
| `code` | `str` | Python source code |
| `image` | `str` | Docker image (must have `python`) |
| `env` | `dict \| None` | Environment variables |
| `volumes` | `list \| None` | Volume specs |
| `gpu` | `dict \| None` | GPU request |

Always synchronous. Raises `CaasError` for HTTP errors; a non-zero exit code is not raised — the traceback is returned as the log string.

---

### `logs(container_id, follow=False) → str`

Fetch logs for a detached job.

```python
logs = client.logs("a3f8d0e12b9c")
print(logs)
```

Pass `follow=True` to stream until the container exits (blocks until done).

---

## Error handling

All non-2xx responses raise `caas.CaasError` with the detail message from the dispatcher:

```python
from caas import CaasClient, CaasError

try:
    client.execute(image="nonexistent:image", detach=False)
except CaasError as e:
    print(f"Dispatcher error: {e}")
```
