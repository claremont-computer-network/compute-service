#!/usr/bin/env bash
# Build caas-dispatcher on a remote machine.
#
# The dispatcher Dockerfile uses the repo root as build context so it can
# COPY both dispatcher/ and ui/ into the image.  Pass HOST, PORT, and
# REPO_DIR as env vars or positional args (see docker/build-remote.sh).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

HOST="${1:-${HOST:-}}"
PORT="${2:-${PORT:-22}}"
REPO_DIR="${3:-${REPO_DIR:-}}"
IMAGE="${IMAGE:-caas-dispatcher:latest}"

if [ -z "$HOST" ] || [ -z "$REPO_DIR" ]; then
  echo "Usage: HOST=user@host REPO_DIR=~/repo [PORT=22] [IMAGE=caas-dispatcher:latest] ./dispatcher/build.sh"
  exit 1
fi

echo "▶ Building $IMAGE on $HOST (port $PORT) …"

# Build context is the repo root so both dispatcher/ and ui/ are in scope.
ssh -p "$PORT" "$HOST" bash -s -- "$REPO_DIR" "$IMAGE" <<'EOF'
set -euo pipefail
REPO_DIR="$1"
IMAGE="$2"
cd "$REPO_DIR"
docker build \
  --progress=plain \
  -f dispatcher/Dockerfile \
  -t "$IMAGE" \
  .
EOF

echo ""
echo "✓ Build complete."
echo "  Verify: ssh -p $PORT $HOST \"docker run --rm $IMAGE python -c 'import app.main'\""
