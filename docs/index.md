# compute-service

**Self-hosted compute-as-a-service** — run containerised workloads on a remote machine via a single HTTP request.

<div class="grid cards" markdown>

-   :material-rocket-launch: **Up in under an hour**

    Clone the repo, fill in a local config file, run the provisioner.  
    [:octicons-arrow-right-24: Quickstart](quickstart.md)

-   :material-api: **Simple HTTP API**

    Any container, any command, any image.  
    [:octicons-arrow-right-24: API Reference](api-reference.md)

-   :material-language-python: **Notebook-native**

    Send a Jupyter cell to a remote machine with `%%dispatch`.  
    [:octicons-arrow-right-24: %%dispatch Magic](client/dispatch-magic.md)

-   :material-gpu: **GPU-ready**

    Pass `--gpu all` to expose every GPU via the NVIDIA runtime.  
    [:octicons-arrow-right-24: GPU Workloads](cookbooks/gpu-workload.md)

</div>

---

## How it works

There are three layers:

**Provisioner (Ansible)**  
Prepares the host machine — installs Docker, creates the service user, and optionally mounts external storage to a consistent local path. Run once per machine; safe to re-run any time.

**Dispatcher (FastAPI)**  
A lightweight HTTP API that runs on the compute node. Accepts a job description — a Docker image, a command, environment variables, and optional volume mappings — and executes it via the Docker daemon. Returns logs either inline or via a separate logs endpoint.

**Jobs (Docker)**  
The dispatcher treats every job as a black box. Whether the container runs Python, Rust, or anything else, the dispatcher only needs to know the image, the command, and the data path.

---

## Repository layout

```
ansible/          Ansible playbook and example configuration
clients/
  python/         Installable Python client and %%dispatch IPython magic
    caas/         Package source (CaasClient, register_magic)
    tests/        Client unit tests
dispatcher/       FastAPI application, Dockerfile, and Compose file
  app/            Application source
scripts/          bootstrap.sh (provisioner) and smoke_test.py (end-to-end)
docs/             This documentation site
.env.example      Template for required environment variables
```

Actual secrets and machine-specific config (`inventory.yml`, `group_vars/all.yml`, `.env`) are gitignored. The repo only contains the templates.
