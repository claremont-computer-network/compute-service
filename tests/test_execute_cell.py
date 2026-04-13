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


# ---------------------------------------------------------------------------
# shm_size / ipc_mode validation
# ---------------------------------------------------------------------------

def test_cell_shm_size_accepted_within_limit(api_client, mock_docker_client):
    """shm_size within the server limit is forwarded to docker."""
    mock_docker_client.containers.run.return_value = b""
    resp = api_client.post(CELL_URL, json={
        "code": "pass", "image": "python:3.12", "shm_size": "256m",
    })
    assert resp.status_code == 200
    kwargs = mock_docker_client.containers.run.call_args.kwargs
    assert kwargs["shm_size"] == "256m"


def test_cell_shm_size_exceeds_limit_rejected(api_client, mock_docker_client):
    """shm_size above MAX_SHM_SIZE_MB is rejected with 400."""
    import app.main as m
    m.MAX_SHM_SIZE_MB = 512
    resp = api_client.post(CELL_URL, json={
        "code": "pass", "image": "python:3.12", "shm_size": "2g",
    })
    assert resp.status_code == 400
    assert "exceeds" in resp.json()["detail"]


def test_cell_ipc_mode_host_rejected_when_not_allowed(api_client, mock_docker_client):
    """ipc_mode=host returns 400 when ALLOW_IPC_HOST is False (the default)."""
    resp = api_client.post(CELL_URL, json={
        "code": "pass", "image": "python:3.12", "ipc_mode": "host",
    })
    assert resp.status_code == 400
    assert "ALLOW_IPC_HOST" in resp.json()["detail"]


def test_cell_ipc_mode_host_accepted_when_allowed(api_client, mock_docker_client):
    """ipc_mode=host is accepted when ALLOW_IPC_HOST is True."""
    import app.main as m
    m.ALLOW_IPC_HOST = True
    mock_docker_client.containers.run.return_value = b""
    resp = api_client.post(CELL_URL, json={
        "code": "pass", "image": "python:3.12", "ipc_mode": "host",
    })
    assert resp.status_code == 200
    kwargs = mock_docker_client.containers.run.call_args.kwargs
    assert kwargs["ipc_mode"] == "host"


def test_cell_shm_size_empty_string_rejected(api_client, mock_docker_client):
    """An empty/whitespace shm_size string returns 400, not a 500 IndexError."""
    resp = api_client.post(CELL_URL, json={
        "code": "pass", "image": "python:3.12", "shm_size": "   ",
    })
    assert resp.status_code == 400
    assert "parse" in resp.json()["detail"].lower()


def test_cell_validation_precedes_image_pull(api_client, mock_docker_client):
    """Invalid shm_size must be rejected before any image pull is attempted."""
    mock_docker_client.images.get.side_effect = docker.errors.ImageNotFound("never")
    resp = api_client.post(CELL_URL, json={
        "code": "pass", "image": "python:3.12", "shm_size": "   ",
    })
    assert resp.status_code == 400
    mock_docker_client.images.pull.assert_not_called()


# ── job-store side-effect tests ───────────────────────────────────────────────

def test_cell_job_registered_and_stopped_on_success(api_client, mock_docker_client):
    """A successful cell run appears in /v1/jobs as stopped with exit_code=0."""
    mock_docker_client.containers.run.return_value = b"ok\n"
    api_client.post(CELL_URL, json={"code": "print('ok')", "image": "python:3.11-slim"})

    jobs = api_client.get("/v1/jobs").json()
    cell_jobs = [j for j in jobs if j["image"] == "python:3.11-slim"]
    assert len(cell_jobs) == 1
    job = cell_jobs[0]
    assert job["status"] == "stopped"
    assert job["exit_code"] == 0


def test_cell_job_stopped_with_nonzero_exit_on_container_error(api_client, mock_docker_client):
    """A ContainerError marks the job stopped with the container's exit status."""
    container_stub = mock_docker_client.containers.run.return_value
    mock_docker_client.containers.run.side_effect = docker.errors.ContainerError(
        container=container_stub,
        exit_status=2,
        command="python -c ...",
        image="python:3.11-slim",
        stderr=b"SyntaxError\n",
    )
    api_client.post(CELL_URL, json={"code": "def f(", "image": "python:3.11-slim"})

    jobs = api_client.get("/v1/jobs").json()
    cell_jobs = [j for j in jobs if j["image"] == "python:3.11-slim"]
    assert len(cell_jobs) == 1
    job = cell_jobs[0]
    assert job["status"] == "stopped"
    assert job["exit_code"] == 2


def test_cell_job_not_enriched_via_docker(api_client, mock_docker_client):
    """Cell jobs (docker_backed=False) must not trigger containers.get() during list."""
    mock_docker_client.containers.run.return_value = b""
    api_client.post(CELL_URL, json={"code": "pass", "image": "python:3.11-slim"})

    # Reset call count so we only track calls made during /v1/jobs
    mock_docker_client.containers.get.reset_mock()
    api_client.get("/v1/jobs")
    mock_docker_client.containers.get.assert_not_called()
