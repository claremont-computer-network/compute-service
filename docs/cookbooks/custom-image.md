# Cookbook: Custom Images

Baking dependencies into a Docker image eliminates the per-cell `pip install` overhead and gives you a reproducible, versioned environment.

---

## Why custom images?

| Approach | Cold start | Reproducible | Versioned |
|----------|-----------|--------------|-----------|
| `pip install` inside cell | Slow (10–60 s) | Depends on PyPI | No |
| Custom image (pre-built) | Fast (1–3 s) | Yes | Yes, via image tag |

For anything beyond a one-off experiment, custom images are the right answer.

---

## Minimal example: NumPy image

Create a `Dockerfile`:

```dockerfile
FROM python:3.12-slim

RUN pip install --no-cache-dir numpy==1.26.4 scipy==1.13.0

CMD ["python"]
```

Build and tag it:

```bash
docker build -t ghcr.io/yourorg/caas-numpy:1.26.4 .
docker push ghcr.io/yourorg/caas-numpy:1.26.4
```

Use it:

```python
%%dispatch --image ghcr.io/yourorg/caas-numpy:1.26.4
import numpy as np
print(np.__version__)
```

---

## PyTorch image with extras

Start from an official PyTorch image and add your project's dependencies:

```dockerfile
FROM pytorch/pytorch:2.3.0-cuda12.1-cudnn8-runtime

RUN pip install --no-cache-dir \
    transformers==4.41.0 \
    accelerate==0.30.0 \
    datasets==2.19.0 \
    evaluate==0.4.2

WORKDIR /workspace
CMD ["python"]
```

```bash
docker build -t ghcr.io/yourorg/caas-pytorch-hf:2.3.0 .
docker push ghcr.io/yourorg/caas-pytorch-hf:2.3.0
```

```python
%%dispatch --image ghcr.io/yourorg/caas-pytorch-hf:2.3.0 --gpu all
from transformers import pipeline
pipe = pipeline("sentiment-analysis")
print(pipe("The remote machine is doing the work."))
```

---

## Building directly on the remote machine

If you don't have a registry, or you want to iterate quickly without a push/pull cycle, you
can build the image on the remote machine itself and use it immediately as a local image.

**1. Copy a Dockerfile to the remote machine**

```bash
scp -P 2222 Dockerfile erik@54.89.192.212:~/caas-numpy/
```

Or write it directly over SSH:

```bash
ssh -p 2222 erik@54.89.192.212 "mkdir -p ~/caas-numpy && cat > ~/caas-numpy/Dockerfile" << 'EOF'
FROM python:3.12-slim
RUN pip install --no-cache-dir numpy scipy
CMD ["python"]
EOF
```

**2. Build on the remote machine**

```bash
ssh -p 2222 erik@54.89.192.212 "docker build -t caas-numpy:latest ~/caas-numpy/"
```

**3. Use it immediately**

Because the image is already present on the host, the dispatcher uses it without pulling:

```python
%%dispatch --image caas-numpy:latest
import numpy as np
print(np.__version__)
```

!!! tip "No registry needed"
    Local image names (no registry prefix) work fine — the dispatcher runs `docker run` on
    the same machine that has the image. You only need a registry if you want to share the
    image or pull it from multiple machines.

!!! note "ARM64 builds"
    Building on the remote machine is especially useful for ARM64 hosts (like a Grace
    Blackwell GB10) where pre-built amd64 images don't work. The image built on the host
    will always match its architecture.

---

## Making the image available on the remote machine

The dispatcher pulls images from the Docker registry when a job runs. Two options:

**Option A: Push to a registry the host can reach**

```bash
docker push ghcr.io/yourorg/caas-numpy:1.26.4
```

The first time a cell runs with a new image, the remote machine pulls it. Subsequent runs start instantly.

**Option B: Pre-pull on the host (avoids first-run latency)**

SSH into the remote machine and pull manually:

```bash
docker pull ghcr.io/yourorg/caas-numpy:1.26.4
```

---

## Baking a script into the image

For jobs that always run the same script, bake it in:

```dockerfile
FROM python:3.12-slim

RUN pip install --no-cache-dir pandas==2.2.2

COPY process.py /app/process.py
WORKDIR /app
```

Submit via `execute` (not `execute_cell`):

```python
from caas import CaasClient
import os

with CaasClient(host=os.environ["CAAS_HOST"], api_key=os.environ.get("DISPATCHER_API_KEY")) as c:
    result = c.execute(
        image="ghcr.io/yourorg/caas-processor:latest",
        cmd=["python", "/app/process.py"],
        volumes=[{"host_path": "/mnt/datasets", "container_path": "/data", "mode": "ro"}],
        detach=False,
    )
    print(result["logs"])
```

---

## Tagging strategy

A simple convention that works well:

| Tag | Meaning |
|-----|---------|
| `latest` | Most recent build — useful for development |
| `YYYY-MM-DD` | Date-stamped snapshot — useful for reproducibility |
| `<lib-version>` | Pin to a specific library version, e.g. `1.26.4` for NumPy |

Avoid using `latest` for production workloads — pin to a specific tag so re-running a notebook a month later gives the same environment.

---

## GitHub Actions: auto-build on push

`.github/workflows/build-image.yml`:

```yaml
name: Build and push image

on:
  push:
    paths:
      - "docker/caas-numpy/**"

jobs:
  build:
    runs-on: ubuntu-latest
    permissions:
      packages: write
    steps:
      - uses: actions/checkout@v4
      - uses: docker/login-action@v3
        with:
          registry: ghcr.io
          username: ${{ github.actor }}
          password: ${{ secrets.GITHUB_TOKEN }}
      - uses: docker/build-push-action@v5
        with:
          context: docker/caas-numpy
          push: true
          tags: ghcr.io/${{ github.repository_owner }}/caas-numpy:latest
```
