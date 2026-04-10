#!/usr/bin/env bash
# Build the caas-scientific image directly on a remote machine.
#
# Usage (arguments take precedence over env vars):
#   HOST=user@example.com REPO_DIR=~/path/to/repo ./docker/scientific/build.sh
#   ./docker/scientific/build.sh user@example.com 2222 ~/path/to/repo
#
set -euo pipefail

usage() {
  echo "Usage:"
  echo "  HOST=user@example.com REPO_DIR=~/path/to/repo ./docker/scientific/build.sh [HOST] [PORT] [REPO_DIR]"
  echo ""
  echo "Arguments / environment variables:"
  echo "  HOST      Remote SSH host  (arg 1 or \$HOST)"
  echo "  PORT      Remote SSH port  (arg 2 or \$PORT, default: 22)"
  echo "  REPO_DIR  Repo path on the remote host (arg 3 or \$REPO_DIR)"
}

HOST="${1:-${HOST:-}}"
PORT="${2:-${PORT:-22}}"
REPO_DIR="${3:-${REPO_DIR:-}}"
IMAGE="caas-scientific:latest"
CONTEXT="docker/scientific"

if [ -z "$HOST" ] || [ -z "$REPO_DIR" ]; then
  usage
  exit 1
fi

echo "▶ Building $IMAGE on $HOST (port $PORT) …"

ssh -p "$PORT" "$HOST" bash -s -- "$REPO_DIR" "$IMAGE" "$CONTEXT" <<'EOF'
set -euo pipefail
REPO_DIR="$1"
IMAGE="$2"
CONTEXT="$3"
cd "$REPO_DIR"
git pull --ff-only
docker build \
  --progress=plain \
  -t "$IMAGE" \
  "$CONTEXT/"
EOF

echo ""
echo "✓ Build complete. Verify with:"
echo "  ssh -p $PORT $HOST \\"
echo "    \"docker run --rm --gpus all $IMAGE \\"
echo "       python -c 'import torch,jax,numpy,scipy,cvxpy; print(torch.__version__, jax.__version__, numpy.__version__, scipy.__version__, cvxpy.__version__)'\""
