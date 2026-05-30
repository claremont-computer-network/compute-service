"""
Tests for the job registry endpoints:
  GET    /v1/jobs
  GET    /v1/jobs/{job_id}
  DELETE /v1/jobs/{job_id}

and for execute() registering jobs on detach=True.
"""
import pytest
import docker.errors
from unittest.mock import MagicMock

EXEC_URL  = "/v1/execute"
JOBS_URL  = "/v1/jobs"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _submit_detach(api_client):
    """Submit a minimal detached job and return the response body."""
    resp = api_client.post(EXEC_URL, json={"image": "alpine:3.18"})
    assert resp.status_code == 200
    return resp.json()


# ---------------------------------------------------------------------------
# execute() registers a job on detach=True
# ---------------------------------------------------------------------------

def test_execute_detach_registers_job(api_client, mock_docker_client):
    """A detached job must appear in the store immediately after submission."""
    import app.main as m
    _submit_detach(api_client)
    assert len(m.job_store.list_all()) == 1


def test_execute_detach_job_has_correct_image(api_client, mock_docker_client):
    """The registered job record must carry the submitted image name."""
    import app.main as m
    api_client.post(EXEC_URL, json={"image": "pytorch/pytorch:latest"})
    job = m.job_store.list_all()[0]
    assert job.image == "pytorch/pytorch:latest"


def test_execute_detach_job_id_is_full_container_id(api_client, mock_docker_client):
    """job_id must equal the full container ID (not the short_id prefix)."""
    import app.main as m
    body = _submit_detach(api_client)
    job = m.job_store.list_all()[0]
    assert job.job_id == body["container_id"]


def test_execute_sync_does_not_register_job(api_client, mock_docker_client):
    """Synchronous (detach=False) runs are fire-and-forget; no registry entry."""
    import app.main as m
    mock_docker_client.containers.run.return_value = b"done\n"
    api_client.post(EXEC_URL, json={"image": "alpine:3.18", "detach": False})
    assert len(m.job_store.list_all()) == 0


# ---------------------------------------------------------------------------
# GET /v1/jobs
# ---------------------------------------------------------------------------

def test_list_jobs_empty(api_client, mock_docker_client):
    """No jobs submitted → empty list."""
    resp = api_client.get(JOBS_URL)
    assert resp.status_code == 200
    assert resp.json() == []


def test_list_jobs_returns_submitted_job(api_client, mock_docker_client):
    """After one detached submit the list contains exactly that job."""
    _submit_detach(api_client)
    resp = api_client.get(JOBS_URL)
    assert resp.status_code == 200
    jobs = resp.json()
    assert len(jobs) == 1
    assert jobs[0]["image"] == "alpine:3.18"
    assert jobs[0]["status"] == "running"


def test_list_jobs_includes_resource_stats(api_client, mock_docker_client):
    """GET /v1/jobs attaches resource stats for running containers."""
    _submit_detach(api_client)

    # Provide a minimal stats payload that _fetch_resources can parse.
    # Must clear the default side_effect first (see conftest).
    container = mock_docker_client.containers.get.return_value
    container.stats.side_effect = None
    container.stats.return_value = _fake_stats()

    resp = api_client.get(JOBS_URL)
    assert resp.status_code == 200
    jobs = resp.json()
    assert jobs[0]["resources"] is not None
    assert "cpu_percent" in jobs[0]["resources"]
    assert "mem_usage_mib" in jobs[0]["resources"]


def test_parse_gpu_stats_computes_memory_percent(monkeypatch):
    """GPU memory percent is derived from used/total nvidia-smi fields."""
    import types
    from app.jobs import _parse_gpu_stats

    def _fake_run(*args, **kwargs):
        return types.SimpleNamespace(
            returncode=0,
            stdout="0, NVIDIA RTX, 45, 512, 8192, 17\n",
        )

    monkeypatch.setattr("subprocess.run", _fake_run)

    stats = _parse_gpu_stats()
    assert stats is not None
    assert stats[0].memory_used_mib == 512.0
    assert stats[0].memory_total_mib == 8192.0
    assert stats[0].memory_percent == 6.25
    assert stats[0].utilization_percent == 17.0


def test_list_jobs_marks_stopped_when_container_gone(api_client, mock_docker_client):
    """If the container has vanished, GET /v1/jobs marks the job stopped."""
    _submit_detach(api_client)
    mock_docker_client.containers.get.side_effect = docker.errors.NotFound("gone")

    resp = api_client.get(JOBS_URL)
    assert resp.status_code == 200
    assert resp.json()[0]["status"] == "stopped"


# ---------------------------------------------------------------------------
# GET /v1/jobs/{job_id}
# ---------------------------------------------------------------------------

def test_get_job_returns_record(api_client, mock_docker_client):
    """GET /v1/jobs/{job_id} returns the specific job record."""
    _submit_detach(api_client)
    import app.main as m
    job_id = m.job_store.list_all()[0].job_id

    container = mock_docker_client.containers.get.return_value
    container.stats.side_effect = None
    container.stats.return_value = _fake_stats()

    resp = api_client.get(f"{JOBS_URL}/{job_id}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["job_id"] == job_id
    assert body["status"] == "running"


def test_get_job_404_for_unknown_id(api_client, mock_docker_client):
    """GET /v1/jobs/nonexistent returns 404."""
    resp = api_client.get(f"{JOBS_URL}/nonexistent")
    assert resp.status_code == 404
    assert "not found" in resp.json()["detail"].lower()


def test_get_job_marks_stopped_when_container_gone(api_client, mock_docker_client):
    """If the container has disappeared, GET /v1/jobs/{id} marks it stopped."""
    _submit_detach(api_client)
    import app.main as m
    job_id = m.job_store.list_all()[0].job_id
    mock_docker_client.containers.get.side_effect = docker.errors.NotFound("gone")

    resp = api_client.get(f"{JOBS_URL}/{job_id}")
    assert resp.status_code == 200
    assert resp.json()["status"] == "stopped"


# ---------------------------------------------------------------------------
# DELETE /v1/jobs/{job_id}
# ---------------------------------------------------------------------------

def test_stop_job_returns_stopped_status(api_client, mock_docker_client):
    """DELETE /v1/jobs/{job_id} returns {"status": "stopped"}."""
    _submit_detach(api_client)
    import app.main as m
    job_id = m.job_store.list_all()[0].job_id

    resp = api_client.delete(f"{JOBS_URL}/{job_id}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "stopped"
    assert body["job_id"] == job_id


def test_stop_job_calls_docker_stop_and_remove(api_client, mock_docker_client):
    """DELETE /v1/jobs/{job_id} calls container.stop() and container.remove()."""
    _submit_detach(api_client)
    import app.main as m
    job_id = m.job_store.list_all()[0].job_id
    container_mock = mock_docker_client.containers.get.return_value

    api_client.delete(f"{JOBS_URL}/{job_id}")

    container_mock.stop.assert_called_once()
    container_mock.remove.assert_called_once()


def test_stop_job_marks_stopped_before_remove(api_client, mock_docker_client):
    """mark_stopped() must be called even when remove() raises DockerException."""
    import app.main as m
    import docker.errors as de

    _submit_detach(api_client)
    job_id = m.job_store.list_all()[0].job_id
    container_mock = mock_docker_client.containers.get.return_value

    # stop() succeeds, remove() blows up
    call_count = 0
    def _get_side_effect(cid):
        nonlocal call_count
        call_count += 1
        if call_count == 2:   # second containers.get() is for remove()
            raise de.DockerException("remove failed")
        return container_mock
    mock_docker_client.containers.get.side_effect = _get_side_effect

    resp = api_client.delete(f"{JOBS_URL}/{job_id}")
    # remove() failure propagates as 500 …
    assert resp.status_code == 500
    # … but the registry must still reflect the stopped state
    assert m.job_store.get(job_id).status == "stopped"


def test_stop_job_404_for_unknown_id(api_client, mock_docker_client):
    """DELETE /v1/jobs/nonexistent returns 404."""
    resp = api_client.delete(f"{JOBS_URL}/nonexistent")
    assert resp.status_code == 404


def test_stop_job_tolerates_already_removed_container(api_client, mock_docker_client):
    """DELETE /v1/jobs/{id} succeeds even if the container is already gone."""
    _submit_detach(api_client)
    import app.main as m
    job_id = m.job_store.list_all()[0].job_id
    mock_docker_client.containers.get.side_effect = docker.errors.NotFound("gone")

    resp = api_client.delete(f"{JOBS_URL}/{job_id}")
    assert resp.status_code == 200
    assert resp.json()["status"] == "stopped"


def test_stop_job_updates_store_status(api_client, mock_docker_client):
    """After DELETE the in-store record must reflect status=stopped."""
    _submit_detach(api_client)
    import app.main as m
    job_id = m.job_store.list_all()[0].job_id

    api_client.delete(f"{JOBS_URL}/{job_id}")

    assert m.job_store.get(job_id).status == "stopped"


# ---------------------------------------------------------------------------
# hydrate_from_docker (startup recovery)
# ---------------------------------------------------------------------------

def test_hydrate_populates_store_from_running_containers(mock_docker_client):
    """hydrate_from_docker() registers pre-existing containers in the store."""
    from app.jobs import JobStore

    c = MagicMock()
    c.short_id = "pre00000000"           # 12-char, matching real Docker short IDs
    c.id = "pre00000000" + "0" * 52      # 64-char full ID
    c.image.tags = ["nginx:latest"]
    c.attrs = {"Config": {"Cmd": ["nginx", "-g", "daemon off;"]}}

    mock_docker_client.containers.list.return_value = [c]

    store = JobStore()
    store.hydrate_from_docker(mock_docker_client)

    # store is keyed by full container ID
    assert store.get(c.id) is not None
    assert store.get(c.id).image == "nginx:latest"


def test_hydrate_skips_already_known_jobs(mock_docker_client):
    """hydrate_from_docker() does not overwrite jobs already in the store."""
    from app.jobs import JobStore
    from datetime import datetime, timezone

    store = JobStore()
    c = MagicMock()
    c.short_id = "knownid00000"          # 12-char
    c.id = "knownid00000" + "0" * 52     # 64-char full ID
    c.image.tags = ["alpine:3.18"]
    c.attrs = {"Config": {"Cmd": None}}

    # pre-register with a known timestamp (keyed by full ID)
    known_time = datetime(2026, 1, 1, tzinfo=timezone.utc)
    from app.jobs import JobRecord
    store._jobs[c.id] = JobRecord(
        job_id=c.id,
        container_id=c.id,
        image="alpine:3.18",
        submitted_at=known_time,
    )

    mock_docker_client.containers.list.return_value = [c]
    store.hydrate_from_docker(mock_docker_client)

    # submitted_at must not be overwritten with epoch
    assert store.get(c.id).submitted_at == known_time


def test_hydrate_filters_by_caas_label(mock_docker_client):
    """hydrate_from_docker() passes the caas.managed=true label filter to Docker
    so the dispatcher container itself is never included in the job list."""
    from app.jobs import JobStore

    store = JobStore()
    mock_docker_client.containers.list.return_value = []
    store.hydrate_from_docker(mock_docker_client)

    mock_docker_client.containers.list.assert_called_once_with(
        filters={"label": "caas.managed=true"}
    )


def test_execute_detach_container_has_caas_label(api_client, mock_docker_client):
    """containers.run() must include the caas.managed=true label so hydration
    and external tooling can distinguish dispatcher-managed containers."""
    _submit_detach(api_client)
    call_kwargs = mock_docker_client.containers.run.call_args.kwargs
    assert call_kwargs.get("labels", {}).get("caas.managed") == "true"


# ---------------------------------------------------------------------------
# Exited-container state synchronisation (round-4 review)
# ---------------------------------------------------------------------------

def test_list_jobs_detects_exited_container(api_client, mock_docker_client):
    """GET /v1/jobs marks a job stopped with the real exit_code when its container
    has exited but the container object still exists in Docker."""
    _submit_detach(api_client)
    container = mock_docker_client.containers.get.return_value
    container.status = "exited"
    container.attrs = {"Config": {"Cmd": None}, "State": {"ExitCode": 42}}

    resp = api_client.get(JOBS_URL)
    assert resp.status_code == 200
    job = resp.json()[0]
    assert job["status"] == "stopped"
    assert job["exit_code"] == 42


def test_get_job_detects_exited_container(api_client, mock_docker_client):
    """GET /v1/jobs/{id} marks a job stopped with the real exit_code when its
    container has exited but the container object still exists in Docker."""
    _submit_detach(api_client)
    import app.main as m
    job_id = m.job_store.list_all()[0].job_id

    container = mock_docker_client.containers.get.return_value
    container.status = "exited"
    container.attrs = {"Config": {"Cmd": None}, "State": {"ExitCode": 1}}

    resp = api_client.get(f"{JOBS_URL}/{job_id}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "stopped"
    assert body["exit_code"] == 1
    # store must also be updated
    assert m.job_store.get(job_id).status == "stopped"
    assert m.job_store.get(job_id).exit_code == 1


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------

def _fake_stats() -> dict:
    """Minimal Docker stats payload sufficient for _fetch_resources()."""
    return {
        "cpu_stats": {
            "cpu_usage": {"total_usage": 2_000_000_000},
            "system_cpu_usage": 100_000_000_000,
            "online_cpus": 4,
        },
        "precpu_stats": {
            "cpu_usage": {"total_usage": 1_000_000_000},
            "system_cpu_usage": 90_000_000_000,
        },
        "memory_stats": {
            "usage": 256 * 1024 * 1024,    # 256 MiB
            "limit": 8192 * 1024 * 1024,   # 8 GiB
        },
    }
