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
| GET | `/v1/logs/{container_id}` | Fetch logs for a detached job; append `?follow=true` to stream |

**Execute request body**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `image` | string | yes | Docker image to run |
| `cmd` | string or list | no | Command to pass to the container |
| `env` | object | no | Environment variables |
| `volumes` | list | no | Host-to-container path mappings (see below) |
| `detach` | boolean | no | If false, block until the container exits and return logs inline. Default: true |

**Volume entry**

| Field | Type | Description |
|-------|------|-------------|
| `host_path` | string | Absolute path on the host (must be within `ALLOWED_HOST_DIRS`) |
| `container_path` | string | Mount point inside the container |
| `mode` | string | `rw` or `ro`. Default: `rw` |

---

## Configuration

All configuration is passed via environment variables, typically via a local `.env` file (see `.env.example`).

| Variable | Description |
|----------|-------------|
| `DISPATCHER_API_KEY` | Secret key required in the `X-API-Key` header. If unset, authentication is disabled — only acceptable for local development. |
| `ALLOWED_HOST_DIRS` | Comma-separated list of host paths that jobs may bind-mount. Requests referencing paths outside this list are rejected with 400. |

---

## Security notes

The dispatcher has access to the Docker socket, which gives it root-equivalent access to the host. Keep this in mind when deciding what network to expose it on. Tailscale or a similar overlay network is a straightforward way to limit access to trusted machines without opening ports to the internet.

Never commit `.env`, `inventory.yml`, or `group_vars/` to version control. The `.gitignore` is configured to prevent this, but the responsibility ultimately lies with the operator.

---

## Development

Install dependencies:

```bash
uv pip install -r requirements-dev.txt
```

Run the test suite:

```bash
uv run pytest -v
```

Run the local smoke test (no Docker daemon required):

```bash
uv run python scripts/smoke_test.py
```
