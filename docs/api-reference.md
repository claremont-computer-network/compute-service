# API Reference

The dispatcher exposes a small, stable HTTP API. All endpoints accept and return JSON.

---

## Authentication

When `DISPATCHER_API_KEY` is set on the dispatcher, every request must include the key in the `X-API-Key` header:

```
X-API-Key: your-secret-key
```

Requests without a valid key return HTTP 401. If `DISPATCHER_API_KEY` is unset or empty, authentication is disabled.

---

## Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/health` | Liveness check |
| `POST` | `/v1/execute` | Submit a container job |
| `POST` | `/v1/execute/cell` | Run a Python code string and return output |
| `GET` | `/v1/logs/{container_id}` | Fetch logs for a detached job |

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

**Response (detached)**

```json
{
  "status": "running",
  "container_id": "a3f8d0e12b9c"
}
```

**Response (synchronous, `"detach": false`)**

```json
{
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
| `400` | Bad request — e.g. volume path not in `ALLOWED_HOST_DIRS` |
| `401` | Missing or invalid `X-API-Key` |
| `422` | Validation error — request body fails schema checks |
| `500` | Dispatcher-side error — Docker daemon unreachable, image pull failed, etc. |

Error bodies follow FastAPI's standard format:

```json
{"detail": "Host path not allowed: /etc"}
```
