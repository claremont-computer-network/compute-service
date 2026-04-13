"""
Tests for GET /health
"""


def test_health_ok(api_client, mock_docker_client):
    """Returns 200 when Docker ping succeeds; includes loaded plugin names."""
    mock_docker_client.ping.return_value = True
    resp = api_client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert "plugins" in body
    # All five built-in plugins must be present.
    assert set(body["plugins"]) == {
        "nvidia-entrypoint",
        "shm-ipc-policy",
        "volume-policy",
        "resource-sampler",
        "log-retention",
    }


def test_health_docker_down(api_client, mock_docker_client):
    """Returns 500 when Docker is unreachable."""
    mock_docker_client.ping.side_effect = Exception("socket not found")
    resp = api_client.get("/health")
    assert resp.status_code == 500
    assert "Docker unreachable" in resp.json()["detail"]
