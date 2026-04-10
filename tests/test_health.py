"""
Tests for GET /health
"""


def test_health_ok(api_client, mock_docker_client):
    """Returns 200 when Docker ping succeeds."""
    mock_docker_client.ping.return_value = True
    resp = api_client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_health_docker_down(api_client, mock_docker_client):
    """Returns 500 when Docker is unreachable."""
    mock_docker_client.ping.side_effect = Exception("socket not found")
    resp = api_client.get("/health")
    assert resp.status_code == 500
    assert "Docker unreachable" in resp.json()["detail"]
