# caas-scientific: ARM64 scientific computing + ML + optimisation image
#
# Base: NVIDIA NGC PyTorch for Grace Blackwell (ARM64 + CUDA 13)
# Tested on: NVIDIA GB10, Driver 580, CUDA 13.0
#
# Build on the remote machine:
#   ./docker/scientific/build.sh <user@host> <port> <remote-repo-dir>
#   or: REPO_DIR=<remote-repo-dir> HOST=<user@host> ./docker/scientific/build.sh
FROM nvcr.io/nvidia/pytorch:25.03-py3

# System packages needed by some Python wheels (scipy, h5py, cvxpy)
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    gfortran \
    libopenblas-dev \
    liblapack-dev \
    libhdf5-dev \
    libffi-dev \
    git \
    curl \
    && rm -rf /var/lib/apt/lists/*

# JAX with CUDA 13 support — install before requirements.txt to avoid
# CUDA version conflicts with other packages that also depend on jaxlib.
RUN pip install --no-cache-dir \
    "jax[cuda13]" \
    flax \
    optax \
    orbax-checkpoint \
    diffrax \
    equinox

# Remaining scientific / ML / optimisation stack
COPY requirements.txt /tmp/scientific-requirements.txt
RUN pip install --no-cache-dir -r /tmp/scientific-requirements.txt

WORKDIR /workspace
