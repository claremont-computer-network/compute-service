"""
Tests for POST /v1/execute/cell (dispatcher-side endpoint).
"""
import pytest
import docker.errors

CELL_URL = "/v1/execute/cell"


def test_cell_execute_returns_logs(api_client, mock_docker_client):
    """Valid code submission returns status=exited and logs inline."""
    mock_docker_client.containers.run.return_value = b"2\n"
    resp = api_client.post(CELL_URL, json={
        "code": "x = 1 + 1\nprint(x)",
        "image": "python:3.11-slim",
    })
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "exited"
    assert body["exit_code"] == 0
    assert body["logs"] == "2\n"


def test_cell_execute_wraps_code_as_command(api_client, mock_docker_client):
    """The code string is passed to the container as a python -c command."""
    mock_docker_client.containers.run.return_value = b""
    api_client.post(CELL_URL, json={
        "code": "print('hello')",
        "image": "python:3.11-slim",
    })
    call_kwargs = mock_docker_client.containers.run.call_args
    cmd = call_kwargs.kwargs.get("command") or call_kwargs.args[1]
    # command must invoke python with the submitted code
    assert "python" in cmd[0]
    assert "print('hello')" in " ".join(cmd)


def test_cell_execute_is_always_synchronous(api_client, mock_docker_client):
    """Cell execution always blocks (detach=False) and removes the container."""
    mock_docker_client.containers.run.return_value = b""
    api_client.post(CELL_URL, json={
        "code": "pass",
        "image": "python:3.11-slim",
    })
    call_kwargs = mock_docker_client.containers.run.call_args
    assert call_kwargs.kwargs.get("detach") is False
    assert call_kwargs.kwargs.get("remove") is True


def test_cell_execute_forwards_gpu(api_client, mock_docker_client):
    """GPU device_requests are forwarded when gpu field is set."""
    mock_docker_client.containers.run.return_value = b"Tesla T4\n"
    resp = api_client.post(CELL_URL, json={
        "code": "import torch; print(torch.cuda.get_device_name(0))",
        "image": "pytorch/pytorch:latest",
        "gpu": {"device_ids": "all"},
    })
    assert resp.status_code == 200
    call_kwargs = mock_docker_client.containers.run.call_args
    dr = call_kwargs.kwargs["device_requests"]
    assert dr is not None
    assert dr[0].count == -1


def test_cell_execute_forwards_env(api_client, mock_docker_client):
    """Environment variables are forwarded to the container."""
    mock_docker_client.containers.run.return_value = b""
    api_client.post(CELL_URL, json={
        "code": "import os; print(os.environ['FOO'])",
        "image": "python:3.11-slim",
        "env": {"FOO": "bar"},
    })
    call_kwargs = mock_docker_client.containers.run.call_args
    assert call_kwargs.kwargs["environment"] == {"FOO": "bar"}


def test_cell_execute_requires_api_key_when_set(api_client, mock_docker_client):
    """Auth is enforced on the cell endpoint."""
    import app.main as m
    m.API_KEY = "s3cret"
    resp = api_client.post(CELL_URL, json={"code": "pass", "image": "python:3.11-slim"})
    assert resp.status_code == 401


def test_cell_execute_docker_error_returns_500(api_client, mock_docker_client):
    """Docker failures map to 500."""
    mock_docker_client.containers.run.side_effect = docker.errors.DockerException("boom")
    resp = api_client.post(CELL_URL, json={"code": "pass", "image": "python:3.11-slim"})
    assert resp.status_code == 500


def test_cell_execute_nonzero_exit_returns_logs(api_client, mock_docker_client):
    """User code that exits non-zero returns 200 with logs, not a 500."""
    err = docker.errors.ContainerError(
        container="fake",
        exit_status=1,
        command="python -c ...",
        image="python:3.11-slim",
        stderr=b"Traceback (most recent call last):\nNameError: name 'x' is not defined\n",
    )
    mock_docker_client.containers.run.side_effect = err
    resp = api_client.post(CELL_URL, json={"code": "print(x)", "image": "python:3.11-slim"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "exited"
    assert body["exit_code"] == 1
    assert "NameError" in body["logs"]


def test_cell_execute_missing_image_returns_422(api_client, mock_docker_client):
    """Omitting image returns a validation error."""
    resp = api_client.post(CELL_URL, json={"code": "pass"})
    assert resp.status_code == 422


def test_cell_execute_missing_code_returns_422(api_client, mock_docker_client):
    """Omitting code returns a validation error."""
    resp = api_client.post(CELL_URL, json={"image": "python:3.11-slim"})
    assert resp.status_code == 422
