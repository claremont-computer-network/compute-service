#!/usr/bin/env bash
# Build caas-scientific on a remote machine.
# Delegates to docker/build-remote.sh — see that file for full usage.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
IMAGE="${IMAGE:-caas-scientific:latest}" \
CONTEXT="${CONTEXT:-docker/scientific}" \
  exec "$SCRIPT_DIR/../build-remote.sh" "$@"
