# Provisioning

Provisioning prepares a machine to run the dispatcher. It is a one-time operation per machine, safe to re-run.

---

## What the playbook does

1. Installs Docker and ensures the daemon is running.
2. Creates the `caas_user` system user (if it doesn't already exist) and adds them to the `docker` group.
3. Optionally mounts an NFS share or external drive to a configured local path (`caas_data_mount_enabled`).
4. Optionally installs NVIDIA drivers and `nvidia-container-toolkit` (`caas_gpu_enabled`).

---

## Before you start

You need three files (all gitignored — create from the examples):

```bash
cp ansible/inventory.example.yml inventory.yml
cp ansible/vars.example.yml ansible/group_vars/all.yml
```

Edit both files. Minimum viable `inventory.yml`:

```yaml
all:
  children:
    compute_nodes:
      hosts:
        compute-node-1:
          ansible_host: 192.168.1.101
          ansible_user: youruser
          ansible_ssh_private_key_file: ~/.ssh/id_ed25519_caas
```

Minimum viable `ansible/group_vars/all.yml`:

```yaml
caas_user: youruser
caas_data_mount_enabled: false
caas_gpu_enabled: false
```

---

## Running the provisioner

```bash
./scripts/bootstrap.sh
```

All arguments are passed straight through to `ansible-playbook`:

```bash
# Password-authenticated SSH
./scripts/bootstrap.sh --ask-pass

# SSH + sudo password required
./scripts/bootstrap.sh --ask-pass --ask-become-pass

# Non-standard SSH port
./scripts/bootstrap.sh -e "ansible_port=2222"

# Specific private key
./scripts/bootstrap.sh --private-key ~/.ssh/id_ed25519_caas
```

You can also set `ansible_port` and `ansible_ssh_private_key_file` in `inventory.yml` to
avoid typing them every time.

---

## Provisioning the machine you're on

If you want to provision the same machine you're running Ansible on (no SSH), set
`ansible_connection: local` in `inventory.yml`:

```yaml
all:
  children:
    compute_nodes:
      hosts:
        compute-node-1:
          ansible_host: localhost
          ansible_connection: local
          ansible_user: youruser
```

---

## Re-running the playbook

The playbook is fully idempotent. Run it again any time:

- To pick up `ansible/group_vars/all.yml` changes (e.g. enabling GPU support later).
- After the machine is rebuilt or reimaged.
- To verify the machine is still in the expected state.

---

## After provisioning: start the dispatcher

SSH into the remote machine and start the service:

```bash
git clone https://github.com/claremont-computer-network/compute-service
cd compute-service/dispatcher
cp ../.env.example .env    # fill in DISPATCHER_API_KEY and ALLOWED_HOST_DIRS
docker compose up -d --build
```

Verify:

```bash
curl http://localhost:8000/health
# {"status":"ok"}
```

---

## Persistent service (systemd)

To start the dispatcher automatically on boot, use the example systemd unit:

```bash
sudo cp dispatcher/compute-service.service.example /etc/systemd/system/compute-service.service
# edit the file to set the correct WorkingDirectory and User
sudo systemctl daemon-reload
sudo systemctl enable --now compute-service
```

---

## Enabling GPU support

Set `caas_gpu_enabled: true` in `ansible/group_vars/all.yml` and re-run the playbook:

```bash
./scripts/bootstrap.sh
```

The playbook installs the NVIDIA driver and `nvidia-container-toolkit` and configures Docker to use the NVIDIA runtime. After it completes, restart the dispatcher:

```bash
docker compose restart
```

Verify:

```bash
docker run --rm --gpus all nvidia/cuda:12.3.2-base-ubuntu22.04 nvidia-smi
```
