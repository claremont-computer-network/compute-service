# compute-service

Self-hosted compute-as-a-service — run containerised workloads on a remote machine via a single HTTP request.

📖 **[Full documentation](https://claremont-computer-network.github.io/compute-service)**

---

## Motivation

Most compute jobs end up tied to a specific machine by implicit assumptions: hardcoded paths, manual SSH sessions, or ad-hoc scripts only one person knows how to run.

This project treats a remote machine as a generic compute node. Provision it once with Ansible, then trigger any containerised workload from anywhere with a single HTTP request. The machine does not need to know what the job does. The job does not need to know what machine it runs on.

---

## How it works

**Provisioner (Ansible)** — prepares the host: installs Docker, creates the service user, mounts external storage. Run once; safe to re-run.

**Dispatcher (FastAPI)** — a lightweight HTTP API on the compute node. Accepts a Docker image, a command, env vars, and optional volume mappings, and executes the job via the Docker daemon.

**Jobs (Docker)** — every job is a black box. The dispatcher only needs the image, the command, and the data path.

---

## Repository layout

```
ansible/          Ansible playbook and example configuration
clients/
  python/         Installable Python client and %%dispatch IPython magic
dispatcher/       FastAPI application, Dockerfile, and Compose file
docker/           Pre-built custom images (scientific, sagemath)
scripts/          bootstrap.sh (provisioner), smoke_test.py (end-to-end)
docs/             MkDocs documentation site
.env.example      Template for required environment variables
```

Secrets and machine-specific config (`inventory.yml`, `group_vars/all.yml`, `.env`) are gitignored. The repo only contains templates.

---

## Quickstart

### 1. Clone and configure

```bash
git clone https://github.com/claremont-computer-network/compute-service
cd compute-service
cp ansible/inventory.example.yml inventory.yml
cp ansible/vars.example.yml ansible/group_vars/all.yml
cp .env.example .env
```

Edit `inventory.yml` to point at your remote machine, `group_vars/all.yml` to set the service user and data mount, and `.env` to set a strong `DISPATCHER_API_KEY`.

### 2. Provision

```bash
./scripts/bootstrap.sh
```

Installs Docker on the remote host, creates the service user, and mounts any configured storage. Safe to re-run.

### 3. Start the dispatcher

On the remote machine, from `dispatcher/`:

```bash
docker compose up -d --build
```

### 4. Submit a job

```bash
curl -X POST http://<host>:8000/v1/execute \
  -H "Content-Type: application/json" \
  -H "X-API-Key: <your-key>" \
  -d '{"image": "alpine:3.18", "cmd": ["echo", "hello"], "detach": false}'
```

→ [Full quickstart](https://claremont-computer-network.github.io/compute-service/quickstart/)

---

## Python client & notebook magic

```bash
pip install "git+https://github.com/claremont-computer-network/compute-service.git#subdirectory=clients/python"
```

```python
from caas import CaasClient
client = CaasClient(host="http://192.168.1.50:8000", api_key="your-key")
result = client.execute(image="python:3.12-slim", cmd=["python", "-c", "print(2**10)"], detach=False)
print(result["logs"])   # "1024\n"
```

The `%%dispatch` IPython magic sends a notebook cell to the remote dispatcher and prints output inline:

```python
%%dispatch --image pytorch/pytorch:2.3.0-cuda12.1-cudnn8-runtime --gpu all
import torch
print(torch.cuda.is_available())
```

→ [CaasClient reference](https://claremont-computer-network.github.io/compute-service/client/caas-client/)
→ [%%dispatch magic](https://claremont-computer-network.github.io/compute-service/client/dispatch-magic/)

---

## Development

```bash
uv pip install -r dispatcher/requirements-dev.txt
uv pip install -e clients/python
uv run pytest -v
uv run python scripts/smoke_test.py   # local mock, no Docker needed
```

→ [Configuration reference](https://claremont-computer-network.github.io/compute-service/configuration/)
→ [API reference](https://claremont-computer-network.github.io/compute-service/api-reference/)
→ [Operations & troubleshooting](https://claremont-computer-network.github.io/compute-service/operations/troubleshooting/)
