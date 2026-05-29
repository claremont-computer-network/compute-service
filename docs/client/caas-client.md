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
| `timeout` | `float` | Read timeout in seconds for blocking requests. Connect/write/pool timeouts stay at httpx defaults (5s). Default: `60.0`. Increase for long-running jobs. |

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
| `shm_size` | `str \| None` | `/dev/shm` size, e.g. `"2g"`. |
| `ipc_mode` | `str \| None` | IPC namespace, e.g. `"host"`. |

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
| `shm_size` | `str \| None` | `/dev/shm` size. |
| `ipc_mode` | `str \| None` | IPC namespace. |
| `verbose` | `bool` | Include stderr in output. Default: `False`. |
| `suppress_entrypoint` | `bool \| None` | Bypass container ENTRYPOINT. Auto-enabled for `nvcr.io/*` images. |

Always synchronous. Raises `CaasError` for HTTP errors; a non-zero exit code is not raised — the traceback is returned as the log string.

If `verbose=True` or the job exits non-zero, the full merged logs (stdout + stderr) are returned so tracebacks are visible.

---

### `logs(container_id, follow=False) → str`

Fetch logs for a detached job.

```python
logs = client.logs("a3f8d0e12b9c")
print(logs)
```

Pass `follow=True` to stream until the container exits (blocks until done).

---

### `jobs(state=None) → list`

Return all jobs currently tracked by the dispatcher.

```python
# Without filter — uses the legacy /v1/jobs endpoint
all_jobs = client.jobs()
for j in all_jobs:
    print(j["job_id"], j["status"], j.get("resources"))

# Filtered — uses /api/jobs?state=running
running_jobs = client.jobs(state="running")
```

**Parameters**

| Parameter | Type | Description |
|-----------|------|-------------|
| `state` | `str \| None` | Optional filter: `"running"`, `"stopped"`, or `"*"`. Defaults to `None` (no filtering, uses `/v1/jobs`). |

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

### `deployment_status(job_id) → dict`

Check the outcome of a deployment — useful for CI systems or polling.

```python
result = client.deployment_status("a3f8d0e12b9c")
print(result["success"])    # True/False
print(result["exit_code"])  # 0 or non-zero
print(result["message"])    # e.g. "Job completed successfully"
```

Returns the job's status, exit code, and a human-readable success/failure label. Raises `CaasError` with HTTP 404 if the job is not found.

---

## Extension API methods

These methods call the extension endpoints (`/api/*`) introduced in PR #32.

### Template methods

#### `templates_list() → list`

```python
templates = client.templates_list()
for t in templates:
    print(t["id"], t["name"], t["image"])
```

---

#### `templates_upsert(name=None, image=None, cmd=None, env=None, volumes=None, gpu=None, id=None) → dict`

Create or update a job template. If `id` is provided and matches an existing template, only the provided fields are updated. Otherwise a new template is created.

```python
# Create a new template
tpl = client.templates_upsert(
    name="training",
    image="pytorch/pytorch:2.3.0",
    cmd=["python", "train.py"],
    env={"EPOCHS": "10"},
)
print(tpl["id"])  # "tpl_abc123"

# Update only the image field
updated = client.templates_upsert(
    id=tpl["id"],
    image="pytorch/pytorch:2.4.0",
)
```

**Parameters**

| Parameter | Type | Description |
|-----------|------|-------------|
| `name` | `str \| None` | Template name. |
| `image` | `str \| None` | Docker image. |
| `cmd` | `str \| list \| None` | Command. |
| `env` | `dict \| None` | Environment variables. |
| `volumes` | `list \| None` | Volume specs. |
| `gpu` | `dict \| None` | GPU request. |
| `id` | `str \| None` | Template ID to update (omit to create new). |

---

#### `templates_delete(template_id) → dict`

```python
client.templates_delete("tpl_abc123")
# {"deleted": "tpl_abc123"}
```

---

### File browsing

#### `files_list(path="/") → dict`

List files in a mounted directory. Only directories under `ALLOWED_HOST_DIRS` may be browsed.

```python
result = client.files_list("/mnt/datasets")
print(result["path"])          # "/mnt/datasets"
for entry in result["entries"]:
    print(entry["name"], entry["is_dir"], entry["size"])
```

Returns `{"path": ..., "entries": [...]}` where each entry has `name`, `permissions`, `size`, `modified`, `is_dir`.

---

### Schedule methods

#### `schedules_list() → list`

```python
schedules = client.schedules_list()
for s in schedules:
    print(s["id"], s["status"], s["delay_seconds"])
```

---

#### `schedules_upsert(template_id=None, delay_seconds=60, image=None, cmd=None, env=None, volumes=None, gpu=None) → dict`

Create a schedule to trigger a job (optionally after a delay). Set `delay_seconds=0` for immediate execution.

```python
# Immediate execution with inline fields
sc = client.schedules_upsert(
    image="python:3.12-slim",
    cmd=["python", "report.py"],
    delay_seconds=0,
)

# Run a template every day
client.schedules_upsert(
    template_id="tpl_abc",
    delay_seconds=86400,
)
```

---

#### `schedule_cancel(schedule_id) → dict`

```python
client.schedule_cancel("sch_abc123")
# {"cancelled": "sch_abc123"}
```

---

### Staging methods

#### `staging_list() → list`

```python
for s in client.staging_list():
    print(s["id"], s["name"], s["host_path"], s["dest_path"])
```

---

#### `staging_create(name, host_path, dest_path=None) → dict`

Create a staging area — a named reference to a host path mount that can be reused across jobs.

```python
stg = client.staging_create(
    name="outputs",
    host_path="/mnt/datasets",
    dest_path="/data",
)
print(stg["id"])  # "stg_abc123"
```

**Parameters**

| Parameter | Type | Description |
|-----------|------|-------------|
| `name` | `str` | Name for this staging area. |
| `host_path` | `str` | Absolute path on the host (must be in `ALLOWED_HOST_DIRS`). |
| `dest_path` | `str \| None` | Destination path inside container. Defaults to `host_path`. |

---

#### `staging_delete(staging_id) → dict`

```python
client.staging_delete("stg_abc123")
# {"deleted": "stg_abc123"}
```

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

### Timeout handling

When a request does not respond within the configured timeout, `CaasTimeoutError` is raised. This is a subclass of `CaasError`.

```python
from caas import CaasClient, CaasTimeoutError

try:
    logs = client.logs("a3f8d0e12b9c", follow=True)
except CaasTimeoutError as e:
    print(f"Timed out — the job may still be running:\n{e}")
```

Increase the timeout at construction time:

```python
client = CaasClient(host="http://192.168.1.50:8000", timeout=300)