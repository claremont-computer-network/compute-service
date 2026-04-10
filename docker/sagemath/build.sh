#!/usr/bin/env bash
# Build the caas-sagemath image directly on a remote machine.
#
# Usage (arguments take precedence over env vars):
#   HOST=user@example.com REPO_DIR=~/path/to/repo ./docker/sagemath/build.sh
#   ./docker/sagemath/build.sh user@example.com 2222 ~/path/to/repo
#
set -euo pipefail

HOST="${1:-${HOST:-}}"
PORT="${2:-${PORT:-22}}"
REPO_DIR="${3:-${REPO_DIR:-}}"
IMAGE="caas-sagemath:latest"
CONTEXT="docker/sagemath"

if [ -z "$HOST" ] || [ -z "$REPO_DIR" ]; then
  echo "Usage: HOST=user@example.com REPO_DIR=~/path/to/repo ./docker/sagemath/build.sh [HOST] [PORT] [REPO_DIR]" >&2
  echo "Error: remote host and repo directory must be provided via arguments or environment variables." >&2
  exit 1
fi

echo "▶ Building $IMAGE on $HOST (port $PORT) …"
echo "  (apt install sagemath takes ~5-10 minutes on first build)"

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
echo "    \"docker run --rm $IMAGE sage --version\""
