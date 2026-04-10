#!/usr/bin/env python3
"""
scripts/smoke_test.py
─────────────────────
End-to-end smoke test for the compute-service dispatcher.

Two modes:
  1. LOCAL (default) – spins up a real uvicorn process, patches Docker with a
     subprocess-safe mock, and fires a hello-world job.  No Docker daemon needed.
     Run: uv run python scripts/smoke_test.py

  2. LIVE – points at a running dispatcher (real Docker daemon required).
     Set CAAS_HOST and optionally DISPATCHER_API_KEY, then run:
       CAAS_HOST=http://192.0.2.10:8000 DISPATCHER_API_KEY=secret \
         uv run python scripts/smoke_test.py --live
"""
import argparse
import os
import sys
import subprocess
import time
import json
import urllib.request
import urllib.error

# ── colour helpers ────────────────────────────────────────────────────────────
def info(msg):    print(f"\033[1;34m[smoke]\033[0m {msg}")
def ok(msg):      print(f"\033[1;32m[smoke]\033[0m {msg}")
def fail(msg):    print(f"\033[1;31m[smoke]\033[0m {msg}", file=sys.stderr); sys.exit(1)

# ── HTTP helpers ──────────────────────────────────────────────────────────────
def get(url, api_key=None):
    req = urllib.request.Request(url)
    if api_key:
        req.add_header("X-API-Key", api_key)
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())

def post(url, payload, api_key=None):
    data = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    if api_key:
        req.add_header("X-API-Key", api_key)
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())

def wait_healthy(base, timeout=20):
    for _ in range(timeout):
        try:
            get(f"{base}/health")
            return True
        except Exception:
            time.sleep(1)
    return False

# ── LOCAL mode ────────────────────────────────────────────────────────────────
LOCAL_SHIM = """\
# shim loaded before the app – replaces docker.from_env with a mock that
# simulates a successful synchronous container run.
import docker as _docker_real
from unittest.mock import MagicMock, patch as _patch

_mock_client = MagicMock()
_mock_client.ping.return_value = True
_mock_client.images.get.return_value = MagicMock()
_mock_client.containers.run.return_value = b"Hello from compute-service!\\n"

_patch("docker.from_env", return_value=_mock_client).start()
"""

def run_local():
    base = "http://127.0.0.1:8000"
    api_key = None  # auth disabled in dev (API_KEY env not set)

    # write shim to a temp file
    import tempfile, pathlib
    root = pathlib.Path(__file__).parent.parent
    dispatcher_dir = root / "dispatcher"

    shim = tempfile.NamedTemporaryFile(
        mode="w", suffix=".pth", delete=False,
        dir=dispatcher_dir,
    )
    shim.write(LOCAL_SHIM)
    shim.close()

    # build uvicorn command using the venv
    venv_python = root / ".venv" / "bin" / "python"
    cmd = [
        str(venv_python), "-c",
        f"exec(open('{shim.name}').read()); import uvicorn; uvicorn.run('app.main:app', host='127.0.0.1', port=8000, log_level='warning')",
    ]

    env = os.environ.copy()
    env["PYTHONPATH"] = str(dispatcher_dir)

    info("Starting dispatcher (local uvicorn, Docker mocked)…")
    proc = subprocess.Popen(cmd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    try:
        if not wait_healthy(base):
            proc.kill()
            stdout, stderr = proc.communicate()
            fail(f"Dispatcher failed to start.\nstdout: {stdout.decode()}\nstderr: {stderr.decode()}")

        ok("Dispatcher is healthy.")
        run_smoke(base, api_key)
    finally:
        proc.terminate()
        proc.wait()
        os.unlink(shim.name)


# ── LIVE mode ─────────────────────────────────────────────────────────────────
def run_live():
    base = os.environ.get("CAAS_HOST", "http://localhost:8000").rstrip("/")
    api_key = os.environ.get("DISPATCHER_API_KEY") or None
    info(f"Targeting live dispatcher at {base}")
    if not wait_healthy(base, timeout=5):
        fail(f"Dispatcher at {base} not reachable. Is it running?")
    ok("Dispatcher is healthy.")
    run_smoke(base, api_key)


# ── shared smoke logic ────────────────────────────────────────────────────────
def run_smoke(base, api_key):
    # ── health check ──
    health = get(f"{base}/health", api_key)
    assert health.get("status") == "ok", f"Unexpected health: {health}"
    ok(f"/health → {health}")

    # ── hello-world job (synchronous) ──
    info("Submitting hello-world job (alpine:3.18, detach=false)…")
    payload = {
        "image":  "alpine:3.18",
        "cmd":    ["sh", "-c", "echo 'Hello from compute-service!'"],
        "detach": False,
    }
    resp = post(f"{base}/v1/execute", payload, api_key)
    print(json.dumps(resp, indent=2))

    status = resp.get("status")
    logs   = resp.get("logs", "")

    if status != "exited":
        fail(f"Expected status=exited, got: {status}")
    if "Hello from compute-service!" not in logs:
        fail(f"Expected greeting in logs. Got: {logs!r}")

    ok(f"Job completed ✓  Output: {logs.strip()!r}")
    ok("Smoke test passed ✓")


# ── entry point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true",
                        help="Target a real running dispatcher (requires Docker daemon)")
    args = parser.parse_args()

    if args.live:
        run_live()
    else:
        run_local()
