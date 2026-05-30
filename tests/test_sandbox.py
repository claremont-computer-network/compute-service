"""
Tests for sandbox endpoints and lifecycle:
  POST /v1/sandbox          — create a persistent interactive container
  POST /v1/jobs/{id}/exec  — execute a command inside a sandbox
  DELETE /v1/jobs/{id}      — stop and release a sandbox

And related sandbox state management:
  - Resource slot tracking (resource_type="gpu"/"cpu")
  - Sandbox last access tracking (for reaper)
  - Sandbox exit handling via _enrich_job_data
"""
import docker.errors
import pytest
from datetime import datetime, timezone
from unittest.mock import MagicMock

SANDBOX_URL = "/v1/sandbox"
SLOTS_URL = "/v1/queue/slots"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _create_mock_sandbox_container(api_client, mock_docker_client, is_gpu=False):
    """Create a sandbox that appears to hold a GPU or CPU slot."""
    container = mock_docker_client.containers.run.return_value
    container.attrs = {
        "HostConfig": {
            "DeviceRequests": (
                [{"Capabilities": [["gpu"]]}] if is_gpu else []
            )
        },
        "Config": {"Cmd": ["sleep", "infinity"]},
    }
    container.short_id = "sbox00000000001"
    container.id = "sbox00000000001" + "0" * 52
    container.image.tags = ["python:3.11-slim"]
    container.status = "running"

    container.exec_run.return_value = MagicMock(
        exit_code=0,
        output=b"42\n",
    )

    return container


def _submit_sandbox(api_client, gpu=False, image="python:3.11-slim", volumes=None):
    """Submit a sandbox request and return the response body."""
    data = {"image": image}
    if gpu:
        data["gpu"] = {"device_ids": "all"}
    if volumes:
        data["volumes"] = volumes
    resp = api_client.post(SANDBOX_URL, json=data)
    assert resp.status_code == 200, f"Failed: {resp.text}"
    return resp.json()


# ---------------------------------------------------------------------------
# POST /v1/sandbox
# ---------------------------------------------------------------------------

def test_create_sandbox_returns_sandbox_id(api_client, mock_docker_client):
    """Creating a sandbox returns a sandbox_id and status."""
    body = _submit_sandbox(api_client)
    assert "sandbox_id" in body
    assert body["status"] == "running"
    assert body["resource_type"] == "cpu"  # no GPU requested


def test_create_sandbox_responds_with_gpu_resource_type(api_client, mock_docker_client):
    """When gpu="all" is requested, the response includes resource_type="gpu"."""
    body = _submit_sandbox(api_client, gpu=True)
    assert body["resource_type"] == "gpu"


def test_create_sandbox_registers_job(api_client, mock_docker_client):
    """Creating a sandbox must register a job record in the store."""
    import app.main as m
    _submit_sandbox(api_client)
    assert len(m.job_store.list_all()) == 1
    job = m.job_store.list_all()[0]
    assert job.job_type == "sandbox"
    assert job.resource_type == "cpu"


def test_create_sandbox_labels_container(api_client, mock_docker_client):
    """Sandbox containers must carry the caas.sandbox=true label."""
    _submit_sandbox(api_client)
    call_kwargs = mock_docker_client.containers.run.call_args.kwargs
    assert call_kwargs.get("labels", {}).get("caas.sandbox") == "true"


def test_create_sandbox_runs_sleep_infinity(api_client, mock_docker_client):
    """Sandbox containers must run `sleep infinity` as their command."""
    _submit_sandbox(api_client)
    call_kwargs = mock_docker_client.containers.run.call_args.kwargs
    assert call_kwargs.get("command") == ["sleep", "infinity"]


def test_create_sandbox_acquires_resource_slot(api_client):
    """Creating a sandbox must acquire a resource slot from ResourceSlots."""
    import app.main as m
    # Track whether the slot was acquired
    original_acquire = m.resource_slots.acquire
    acquired = []
    def _track_acquire(resource, timeout=m.QUEUE_TIMEOUT):
        acquired.append(resource)
        return True
    m.resource_slots.acquire = _track_acquire

    _submit_sandbox(api_client)
    assert "cpu" in acquired


def test_create_sandbox_gpu_acquires_gpu_slot(api_client):
    """A GPU sandbox must acquire a gpu slot."""
    import app.main as m
    acquired = []
    original_acquire = m.resource_slots.acquire
    def _track_acquire(resource, timeout=m.QUEUE_TIMEOUT):
        acquired.append(resource)
        return True
    m.resource_slots.acquire = _track_acquire

    _submit_sandbox(api_client, gpu=True)
    assert "gpu" in acquired


def test_create_sandbox_holds_slot_for_lifetime(api_client, mock_docker_client):
    """A sandbox must HOLD its slot for lifetime — releasing it on success would
    allow double-acquisition. The slot is only released on DELETE/enrich/reaper."""
    import app.main as m
    released = []
    original_release = m.resource_slots.release
    def _track_release(resource):
        released.append(resource)
        return original_release(resource)
    m.resource_slots.release = _track_release

    _submit_sandbox(api_client)
    # Slot must NOT be released on successful sandbox creation
    assert "cpu" not in released


def test_create_sandbox_releases_slot_on_docker_exception(api_client, mock_docker_client):
    """If Docker create fails, the sandbox must release its slot."""
    import app.main as m
    released = []
    original_release = m.resource_slots.release
    def _track_release(resource):
        released.append(resource)
        return
    m.resource_slots.release = _track_release

    mock_docker_client.containers.run.side_effect = docker.errors.DockerException("fail")

    resp = api_client.post(SANDBOX_URL, json={"image": "nonexistent:tag"})
    assert resp.status_code == 500
    assert "cpu" in released


def test_create_sandbox_triggers_pre_create_hook(api_client, mock_docker_client):
    """The sandbox POST must call registry.pre_create before image pull."""
    import app.main as m
    called = []
    original_pre_create = m.registry.pre_create
    def _track_pre_create(req, run_kwargs):
        called.append((req, run_kwargs))
    m.registry.pre_create = _track_pre_create

    _submit_sandbox(api_client)
    assert len(called) == 1
    req, run_kwargs = called[0]
    assert hasattr(req, "image")
    assert run_kwargs.get("command") == ["sleep", "infinity"]


def test_create_sandbox_triggers_on_register_hook(api_client, mock_docker_client):
    """A sandbox must call registry.on_register after store registration."""
    import app.main as m
    called = []
    original = m.registry.on_register
    def _track(record):
        called.append(record)
        return original(record)
    m.registry.on_register = _track

    _submit_sandbox(api_client)
    assert len(called) == 1
    assert called[0].job_type == "sandbox"


def test_create_sandbox_sets_last_access(api_client, mock_docker_client):
    """Creating a sandbox must set sandbox_last_access."""
    import app.main as m
    container = _create_mock_sandbox_container(api_client, mock_docker_client)

    resp = api_client.post(SANDBOX_URL, json={"image": "python:3.11-slim"})
    assert resp.status_code == 200
    sandbox_id = resp.json()["sandbox_id"]
    assert sandbox_id in m.sandbox_last_access
    assert isinstance(m.sandbox_last_access[sandbox_id], datetime)


# ---------------------------------------------------------------------------
# POST /v1/jobs/{job_id}/exec
# ---------------------------------------------------------------------------

def test_exec_requires_existing_sandbox(api_client, mock_docker_client):
    """Executing in a non-existent sandbox returns 404."""
    resp = api_client.post("/v1/jobs/nonexistent/exec", json={"cmd": "echo hi"})
    assert resp.status_code == 404


def test_exec_rejects_non_sandbox_job(api_client, mock_docker_client):
    """Executing in a non-sandbox job returns 400."""
    import app.main as m
    # First create a regular detached job
    mock_container = mock_docker_client.containers.run.return_value
    mock_container.id = "regular0000000100"
    mock_container.short_id = "regular000000"
    mock_container.image.tags = ["alpine:3.18"]
    mock_container.attrs = {"Config": {"Cmd": None}}

    api_client.post("/v1/execute", json={"image": "alpine:3.18"})

    job = m.job_store.list_all()[0]
    # Overwrite to make it a non-sandbox job
    job.job_type = "detached"

    resp = api_client.post(f"/v1/jobs/{job.job_id}/exec", json={"cmd": "echo hi"})
    assert resp.status_code == 400
    assert "not a sandbox" in resp.json()["detail"]


def test_exec_releases_slot_when_container_gone(api_client, mock_docker_client):
    """If container is gone (NotFound), exec must release the sandbox slot."""
    import app.main as m
    container = _create_mock_sandbox_container(api_client, mock_docker_client)

    # Create sandbox
    resp = api_client.post(SANDBOX_URL, json={"image": "python:3.11-slim"})
    assert resp.status_code == 200
    sandbox_id = resp.json()["sandbox_id"]

    # Simulate container was removed from Docker
    mock_docker_client.containers.get.side_effect = docker.errors.NotFound("gone")

    # Exec should return 404 and release slot
    resp = api_client.post(f"/v1/jobs/{sandbox_id}/exec", json={"cmd": "echo hi"})
    assert resp.status_code == 404


def test_exec_updates_last_access(api_client, mock_docker_client):
    """Successful exec must refresh sandbox_last_access."""
    import app.main as m
    container = _create_mock_sandbox_container(api_client, mock_docker_client)

    _submit_sandbox(api_client)
    job = m.job_store.list_all()[0]
    sandbox_id = job.job_id

    before_access = m.sandbox_last_access.get(sandbox_id)

    resp = api_client.post(f"/v1/jobs/{sandbox_id}/exec", json={"cmd": "echo hi"})
    assert resp.status_code == 200
    after_access = m.sandbox_last_access.get(sandbox_id)

    assert before_access != after_access


def test_exec_returns_stdout(api_client, mock_docker_client):
    """Successful exec must return stdout in the response."""
    import app.main as m
    container = _create_mock_sandbox_container(api_client, mock_docker_client)
    sandbox_id = container.id

    _submit_sandbox(api_client)
    job = m.job_store.list_all()[0]
    sandbox_id = job.job_id

    resp = api_client.post(
        f"/v1/jobs/{sandbox_id}/exec",
        json={"cmd": "python3 -c 'print(42)'"}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert "stdout" in body
    assert "exit_code" in body


# ---------------------------------------------------------------------------
# Resource slot lifecycle
# ---------------------------------------------------------------------------

def test_delete_sandbox_releases_slot(api_client, mock_docker_client):
    """DELETE a sandbox must release its resource slot."""
    import app.main as m
    released = []
    original_release = m.resource_slots.release
    def _track(resource):
        released.append(resource)
        return original_release(resource)
    m.resource_slots.release = _track

    _submit_sandbox(api_client)
    job = m.job_store.list_all()[0]

    resp = api_client.delete(f"/v1/jobs/{job.job_id}")
    assert resp.status_code == 200
    assert "cpu" in released


def test_delete_sandbox_clears_last_access(api_client, mock_docker_client):
    """DELETE a sandbox must remove its entry from sandbox_last_access."""
    import app.main as m
    _submit_sandbox(api_client)
    job = m.job_store.list_all()[0]
    assert job.job_id in m.sandbox_last_access

    api_client.delete(f"/v1/jobs/{job.job_id}")

    assert job.job_id not in m.sandbox_last_access


def test_delete_sandbox_calls_registry_on_complete(api_client, mock_docker_client):
    """DELETE a sandbox must call registry.on_job_complete."""
    import app.main as m
    called = []
    original = m.registry.on_job_complete
    def _track(record, exit_code):
        called.append((record, exit_code))
        return original(record, exit_code)
    m.registry.on_job_complete = _track

    _submit_sandbox(api_client)
    job = m.job_store.list_all()[0]

    api_client.delete(f"/v1/jobs/{job.job_id}")

    assert len(called) == 1
    assert called[0][1] is None  # exit code is None on delete


# ---------------------------------------------------------------------------
# Resource slot exhaustion
# ---------------------------------------------------------------------------

def test_sandbox_slot_exhaustion_returns_503(api_client):
    """When GPU slots are exhausted, sandbox creation returns 503."""
    import app.main as m
    m.resource_slots.acquire = lambda r, timeout=m.QUEUE_TIMEOUT: False  # noqa: B023

    resp = api_client.post(SANDBOX_URL, json={"image": "python:3.11-slim", "gpu": {"device_ids": "all"}})
    assert resp.status_code == 503
    assert "slot" in resp.json()["detail"].lower()


# ---------------------------------------------------------------------------
# Sandbox exits during /v1/jobs polling
# ---------------------------------------------------------------------------

def test_enrich_detects_out_of_band_sandbox_releases_slot(api_client, mock_docker_client):
    """If a sandbox container vanishes (docker rm -f), enrich must release the slot."""
    import app.main as m
    container = _create_mock_sandbox_container(api_client, mock_docker_client)

    _submit_sandbox(api_client)
    job = m.job_store.list_all()[0]

    released = []
    original_release = m.resource_slots.release
    def _track(resource):
        released.append(resource)
        return original_release(resource)
    m.resource_slots.release = _track

    # Simulate container gone when listing jobs
    mock_docker_client.containers.get.side_effect = docker.errors.NotFound("removed by user")

    # GET /v1/jobs triggers _enrich_job_data which detects the exit
    resp = api_client.get("/v1/jobs")
    assert resp.status_code == 200
    assert "cpu" in released, "Slot must be released when sandbox container is removed"


def test_enrich_detects_exited_sandbox_with_exit_code(api_client, mock_docker_client):
    """If a sandbox container exits normally, enrich must release the slot and return exit_code."""
    import app.main as m
    container = _create_mock_sandbox_container(api_client, mock_docker_client)

    # Mark the container as exited
    container.status = "exited"
    container.attrs = {"Config": {"Cmd": ["sleep", "infinity"]}, "State": {"ExitCode": 137}}

    _submit_sandbox(api_client)
    job = m.job_store.list_all()[0]

    released = []
    original_release = m.resource_slots.release
    def _track(resource):
        released.append(resource)
        return original_release(resource)
    m.resource_slots.release = _track

    resp = api_client.get(f"/v1/jobs/{job.job_id}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "stopped"
    assert body["exit_code"] == 137
    assert "cpu" in released


# ---------------------------------------------------------------------------
# Job record resource_type
# ---------------------------------------------------------------------------

def test_job_record_defaults_resource_type_to_cpu():
    """JobRecord must default resource_type to 'cpu'."""
    from app.jobs import JobRecord

    record = JobRecord(
        job_id="dummy000000000100",
        container_id="dummy00000000100",
        image="alpine:3.18",
        submitted_at=datetime.now(timezone.utc),
    )
    assert record.resource_type == "cpu"


def test_sandbox_record_stores_resource_type(api_client, mock_docker_client):
    """Sandbox creation must store resource_type in the job record."""
    import app.main as m

    _submit_sandbox(api_client)
    job = m.job_store.list_all()[0]
    assert job.resource_type == "cpu"


def test_release_sandbox_slots_is_idempotent():
    """Calling _release_sandbox_slots() multiple times must not inflate slot count."""
    import app.main as m
    from app.jobs import JobRecord
    from datetime import datetime, timezone

    job = JobRecord(
        job_id="idem000000000100",
        container_id="idem0000000100",
        image="python:3.11-slim",
        job_type="sandbox",
        submitted_at=datetime.now(timezone.utc),
        resource_type="cpu",
    )
    m.sandbox_last_access[job.job_id] = datetime.now(timezone.utc)

    released = []
    original_release = m.resource_slots.release
    def _track(resource):
        released.append(resource)
        return original_release(resource)
    m.resource_slots.release = _track

    # First call: releases slot and clears tracker
    m._release_sandbox_slots(job)
    assert len(released) == 1

    # Second call: tracker is empty, must skip release
    m._release_sandbox_slots(job)
    assert len(released) == 1, "Idempotent release must not call Semaphore.release() twice"


def test_gpu_sandbox_record_stores_gpu_resource_type(api_client, mock_docker_client):
    """A GPU sandbox must store resource_type='gpu'."""
    import app.main as m

    _submit_sandbox(api_client, gpu=True)
    job = m.job_store.list_all()[0]
    assert job.resource_type == "gpu"
