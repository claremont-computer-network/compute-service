# API Reference

The dispatcher exposes a small, stable HTTP API. Endpoints accept and return JSON, except `GET /v1/logs/{container_id}?follow=true` which streams a `text/plain` response.

---

## Authentication

When `DISPATCHER_API_KEY` is set on the dispatcher, every request must include the key in the `X-API-Key` header:

```
X-API-Key: your-secret-key
```

Requests without a valid key return HTTP 401. If `DISPATCHER_API_KEY` is unset or empty, authentication is disabled.

---

## Endpoints

### Core API (`/v1/*`)

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/health` | Liveness check |
| `POST` | `/v1/execute` | Submit a container job |
| `POST` | `/v1/execute/cell` | Run a Python code string and return output |
| `GET` | `/v1/jobs` | List all tracked jobs |
| `GET` | `/v1/jobs/{job_id}` | Get a single job by ID |
| `DELETE` | `/v1/jobs/{job_id}` | Stop and remove a job |
| `GET` | `/v1/logs/{container_id}` | Fetch logs for a detached job |

### Extension API (`/api/*`)

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/templates` | List job templates |
| `POST` | `/api/templates` | Create or update a job template |
| `DELETE` | `/api/templates/{id}` | Delete a template |
| `GET` | `/api/files` | Browse files on mounted host directories |
| `GET` | `/api/schedule` | List all schedules |
| `POST` | `/api/schedule` | Create a schedule (trigger a job after a delay) |
| `DELETE` | `/api/schedule/{id}` | Cancel a pending schedule |
| `GET` | `/api/staging` | List staging areas |
| `POST` | `/api/staging` | Create a staging area (named mount config) |
| `DELETE` | `/api/staging/{id}` | Remove a staging area |
| `GET` | `/api/jobs?state=X` | List jobs filtered by state |
| `GET` | `/api/deployments/{id}/status` | Check deployment outcome |

---

### `GET /health`

Returns a simple liveness response. Returns HTTP 500 if the Docker daemon is unreachable — the service may start successfully but this endpoint will fail until Docker is available.

**Response**

```json
{"status": "ok"}
```

---

### `POST /v1/execute`

Submit a container job. Jobs can run synchronously (blocking until exit) or detached (returning immediately with a container ID).

**Request body**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `image` | string | ✓ | Docker image to run |
| `cmd` | string \| list | — | Command to pass to the container |
| `env` | object | — | Environment variables (`{"KEY": "VALUE"}`) |
| `volumes` | list of [VolumeSpec](#volumespec) | — | Host paths to bind-mount |
| `detach` | boolean | — | If `false`, block until the container exits and return logs inline. Default: `true`. |
| `gpu` | [GpuRequest](#gpurequest) | — | GPU access request. Requires `nvidia-container-toolkit` on the host. |
| `shm_size` | string | — | Shared memory size, e.g. `"2g"`. |
| `ipc_mode` | string | — | IPC namespace, e.g. `"host"`. |

**Response (detached)**

```json
{
  "job_id": "a3f8d0e12b9c1234",
  "container_id": "a3f8d0e12b9c",
  "status": "running"
}
```

**Response (synchronous, `"detach": false`)**

```json
{
  "container_id": null,
  "status": "exited",
  "exit_code": 0,
  "logs": "hello world\n"
}
```

!!! note "Non-zero exit codes"
    A container that exits with a non-zero code returns HTTP 200 with
    `"exit_code": <n>` and the output in `"logs"`. It is the caller's responsibility to check
    the exit code.

**Example**

```bash
curl -X POST http://192.168.1.50:8000/v1/execute \
  -H "Content-Type: application/json" \
  -H "X-API-Key: your-key" \
  -d '{
    "image": "python:3.12-slim",
    "cmd": ["python", "-c", "print(1+1)"],
    "detach": false
  }'
```

---

### `POST /v1/execute/cell`

Run a Python code string inside a container and return its output. Always synchronous. Used by the `%%dispatch` notebook magic.

**Request body**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `code` | string | ✓ | Python source code. Executed as `python -c <code>`. |
| `image` | string | ✓ | Docker image. Must have `python` available. |
| `env` | object | — | Environment variables |
| `volumes` | list of [VolumeSpec](#volumespec) | — | Host paths to bind-mount |
| `gpu` | [GpuRequest](#gpurequest) | — | GPU access request |
| `shm_size` | string | — | Shared memory size. |
| `ipc_mode` | string | — | IPC namespace. |
| `suppress_entrypoint` | boolean | — | Bypass the container ENTRYPOINT. Auto-enabled for `nvcr.io/*` images. |

**Response**

```json
{
  "status": "exited",
  "exit_code": 0,
  "logs": "hello from the container\n"
}
```

A non-zero exit code (e.g. a traceback) returns HTTP 200 with the traceback in `logs`.

**Example**

```bash
curl -X POST http://192.168.1.50:8000/v1/execute/cell \
  -H "Content-Type: application/json" \
  -H "X-API-Key: your-key" \
  -d '{
    "code": "import platform; print(platform.node())",
    "image": "python:3.12-slim"
  }'
```

---

### `GET /v1/jobs`

Return all jobs that the dispatcher currently knows about (running and recently stopped).
Jobs are tracked in memory and hydrated from Docker on startup; restarting the dispatcher
clears the in-memory store, but containers that are still running are re-discovered automatically.

**Response**

```json
{
  "jobs": [
    {
      "job_id": "a3f8d0e12b9c",
      "container_id": "a3f8d0e12b9cdeadbeef",
      "image": "python:3.12-slim",
      "cmd": ["python", "-c", "print('hello')"],
      "status": "running",
      "submitted_at": "2026-04-12T10:00:00",
      "exit_code": null,
      "resources": {
        "cpu_percent": 12.4,
        "mem_usage_mib": 48.1,
        "mem_limit_mib": 32768.0,
        "mem_percent": 0.15
      }
    }
  ]
}
```

The `resources` field is populated for running containers only; it is `null` for stopped jobs.

---

### `GET /v1/jobs/{job_id}`

Get a single job by ID.

**Response** — same shape as an element of the `"jobs"` array above.

Returns HTTP 404 if the job ID is unknown.

---

### `DELETE /v1/jobs/{job_id}`

Stop a running job. Sends `SIGKILL` to the container and marks it as `stopped`.

**Response**

```json
{"job_id": "a3f8d0e12b9c", "status": "stopped"}
```

Returns HTTP 404 if the job ID is unknown. Returns HTTP 409 if the job has already stopped.

---

### `GET /v1/logs/{container_id}`

Fetch logs for a detached job.

**Query parameters**

| Parameter | Description |
|-----------|-------------|
| `follow` | Set to `true` to stream logs until the container exits. Default: `false`. |

**Example**

```bash
# Fetch accumulated logs
curl http://192.168.1.50:8000/v1/logs/a3f8d0e12b9c \
  -H "X-API-Key: your-key"

# Stream logs live
curl "http://192.168.1.50:8000/v1/logs/a3f8d0e12b9c?follow=true" \
  -H "X-API-Key: your-key"
```

---

## Extension API reference

### `GET /api/templates`

List all job templates. Templates are reusable job configurations stored on the dispatcher.

**Response**

```json
[
  {
    "id": "tpl_abc123",
    "name": "training",
    "image": "pytorch/pytorch:2.3.0-cuda12.1-cudnn8-runtime",
    "cmd": ["python", "train.py"],
    "env": {"EPOCHS": "10"},
    "volumes": [{"host_path": "/mnt/data", "container_path": "/data"}],
    "gpu": {"device_ids": "all"},
    "created_at": "2025-01-01T00:00:00+00:00",
    "modified_at": "2025-01-01T00:00:00+00:00"
  }
]
```

---

### `POST /api/templates`

Create or update a job template. If `id` is provided and matches an existing template, it is updated (only the provided fields). Otherwise a new template is created.

**Request body**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `name` | string | — | Template name. Default: `""`. |
| `image` | string | — | Docker image. |
| `cmd` | string \| list | — | Command. |
| `env` | object | — | Environment variables. |
| `volumes` | list of [VolumeSpec](#volumespec) | — | Host paths to bind-mount. |
| `gpu` | [GpuRequest](#gpurequest) | — | GPU request. |
| `id` | string | — | Template ID to update (omit to create new). |

**Response** — the saved template dict with `id`, `created_at`, `modified_at`.

- Returns HTTP 201 on creation.
- Returns HTTP 404 on update when the template ID is not found.

---

### `DELETE /api/templates/{template_id}`

Delete a template by ID.

**Response**

```json
{"deleted": "tpl_abc123"}
```

Returns HTTP 404 if the template is not found.

---

### `GET /api/files`

List files in a mounted directory. Only directories under `ALLOWED_HOST_DIRS` may be browsed.

**Query parameters**

| Parameter | Description |
|-----------|-------------|
| `path` | Host path to list. Default: `/`. |

**Response**

```json
{
  "path": "/mnt/datasets",
  "entries": [
    {
      "name": "train.pt",
      "permissions": "644",
      "size": 1048576,
      "modified": "2025-01-01T00:00:00+00:00",
      "is_dir": false
    },
    {
      "name": "logs/",
      "permissions": "755",
      "size": 4096,
      "modified": "2025-01-01T00:00:00+00:00",
      "is_dir": true
    }
  ]
}
```

| Field | Description |
|-------|-------------|
| `name` | File name |
| `permissions` | Octal permissions string |
| `size` | File size in bytes |
| `modified` | ISO 8601 modification timestamp |
| `is_dir` | Whether it is a directory |

Returns HTTP 400 if the path is not in `ALLOWED_HOST_DIRS`, 404 if not found, 403 if permission denied.

---

### `GET /api/schedule`

List all schedules.

**Response**

```json
[
  {
    "id": "sch_abc123",
    "name": "Schedule sch_abc123",
    "status": "pending",
    "template_id": "tpl_abc",
    "delay_seconds": 86400,
    "created_at": "2025-01-01T00:00:00+00:00",
    "triggered_at": null,
    "jobs": []
  }
]
```

| Field | Description |
|-------|-------------|
| `id` | Schedule ID |
| `name` | Human-readable name |
| `status` | `pending`, `active`, `cancelled`, or `error` |
| `template_id` | Referenced template ID (if any) |
| `delay_seconds` | Seconds to wait before triggering |
| `created_at` | Creation timestamp |
| `triggered_at` | When the job was triggered (null if deferred) |
| `jobs` | List of job records triggered by this schedule |

---

### `POST /api/schedule`

Create a schedule to trigger a job, optionally after a delay.

**Request body**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `template_id` | string | — | Referenced template ID. Mutually exclusive with inline fields. |
| `delay_seconds` | int | — | Seconds to wait before triggering. Set to `0` for immediate execution. Default: `60`. |
| `image` | string | — | Docker image (inline, without template). |
| `cmd` | string \| list | — | Command (inline). |
| `env` | object | — | Environment variables (inline). |
| `volumes` | list | — | Volumes (inline). |
| `gpu` | object | — | GPU request (inline). |

**Response** — the new schedule dict with `id`, `created_at`, `status`.

- Returns HTTP 201 on creation.
- Returns HTTP 404 if the referenced template was not found.

If `delay_seconds` is `0` the job executes immediately. If `delay_seconds` is `> 0` the status changes to `active` when the delay elapses.

---

### `DELETE /api/schedule/{schedule_id}`

Cancel a pending schedule.

**Response**

```json
{"cancelled": "sch_abc123"}
```

Returns HTTP 404 if the schedule is not found.

---

### `GET /api/staging`

List all staging areas — named references to host path mounts that can be reused across jobs.

**Response**

```json
[
  {
    "id": "stg_abc123",
    "name": "outputs",
    "host_path": "/mnt/datasets",
    "dest_path": "/data",
    "created_at": "2025-01-01T00:00:00+00:00",
    "description": ""
  }
]
```

---

### `POST /api/staging`

Create a staging area — a named reference to a host path mount.

**Request body**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `name` | string | ✓ | Name for this staging area. |
| `host_path` | string | ✓ | Absolute path on the host. Must be within `ALLOWED_HOST_DIRS`. |
| `dest_path` | string | — | Destination path inside container. Defaults to `host_path`. |

**Response** — the new staging area dict with `id`, `created_at`.

Returns HTTP 400 if `host_path` is not allowed.

---

### `DELETE /api/staging/{staging_id}`

Remove a staging area.

**Response**

```json
{"deleted": "stg_abc123"}
```

Returns HTTP 404 if the staging area is not found.

---

### `GET /api/jobs?state=X`

List jobs filtered by status. Equivalent to `/v1/jobs` but with an optional `state` query parameter for filtering.

**Query parameters**

| Parameter | Description |
|-----------|-------------|
| `state` | `running`, `stopped`, or `*` for all. Default: `*`. |

**Response** — list of job records (same shape as `GET /v1/jobs` elements).

---

### `GET /api/deployments/{job_id}/status`

Check the outcome of a deployment — useful for CI systems that need to verify whether a job succeeded.

**Response**

```json
{
  "job_id": "a3f8d0e12b9c",
  "status": "stopped",
  "exit_code": 0,
  "success": true,
  "message": "Job completed successfully"
}
```

Fields:

| Field | Description |
|-------|-------------|
| `job_id` | Job ID |
| `status` | `running`, `stopped`, `exited`, `dead` |
| `exit_code` | Exit code (integer or null while running) |
| `success` | Whether the exit code was 0 |
| `message` | Human-readable status |

Returns HTTP 404 if no job is found for the given ID.

---

## Schema reference

### VolumeSpec

| Field | Type | Description |
|-------|------|-------------|
| `host_path` | string | Absolute path on the host. Must be within `ALLOWED_HOST_DIRS`. |
| `container_path` | string | Mount point inside the container. |
| `mode` | string | `rw` (read-write) or `ro` (read-only). Default: `rw`. |

```json
{
  "host_path": "/mnt/datasets",
  "container_path": "/data",
  "mode": "ro"
}
```

### GpuRequest

| Field | Type | Description |
|-------|------|-------------|
| `device_ids` | `"all"` \| list of strings | `"all"` exposes every GPU; a list like `["0", "1"]` exposes specific devices. |
| `capabilities` | list of strings | Driver capabilities forwarded to the NVIDIA runtime. Default: `["gpu"]`. |

```json
{"device_ids": "all"}
{"device_ids": ["0", "1"], "capabilities": ["gpu", "utility"]}
```

---

## Error responses

| HTTP Status | Meaning |
|-------------|---------|
| `400` | Bad request — e.g. volume path not in `ALLOWED_HOST_DIRS`, invalid staging path |
| `401` | Missing or invalid `X-API-Key` |
| `404` | Resource (job, template, schedule, staging area, path) not found |
| `409` | Job already stopped |
| `422` | Validation error — request body fails schema checks |
| `500` | Dispatcher-side error — Docker daemon unreachable, image pull failed, etc. |
| `503` | No resource slots available — all GPU or CPU slots were busy for the full `QUEUE_TIMEOUT_SECS` window. Retry after a running job finishes. |

Error bodies follow FastAPI's standard format:

```json
{"detail": "Host path not allowed: /etc"}
```
