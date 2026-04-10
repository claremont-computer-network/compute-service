#!/usr/bin/env bash
# Build the caas-sagemath image directly on the remote GB10 machine.
#
# Usage:
#   ./docker/sagemath/build.sh                          # default host/port
#   ./docker/sagemath/build.sh erik@192.168.1.101 2222
#
set -euo pipefail

HOST="${1:-erik@192.168.1.101}"
PORT="${2:-2222}"
REPO_DIR="${3:-~/git-projects/compute-service}"
IMAGE="caas-sagemath:latest"
CONTEXT="docker/sagemath"

echo "▶ Building $IMAGE on $HOST:$PORT …"
echo "  (apt install sagemath takes ~5-10 minutes on first build)"

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
echo "    \"docker run --rm $IMAGE sage --version\""
