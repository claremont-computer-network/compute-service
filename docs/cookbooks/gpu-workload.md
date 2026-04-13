# Cookbook: GPU Workloads

Run GPU-accelerated code on a remote machine from a notebook cell.

---

## Prerequisites

- The remote machine has an NVIDIA GPU.
- The Ansible playbook was run with `caas_gpu_enabled: true` in `ansible/group_vars/all.yml`.
- `nvidia-container-toolkit` is installed on the host (the playbook handles this).
- The dispatcher is running (Docker Compose on the remote machine).

---

## Verify GPU availability

Before running any GPU workload, check that the dispatcher can see the GPU. `nvidia-smi` is a
shell tool, not a Python script, so use `CaasClient.execute` (not `execute_cell`) with the
`nvidia/cuda` base image, which has `nvidia-smi` but no Python:

```python
from caas import CaasClient
import os

with CaasClient(
    host=os.environ["CAAS_HOST"],
    api_key=os.environ.get("DISPATCHER_API_KEY"),
) as client:
    result = client.execute(
        image="nvidia/cuda:12.3.2-base-ubuntu22.04",
        cmd=["nvidia-smi"],
        gpu={"device_ids": "all"},
        detach=False,
    )
    print(result["logs"])
```

Expected output:

```
+-----------------------------------------------------------------------------+
| NVIDIA-SMI 545.23.08    Driver Version: 545.23.08    CUDA Version: 12.3     |
|-------------------------------+----------------------+----------------------+
| GPU  Name        Persistence-M| Bus-Id        Disp.A | Volatile Uncorr. ECC |
...
```

!!! warning "Don't use `%%dispatch` for this check"
    `%%dispatch` calls `/v1/execute/cell`, which runs `python -c <code>`. The `nvidia/cuda`
    base image has no Python, so the container will fail with
    `exec: "python": executable file not found in $PATH`. Use `CaasClient.execute` with an
    explicit `cmd` instead, as shown above.

If you see `nvidia-smi: not found`, the GPU provisioning step did not complete. Re-run the
playbook with `caas_gpu_enabled: true`.

---

!!! warning "ARM64 / aarch64 machines"
    The official `pytorch/pytorch` images on Docker Hub are `amd64`-only. If your remote
    machine is ARM64 (e.g. an Ampere cloud instance, AWS Graviton + NVIDIA GPU, or similar),
    you will get `exec format error` when trying to run them. See the
    [ARM64 section below](#arm64-aarch64-machines) for alternatives.

## PyTorch: check CUDA availability

```python
%%dispatch --image pytorch/pytorch:2.3.0-cuda12.1-cudnn8-runtime --gpu all
import torch

print("CUDA available :", torch.cuda.is_available())
print("Device count   :", torch.cuda.device_count())
for i in range(torch.cuda.device_count()):
    print(f"  GPU {i}: {torch.cuda.get_device_name(i)}")
```

---

## PyTorch: tensor on GPU

```python
%%dispatch --image pytorch/pytorch:2.3.0-cuda12.1-cudnn8-runtime --gpu all
import torch

device = torch.device("cuda")
a = torch.randn(1000, 1000, device=device)
b = torch.randn(1000, 1000, device=device)
c = a @ b
print(f"result shape : {c.shape}")
print(f"result mean  : {c.mean().item():.4f}")
print(f"device       : {c.device}")
```

---

## PyTorch: simple training loop

A minimal example — random data, two-layer network, cross-entropy loss.

```python
%%dispatch --image pytorch/pytorch:2.3.0-cuda12.1-cudnn8-runtime --gpu all
import torch
import torch.nn as nn

device = torch.device("cuda")

# Toy dataset
X = torch.randn(256, 16, device=device)
y = torch.randint(0, 4, (256,), device=device)

model = nn.Sequential(
    nn.Linear(16, 64),
    nn.ReLU(),
    nn.Linear(64, 4),
).to(device)

opt = torch.optim.Adam(model.parameters(), lr=1e-3)
loss_fn = nn.CrossEntropyLoss()

for epoch in range(20):
    opt.zero_grad()
    loss = loss_fn(model(X), y)
    loss.backward()
    opt.step()
    if epoch % 5 == 0:
        print(f"epoch {epoch:2d}  loss={loss.item():.4f}")
```

---

## Selecting specific GPUs

If the machine has multiple GPUs and you only want to use GPU 1:

```python
%%dispatch --image pytorch/pytorch:2.3.0-cuda12.1-cudnn8-runtime --gpu 1
import torch

print(torch.cuda.get_device_name(0))  # device_id 0 inside the container = GPU 1 on the host
```

Or to use GPUs 0 and 1:

```python
%%dispatch --image pytorch/pytorch:2.3.0-cuda12.1-cudnn8-runtime --gpu 0,1
import torch

print(f"{torch.cuda.device_count()} GPUs available")
```

---

## Using `CaasClient` directly

The `%%dispatch` magic is a convenience wrapper. For programmatic use, pass the `gpu` dict directly:

```python
from caas import CaasClient
import os

code = """
import torch
print(torch.cuda.get_device_name(0))
"""

with CaasClient(
    host=os.environ["CAAS_HOST"],
    api_key=os.environ.get("DISPATCHER_API_KEY"),
) as client:
    logs = client.execute_cell(
        code=code,
        image="pytorch/pytorch:2.3.0-cuda12.1-cudnn8-runtime",
        gpu={"device_ids": "all"},
    )
    print(logs)
```

---

## Troubleshooting

### `RuntimeError: No CUDA GPUs are available`

The container started but couldn't see the GPU. Check:

1. The `--gpu` flag was passed to `%%dispatch` (or `gpu` to `execute_cell`).
2. `nvidia-container-toolkit` is installed on the host: `nvidia-ctk --version`.
3. The Docker daemon is configured to use the NVIDIA runtime. Re-run the playbook with `caas_gpu_enabled: true`.

### `docker: Error response from daemon: could not select device driver "nvidia"`

The NVIDIA container runtime is not registered with Docker. On the remote machine:

```bash
sudo nvidia-ctk runtime configure --runtime=docker
sudo systemctl restart docker
```

Then restart the dispatcher:

```bash
cd compute-service/dispatcher && docker compose restart
```

---

## ARM64 / aarch64 machines

The official `pytorch/pytorch` images are built for `amd64` only. On an ARM64 host you will see:

```
exec /opt/conda/bin/python: exec format error
```

**Confirm the architecture first:**

```python
from caas import CaasClient
import os

with CaasClient(host=os.environ["CAAS_HOST"], api_key=os.environ.get("DISPATCHER_API_KEY")) as c:
    result = c.execute(
        image="python:3.12-slim",
        cmd=["uname", "-m"],
        detach=False,
    )
    print(result["logs"].strip())   # aarch64 = ARM64, x86_64 = amd64
```

### NVIDIA Grace Blackwell (GB10) and Grace Hopper (GH200)

Grace Blackwell and Grace Hopper are ARM64+GPU SoCs — the CPU is an NVIDIA Grace (ARM) core,
so amd64 images will never work. Use NVIDIA's official NGC containers, which are built
natively for these chips:

```python
%%dispatch --image nvcr.io/nvidia/pytorch:25.03-py3 --gpu all
import torch

print("arch         :", torch.version.cuda)
print("CUDA avail   :", torch.cuda.is_available())
print("device       :", torch.cuda.get_device_name(0))
```

!!! tip "NVIDIA entrypoint banner"
    NGC images run an entrypoint shell script that prints a multi-page copyright/release banner
    to stdout before executing your code.  The `caas` client **automatically suppresses this**
    for any image whose name starts with `nvcr.io/` — no extra flag needed.  If you still see
    the banner, your client package is out of date; see the
    [--suppress-entrypoint docs](../client/dispatch-magic.md#--suppress-entrypoint----no-suppress-entrypoint)
    for how to reinstall it.

The `nvcr.io/nvidia/pytorch` images are large (~20 GB) but include CUDA, cuDNN, NCCL, and
PyTorch pre-built against the exact driver on the host. Pull once on the remote machine to
avoid timeout on first use:

```bash
docker pull nvcr.io/nvidia/pytorch:25.03-py3
```

Tags follow the pattern `YY.MM-py3`. Check
[NGC](https://catalog.ngc.nvidia.com/orgs/nvidia/containers/pytorch) for the latest.

!!! tip "Match the CUDA version"
    Run `nvidia-smi` to find the CUDA version reported on your host (e.g. `13.0`), then pick
    an NGC tag whose release notes list a compatible CUDA toolkit. The NGC containers ship
    their own CUDA toolkit so the host only needs a recent enough driver.

### Other ARM64 machines (Jetson, cloud ARM instances)

For Jetson boards, use `nvcr.io/nvidia/l4t-pytorch`. For generic ARM64 cloud instances with
discrete NVIDIA GPUs, build a custom image using PyTorch's ARM64 nightly wheels:

```dockerfile
FROM arm64v8/python:3.12-slim

RUN pip install --pre torch --index-url https://download.pytorch.org/whl/nightly/cu121

CMD ["python3"]
```
