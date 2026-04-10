# Configuration

All runtime configuration is supplied via environment variables. Machine-specific provisioning config is supplied via Ansible variables.

---

## Dispatcher environment variables

Stored in `dispatcher/.env` (loaded automatically by Docker Compose and by `python-dotenv` if running outside Docker).

| Variable | Required | Description |
|----------|----------|-------------|
| `DISPATCHER_API_KEY` | No | Secret key required in the `X-API-Key` request header. If unset or empty, the dispatcher accepts all requests without authentication — only acceptable on a trusted private network. |
| `ALLOWED_HOST_DIRS` | No | Comma-separated list of absolute host paths that jobs may bind-mount. Requests referencing paths outside this list are rejected with HTTP 400. If unset, no bind-mounts are allowed. |

**Example `dispatcher/.env`:**

```bash
DISPATCHER_API_KEY=a-strong-random-secret-here
ALLOWED_HOST_DIRS=/home/eriksson/data,/mnt/datasets
```

!!! tip "Empty string vs unset"
    Setting `DISPATCHER_API_KEY=` (empty value) is equivalent to leaving it unset — authentication
    is disabled. Make sure the value you set is non-empty if you intend to require a key.

---

## Client environment variables

Read by `register_magic()` and available to callers of `CaasClient`.

| Variable | Description |
|----------|-------------|
| `CAAS_HOST` | Full base URL of the dispatcher, e.g. `http://192.168.1.50:8000` |
| `DISPATCHER_API_KEY` | API key sent in `X-API-Key`. Must match the value set on the dispatcher. |
| `CAAS_DEFAULT_IMAGE` | Docker image used by `%%dispatch` when no `--image` flag is given. |

---

## Ansible variables

Stored in `ansible/group_vars/all.yml` (gitignored — create from `ansible/vars.example.yml`).

| Variable | Default | Description |
|----------|---------|-------------|
| `caas_user` | — | **Required.** Linux user that will own the dispatcher files and be added to the `docker` group. |
| `caas_data_mount_enabled` | `false` | Set to `true` to mount an NFS share or external drive. Requires `caas_data_nfs_server`, `caas_data_nfs_export`, and `caas_data_local_path`. |
| `caas_data_nfs_server` | — | NFS server hostname or IP. Only used when `caas_data_mount_enabled: true`. |
| `caas_data_nfs_export` | — | NFS export path on the server, e.g. `/exports/datasets`. |
| `caas_data_local_path` | — | Local mount point on the host, e.g. `/mnt/datasets`. |
| `caas_gpu_enabled` | `false` | Set to `true` to install NVIDIA drivers and `nvidia-container-toolkit`. Requires a compatible NVIDIA GPU. |

**Minimal `ansible/group_vars/all.yml` (no NAS, no GPU):**

```yaml
caas_user: youruser
caas_data_mount_enabled: false
caas_gpu_enabled: false
```

---

## Inventory variables

Stored in `inventory.yml` (gitignored — create from `ansible/inventory.example.yml`).

Standard Ansible inventory variables. The most commonly needed ones:

| Variable | Description |
|----------|-------------|
| `ansible_host` | IP address or hostname of the remote machine |
| `ansible_port` | SSH port. Defaults to 22. |
| `ansible_user` | SSH login user |
| `ansible_ssh_private_key_file` | Path to the SSH private key on your local machine |
| `ansible_connection` | Set to `local` when provisioning the machine you're running Ansible on |

**Example `inventory.yml`:**

```yaml
all:
  children:
    compute_nodes:
      hosts:
        compute-node-1:
          ansible_host: 192.168.1.101
          ansible_port: 22
          ansible_user: youruser
          ansible_ssh_private_key_file: ~/.ssh/id_ed25519_caas
```
