#!/usr/bin/env bash
# Build caas-sagemath on a remote machine.
# Delegates to docker/build-remote.sh — see that file for full usage.
# Note: apt install sagemath takes ~5–10 minutes on first build.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
IMAGE="${IMAGE:-caas-sagemath:latest}" \
CONTEXT="${CONTEXT:-docker/sagemath}" \
  exec "$SCRIPT_DIR/../build-remote.sh" "$@"
