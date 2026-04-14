"""
Tests for the three new plugin lifecycle hooks:
  - on_job_complete: fires for both detached and cell jobs when terminal
  - on_enrich: fires on every GET /v1/jobs and GET /v1/jobs/{id} response
  - PluginServices injection: plugins receive job_store + docker_client
"""
from unittest.mock import MagicMock, patch

import pytest

from app.core.plugin import CaasPlugin, PluginRegistry, PluginServices
from app.jobs import JobStore


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class _RecordingPlugin(CaasPlugin):
    """Plugin that records every hook call for assertion."""
    name = "recording"
    priority = 200

    def __init__(self):
        self.complete_calls: list[tuple] = []   # (record, exit_code)
        self.enrich_calls: list[tuple] = []     # (record, data)

    def on_job_complete(self, record, exit_code):
        self.complete_calls.append((record, exit_code))

    def on_enrich(self, record, data):
        data["_test_enriched"] = True
        self.enrich_calls.append((record, data))


CELL_URL = "/v1/execute/cell"
EXEC_URL = "/v1/execute"


@pytest.fixture()
def recording_plugin(mock_docker_client):
    """Register the recording plugin for the duration of a test."""
    from app.core.plugin import registry
    plugin = _RecordingPlugin()
    registry.register(plugin)
    yield plugin
    registry._plugins.remove(plugin)


# ---------------------------------------------------------------------------
# on_job_complete — detached jobs
# ---------------------------------------------------------------------------

def test_on_job_complete_fires_for_detached_job_via_enrich(
    api_client, mock_docker_client, recording_plugin
):
    """on_job_complete fires for a detached job when _enrich_job_data detects
    the container is in a terminal state."""
    container = mock_docker_client.containers.run.return_value
    container.id = "detached_complete_001"

    # Submit the job
    resp = api_client.post(EXEC_URL, json={"image": "python:3.11-slim", "detach": True})
    assert resp.status_code == 200

    # Simulate container reaching terminal state (Docker reports "exited")
    container.status = "exited"
    container.attrs = {"State": {"ExitCode": 0}, "Config": {"Cmd": None}}

    # Trigger _enrich_job_data via GET
    resp = api_client.get(f"/v1/jobs/{container.id}")
    assert resp.status_code == 200

    assert len(recording_plugin.complete_calls) == 1
    record, exit_code = recording_plugin.complete_calls[0]
    assert record.job_id == container.id
    assert exit_code == 0


def test_on_job_complete_fires_for_detached_job_not_found(
    api_client, mock_docker_client, recording_plugin
):
    """on_job_complete fires when the container is NotFound during enrich."""
    from docker.errors import NotFound

    container = mock_docker_client.containers.run.return_value
    container.id = "detached_notfound_001"

    api_client.post(EXEC_URL, json={"image": "python:3.11-slim", "detach": True})

    mock_docker_client.containers.get.side_effect = NotFound("gone")
    resp = api_client.get(f"/v1/jobs/{container.id}")
    assert resp.status_code == 200

    assert len(recording_plugin.complete_calls) == 1
    record, exit_code = recording_plugin.complete_calls[0]
    assert exit_code is None


def test_on_job_complete_fires_on_stop_job(
    api_client, mock_docker_client, recording_plugin
):
    """on_job_complete fires when DELETE /v1/jobs/{id} cancels a running job."""
    container = mock_docker_client.containers.run.return_value
    container.id = "stop_job_001"

    api_client.post(EXEC_URL, json={"image": "python:3.11-slim", "detach": True})

    resp = api_client.delete(f"/v1/jobs/{container.id}")
    assert resp.status_code == 200

    assert len(recording_plugin.complete_calls) == 1
    record, exit_code = recording_plugin.complete_calls[0]
    assert record.job_id == container.id


def test_on_job_complete_not_fired_twice_for_already_stopped(
    api_client, mock_docker_client, recording_plugin
):
    """on_job_complete should not fire on subsequent GET calls once the job is
    already marked stopped (the enrich fast-path returns early)."""
    container = mock_docker_client.containers.run.return_value
    container.id = "no_double_fire_001"

    api_client.post(EXEC_URL, json={"image": "python:3.11-slim", "detach": True})

    container.status = "exited"
    container.attrs = {"State": {"ExitCode": 0}, "Config": {"Cmd": None}}

    api_client.get(f"/v1/jobs/{container.id}")  # first GET — marks stopped, fires hook
    api_client.get(f"/v1/jobs/{container.id}")  # second GET — already stopped, no re-fire

    assert len(recording_plugin.complete_calls) == 1


# ---------------------------------------------------------------------------
# on_enrich
# ---------------------------------------------------------------------------

def test_on_enrich_fires_on_get_job(api_client, mock_docker_client, recording_plugin):
    """on_enrich is called for every GET /v1/jobs/{id} response."""
    container = mock_docker_client.containers.run.return_value
    container.id = "enrich_get_001"

    api_client.post(EXEC_URL, json={"image": "python:3.11-slim", "detach": True})
    resp = api_client.get(f"/v1/jobs/{container.id}")

    assert resp.status_code == 200
    assert resp.json().get("_test_enriched") is True
    assert len(recording_plugin.enrich_calls) == 1


def test_on_enrich_fires_on_list_jobs(api_client, mock_docker_client, recording_plugin):
    """on_enrich is called once per job in GET /v1/jobs responses."""
    container = mock_docker_client.containers.run.return_value
    container.id = "enrich_list_001"

    api_client.post(EXEC_URL, json={"image": "python:3.11-slim", "detach": True})
    resp = api_client.get("/v1/jobs")

    assert resp.status_code == 200
    jobs = resp.json()
    assert all(j.get("_test_enriched") is True for j in jobs)
    assert len(recording_plugin.enrich_calls) == len(jobs)


# ---------------------------------------------------------------------------
# PluginServices injection
# ---------------------------------------------------------------------------

def test_services_injected_at_configure(mock_docker_client):
    """configure_services() injects a PluginServices into all plugins."""
    fresh_registry = PluginRegistry()
    plugin = _RecordingPlugin()
    fresh_registry.register(plugin)

    job_store = JobStore()
    docker_client = MagicMock()
    fresh_registry.configure_services(job_store, docker_client)

    assert plugin.services is not None
    assert plugin.services.job_store is job_store
    assert plugin.services.docker_client is docker_client


def test_services_injected_on_late_register(mock_docker_client):
    """A plugin registered after configure_services() still receives services."""
    fresh_registry = PluginRegistry()
    job_store = JobStore()
    docker_client = MagicMock()
    fresh_registry.configure_services(job_store, docker_client)

    plugin = _RecordingPlugin()
    fresh_registry.register(plugin)

    assert plugin.services is not None
    assert plugin.services.job_store is job_store


def test_services_reinjected_after_configure(mock_docker_client):
    """Calling configure_services() a second time updates all existing plugins."""
    fresh_registry = PluginRegistry()
    plugin = _RecordingPlugin()
    fresh_registry.register(plugin)

    store1, store2 = JobStore(), JobStore()
    dc = MagicMock()

    fresh_registry.configure_services(store1, dc)
    assert plugin.services.job_store is store1

    fresh_registry.configure_services(store2, dc)
    assert plugin.services.job_store is store2


def test_clear_does_not_lose_services_config(mock_docker_client):
    """registry.clear() removes plugins but preserves the services config so
    plugins registered afterwards still receive services."""
    fresh_registry = PluginRegistry()
    job_store = JobStore()
    dc = MagicMock()
    fresh_registry.configure_services(job_store, dc)
    fresh_registry.clear()

    plugin = _RecordingPlugin()
    fresh_registry.register(plugin)
    assert plugin.services is not None
    assert plugin.services.job_store is job_store
