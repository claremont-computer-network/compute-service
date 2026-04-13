#!/usr/bin/env bash
# Build a caas image directly on a remote machine.
#
# The remote repo must already be checked out on the correct branch and be
# clean — this script does NOT run git pull so it builds exactly what is
# on disk. Sync the remote manually before running if needed:
#   ssh <host> "cd <repo> && git fetch && git checkout <branch> && git pull --ff-only"
#
# Usage (arguments take precedence over env vars):
#   IMAGE=caas-scientific:latest CONTEXT=docker/scientific \
#     HOST=user@example.com REPO_DIR=~/path/to/repo ./docker/build-remote.sh
#
#   ./docker/build-remote.sh user@example.com 2222 ~/path/to/repo caas-scientific:latest docker/scientific
#
set -euo pipefail

usage() {
  echo "Usage:"
  echo "  HOST=user@host REPO_DIR=~/repo IMAGE=name:tag CONTEXT=docker/ctx ./docker/build-remote.sh"
  echo "  ./docker/build-remote.sh HOST PORT REPO_DIR IMAGE CONTEXT"
  echo ""
  echo "  HOST     Remote SSH host  (arg 1 or \$HOST)"
  echo "  PORT     Remote SSH port  (arg 2 or \$PORT, default: 22)"
  echo "  REPO_DIR Repo path on the remote (arg 3 or \$REPO_DIR)"
  echo "  IMAGE    Docker image name:tag to build (arg 4 or \$IMAGE)"
  echo "  CONTEXT  Build context path relative to REPO_DIR (arg 5 or \$CONTEXT)"
}

HOST="${1:-${HOST:-}}"
PORT="${2:-${PORT:-22}}"
REPO_DIR="${3:-${REPO_DIR:-}}"
IMAGE="${4:-${IMAGE:-}}"
CONTEXT="${5:-${CONTEXT:-}}"

if [ -z "$HOST" ] || [ -z "$REPO_DIR" ] || [ -z "$IMAGE" ] || [ -z "$CONTEXT" ]; then
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
docker build \
  --progress=plain \
  -t "$IMAGE" \
  "$CONTEXT/"
EOF

echo ""
echo "✓ Build complete."
echo "  Verify: ssh -p $PORT $HOST \"docker run --rm $IMAGE <verify-cmd>\""
