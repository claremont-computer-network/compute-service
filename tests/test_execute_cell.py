"""
Tests for POST /v1/execute/cell (dispatcher-side endpoint).
"""
import pytest
import docker.errors

CELL_URL = "/v1/execute/cell"


def test_cell_execute_returns_logs(api_client, mock_docker_client):
    """Valid code submission returns status=exited and logs inline."""
    mock_docker_client.containers.create.return_value.logs.return_value = b"2\n"
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
    api_client.post(CELL_URL, json={
        "code": "print('hello')",
        "image": "python:3.11-slim",
    })
    call_kwargs = mock_docker_client.containers.create.call_args
    cmd = call_kwargs.kwargs.get("command") or call_kwargs.args[1]
    # command must invoke python with the submitted code
    assert "python" in cmd[0]
    assert "print('hello')" in " ".join(cmd)


def test_cell_execute_is_docker_backed(api_client, mock_docker_client):
    """Cell execution now uses containers.create (docker-backed), not containers.run."""
    api_client.post(CELL_URL, json={
        "code": "pass",
        "image": "python:3.11-slim",
    })
    mock_docker_client.containers.create.assert_called_once()
    mock_docker_client.containers.run.assert_not_called()


def test_cell_execute_forwards_gpu(api_client, mock_docker_client):
    """GPU device_requests are forwarded when gpu field is set."""
    mock_docker_client.containers.create.return_value.logs.return_value = b"Tesla T4\n"
    resp = api_client.post(CELL_URL, json={
        "code": "import torch; print(torch.cuda.get_device_name(0))",
        "image": "pytorch/pytorch:latest",
        "gpu": {"device_ids": "all"},
    })
    assert resp.status_code == 200
    call_kwargs = mock_docker_client.containers.create.call_args
    dr = call_kwargs.kwargs["device_requests"]
    assert dr is not None
    assert dr[0].count == -1


def test_cell_execute_forwards_env(api_client, mock_docker_client):
    """Environment variables are forwarded to the container."""
    api_client.post(CELL_URL, json={
        "code": "import os; print(os.environ['FOO'])",
        "image": "python:3.11-slim",
        "env": {"FOO": "bar"},
    })
    call_kwargs = mock_docker_client.containers.create.call_args
    assert call_kwargs.kwargs["environment"] == {"FOO": "bar"}


def test_cell_execute_requires_api_key_when_set(api_client, mock_docker_client):
    """Auth is enforced on the cell endpoint."""
    import app.main as m
    m.API_KEY = "s3cret"
    resp = api_client.post(CELL_URL, json={"code": "pass", "image": "python:3.11-slim"})
    assert resp.status_code == 401


def test_cell_execute_docker_error_returns_500(api_client, mock_docker_client):
    """Docker failures map to 500."""
    mock_docker_client.containers.create.side_effect = docker.errors.DockerException("boom")
    resp = api_client.post(CELL_URL, json={"code": "pass", "image": "python:3.11-slim"})
    assert resp.status_code == 500


def test_cell_execute_nonzero_exit_returns_logs(api_client, mock_docker_client):
    """User code that exits non-zero returns 200 with logs, not a 500."""
    container = mock_docker_client.containers.create.return_value
    container.wait.return_value = {"StatusCode": 1}
    container.logs.return_value = b"Traceback (most recent call last):\nNameError: name 'x' is not defined\n"
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
    resp = api_client.post(CELL_URL, json={
        "code": "pass", "image": "python:3.12", "shm_size": "256m",
    })
    assert resp.status_code == 200
    kwargs = mock_docker_client.containers.create.call_args.kwargs
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
    resp = api_client.post(CELL_URL, json={
        "code": "pass", "image": "python:3.12", "ipc_mode": "host",
    })
    assert resp.status_code == 200
    kwargs = mock_docker_client.containers.create.call_args.kwargs
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
    container = mock_docker_client.containers.create.return_value
    container.id = "celljob000001"
    container.wait.return_value = {"StatusCode": 0}
    container.logs.return_value = b"ok\n"
    api_client.post(CELL_URL, json={"code": "print('ok')", "image": "python:3.11-slim"})

    jobs = api_client.get("/v1/jobs").json()
    cell_jobs = [j for j in jobs if j["image"] == "python:3.11-slim"]
    assert len(cell_jobs) == 1
    job = cell_jobs[0]
    assert job["status"] == "stopped"
    assert job["exit_code"] == 0


def test_cell_job_stopped_with_nonzero_exit_on_container_error(api_client, mock_docker_client):
    """A non-zero exit marks the job stopped with the container's exit status."""
    container = mock_docker_client.containers.create.return_value
    container.id = "celljob000002"
    container.wait.return_value = {"StatusCode": 2}
    container.logs.return_value = b"SyntaxError\n"
    api_client.post(CELL_URL, json={"code": "def f(", "image": "python:3.11-slim"})

    jobs = api_client.get("/v1/jobs").json()
    cell_jobs = [j for j in jobs if j["image"] == "python:3.11-slim"]
    assert len(cell_jobs) == 1
    job = cell_jobs[0]
    assert job["status"] == "stopped"
    assert job["exit_code"] == 2


def test_cell_job_has_real_container_id(api_client, mock_docker_client):
    """Cell jobs now have a real Docker container ID (docker_backed=True)."""
    container = mock_docker_client.containers.create.return_value
    container.id = "realcontainerid001"
    api_client.post(CELL_URL, json={"code": "pass", "image": "python:3.11-slim"})

    jobs = api_client.get("/v1/jobs").json()
    assert len(jobs) == 1
    assert jobs[0]["container_id"] == "realcontainerid001"
    assert jobs[0]["docker_backed"] is True


def test_stop_cell_job_is_supported(api_client, mock_docker_client):
    """DELETE /v1/jobs/{id} must succeed for cell jobs and actually stop the container."""
    container = mock_docker_client.containers.create.return_value
    container.id = "stoppablecell001"
    api_client.post(CELL_URL, json={"code": "pass", "image": "python:3.11-slim"})

    jobs = api_client.get("/v1/jobs").json()
    assert len(jobs) == 1
    job_id = jobs[0]["job_id"]

    # Wire containers.get so stop_job can retrieve the container.
    mock_docker_client.containers.get.return_value = container

    resp = api_client.delete(f"/v1/jobs/{job_id}")
    assert resp.status_code == 200

    # The stop path must have called containers.get with the real container ID
    # and then invoked stop() on the returned container object.
    mock_docker_client.containers.get.assert_called_with(job_id)
    container.stop.assert_called()


# ── log storage ───────────────────────────────────────────────────────────────

def test_cell_logs_stored_in_job_record(api_client, mock_docker_client):
    """Logs captured at cell exit are stored in the job record's stored_logs field."""
    container = mock_docker_client.containers.create.return_value
    container.id = "storedlogstest001"
    container.logs.return_value = b"Hello from cell\n"
    api_client.post(CELL_URL, json={"code": "print('Hello from cell')", "image": "python:3.11-slim"})

    import app.main as m
    record = m.job_store.get("storedlogstest001")
    assert record is not None
    assert record.stored_logs == "Hello from cell\n"


def test_cell_logs_truncated_at_256kib(api_client, mock_docker_client):
    """Logs exceeding 256 KiB are truncated before storage."""
    from app.jobs import JobStore
    container = mock_docker_client.containers.create.return_value
    container.id = "storedlogstest002"
    # Generate slightly over 256 KiB of output.
    big_output = b"x" * (JobStore.LOG_MAX_BYTES + 1024)
    container.logs.return_value = big_output
    api_client.post(CELL_URL, json={"code": "pass", "image": "python:3.11-slim"})

    import app.main as m
    record = m.job_store.get("storedlogstest002")
    assert record is not None
    assert len(record.stored_logs.encode("utf-8")) <= JobStore.LOG_MAX_BYTES + 100  # marker overhead
    assert "truncated" in record.stored_logs


# ── job eviction ──────────────────────────────────────────────────────────────

def test_job_store_evicts_oldest_stopped_when_full(api_client, mock_docker_client):
    """When MAX_JOBS is reached, the oldest stopped jobs are evicted on the next register."""
    from app.jobs import JobStore
    import app.main as m

    original_max = JobStore.MAX_JOBS
    try:
        # Lower the cap so we can test eviction without registering 500 jobs.
        JobStore.MAX_JOBS = 3

        ids = ["evicttest00a", "evicttest00b", "evicttest00c"]
        for cid in ids:
            container = mock_docker_client.containers.create.return_value
            container.id = cid
            api_client.post(CELL_URL, json={"code": "pass", "image": "python:3.11-slim"})

        # All three stopped jobs are in the store.
        assert m.job_store.get("evicttest00a") is not None
        assert m.job_store.get("evicttest00b") is not None
        assert m.job_store.get("evicttest00c") is not None

        # Registering a fourth job must evict the oldest one (evicttest00a).
        container = mock_docker_client.containers.create.return_value
        container.id = "evicttest00d"
        api_client.post(CELL_URL, json={"code": "pass", "image": "python:3.11-slim"})

        assert m.job_store.get("evicttest00a") is None, "oldest job should have been evicted"
        assert m.job_store.get("evicttest00d") is not None, "newest job must be present"
    finally:
        JobStore.MAX_JOBS = original_max
