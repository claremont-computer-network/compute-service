"""
Tests for GET /v1/logs/{container_id}
"""
import docker.errors

LOGS_URL = "/v1/logs/{}"


def test_logs_returns_output(api_client, mock_docker_client):
    """Returns the container's stdout/stderr as a JSON response."""
    container = mock_docker_client.containers.get.return_value
    container.logs.return_value = b"step 1\nstep 2\n"

    resp = api_client.get(LOGS_URL.format("abc123deadbeef"))
    assert resp.status_code == 200
    body = resp.json()
    assert body["container_id"] == "abc123deadbeef"
    assert "step 1" in body["logs"]
    assert "step 2" in body["logs"]


def test_logs_unknown_container_returns_404(api_client, mock_docker_client):
    """Missing container ID returns 404."""
    mock_docker_client.containers.get.side_effect = docker.errors.NotFound("gone")
    resp = api_client.get(LOGS_URL.format("doesnotexist"))
    assert resp.status_code == 404
    assert "Container not found" in resp.json()["detail"]


def test_logs_stream_mode(api_client, mock_docker_client):
    """?follow=true returns plain text (streaming response)."""
    container = mock_docker_client.containers.get.return_value
    container.logs.return_value = iter([b"chunk1\n", b"chunk2\n"])

    resp = api_client.get(LOGS_URL.format("abc123deadbeef") + "?follow=true")
    assert resp.status_code == 200
    assert "text/plain" in resp.headers["content-type"]
    assert b"chunk1" in resp.content


def test_logs_requires_api_key_when_set(api_client, mock_docker_client):
    """Auth is enforced on the logs endpoint too."""
    import app.main as m  # resolves via dispatcher/ on sys.path (added by conftest)
    m.API_KEY = "s3cret"

    resp = api_client.get(LOGS_URL.format("abc123deadbeef"))
    assert resp.status_code == 401

    resp2 = api_client.get(
        LOGS_URL.format("abc123deadbeef"),
        headers={"X-API-Key": "s3cret"},
    )
    assert resp2.status_code == 200
