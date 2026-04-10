# Security

Practical security notes for operating compute-service in a real environment.

---

## The Docker socket is root-equivalent

The dispatcher runs with access to `/var/run/docker.sock`. Any process that can send requests to the dispatcher can, in principle, mount the host filesystem, run privileged containers, or escalate to root. This is inherent to the Docker architecture.

**Mitigation**: limit network access. Do not expose port 8000 to the public internet.

---

## Network isolation options

### Tailscale (recommended)

[Tailscale](https://tailscale.com) creates a zero-config overlay network between trusted machines. The dispatcher only needs to be reachable on the Tailscale interface — port 8000 is never exposed publicly.

```bash
# On the remote machine
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up

# In dispatcher/.env, restrict to the Tailscale IP
# (no extra config needed — just don't forward port 8000 in your router)
```

From your notebook machine, use the Tailscale IP:

```python
os.environ["CAAS_HOST"] = "http://100.x.y.z:8000"
```

### SSH tunnel

If Tailscale isn't an option, an SSH tunnel works well:

```bash
ssh -L 8000:localhost:8000 youruser@remote-machine
```

Then connect locally:

```python
os.environ["CAAS_HOST"] = "http://localhost:8000"
```

### Firewall rule

If the remote machine is on a known private network and you want a simpler setup:

```bash
# Allow only your local subnet
sudo ufw allow from 192.168.1.0/24 to any port 8000
sudo ufw deny 8000
```

---

## API key management

- Use a randomly generated key with at least 32 bytes of entropy:
  ```bash
  python -c "import secrets; print(secrets.token_hex(32))"
  ```
- Never commit the key to version control. The `.gitignore` blocks `.env` and `group_vars/` by default.
- Rotate the key by updating `dispatcher/.env` on the remote machine and restarting the dispatcher, then updating the key in your notebook environment.

---

## `ALLOWED_HOST_DIRS`

The dispatcher enforces an explicit allowlist of host paths that may be bind-mounted into jobs. Requests referencing paths outside the list are rejected with HTTP 400.

- Set this to only the directories jobs actually need.
- An empty `ALLOWED_HOST_DIRS` means no bind-mounts are allowed at all.
- Never add `/` or `/etc` to this list.

---

## Secrets in cells

Don't put secrets directly in `%%dispatch` cells — they'll appear in the notebook file and any output logs.

Pass secrets as environment variables via the `env` field:

```python
from caas import CaasClient
import os

with CaasClient(host=os.environ["CAAS_HOST"], api_key=os.environ.get("DISPATCHER_API_KEY")) as c:
    logs = c.execute_cell(
        code="import os; print(os.environ['DB_PASSWORD'][:3] + '***')",
        image="python:3.12-slim",
        env={"DB_PASSWORD": os.environ["DB_PASSWORD"]},
    )
    print(logs)
```

The value is passed over the network to the dispatcher and into the container's environment, but is not stored in the notebook.

---

## What is and isn't gitignored

The `.gitignore` in this repo blocks:

- `*.env` and `.env*` — dispatcher API key
- `inventory.yml` — IP addresses and SSH key paths
- `ansible/group_vars/` — service user names and mount configuration

These are operator-local files. The repo only contains the templates (`*.example.*`).

!!! warning "Verify before pushing"
    Run `git status` before any push and confirm none of the above files appear as staged or untracked.
    If they do, add them to `.gitignore` immediately.
