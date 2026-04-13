"""
Tests for GET /v1/logs/{container_id}
"""
import docker.errors

LOGS_URL = "/v1/logs/{}"
CELL_URL = "/v1/execute/cell"


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


# ── stored-logs path (cell jobs whose container has already been removed) ─────

def test_logs_served_from_record_after_container_removed(api_client, mock_docker_client):
    """After a cell job completes, logs are served from the job record even
    though the container has been removed (containers.get would 404)."""
    container = mock_docker_client.containers.create.return_value
    container.id = "celllogtest001"
    container.logs.return_value = b"Training complete\nFinal accuracy: 99.1%\n"

    api_client.post(CELL_URL, json={"code": "pass", "image": "python:3.11-slim"})

    # Simulate the container being gone from Docker's perspective.
    mock_docker_client.containers.get.side_effect = docker.errors.NotFound("removed")

    resp = api_client.get(LOGS_URL.format("celllogtest001"))
    assert resp.status_code == 200
    body = resp.json()
    assert "Final accuracy" in body["logs"]
    assert body["container_id"] == "celllogtest001"


def test_logs_follow_served_from_record_as_plain_text(api_client, mock_docker_client):
    """?follow=true on a stopped cell job streams stored logs as text/plain."""
    container = mock_docker_client.containers.create.return_value
    container.id = "celllogtest002"
    container.logs.return_value = b"done\n"

    api_client.post(CELL_URL, json={"code": "pass", "image": "python:3.11-slim"})

    mock_docker_client.containers.get.side_effect = docker.errors.NotFound("removed")

    resp = api_client.get(LOGS_URL.format("celllogtest002") + "?follow=true")
    assert resp.status_code == 200
    assert "text/plain" in resp.headers["content-type"]
    assert b"done" in resp.content


def test_logs_live_container_not_shadowed_by_empty_stored_logs(api_client, mock_docker_client):
    """A running job with no stored_logs still hits the live Docker path."""
    live_container = mock_docker_client.containers.get.return_value
    live_container.logs.return_value = b"still running...\n"
    # containers.get succeeds (container is alive) and has no stored_logs entry.
    mock_docker_client.containers.get.side_effect = None

    resp = api_client.get(LOGS_URL.format("livecontainer001"))
    assert resp.status_code == 200
    assert "still running" in resp.json()["logs"]
