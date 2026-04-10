"""
Tests for POST /v1/execute
"""
import pytest
import docker.errors


EXEC_URL = "/v1/execute"


# ---------------------------------------------------------------------------
# Happy-path
# ---------------------------------------------------------------------------

def test_execute_minimal(api_client, mock_docker_client):
    """Minimal request: only image specified."""
    resp = api_client.post(EXEC_URL, json={"image": "alpine:3.18"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["container_id"] == "abc123deadbeef"
    assert body["status"] == "running"
    mock_docker_client.containers.run.assert_called_once()


def test_execute_with_cmd_and_env(api_client, mock_docker_client):
    """Passes command and environment variables through to docker."""
    resp = api_client.post(EXEC_URL, json={
        "image": "alpine:3.18",
        "cmd": ["sh", "-c", "echo hi"],
        "env": {"FOO": "bar"},
    })
    assert resp.status_code == 200
    call_kwargs = mock_docker_client.containers.run.call_args
    assert call_kwargs.kwargs["command"] == ["sh", "-c", "echo hi"]
    assert call_kwargs.kwargs["environment"] == {"FOO": "bar"}


def test_execute_with_allowed_volume(api_client, mock_docker_client):
    """Volume inside an allowed host dir is accepted."""
    resp = api_client.post(EXEC_URL, json={
        "image": "alpine:3.18",
        "volumes": [{"host_path": "/mnt/caas-data", "container_path": "/data", "mode": "rw"}],
    })
    assert resp.status_code == 200
    call_kwargs = mock_docker_client.containers.run.call_args
    vols = call_kwargs.kwargs["volumes"]
    assert "/mnt/caas-data" in vols
    assert vols["/mnt/caas-data"] == {"bind": "/data", "mode": "rw"}


def test_execute_pulls_missing_image(api_client, mock_docker_client):
    """If image is not found locally it should be pulled."""
    mock_docker_client.images.get.side_effect = docker.errors.ImageNotFound("nope")
    resp = api_client.post(EXEC_URL, json={"image": "myrepo/myimage:latest"})
    assert resp.status_code == 200
    mock_docker_client.images.pull.assert_called_once_with("myrepo/myimage:latest")


# ---------------------------------------------------------------------------
# Volume path validation
# ---------------------------------------------------------------------------

def test_execute_disallowed_volume(api_client, mock_docker_client):
    """Volume pointing outside ALLOWED_HOST_DIRS must be rejected with 400."""
    resp = api_client.post(EXEC_URL, json={
        "image": "alpine:3.18",
        "volumes": [{"host_path": "/etc/passwd", "container_path": "/secrets/passwd", "mode": "ro"}],
    })
    assert resp.status_code == 400
    assert "not allowed" in resp.json()["detail"]


def test_execute_volume_path_traversal(api_client, mock_docker_client):
    """Path traversal attempts must be blocked."""
    resp = api_client.post(EXEC_URL, json={
        "image": "alpine:3.18",
        "volumes": [{"host_path": "/mnt/../etc", "container_path": "/sneaky", "mode": "rw"}],
    })
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

def test_execute_requires_api_key_when_set(api_client, mock_docker_client):
    """When DISPATCHER_API_KEY is configured, requests without it get 401."""
    import app.main as m
    m.API_KEY = "supersecret"

    resp = api_client.post(EXEC_URL, json={"image": "alpine:3.18"})
    assert resp.status_code == 401

    resp2 = api_client.post(
        EXEC_URL,
        json={"image": "alpine:3.18"},
        headers={"X-API-Key": "supersecret"},
    )
    assert resp2.status_code == 200


def test_execute_wrong_api_key(api_client, mock_docker_client):
    """Wrong API key is rejected with 401."""
    import app.main as m
    m.API_KEY = "correct"
    resp = api_client.post(
        EXEC_URL,
        json={"image": "alpine:3.18"},
        headers={"X-API-Key": "wrong"},
    )
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Docker errors
# ---------------------------------------------------------------------------

def test_execute_docker_error_returns_500(api_client, mock_docker_client):
    """Docker SDK exceptions map to HTTP 500."""
    mock_docker_client.containers.run.side_effect = docker.errors.DockerException("boom")
    resp = api_client.post(EXEC_URL, json={"image": "alpine:3.18"})
    assert resp.status_code == 500
    assert "boom" in resp.json()["detail"]
