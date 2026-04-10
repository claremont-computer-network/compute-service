# caas-sagemath: Native ARM64 SageMath image
#
# sagemath/sagemath:latest is amd64-only; this builds from the Ubuntu 22.04
# apt package which ships a pre-built ARM64 binary.
#
# Build on the remote machine:
#   ./docker/sagemath/build.sh <user@host> <port>
FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
    sagemath \
    python3-pip \
    python-is-python3 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /workspace
