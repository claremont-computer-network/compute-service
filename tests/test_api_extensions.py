"""
Tests for the extension API endpoints:

  GET    /api/templates
  POST   /api/templates
  DELETE /api/templates/{template_id}
  GET    /api/files
  GET    /api/schedule
  POST   /api/schedule
  DELETE /api/schedule/{schedule_id}
  GET    /api/staging
  POST   /api/staging
  DELETE /api/staging/{staging_id}
  GET    /api/jobs?state=...
  GET    /api/deployments/{job_id}/status
"""
import os
import pytest


# ── Test base URL helpers ──────────────────────────────────────────────────────

TEMPLATES_URL  = "/api/templates"
FILES_URL      = "/api/files"
SCHEDULE_URL   = "/api/schedule"
STAGING_URL    = "/api/staging"
FILTER_URL     = "/api/jobs"
DEPL_URL       = "/api/deployments"


# ── Helpers ────────────────────────────────────────────────────────────────────

def _reset_data_dir(api_client, tmp_dir):
    """Ensure the DataStore reads/writes from tmp_dir instead of the default."""
    import app.main as m
    m._data_store = None
    m._DATA_DIR = tmp_dir
    m.data_store = m._get_data_store()


def _submit_detach(api_client):
    """Submit a minimal detached job and return the response body."""
    resp = api_client.post("/v1/execute", json={"image": "alpine:3.18"})
    assert resp.status_code == 200
    return resp.json()


# ── /api/templates ─────────────────────────────────────────────────────────────

def test_templates_list_empty(api_client, mock_docker_client, tmp_path):
    """No templates → empty list."""
    _reset_data_dir(api_client, str(tmp_path / "data"))
    resp = api_client.get(TEMPLATES_URL)
    assert resp.status_code == 200
    assert resp.json() == []


def test_templates_create(api_client, mock_docker_client, tmp_path):
    """POST creates a new template with 201 status."""
    _reset_data_dir(api_client, str(tmp_path / "data"))
    resp = api_client.post(TEMPLATES_URL, json={
        "name": "test-tpl",
        "image": "pytorch/pytorch:latest",
        "cmd": ["python", "train.py"],
    })
    assert resp.status_code == 201
    body = resp.json()
    assert body["name"] == "test-tpl"
    assert body["image"] == "pytorch/pytorch:latest"
    assert body["id"].startswith("tpl_")
    assert body["cmd"] == ["python", "train.py"]
    assert body["created_at"]
    assert body["modified_at"]


def test_templates_list_after_create(api_client, mock_docker_client, tmp_path):
    """Listing returns templates that were created."""
    _reset_data_dir(api_client, str(tmp_path / "data"))
    api_client.post(TEMPLATES_URL, json={"name": "tpl1", "image": "alpine:3.18"})
    api_client.post(TEMPLATES_URL, json={"name": "tpl2", "image": "ubuntu:22.04"})
    resp = api_client.get(TEMPLATES_URL)
    assert len(resp.json()) == 2


def test_templates_upsert(api_client, mock_docker_client, tmp_path):
    """Post with existing id updates the template."""
    _reset_data_dir(api_client, str(tmp_path / "data"))
    resp1 = api_client.post(TEMPLATES_URL, json={"name": "tpl1", "image": "alpine:3.18"})
    assert resp1.status_code == 201
    tpl_id = resp1.json()["id"]

    resp2 = api_client.post(TEMPLATES_URL, json={"id": tpl_id, "name": "updated"})
    assert resp2.status_code == 200
    assert resp2.json()["name"] == "updated"


def test_templates_upsert_not_found(api_client, mock_docker_client, tmp_path):
    """Updating a template with a non-existent id returns 404."""
    _reset_data_dir(api_client, str(tmp_path / "data"))
    resp = api_client.post(TEMPLATES_URL, json={
        "id": "tpl_nonexistent", "name": "ghost"
    })
    assert resp.status_code == 404


def test_templates_delete(api_client, mock_docker_client, tmp_path):
    """Deleting an existing template returns 200."""
    _reset_data_dir(api_client, str(tmp_path / "data"))
    resp = api_client.post(TEMPLATES_URL, json={"name": "tpl-del", "image": "alpine"})
    tpl_id = resp.json()["id"]
    del_resp = api_client.delete(f"{TEMPLATES_URL}/{tpl_id}")
    assert del_resp.status_code == 200
    assert del_resp.json()["deleted"] == tpl_id


def test_templates_delete_not_found(api_client, mock_docker_client, tmp_path):
    """Deleting a non-existent template returns 404."""
    _reset_data_dir(api_client, str(tmp_path / "data"))
    resp = api_client.delete(f"{TEMPLATES_URL}/tpl_nonexistent")
    assert resp.status_code == 404


def test_templates_fields_preserved(api_client, mock_docker_client, tmp_path):
    """Template fields like env, volumes, gpu are preserved."""
    _reset_data_dir(api_client, str(tmp_path / "data"))
    resp = api_client.post(TEMPLATES_URL, json={
        "name": "full-tpl",
        "image": "pytorch:latest",
        "cmd": ["python", "train.py"],
        "env": {"EPOCHS": "100"},
        "volumes": [{"host_path": "/data", "container_path": "/data", "mode": "rw"}],
        "gpu": {"device_ids": "all"},
    })
    body = resp.json()
    assert body["env"] == {"EPOCHS": "100"}
    assert body["volumes"][0]["host_path"] == "/data"
    assert body["gpu"]["device_ids"] == "all"


# ── /api/files ─────────────────────────────────────────────────────────────────

def test_files_list_root(api_client, mock_docker_client, tmp_path):
    """Listing / (or any allowed dir) returns entries."""
    _reset_data_dir(api_client, str(tmp_path / "data"))
    # Create some files in an allowed directory.
    allowed = str(tmp_path / "allowed")
    import app.main as m
    m.ALLOWED_HOST_DIRS = [allowed]
    os.makedirs(allowed, exist_ok=True)
    (tmp_path / "allowed" / "file1.txt").write_text("hello")
    (tmp_path / "allowed" / "file2.txt").write_text("world")
    subdir = tmp_path / "allowed" / "subdir"
    subdir.mkdir()

    resp = api_client.get(FILES_URL, params={"path": allowed})
    assert resp.status_code == 200
    body = resp.json()
    assert body["path"] == allowed
    names = [e["name"] for e in body["entries"]]
    assert "file1.txt" in names
    assert "file2.txt" in names
    assert "subdir" in names


def test_files_disallowed_path(api_client, mock_docker_client, tmp_path):
    """Listing a path outside ALLOWED_HOST_DIRS returns 400."""
    _reset_data_dir(api_client, str(tmp_path / "data"))
    allowed = str(tmp_path / "allowed")
    import app.main as m
    m.ALLOWED_HOST_DIRS = [allowed]
    resp = api_client.get(FILES_URL, params={"path": "/etc"})
    assert resp.status_code == 400
    assert "not under" in resp.json()["detail"]


def test_files_empty_directory(api_client, mock_docker_client, tmp_path):
    """An empty directory returns an empty entries list."""
    _reset_data_dir(api_client, str(tmp_path / "data"))
    allowed = str(tmp_path / "allowed")
    import app.main as m
    m.ALLOWED_HOST_DIRS = [allowed]
    os.makedirs(allowed, exist_ok=True)
    resp = api_client.get(FILES_URL, params={"path": allowed})
    assert resp.status_code == 200
    assert resp.json()["entries"] == []


# ── /api/schedule ──────────────────────────────────────────────────────────────

def test_schedules_list_empty(api_client, mock_docker_client, tmp_path):
    """No schedules → empty list."""
    _reset_data_dir(api_client, str(tmp_path / "data"))
    resp = api_client.get(SCHEDULE_URL)
    assert resp.status_code == 200
    assert resp.json() == []


def test_schedule_create_with_delay(api_client, mock_docker_client, tmp_path):
    """Create a schedule with a positive delay → pending."""
    _reset_data_dir(api_client, str(tmp_path / "data"))
    resp = api_client.post(SCHEDULE_URL, json={
        "delay_seconds": 300,
    })
    assert resp.status_code == 201
    body = resp.json()
    assert body["status"] == "pending"
    assert body["delay_seconds"] == 300


def test_schedule_create_immediate(api_client, mock_docker_client, tmp_path):
    """Create a schedule with delay_seconds=0 → triggers immediately (mock docker used)."""
    _reset_data_dir(api_client, str(tmp_path / "data"))
    # The schedule creation with delay=0 triggers execution, which calls Docker.
    # The mock_docker_client fixture provides the mock, so this should succeed.
    resp = api_client.post(SCHEDULE_URL, json={
        "delay_seconds": 0,
        "image": "alpine:3.18",
        "cmd": ["echo", "hello"],
    })
    assert resp.status_code == 201
    body = resp.json()
    # Immediately-executed schedules should record the triggered_at timestamp.
    assert "triggered_at" in body
    # The schedule item is stored so it can be retrieved.
    resp2 = api_client.get(SCHEDULE_URL)
    schedules = resp2.json()
    assert len(schedules) == 1


def test_schedule_create_with_template(api_client, mock_docker_client, tmp_path):
    """Trigger a schedule using a template → 404 if template missing."""
    _reset_data_dir(api_client, str(tmp_path / "data"))
    resp = api_client.post(SCHEDULE_URL, json={
        "template_id": "tpl_nonexistent",
        "delay_seconds": 60,
    })
    assert resp.status_code == 404


def test_schedule_cancel(api_client, mock_docker_client, tmp_path):
    """Cancelling a pending schedule returns 200."""
    _reset_data_dir(api_client, str(tmp_path / "data"))
    resp = api_client.post(SCHEDULE_URL, json={"delay_seconds": 300})
    sched_id = resp.json()["id"]
    cancel_resp = api_client.delete(f"{SCHEDULE_URL}/{sched_id}")
    assert cancel_resp.status_code == 200


def test_schedule_cancel_not_found(api_client, mock_docker_client, tmp_path):
    """Cancelling a non-existent schedule returns 404."""
    _reset_data_dir(api_client, str(tmp_path / "data"))
    resp = api_client.delete(f"{SCHEDULE_URL}/sch_nonexistent")
    assert resp.status_code == 404


# ── /api/staging ───────────────────────────────────────────────────────────────

def test_staging_list_empty(api_client, mock_docker_client, tmp_path):
    """No staging areas → empty list."""
    _reset_data_dir(api_client, str(tmp_path / "data"))
    resp = api_client.get(STAGING_URL)
    assert resp.status_code == 200
    assert resp.json() == []


def test_staging_create(api_client, mock_docker_client, tmp_path):
    """Create a staging area → 201."""
    _reset_data_dir(api_client, str(tmp_path / "data"))
    resp = api_client.post(STAGING_URL, json={
        "name": "output-data",
        "host_path": "/mnt/training-output",
        "dest_path": "/outputs",
    })
    assert resp.status_code == 201
    body = resp.json()
    assert body["name"] == "output-data"
    assert body["host_path"] == "/mnt/training-output"
    assert body["id"].startswith("stg_")


def test_staging_delete(api_client, mock_docker_client, tmp_path):
    """Delete an existing staging area."""
    _reset_data_dir(api_client, str(tmp_path / "data"))
    resp = api_client.post(STAGING_URL, json={
        "name": "staging1",
        "host_path": "/mnt/data",
    })
    stg_id = resp.json()["id"]
    del_resp = api_client.delete(f"{STAGING_URL}/{stg_id}")
    assert del_resp.status_code == 200


def test_staging_delete_not_found(api_client, mock_docker_client, tmp_path):
    """Delete non-existent staging area → 404."""
    _reset_data_dir(api_client, str(tmp_path / "data"))
    resp = api_client.delete(f"{STAGING_URL}/stg_nonexistent")
    assert resp.status_code == 404


# ── /api/jobs?state=... ───────────────────────────────────────────────────────

def test_jobs_filter_all(api_client, mock_docker_client, tmp_path):
    """GET /api/jobs (no filter) returns all jobs."""
    _reset_data_dir(api_client, str(tmp_path / "data"))
    _submit_detach(api_client)
    resp = api_client.get(FILTER_URL)
    assert resp.status_code == 200
    assert len(resp.json()) >= 1


def test_jobs_filter_running(api_client, mock_docker_client, tmp_path):
    """GET /api/jobs?state=running returns only running jobs."""
    _reset_data_dir(api_client, str(tmp_path / "data"))
    _submit_detach(api_client)
    resp = api_client.get(FILTER_URL, params={"state": "running"})
    assert resp.status_code == 200
    assert len(resp.json()) >= 1
    for job in resp.json():
        assert job["status"] == "running"


def test_jobs_filter_stopped(api_client, mock_docker_client, tmp_path):
    """GET /api/jobs?state=stopped returns only stopped jobs."""
    _reset_data_dir(api_client, str(tmp_path / "data"))
    # Create one running job then manually add a stopped one with a different ID.
    _submit_detach(api_client)
    import app.main as m
    m.job_store.register_sync(
        job_id="manual_stopped_job_id",
        image="alpine:3.18",
    )
    m.job_store.mark_stopped("manual_stopped_job_id", exit_code=1)
    stopped_resp = api_client.get(FILTER_URL, params={"state": "stopped"})
    all_resp = api_client.get(FILTER_URL)
    assert stopped_resp.status_code == 200
    assert all_resp.status_code == 200
    assert len(stopped_resp.json()) < len(all_resp.json())
    for job in stopped_resp.json():
        assert job["status"] == "stopped"


def test_jobs_filter_nonexistent_state(api_client, mock_docker_client, tmp_path):
    """GET /api/jobs?state=unknown returns empty list."""
    _reset_data_dir(api_client, str(tmp_path / "data"))
    _submit_detach(api_client)
    resp = api_client.get(FILTER_URL, params={"state": "unknown"})
    assert resp.status_code == 200
    assert resp.json() == []


# ── /api/deployments/{id}/status ──────────────────────────────────────────────

def test_deployment_status_running(api_client, mock_docker_client, tmp_path):
    """A running job returns status=running with success=False."""
    _reset_data_dir(api_client, str(tmp_path / "data"))
    body = _submit_detach(api_client)
    job_id = body["container_id"]

    resp = api_client.get(f"{DEPL_URL}/{job_id}/status")
    assert resp.status_code == 200
    data = resp.json()
    assert data["job_id"] == job_id
    assert data["status"] == "running"
    assert data["success"] is False
    assert data["exit_code"] is None


def test_deployment_status_stopped_success(api_client, mock_docker_client, tmp_path):
    """A completed job with exit_code=0 returns success=True."""
    _reset_data_dir(api_client, str(tmp_path / "data"))
    body = _submit_detach(api_client)
    job_id = body["container_id"]
    import app.main as m
    m.job_store.mark_stopped(job_id, exit_code=0)

    resp = api_client.get(f"{DEPL_URL}/{job_id}/status")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "stopped"
    assert data["exit_code"] == 0
    assert data["success"] is True


def test_deployment_status_stopped_failure(api_client, mock_docker_client, tmp_path):
    """A completed job with non-zero exit_code returns success=False."""
    _reset_data_dir(api_client, str(tmp_path / "data"))
    body = _submit_detach(api_client)
    job_id = body["container_id"]
    import app.main as m
    m.job_store.mark_stopped(job_id, exit_code=1)

    resp = api_client.get(f"{DEPL_URL}/{job_id}/status")
    assert resp.status_code == 200
    assert resp.json()["success"] is False
    assert resp.json()["exit_code"] == 1


def test_deployment_status_not_found(api_client, mock_docker_client, tmp_path):
    """A non-existent job ID returns 404."""
    _reset_data_dir(api_client, str(tmp_path / "data"))
    import docker.errors
    mock_docker_client.containers.get.side_effect = docker.errors.NotFound("not found")
    resp = api_client.get(f"{DEPL_URL}/nonexistent_job_id/status")
    assert resp.status_code == 404
    assert "No job found" in resp.json()["detail"]


# ── Auth enforcement ──────────────────────────────────────────────────────────

def test_templates_auth_required(api_client, mock_docker_client, tmp_path):
    """GET /api/templates requires API key when set."""
    _reset_data_dir(api_client, str(tmp_path / "data"))
    import app.main as m
    m.API_KEY = "s3cret"

    resp = api_client.get(TEMPLATES_URL)
    assert resp.status_code == 401

    resp2 = api_client.get(TEMPLATES_URL, headers={"X-API-Key": "s3cret"})
    assert resp2.status_code == 200


def test_files_auth_required(api_client, mock_docker_client, tmp_path):
    """GET /api/files requires API key when set."""
    _reset_data_dir(api_client, str(tmp_path / "data"))
    import app.main as m
    m.API_KEY = "s3cret"
    resp = api_client.get(FILES_URL)
    assert resp.status_code == 401


def test_staging_auth_required(api_client, mock_docker_client, tmp_path):
    """POST /api/staging requires API key when set."""
    _reset_data_dir(api_client, str(tmp_path / "data"))
    import app.main as m
    m.API_KEY = "s3cret"
    resp = api_client.post(STAGING_URL, json={"name": "x", "host_path": "/data"})
    assert resp.status_code == 401


def test_schedule_auth_required(api_client, mock_docker_client, tmp_path):
    """POST /api/schedule requires API key when set."""
    _reset_data_dir(api_client, str(tmp_path / "data"))
    import app.main as m
    m.API_KEY = "s3cret"
    resp = api_client.post(SCHEDULE_URL, json={"delay_seconds": 0})
    assert resp.status_code == 401


def test_jobs_filter_auth_required(api_client, mock_docker_client, tmp_path):
    """GET /api/jobs requires API key when set."""
    _reset_data_dir(api_client, str(tmp_path / "data"))
    import app.main as m
    m.API_KEY = "s3cret"
    resp = api_client.get(FILTER_URL)
    assert resp.status_code == 401


def test_deployment_status_auth_required(api_client, mock_docker_client, tmp_path):
    """GET /api/deployments/{id}/status requires API key when set."""
    _reset_data_dir(api_client, str(tmp_path / "data"))
    import app.main as m
    m.API_KEY = "s3cret"
    resp = api_client.get(f"{DEPL_URL}/someid/status")
    assert resp.status_code == 401


# ── DataStore persistence ────────────────────────────────────────────────────

def test_templates_persist_across_reads(api_client, mock_docker_client, tmp_path):
    """Templates survive multiple read/write cycles."""
    _reset_data_dir(api_client, str(tmp_path / "data"))
    ds = _get_data_store_from_main()
    api_client.post(TEMPLATES_URL, json={"name": "tpl1", "image": "alpine"})
    api_client.post(TEMPLATES_URL, json={"name": "tpl2", "image": "ubuntu"})
    items = ds.read("templates")
    assert len(items) == 2
    assert items[0]["name"] == "tpl1"
    assert items[1]["name"] == "tpl2"


def test_schedule_persist_across_reads(api_client, mock_docker_client, tmp_path):
    """Schedules survive multiple read/write cycles."""
    _reset_data_dir(api_client, str(tmp_path / "data"))
    ds = _get_data_store_from_main()
    api_client.post(SCHEDULE_URL, json={"delay_seconds": 300})
    api_client.post(SCHEDULE_URL, json={"delay_seconds": 60})
    items = ds.read("schedules")
    assert len(items) == 2


def test_staging_persist_across_reads(api_client, mock_docker_client, tmp_path):
    """Staging areas survive multiple read/write cycles."""
    _reset_data_dir(api_client, str(tmp_path / "data"))
    ds = _get_data_store_from_main()
    api_client.post(STAGING_URL, json={"name": "stg1", "host_path": "/data"})
    api_client.post(STAGING_URL, json={"name": "stg2", "host_path": "/mnt"})
    items = ds.read("staging")
    assert len(items) == 2


def _get_data_store_from_main():
    import app.main as m
    return m.data_store
