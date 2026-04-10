#!/usr/bin/env bash
# Build the caas-scientific image directly on the remote GB10 machine.
#
# Usage:
#   ./docker/scientific/build.sh                          # default host/port
#   ./docker/scientific/build.sh erik@192.168.1.101 2222
#
set -euo pipefail

HOST="${1:-erik@192.168.1.101}"
PORT="${2:-2222}"
REPO_DIR="${3:-~/git-projects/compute-service}"
IMAGE="caas-scientific:latest"
CONTEXT="docker/scientific"

echo "▶ Building $IMAGE on $HOST:$PORT …"

ssh -p "$PORT" "$HOST" "
  set -euo pipefail
  cd $REPO_DIR
  git pull --ff-only
  docker build \
    --progress=plain \
    -t $IMAGE \
    $CONTEXT/
"

echo ""
echo "✓ Build complete. Verify with:"
echo "  ssh -p $PORT $HOST \\"
echo "    \"docker run --rm --gpus all $IMAGE \\"
echo "       python -c 'import torch,jax,numpy,scipy,cvxpy; print(torch.__version__, jax.__version__, numpy.__version__, scipy.__version__, cvxpy.__version__)'\""
