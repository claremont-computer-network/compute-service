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
    container.logs.return_value = b"hello from container\n"
    client.containers.run.return_value = container

    # containers.get returns the same stub by default
    client.containers.get.return_value = container

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
