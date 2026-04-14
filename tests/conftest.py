"""
conftest.py – shared fixtures for compute-service dispatcher tests.

We patch `docker.from_env` BEFORE the app module is imported so the real Docker
socket is never touched during the test run.
"""
import sys
import os
import types
import pytest
from unittest.mock import MagicMock, patch

# Make the repo root importable so `dispatcher.app.main` resolves correctly
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
# Also expose the dispatcher directory so `app.main` resolves as a fallback
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "dispatcher"))

# ---------------------------------------------------------------------------
# Shared test helpers
# ---------------------------------------------------------------------------

def set_cell_logs(container, stdout: bytes = b"", stderr: bytes = b""):
    """Configure a container mock to return specific stdout/stderr bytes.

    After calling this helper, ``container.logs(stdout=True, stderr=False)``
    returns *stdout*, ``container.logs(stdout=False, stderr=True)`` returns
    *stderr*, and the legacy ``container.logs(stdout=True, stderr=True)``
    call returns the merged stream — mirroring the real Docker SDK behaviour.
    """
    def _logs(stdout=True, stderr=True, **kwargs):  # noqa: F811
        out = stdout_bytes if stdout else b""
        err = stderr_bytes if stderr else b""
        if stdout and stderr:
            return out + err
        return out if stdout else err
    stdout_bytes = stdout
    stderr_bytes = stderr
    container.logs.side_effect = _logs


# ---------------------------------------------------------------------------
# Build a minimal fake docker client that every test can shape
# ---------------------------------------------------------------------------

def make_docker_client() -> MagicMock:
    client = MagicMock()

    # ping succeeds by default (health check)
    client.ping.return_value = True

    # images.get succeeds by default (image "already present")
    client.images.get.return_value = MagicMock()

    # containers.run returns a container stub
    container = MagicMock()
    container.id = "abc123deadbeef"
    container.short_id = "abc123deadbeef"[:12]
    container.image.tags = ["alpine:3.18"]
    container.attrs = {"Config": {"Cmd": None}, "State": {"ExitCode": 0}}
    container.status = "running"
    container.reload.return_value = None  # container.reload() is a no-op in tests
    # logs() is called separately for stdout and stderr by execute_cell.
    # Default: stdout has content, stderr is empty (no banner noise in tests).
    def _logs(stdout=True, stderr=True, **kwargs):
        out = b"hello from container\n" if stdout else b""
        err = b""                        if stderr else b""
        # when both are requested (legacy path / detached jobs) return merged
        if stdout and stderr:
            return out
        return out if stdout else err
    container.logs.side_effect = _logs
    # stats raises by default — _fetch_resources returns None gracefully.
    # Tests that need live stats should set container.stats.return_value explicitly.
    container.stats.side_effect = Exception("no stats in tests by default")
    container.wait.return_value = {"StatusCode": 0}
    client.containers.run.return_value = container

    # containers.create returns the same stub (used by execute_cell)
    client.containers.create.return_value = container

    # containers.get returns the same stub by default
    client.containers.get.return_value = container

    # containers.list returns empty by default (no pre-existing containers)
    client.containers.list.return_value = []

    return client


@pytest.fixture(autouse=True)
def mock_docker_client(monkeypatch):
    """
    Replace docker.from_env so the real Docker socket is never used.
    Also reloads the app module so the patched client is wired in.
    """
    dc = make_docker_client()

    # Patch at the module level that main.py imports from
    with patch("docker.from_env", return_value=dc):
        # Force a clean import of the app module on every test so the
        # patched client is picked up.
        for mod_name in list(sys.modules):
            if "app.main" in mod_name:
                del sys.modules[mod_name]

        import app.main as main_module  # resolves via dispatcher/ on sys.path
        main_module.client = dc          # belt-and-suspenders
        main_module.API_KEY = None       # no auth by default
        main_module.ALLOWED_HOST_DIRS = ["/mnt", "/data", "/tmp"]
        main_module.ALLOW_IPC_HOST = False
        main_module.MAX_SHM_SIZE_MB = 8192
        main_module.job_store = type(main_module.job_store)()  # fresh store per test
        main_module.resource_slots = main_module.ResourceSlots.from_env()  # fresh slots per test
        # Re-inject fresh services so plugins that use self.services see the
        # new job_store instance (configure_services ran at module import time
        # with the old store object).
        from app.core.plugin import registry
        registry.configure_services(main_module.job_store, dc)

        yield dc


@pytest.fixture()
def api_client(mock_docker_client):
    """Return a TestClient wired to the (re-imported) app."""
    from httpx import ASGITransport, AsyncClient
    import app.main as main_module  # resolves via dispatcher/ on sys.path
    import anyio

    # Use starlette's sync TestClient for simplicity
    from starlette.testclient import TestClient
    return TestClient(main_module.app)
