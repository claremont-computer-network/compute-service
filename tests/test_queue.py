"""
Tests for the resource queue (ResourceSlots / _acquire_slot).

Covers:
  - Slot is acquired and released after a successful blocking execute()
  - Slot is acquired and released after a successful execute_cell()
  - Slot is released even when containers.run raises DockerException
  - Slot is released even when containers.run raises ContainerError (non-zero exit)
  - 503 is returned when all slots are exhausted (QUEUE_TIMEOUT=0)
  - GPU vs CPU resource selection (gpu={"device_ids":"all"} → "gpu", absent → "cpu")
"""
import threading
import pytest
import docker.errors
from unittest.mock import MagicMock, patch

EXEC_URL = "/v1/execute"
CELL_URL = "/v1/execute/cell"

# Payload fragments for GPU vs CPU requests
_GPU_FIELD = {"device_ids": "all"}   # valid GpuRequest body
_NO_GPU = None                        # omit the field entirely


def _gpu_payload(use_gpu: bool) -> dict:
    """Return the gpu key/value to merge into a request body."""
    return {"gpu": _GPU_FIELD} if use_gpu else {}


def _resource(use_gpu: bool) -> str:
    return "gpu" if use_gpu else "cpu"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _slot_value(sem: threading.Semaphore) -> int:
    """Return the current internal counter of a Semaphore without acquiring it."""
    return sem._value  # CPython implementation detail; fine for tests


def _drain(sem: threading.Semaphore) -> int:
    """Acquire all available slots; return the number acquired."""
    acquired = 0
    while sem.acquire(blocking=False):
        acquired += 1
    return acquired


# ---------------------------------------------------------------------------
# Slot released after success
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("use_gpu", [False, True])
def test_slot_released_after_blocking_execute(api_client, mock_docker_client, use_gpu):
    """After a blocking execute() the slot count returns to its original value."""
    import app.main as m

    resource = _resource(use_gpu)
    before = _slot_value(m.resource_slots._slots[resource])

    mock_docker_client.containers.run.return_value = b"ok"
    payload = {"image": "alpine:3.18", "detach": False, **_gpu_payload(use_gpu)}
    resp = api_client.post(EXEC_URL, json=payload)
    assert resp.status_code == 200

    after = _slot_value(m.resource_slots._slots[resource])
    assert after == before, (
        f"Expected {resource} slot count to return to {before} after execute, got {after}"
    )


@pytest.mark.parametrize("use_gpu", [False, True])
def test_slot_released_after_execute_cell(api_client, mock_docker_client, use_gpu):
    """After execute_cell() the slot count returns to its original value."""
    import app.main as m

    resource = _resource(use_gpu)
    before = _slot_value(m.resource_slots._slots[resource])

    mock_docker_client.containers.run.return_value = b"result"
    payload = {"image": "python:3.11-slim", "code": "print(1)", **_gpu_payload(use_gpu)}
    resp = api_client.post(CELL_URL, json=payload)
    assert resp.status_code == 200

    after = _slot_value(m.resource_slots._slots[resource])
    assert after == before


# ---------------------------------------------------------------------------
# Slot released on error paths
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("use_gpu", [False, True])
def test_slot_released_on_docker_exception(api_client, mock_docker_client, use_gpu):
    """Slot is released even when containers.run raises a DockerException."""
    import app.main as m

    resource = _resource(use_gpu)
    before = _slot_value(m.resource_slots._slots[resource])

    mock_docker_client.containers.run.side_effect = docker.errors.DockerException("boom")
    payload = {"image": "alpine:3.18", "detach": False, **_gpu_payload(use_gpu)}
    resp = api_client.post(EXEC_URL, json=payload)
    assert resp.status_code == 500

    after = _slot_value(m.resource_slots._slots[resource])
    assert after == before, (
        f"Expected slot to be released after DockerException; {resource}: {before} → {after}"
    )


@pytest.mark.parametrize("use_gpu", [False, True])
def test_slot_released_on_container_error(api_client, mock_docker_client, use_gpu):
    """Slot is released even when the container exits non-zero (ContainerError)."""
    import app.main as m

    resource = _resource(use_gpu)
    before = _slot_value(m.resource_slots._slots[resource])

    container_stub = MagicMock()
    container_stub.logs.return_value = b"error output"
    mock_docker_client.containers.run.side_effect = docker.errors.ContainerError(
        container=container_stub,
        exit_status=1,
        command="python -c 'raise'",
        image="python:3.11-slim",
        stderr=b"error output",
    )
    payload = {"image": "python:3.11-slim", "detach": False, **_gpu_payload(use_gpu)}
    resp = api_client.post(EXEC_URL, json=payload)
    # ContainerError → non-500 body with exit_code, not an HTTP error
    assert resp.status_code == 200
    assert resp.json()["exit_code"] != 0

    after = _slot_value(m.resource_slots._slots[resource])
    assert after == before


@pytest.mark.parametrize("use_gpu", [False, True])
def test_cell_slot_released_on_docker_exception(api_client, mock_docker_client, use_gpu):
    """execute_cell() releases its slot even on DockerException."""
    import app.main as m

    resource = _resource(use_gpu)
    before = _slot_value(m.resource_slots._slots[resource])

    mock_docker_client.containers.run.side_effect = docker.errors.DockerException("cell boom")
    payload = {"image": "python:3.11-slim", "code": "x=1", **_gpu_payload(use_gpu)}
    resp = api_client.post(CELL_URL, json=payload)
    assert resp.status_code == 500

    after = _slot_value(m.resource_slots._slots[resource])
    assert after == before


# ---------------------------------------------------------------------------
# 503 when queue is full
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("use_gpu,resource", [(False, "cpu"), (True, "gpu")])
def test_queue_full_returns_503(api_client, mock_docker_client, use_gpu, resource):
    """When all slots are exhausted and QUEUE_TIMEOUT=0, a 503 is returned immediately."""
    import app.main as m

    # Drain all available slots so the semaphore is at zero.
    drained = _drain(m.resource_slots._slots[resource])

    # Set timeout to 0 so _acquire_slot fails instantly.
    original_timeout = m.QUEUE_TIMEOUT
    m.QUEUE_TIMEOUT = 0
    try:
        payload = {"image": "alpine:3.18", "detach": False, **_gpu_payload(use_gpu)}
        resp = api_client.post(EXEC_URL, json=payload)
        assert resp.status_code == 503
        body = resp.json()
        assert resource.upper() in body["detail"]
    finally:
        m.QUEUE_TIMEOUT = original_timeout
        # Restore drained slots so other tests aren't affected.
        for _ in range(drained):
            m.resource_slots._slots[resource].release()


@pytest.mark.parametrize("use_gpu,resource", [(False, "cpu"), (True, "gpu")])
def test_queue_full_returns_503_for_cell(api_client, mock_docker_client, use_gpu, resource):
    """execute_cell() also returns 503 when all slots are exhausted."""
    import app.main as m

    drained = _drain(m.resource_slots._slots[resource])
    original_timeout = m.QUEUE_TIMEOUT
    m.QUEUE_TIMEOUT = 0
    try:
        payload = {"image": "python:3.11-slim", "code": "x=1", **_gpu_payload(use_gpu)}
        resp = api_client.post(CELL_URL, json=payload)
        assert resp.status_code == 503
        assert resource.upper() in resp.json()["detail"]
    finally:
        m.QUEUE_TIMEOUT = original_timeout
        for _ in range(drained):
            m.resource_slots._slots[resource].release()


# ---------------------------------------------------------------------------
# Detached execute() releases slot immediately
# ---------------------------------------------------------------------------

def test_detach_releases_slot_immediately(api_client, mock_docker_client):
    """
    A detached (fire-and-forget) job should release the CPU slot as soon as
    the container is submitted, not after the container exits.
    """
    import app.main as m

    before = _slot_value(m.resource_slots._slots["cpu"])

    container = MagicMock()
    container.id = "detach123deadbeef"
    container.short_id = "detach123dead"
    container.image.tags = ["alpine:3.18"]
    container.attrs = {"Config": {"Cmd": None}, "State": {"ExitCode": 0}}
    container.status = "running"
    container.reload.return_value = None
    container.logs.return_value = b""
    container.stats.side_effect = Exception("no stats")
    mock_docker_client.containers.run.return_value = container

    resp = api_client.post(EXEC_URL, json={"image": "alpine:3.18", "detach": True})
    assert resp.status_code == 200

    after = _slot_value(m.resource_slots._slots["cpu"])
    assert after == before, (
        f"Expected slot to be released after detached submit; cpu: {before} → {after}"
    )
