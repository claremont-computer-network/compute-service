# Troubleshooting

A catalogue of errors encountered during provisioning and operation, with step-by-step fixes.

---

## Provisioning errors

### `Permission denied (publickey,password)`

Ansible cannot connect to the remote machine.

**1. Verify SSH works at all**

```bash
ssh -p <port> <user>@<host>
```

If this fails, fix SSH before touching Ansible.

**2. Provisioning the machine you're sitting at**

Set `ansible_connection: local` in `inventory.yml` — no SSH is involved:

```yaml
compute-node-1:
  ansible_host: localhost
  ansible_connection: local
  ansible_user: youruser
```

**3. SSH works with a password but not a key**

Generate a new key and copy it to the remote (one-time):

```bash
ssh-keygen -t ed25519 -f ~/.ssh/id_ed25519_caas -N ""
ssh-copy-id -i ~/.ssh/id_ed25519_caas -p <port> <user>@<host>
```

Then set `ansible_ssh_private_key_file: ~/.ssh/id_ed25519_caas` in `inventory.yml`.

**4. Key exists but the agent refuses to use it**

The key has a passphrase and the agent does not have it loaded:

```bash
ssh-add ~/.ssh/id_ed25519_caas
```

**5. Wrong username**

The username in `inventory.yml` must match what SSH accepts:

```bash
ssh -v -p <port> <user>@<host> 2>&1 | grep "Authentications"
```

---

### `sshpass: not found`

Ansible requires `sshpass` for password-authenticated SSH:

```bash
sudo apt install sshpass
```

---

### Missing sudo password

Ansible connected but cannot run privileged tasks. Pass the sudo password:

```bash
./scripts/bootstrap.sh --ask-become-pass
```

---

### `[WARNING]: found a duplicate dict key (children)`

`inventory.yml` has the same key written twice. The file must have a single `children:` block:

```yaml
all:
  children:
    compute_nodes:
      hosts:
        compute-node-1:
          ansible_host: 192.168.1.101
          ansible_user: youruser
```

---

### `caas_user is undefined`

`ansible/group_vars/all.yml` is missing or does not contain `caas_user`. Create it from the example:

```bash
mkdir -p ansible/group_vars
cp ansible/vars.example.yml ansible/group_vars/all.yml
```

Then edit it and set `caas_user` to the Linux user that will run the dispatcher.

---

### Mount task fails — no NAS or external storage

If you do not have an NFS share or external drive, disable the mount tasks:

```yaml
# ansible/group_vars/all.yml
caas_data_mount_enabled: false
```

The dispatcher does not require a mount. Volume mounts in jobs can use any local path allowed by `ALLOWED_HOST_DIRS`.

---

## Dispatcher errors

### `Permission denied: /var/run/docker.sock`

The dispatcher container cannot reach the Docker daemon. The `docker-compose.yml` in this repo already sets `user: root` to fix this, but if you see it after a manual edit or upgrade, check that `user: root` is still present in the `dispatcher` service.

---

### `Invalid API Key` (HTTP 401) — even with the correct key

**Cause**: `.env` was placed in the repo root instead of `dispatcher/`. Docker Compose only reads
`.env` from the directory it is invoked from.

```bash
cp .env dispatcher/.env
cd dispatcher && docker compose restart
```

**Cause 2**: `DISPATCHER_API_KEY=` is set to an empty string in `.env`. An empty value disables authentication, but only if the code treats it as unset. Make sure the key is a non-empty string.

---

### Stale API key cached in the notebook kernel

If you changed the key on the dispatcher but the notebook still gets 401:

```python
import os
os.environ.pop("DISPATCHER_API_KEY", None)
os.environ["DISPATCHER_API_KEY"] = "new-correct-key"
```

Or restart the kernel entirely.

---

### `Connection refused` / `httpx.ConnectError`

The dispatcher isn't running. On the remote machine:

```bash
cd compute-service/dispatcher
docker compose up -d
```

Check logs if it fails to start:

```bash
docker compose logs dispatcher
```

---

### Cell returns a traceback instead of output

This is normal behaviour — the dispatcher returns the traceback as the log string rather than raising an HTTP error. Check the content of `result["logs"]` or the inline output in the notebook for the actual Python error.

---

## GPU errors

### `RuntimeError: No CUDA GPUs are available`

The container started but couldn't see the GPU:

1. Confirm you passed `--gpu all` (or a device list) to `%%dispatch`.
2. Confirm `nvidia-container-toolkit` is installed: `nvidia-ctk --version` on the remote machine.
3. Confirm Docker is using the NVIDIA runtime: `docker info | grep -i runtime`.
4. Re-run the Ansible playbook with `caas_gpu_enabled: true`.

### `exec: "python": executable file not found in $PATH`

The image you passed to `%%dispatch` (or `execute_cell`) does not have Python installed.
Common culprit: `nvidia/cuda:*-base` images, which contain CUDA libraries and `nvidia-smi`
but no Python interpreter.

- For `%%dispatch` cells, use an image that includes Python, e.g.
  `pytorch/pytorch:2.3.0-cuda12.1-cudnn8-runtime`.
- To run shell commands like `nvidia-smi`, use `CaasClient.execute` with an explicit `cmd`
  instead of `%%dispatch`:

```python
from caas import CaasClient
import os

with CaasClient(host=os.environ["CAAS_HOST"], api_key=os.environ.get("DISPATCHER_API_KEY")) as c:
    result = c.execute(
        image="nvidia/cuda:12.3.2-base-ubuntu22.04",
        cmd=["nvidia-smi"],
        gpu={"device_ids": "all"},
        detach=False,
    )
    print(result["logs"])
```

### `could not select device driver "nvidia"`

The NVIDIA container runtime is not registered with Docker:

```bash
sudo nvidia-ctk runtime configure --runtime=docker
sudo systemctl restart docker
```

Then restart the dispatcher:

```bash
cd compute-service/dispatcher && docker compose restart
```
