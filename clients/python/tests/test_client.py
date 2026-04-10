"""
Tests for caas.client.CaasClient
"""
import pytest
import httpx
from tests.conftest import BASE_URL, API_KEY, _make_response


# ---------------------------------------------------------------------------
# health
# ---------------------------------------------------------------------------

def test_health_ok(client, mock_transport):
    mock_transport[("GET", f"{BASE_URL}/health")] = _make_response(200, {"status": "ok"})
    assert client.health() == {"status": "ok"}


def test_health_sends_api_key(client, mock_transport):
    def _check(request):
        assert request.headers.get("x-api-key") == API_KEY
        return _make_response(200, {"status": "ok"})
    mock_transport[("GET", f"{BASE_URL}/health")] = _check
    client.health()


def test_health_raises_on_error(client, mock_transport):
    from caas.client import CaasError
    mock_transport[("GET", f"{BASE_URL}/health")] = _make_response(500, {"detail": "Docker unreachable"})
    with pytest.raises(CaasError, match="Docker unreachable"):
        client.health()


# ---------------------------------------------------------------------------
# execute
# ---------------------------------------------------------------------------

def test_execute_minimal(client, mock_transport):
    mock_transport[("POST", f"{BASE_URL}/v1/execute")] = _make_response(
        200, {"container_id": "abc123", "status": "running"}
    )
    result = client.execute(image="alpine:3.18")
    assert result["container_id"] == "abc123"
    assert result["status"] == "running"


def test_execute_sends_full_payload(client, mock_transport):
    import json

    def _check(request):
        body = json.loads(request.content)
        assert body["image"] == "pytorch/pytorch:latest"
        assert body["cmd"] == ["python", "train.py"]
        assert body["env"] == {"EPOCHS": "10"}
        assert body["gpu"] == {"device_ids": "all", "capabilities": ["gpu"]}
        assert body["detach"] is True
        return _make_response(200, {"container_id": "xyz", "status": "running"})

    mock_transport[("POST", f"{BASE_URL}/v1/execute")] = _check
    client.execute(
        image="pytorch/pytorch:latest",
        cmd=["python", "train.py"],
        env={"EPOCHS": "10"},
        gpu={"device_ids": "all", "capabilities": ["gpu"]},
        detach=True,
    )


def test_execute_raises_on_401(client, mock_transport):
    from caas.client import CaasError
    mock_transport[("POST", f"{BASE_URL}/v1/execute")] = _make_response(
        401, {"detail": "Invalid API Key"}
    )
    with pytest.raises(CaasError, match="Invalid API Key"):
        client.execute(image="alpine:3.18")


def test_execute_raises_on_400(client, mock_transport):
    from caas.client import CaasError
    mock_transport[("POST", f"{BASE_URL}/v1/execute")] = _make_response(
        400, {"detail": "Host path not allowed: /etc"}
    )
    with pytest.raises(CaasError, match="not allowed"):
        client.execute(image="alpine:3.18")


# ---------------------------------------------------------------------------
# execute_cell
# ---------------------------------------------------------------------------

def test_execute_cell_returns_logs(client, mock_transport):
    mock_transport[("POST", f"{BASE_URL}/v1/execute/cell")] = _make_response(
        200, {"status": "exited", "logs": "hello from cell\n"}
    )
    logs = client.execute_cell(code="print('hello from cell')", image="python:3.11-slim")
    assert logs == "hello from cell\n"


def test_execute_cell_sends_code_and_image(client, mock_transport):
    import json

    def _check(request):
        body = json.loads(request.content)
        assert body["code"] == "x = 1 + 1\nprint(x)"
        assert body["image"] == "python:3.11-slim"
        return _make_response(200, {"status": "exited", "logs": "2\n"})

    mock_transport[("POST", f"{BASE_URL}/v1/execute/cell")] = _check
    client.execute_cell(code="x = 1 + 1\nprint(x)", image="python:3.11-slim")


def test_execute_cell_forwards_gpu(client, mock_transport):
    import json

    def _check(request):
        body = json.loads(request.content)
        assert body["gpu"] == {"device_ids": "all", "capabilities": ["gpu"]}
        return _make_response(200, {"status": "exited", "logs": "Tesla T4\n"})

    mock_transport[("POST", f"{BASE_URL}/v1/execute/cell")] = _check
    client.execute_cell(
        code="import torch; print(torch.cuda.get_device_name(0))",
        image="pytorch/pytorch:latest",
        gpu={"device_ids": "all", "capabilities": ["gpu"]},
    )


def test_execute_cell_raises_on_error(client, mock_transport):
    from caas.client import CaasError
    mock_transport[("POST", f"{BASE_URL}/v1/execute/cell")] = _make_response(
        500, {"detail": "container failed"}
    )
    with pytest.raises(CaasError):
        client.execute_cell(code="raise ValueError()", image="python:3.11-slim")


# ---------------------------------------------------------------------------
# logs
# ---------------------------------------------------------------------------

def test_logs_returns_text(client, mock_transport):
    mock_transport[("GET", f"{BASE_URL}/v1/logs/abc123")] = _make_response(
        200, {"container_id": "abc123", "logs": "step 1\nstep 2\n"}
    )
    logs = client.logs("abc123")
    assert "step 1" in logs


def test_logs_raises_on_404(client, mock_transport):
    from caas.client import CaasError
    mock_transport[("GET", f"{BASE_URL}/v1/logs/gone")] = _make_response(
        404, {"detail": "Container not found"}
    )
    with pytest.raises(CaasError, match="Container not found"):
        client.logs("gone")


# ---------------------------------------------------------------------------
# No API key
# ---------------------------------------------------------------------------

def test_client_works_without_api_key(mock_transport):
    """When api_key is None no X-API-Key header should be sent."""
    from caas.client import CaasClient
    import json

    def _check(request):
        assert "x-api-key" not in request.headers
        return _make_response(200, {"status": "ok"})

    mock_transport[("GET", f"{BASE_URL}/health")] = _check

    class _T(httpx.BaseTransport):
        def handle_request(self, request):
            return mock_transport[(request.method, str(request.url).split("?")[0])](request)

    c = CaasClient(host=BASE_URL, api_key=None, http_client=httpx.Client(transport=_T()))
    c.health()
