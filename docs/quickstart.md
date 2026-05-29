# Quickstart

Get from zero to running your first remote cell in under an hour.

---

## Prerequisites

- **Local machine**: Python 3.10+, `uv` or `pip`, Ansible, SSH access to the remote machine.
- **Remote machine**: A Linux box you control (bare metal, VM, or cloud instance). Docker will be installed by the provisioner.

---

## 1. Clone and configure

```bash
git clone https://github.com/claremont-computer-network/compute-service
cd compute-service
```

Copy the example files and fill them in:

```bash
cp ansible/inventory.example.yml inventory.yml
cp ansible/vars.example.yml ansible/group_vars/all.yml
cp .env.example dispatcher/.env
```

### `inventory.yml`

Point at your remote machine:

```yaml
all:
  children:
    compute_nodes:
      hosts:
        compute-node-1:
          ansible_host: 192.168.1.101   # or a public IP / hostname
          ansible_port: 22              # change if using a non-standard SSH port
          ansible_user: youruser
          ansible_ssh_private_key_file: ~/.ssh/id_ed25519_caas
```

### `ansible/group_vars/all.yml`

Set the service user and storage options:

```yaml
caas_user: youruser           # Linux user that will own and run the dispatcher
caas_data_mount_enabled: false  # set to true only if you have an NFS/external drive to mount
caas_gpu_enabled: false         # set to true to install the NVIDIA container toolkit
```

### `dispatcher/.env`

```bash
DISPATCHER_API_KEY=change-me-to-something-strong
ALLOWED_HOST_DIRS=/home/youruser/data,/mnt/datasets
```

!!! warning "Key placement"
    The `.env` file must live in the `dispatcher/` directory so Docker Compose picks it up.
    Copy with `cp .env.example dispatcher/.env`, not to the repo root.

---

## 2. Provision the remote machine

Requires Ansible installed locally and SSH access to the remote machine.

```bash
./scripts/bootstrap.sh
```

The script passes any extra arguments through to `ansible-playbook`, so all standard flags work:

```bash
./scripts/bootstrap.sh --ask-pass                    # SSH password prompt
./scripts/bootstrap.sh --ask-pass --ask-become-pass  # SSH + sudo password
./scripts/bootstrap.sh --private-key ~/.ssh/my_key   # key at a custom path
./scripts/bootstrap.sh -e "ansible_port=2222"        # non-standard SSH port
```

This installs Docker on the remote host, creates the service user, and optionally mounts the configured storage path. It is safe to run again if anything changes.

!!! tip "Provisioning the machine you're sitting at"
    Set `ansible_connection: local` in `inventory.yml` and omit `ansible_host`. No SSH is involved.

---

## 3. Start the dispatcher

SSH into the remote machine, clone the repo there, and start the service:

```bash
git clone https://github.com/claremont-computer-network/compute-service
cd compute-service/dispatcher
cp ../.env.example .env     # fill this in with your key
docker compose up -d --build
```

Check it's healthy:

```bash
curl http://localhost:8000/health
# {"status":"ok"}
```

---

## 4. Submit your first job

From any machine that can reach the remote host:

```bash
curl -X POST http://<host>:8000/v1/execute \
  -H "Content-Type: application/json" \
  -H "X-API-Key: <your-key>" \
  -d '{
    "image": "python:3.12-slim",
    "cmd": ["python", "-c", "import platform; print(platform.node())"],
    "detach": false
  }'
```

You should see a response like:

```json
{"container_id": null, "status": "exited", "exit_code": 0, "logs": "compute-node-1\n"}
```

---

## 5. Use the notebook client

Install the Python client:

```bash
pip install "git+https://github.com/claremont-computer-network/compute-service.git#subdirectory=clients/python[notebook]"
```

In a Jupyter notebook:

```python
import os
os.environ["CAAS_HOST"]          = "http://192.168.1.101:8000"
os.environ["DISPATCHER_API_KEY"] = "your-secret-key"
os.environ["CAAS_DEFAULT_IMAGE"] = "python:3.12-slim"

from caas import register_magic
register_magic()
```

Then in the next cell:

```python
%%dispatch
import platform
print("running on", platform.node())
```

Output appears inline, as if the cell ran locally.

---

## 6. Use the Web UI

 A lightweight job monitor is served from the dispatcher itself.  
  Open `http://<host>:8000/ui/` in a browser, enter your API key once. The **Monitor** tab shows all known jobs with live CPU/memory sparklines. The **Launch** tab has a "Load Template" dropdown and a "Load Staging Area" selector — pre-populated fields always defer to user input. The **Templates** tab lets you create, edit, and delete job templates that map directly to the job launch form fields. The **Schedules** tab lets you queue delayed or immediate jobs — fill in a template or custom fields and set the delay in seconds. Select a GPU, mount volumes, and watch jobs appear in the Monitor tab and schedules list.

---

## What's next?

- [Configuration reference](configuration.md) — all environment variables and Ansible variables
- [API Reference](api-reference.md) — full endpoint documentation
- [Cookbooks](cookbooks/first-cell.md) — real-world usage patterns
- [Troubleshooting](operations/troubleshooting.md) — common provisioning and runtime errors
