#!/usr/bin/env bash
# smoke_test.sh – end-to-end hello-world test for compute-service dispatcher.
# Starts the dispatcher via Docker Compose, sends a job, checks output, then tears down.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COMPOSE_DIR="$SCRIPT_DIR/../dispatcher"
API_BASE="http://localhost:8000"
API_KEY="${DISPATCHER_API_KEY:-}"         # leave empty to run without auth
MAX_WAIT=30                               # seconds to wait for dispatcher to become healthy

# ── helpers ──────────────────────────────────────────────────────────────────
info()    { echo -e "\033[1;34m[smoke]\033[0m $*"; }
success() { echo -e "\033[1;32m[smoke]\033[0m $*"; }
fail()    { echo -e "\033[1;31m[smoke]\033[0m $*" >&2; exit 1; }

auth_header() {
    if [[ -n "$API_KEY" ]]; then
        echo "-H" "X-API-Key: $API_KEY"
    fi
}

# ── 1. build + start dispatcher ───────────────────────────────────────────────
info "Building and starting dispatcher (docker compose)…"
docker compose -f "$COMPOSE_DIR/docker-compose.yml" up -d --build

# ── 2. wait for /health ───────────────────────────────────────────────────────
info "Waiting for dispatcher to become healthy (up to ${MAX_WAIT}s)…"
elapsed=0
until curl -sf "$API_BASE/health" > /dev/null 2>&1; do
    sleep 1
    elapsed=$((elapsed + 1))
    if [[ $elapsed -ge $MAX_WAIT ]]; then
        fail "Dispatcher did not become healthy in ${MAX_WAIT}s"
    fi
done
success "Dispatcher is healthy."

# ── 3. submit hello-world job (synchronous / detach=false) ───────────────────
info "Submitting hello-world job (alpine:3.18 echo)…"
RESPONSE=$(curl -sf -X POST "$API_BASE/v1/execute" \
    $(auth_header) \
    -H "Content-Type: application/json" \
    -d '{
        "image":  "alpine:3.18",
        "cmd":    ["sh", "-c", "echo Hello from compute-service!"],
        "detach": false
    }')

echo "$RESPONSE" | python3 -m json.tool
LOGS=$(echo "$RESPONSE" | python3 -c "import sys,json; print(json.load(sys.stdin)['logs'])" 2>/dev/null || true)
STATUS=$(echo "$RESPONSE" | python3 -c "import sys,json; print(json.load(sys.stdin)['status'])" 2>/dev/null || true)

if [[ "$STATUS" != "exited" ]]; then
    fail "Unexpected status: $STATUS"
fi
if echo "$LOGS" | grep -q "Hello from compute-service!"; then
    success "Job completed. Output: $LOGS"
else
    fail "Expected output not found in logs. Got: $LOGS"
fi

# ── 4. check /health one more time ───────────────────────────────────────────
curl -sf "$API_BASE/health" | python3 -m json.tool

# ── 5. tear down ─────────────────────────────────────────────────────────────
info "Tearing down dispatcher…"
docker compose -f "$COMPOSE_DIR/docker-compose.yml" down

success "Smoke test passed ✓"
