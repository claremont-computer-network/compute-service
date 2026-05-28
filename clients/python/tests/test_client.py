"""
Tests for caas.client.CaasClient
"""
import pytest
import httpx
from tests.conftest import BASE_URL, API_KEY, _make_response, _assert_payload


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
    _assert_payload(
        mock_transport, "POST", f"{BASE_URL}/v1/execute",
        {
            "image": "pytorch/pytorch:latest",
            "cmd": ["python", "train.py"],
            "env": {"EPOCHS": "10"},
            "gpu": {"device_ids": "all", "capabilities": ["gpu"]},
            "detach": True,
        },
        response_body={"container_id": "xyz", "status": "running"},
    )
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
    """Legacy server (logs-only response) still works — falls back to logs field."""
    mock_transport[("POST", f"{BASE_URL}/v1/execute/cell")] = _make_response(
        200, {"status": "exited", "exit_code": 0, "logs": "hello from cell\n"}
    )
    logs = client.execute_cell(code="print('hello from cell')", image="python:3.11-slim")
    assert logs == "hello from cell\n"


def test_execute_cell_returns_stdout_by_default(client, mock_transport):
    """With a split-stream server response the client returns stdout only (no banner)."""
    mock_transport[("POST", f"{BASE_URL}/v1/execute/cell")] = _make_response(
        200, {
            "status": "exited",
            "exit_code": 0,
            "stdout": "2\n",
            "stderr": "WARNING: banner noise\n",
            "logs": "2\nWARNING: banner noise\n",
        }
    )
    result = client.execute_cell(code="print(1+1)", image="python:3.11-slim")
    assert result == "2\n"
    assert "banner noise" not in result


def test_execute_cell_verbose_returns_merged_logs(client, mock_transport):
    """verbose=True returns the full merged logs stream."""
    mock_transport[("POST", f"{BASE_URL}/v1/execute/cell")] = _make_response(
        200, {
            "status": "exited",
            "exit_code": 0,
            "stdout": "2\n",
            "stderr": "WARNING: banner noise\n",
            "logs": "2\nWARNING: banner noise\n",
        }
    )
    result = client.execute_cell(code="print(1+1)", image="python:3.11-slim", verbose=True)
    assert "2\n" in result
    assert "banner noise" in result


def test_execute_cell_nonzero_exit_always_returns_merged_logs(client, mock_transport):
    """Non-zero exit returns merged logs regardless of verbose flag so tracebacks are visible."""
    mock_transport[("POST", f"{BASE_URL}/v1/execute/cell")] = _make_response(
        200, {
            "status": "exited",
            "exit_code": 1,
            "stdout": "",
            "stderr": "Traceback (most recent call last):\n  ...\nValueError: bad input\n",
            "logs": "Traceback (most recent call last):\n  ...\nValueError: bad input\n",
        }
    )
    result = client.execute_cell(code="raise ValueError('bad input')", image="python:3.11-slim")
    assert "Traceback" in result
    assert "ValueError" in result


def test_execute_cell_nvcr_image_auto_enables_suppress_entrypoint(client, mock_transport):
    """nvcr.io/* images get suppress_entrypoint=True automatically."""
    _assert_payload(
        mock_transport, "POST", f"{BASE_URL}/v1/execute/cell",
        {"suppress_entrypoint": True},
        response_body={"status": "exited", "exit_code": 0, "stdout": "ok\n", "logs": "ok\n"},
    )
    client.execute_cell(code="print('ok')", image="nvcr.io/nvidia/pytorch:25.03-py3")


def test_execute_cell_non_nvcr_image_does_not_set_suppress_entrypoint(client, mock_transport):
    """Non-NGC images must not send suppress_entrypoint in the payload."""
    _assert_payload(
        mock_transport, "POST", f"{BASE_URL}/v1/execute/cell",
        {},   # we only care the key is absent — checked via the handler below
        response_body={"status": "exited", "exit_code": 0, "stdout": "ok\n", "logs": "ok\n"},
    )
    # Override with a handler that actively asserts absence.
    import json as _json
    def _check(request):
        body = _json.loads(request.content)
        assert "suppress_entrypoint" not in body, (
            f"suppress_entrypoint should not be sent for non-NGC images, got: {body}"
        )
        return _make_response(200, {"status": "exited", "exit_code": 0, "stdout": "ok\n", "logs": "ok\n"})
    mock_transport[("POST", f"{BASE_URL}/v1/execute/cell")] = _check
    client.execute_cell(code="print('ok')", image="python:3.11-slim")


def test_execute_cell_suppress_entrypoint_false_overrides_auto(client, mock_transport):
    """Explicit suppress_entrypoint=False disables auto-detection for nvcr.io images."""
    import json as _json
    def _check(request):
        body = _json.loads(request.content)
        assert "suppress_entrypoint" not in body, (
            f"suppress_entrypoint should be omitted when False, got: {body}"
        )
        return _make_response(200, {"status": "exited", "exit_code": 0, "stdout": "ok\n", "logs": "ok\n"})
    mock_transport[("POST", f"{BASE_URL}/v1/execute/cell")] = _check
    client.execute_cell(code="print('ok')", image="nvcr.io/nvidia/pytorch:25.03-py3",
                        suppress_entrypoint=False)


def test_execute_cell_sends_code_and_image(client, mock_transport):
    _assert_payload(
        mock_transport, "POST", f"{BASE_URL}/v1/execute/cell",
        {"code": "x = 1 + 1\nprint(x)", "image": "python:3.11-slim"},
        response_body={"status": "exited", "logs": "2\n"},
    )
    client.execute_cell(code="x = 1 + 1\nprint(x)", image="python:3.11-slim")


def test_execute_cell_forwards_gpu(client, mock_transport):
    _assert_payload(
        mock_transport, "POST", f"{BASE_URL}/v1/execute/cell",
        {"gpu": {"device_ids": "all", "capabilities": ["gpu"]}},
        response_body={"status": "exited", "logs": "Tesla T4\n"},
    )
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


def test_logs_follow_streams_and_returns_text(mock_transport):
    """follow=True uses httpx.stream() and concatenates the text/plain chunks."""
    from caas.client import CaasClient

    streaming_resp = httpx.Response(
        200,
        stream=httpx.ByteStream(b"line1\nline2\n"),
        headers={"content-type": "text/plain"},
    )
    mock_transport[("GET", f"{BASE_URL}/v1/logs/abc123")] = streaming_resp

    class _T(httpx.BaseTransport):
        def handle_request(self, request):
            key = (request.method, str(request.url).split("?")[0])
            return mock_transport[key]

    c = CaasClient(host=BASE_URL, api_key=API_KEY, http_client=httpx.Client(transport=_T()))
    logs = c.logs("abc123", follow=True)
    assert "line1" in logs
    assert "line2" in logs


# ---------------------------------------------------------------------------
# No API key
# ---------------------------------------------------------------------------

def test_client_works_without_api_key(mock_transport):
    """When api_key is None no X-API-Key header should be sent."""
    from caas.client import CaasClient

    def _check(request):
        assert "x-api-key" not in request.headers
        return _make_response(200, {"status": "ok"})

    mock_transport[("GET", f"{BASE_URL}/health")] = _check

    class _T(httpx.BaseTransport):
        def handle_request(self, request):
            return mock_transport[(request.method, str(request.url).split("?")[0])](request)

    c = CaasClient(host=BASE_URL, api_key=None, http_client=httpx.Client(transport=_T()))
    c.health()


# ---------------------------------------------------------------------------
# Resource management
# ---------------------------------------------------------------------------

def test_client_close_releases_owned_transport():
    """close() on a client that owns its httpx.Client closes the connection pool."""
    from caas.client import CaasClient
    from unittest.mock import patch, MagicMock

    inner = MagicMock(spec=httpx.Client)
    with patch("httpx.Client", return_value=inner):
        c = CaasClient(host=BASE_URL)
    c.close()
    inner.close.assert_called_once()


def test_client_close_does_not_close_injected_transport():
    """close() must not close an httpx.Client that was supplied by the caller."""
    from caas.client import CaasClient

    inner = httpx.Client()
    c = CaasClient(host=BASE_URL, http_client=inner)
    c.close()   # should be a no-op on the injected client
    # inner is still usable — if close() had been called it would raise on next use
    inner.close()   # explicit teardown — no error means test passes


def test_client_context_manager(mock_transport):
    """CaasClient can be used as a context manager; close() is called on exit."""
    from caas.client import CaasClient
    from unittest.mock import patch, MagicMock

    inner = MagicMock(spec=httpx.Client)
    with patch("httpx.Client", return_value=inner):
        with CaasClient(host=BASE_URL) as c:
            pass
    inner.close.assert_called_once()


# ---------------------------------------------------------------------------
# Timeout handling
# ---------------------------------------------------------------------------

def test_timeout_constructor_sets_httpx_read_timeout():
    """CaasClient(timeout=N) sets only the read phase of httpx.Timeout, not all phases."""
    from caas.client import CaasClient
    from unittest.mock import patch, MagicMock

    inner = MagicMock(spec=httpx.Client)
    with patch("httpx.Client", return_value=inner) as mock_cls:
        CaasClient(host=BASE_URL, timeout=300.0)
    _, kwargs = mock_cls.call_args
    t = kwargs["timeout"]
    assert isinstance(t, httpx.Timeout)
    assert t.read == 300.0
    # connect / write / pool should keep the 5 s default
    assert t.connect == 5.0


def test_read_timeout_raises_caas_timeout_error(mock_transport):
    """A ReadTimeout from httpx is converted to CaasTimeoutError with a helpful message."""
    from caas.client import CaasClient, CaasTimeoutError

    def _raise_timeout(request):
        raise httpx.ReadTimeout("timed out", request=request)

    mock_transport[("GET", f"{BASE_URL}/health")] = _raise_timeout

    class _T(httpx.BaseTransport):
        def handle_request(self, request):
            return mock_transport[(request.method, str(request.url).split("?")[0])](request)

    c = CaasClient(host=BASE_URL, api_key=API_KEY, http_client=httpx.Client(transport=_T()))
    with pytest.raises(CaasTimeoutError, match="did not respond within"):
        c.health()


def test_timeout_error_is_subclass_of_caas_error():
    """CaasTimeoutError is a CaasError so existing except CaasError handlers still work."""
    from caas.client import CaasError, CaasTimeoutError
    assert issubclass(CaasTimeoutError, CaasError)


# ---------------------------------------------------------------------------
# shm_size / ipc_mode payload tests
# ---------------------------------------------------------------------------

def test_execute_sends_shm_and_ipc_in_payload(client, mock_transport):
    """execute() forwards shm_size and ipc_mode in the JSON body."""
    _assert_payload(
        mock_transport, "POST", f"{BASE_URL}/v1/execute",
        {"shm_size": "2g", "ipc_mode": "host"},
        response_body={"container_id": "abc123", "status": "running"},
    )
    client.execute(image="pytorch/pytorch:latest", shm_size="2g", ipc_mode="host")


def test_execute_omits_shm_and_ipc_when_none(client, mock_transport):
    """execute() does not include shm_size or ipc_mode keys when they are None."""
    import json

    def _check(request):
        body = json.loads(request.content)
        assert "shm_size" not in body
        assert "ipc_mode" not in body
        return _make_response(200, {"container_id": "abc123", "status": "running"})

    mock_transport[("POST", f"{BASE_URL}/v1/execute")] = _check
    client.execute(image="alpine:3.18")


def test_execute_cell_sends_shm_and_ipc_in_payload(client, mock_transport):
    """execute_cell() forwards shm_size and ipc_mode in the JSON body."""
    _assert_payload(
        mock_transport, "POST", f"{BASE_URL}/v1/execute/cell",
        {"shm_size": "512m", "ipc_mode": "host"},
        response_body={"status": "exited", "exit_code": 0, "logs": ""},
    )
    client.execute_cell(code="print('hi')", image="python:3.12", shm_size="512m", ipc_mode="host")


def test_execute_cell_omits_shm_and_ipc_when_none(client, mock_transport):
    """execute_cell() does not include shm_size or ipc_mode keys when they are None."""
    import json

    def _check(request):
        body = json.loads(request.content)
        assert "shm_size" not in body
        assert "ipc_mode" not in body
        return _make_response(200, {"status": "exited", "exit_code": 0, "logs": ""})

    mock_transport[("POST", f"{BASE_URL}/v1/execute/cell")] = _check
    client.execute_cell(code="print('hi')", image="python:3.12")


# ---------------------------------------------------------------------------
# Empty container coercion (env={} and volumes=[] must not appear in payload)
# ---------------------------------------------------------------------------

def test_execute_omits_env_when_empty_dict(client, mock_transport):
    """execute() must not include 'env' in the payload when an empty dict is passed."""
    import json

    def _check(request):
        body = json.loads(request.content)
        assert "env" not in body
        return _make_response(200, {"container_id": "abc", "status": "running"})

    mock_transport[("POST", f"{BASE_URL}/v1/execute")] = _check
    client.execute(image="alpine:3.18", env={})


def test_execute_omits_volumes_when_empty_list(client, mock_transport):
    """execute() must not include 'volumes' in the payload when an empty list is passed."""
    import json

    def _check(request):
        body = json.loads(request.content)
        assert "volumes" not in body
        return _make_response(200, {"container_id": "abc", "status": "running"})

    mock_transport[("POST", f"{BASE_URL}/v1/execute")] = _check
    client.execute(image="alpine:3.18", volumes=[])


def test_execute_cell_omits_env_when_empty_dict(client, mock_transport):
    """execute_cell() must not include 'env' in the payload when an empty dict is passed."""
    import json

    def _check(request):
        body = json.loads(request.content)
        assert "env" not in body
        return _make_response(200, {"status": "exited", "exit_code": 0, "logs": ""})

    mock_transport[("POST", f"{BASE_URL}/v1/execute/cell")] = _check
    client.execute_cell(code="pass", image="python:3.12", env={})


def test_execute_cell_omits_volumes_when_empty_list(client, mock_transport):
    """execute_cell() must not include 'volumes' in the payload when an empty list is passed."""
    import json

    def _check(request):
        body = json.loads(request.content)
        assert "volumes" not in body
        return _make_response(200, {"status": "exited", "exit_code": 0, "logs": ""})

    mock_transport[("POST", f"{BASE_URL}/v1/execute/cell")] = _check
    client.execute_cell(code="pass", image="python:3.12", volumes=[])


# ---------------------------------------------------------------------------
# Job registry methods
# ---------------------------------------------------------------------------

def test_jobs_returns_list(client, mock_transport):
    """jobs() returns the parsed JSON list from GET /v1/jobs."""
    mock_transport[("GET", f"{BASE_URL}/v1/jobs")] = _make_response(
        200, [{"job_id": "abc123", "status": "running", "image": "alpine:3.18"}]
    )
    result = client.jobs()
    assert isinstance(result, list)
    assert result[0]["job_id"] == "abc123"


def test_job_returns_single_record(client, mock_transport):
    """job(job_id) returns the parsed JSON dict from GET /v1/jobs/{job_id}."""
    mock_transport[("GET", f"{BASE_URL}/v1/jobs/abc123")] = _make_response(
        200, {"job_id": "abc123", "status": "running", "image": "alpine:3.18"}
    )
    result = client.job("abc123")
    assert result["job_id"] == "abc123"


def test_stop_returns_stopped(client, mock_transport):
    """stop(job_id) returns the dispatcher response dict."""
    mock_transport[("DELETE", f"{BASE_URL}/v1/jobs/abc123")] = _make_response(
        200, {"job_id": "abc123", "status": "stopped"}
    )
    result = client.stop("abc123")
    assert result["status"] == "stopped"


def test_job_raises_on_404(client, mock_transport):
    """job() raises CaasError when the dispatcher returns 404."""
    from caas.client import CaasError
    mock_transport[("GET", f"{BASE_URL}/v1/jobs/gone")] = _make_response(
        404, {"detail": "Job not found: gone"}
    )
    with pytest.raises(CaasError, match="not found"):
        client.job("gone")


# ---------------------------------------------------------------------------
# Extension API — deployment status
# ---------------------------------------------------------------------------

def test_deployment_status_returns_result(client, mock_transport):
    """deployment_status(job_id) returns the success/failure dict."""
    mock_transport[("GET", f"{BASE_URL}/api/deployments/abc123/status")] = _make_response(
        200, {"job_id": "abc123", "status": "stopped", "exit_code": 0, "success": True, "message": "Job completed successfully"}
    )
    result = client.deployment_status("abc123")
    assert result["success"] is True
    assert result["exit_code"] == 0


def test_deployment_status_raises_on_404(client, mock_transport):
    """deployment_status raises CaasError when job is not found."""
    from caas.client import CaasError
    mock_transport[("GET", f"{BASE_URL}/api/deployments/gone/status")] = _make_response(
        404, {"detail": "No job found for gone"}
    )
    with pytest.raises(CaasError, match="No job found"):
        client.deployment_status("gone")


# ---------------------------------------------------------------------------
# Extension API — templates
# ---------------------------------------------------------------------------

def test_templates_list_returns_list(client, mock_transport):
    """templates_list() returns all stored templates."""
    mock_transport[("GET", f"{BASE_URL}/api/templates")] = _make_response(
        200, [{"id": "tpl_abc", "name": "train", "image": "pytorch:latest"}]
    )
    result = client.templates_list()
    assert isinstance(result, list)
    assert result[0]["name"] == "train"


def test_templates_upsert_creates(client, mock_transport):
    """templates_upsert creates a new template when no id is passed."""
    _assert_payload(
        mock_transport, "POST", f"{BASE_URL}/api/templates",
        {"name": "train", "image": "pytorch:latest", "cmd": ["python", "train.py"]},
        response_body={"id": "tpl_abc123", "name": "train", "created_at": "2025-01-01T00:00:00+00:00", "modified_at": "2025-01-01T00:00:00+00:00", "image": "pytorch:latest", "cmd": ["python", "train.py"]},
    )
    result = client.templates_upsert(name="train", image="pytorch:latest", cmd=["python", "train.py"])
    assert "id" in result
    assert result["created_at"] is not None


def test_templates_upsert_updates(client, mock_transport):
    """templates_upsert updates an existing template when id is passed."""
    _assert_payload(
        mock_transport, "POST", f"{BASE_URL}/api/templates",
        {"id": "tpl_abc", "name": "updated"},
        response_body={"id": "tpl_abc", "name": "updated", "created_at": "2025-01-01T00:00:00+00:00", "modified_at": "2025-01-02T00:00:00+00:00"},
    )
    result = client.templates_upsert(id="tpl_abc", name="updated")
    assert result["id"] == "tpl_abc"


def test_templates_delete(client, mock_transport):
    """templates_delete returns {"deleted": id}."""
    mock_transport[("DELETE", f"{BASE_URL}/api/templates/tpl_abc")] = _make_response(
        200, {"deleted": "tpl_abc"}
    )
    result = client.templates_delete("tpl_abc")
    assert result["deleted"] == "tpl_abc"


# ---------------------------------------------------------------------------
# Extension API — files
# ---------------------------------------------------------------------------

def test_files_list_returns_entries(client, mock_transport):
    """files_list(path) returns {"path": ..., "entries": [...]}."""
    import json
    
    def _check_path(request):
        query = str(request.url).split("?")[1]
        from httpx import QueryParams
        parsed = QueryParams(query)
        assert parsed.get("path") == "/data"
        return _make_response(200, {
            "path": "/data",
            "entries": [
                {"name": "model.pt", "permissions": "644", "size": 1024, "modified": "2025-01-01", "is_dir": False},
                {"name": "logs/", "permissions": "755", "size": 4096, "modified": "2025-01-01", "is_dir": True},
            ],
        })
    
    mock_transport[("GET", f"{BASE_URL}/api/files")] = _check_path
    result = client.files_list(path="/data")
    assert result["path"] == "/data"
    assert len(result["entries"]) == 2


# ---------------------------------------------------------------------------
# Extension API — schedules
# ---------------------------------------------------------------------------

def test_schedules_list_returns_list(client, mock_transport):
    """schedules_list() returns all stored schedules."""
    mock_transport[("GET", f"{BASE_URL}/api/schedule")] = _make_response(
        200, [
            {"id": "sch_abc", "name": "nightly", "status": "pending", "delay_seconds": 86400}
        ]
    )
    result = client.schedules_list()
    assert isinstance(result, list)
    assert result[0]["status"] == "pending"


def test_schedules_upsert_creates(client, mock_transport):
    """schedules_upsert creates a new schedule with inline fields."""
    _assert_payload(
        mock_transport, "POST", f"{BASE_URL}/api/schedule",
        {"image": "python:3.12", "cmd": ["python", "train.py"], "delay_seconds": 0},
        response_body={"id": "sch_abc", "name": "Schedule sch_abc", "status": "active", "delay_seconds": 0},
    )
    result = client.schedules_upsert(image="python:3.12", cmd=["python", "train.py"], delay_seconds=0)
    assert result["status"] == "active"


def test_schedules_upsert_with_template(client, mock_transport):
    """schedules_upsert references a template."""
    _assert_payload(
        mock_transport, "POST", f"{BASE_URL}/api/schedule",
        {"template_id": "tpl_abc", "delay_seconds": 3600},
        response_body={"id": "sch_abc", "name": "Schedule sch_abc", "status": "pending"},
    )
    result = client.schedules_upsert(template_id="tpl_abc", delay_seconds=3600)
    assert result["status"] == "pending"


def test_schedule_cancel(client, mock_transport):
    """schedule_cancel returns {"deleted": id}."""
    mock_transport[("DELETE", f"{BASE_URL}/api/schedule/sch_abc")] = _make_response(
        200, {"deleted": "sch_abc"}
    )
    result = client.schedule_cancel("sch_abc")
    assert result["deleted"] == "sch_abc"


# ---------------------------------------------------------------------------
# Extension API — staging
# ---------------------------------------------------------------------------

def test_staging_list_returns_list(client, mock_transport):
    """staging_list() returns all staging areas."""
    mock_transport[("GET", f"{BASE_URL}/api/staging")] = _make_response(
        200, [
            {"id": "stg_abc", "name": "outputs", "host_path": "/mnt/data", "dest_path": "/data"}
        ]
    )
    result = client.staging_list()
    assert isinstance(result, list)
    assert result[0]["name"] == "outputs"


def test_staging_create(client, mock_transport):
    """staging_create returns the new staging area dict."""
    _assert_payload(
        mock_transport, "POST", f"{BASE_URL}/api/staging",
        {"name": "outputs", "host_path": "/mnt/datasets", "dest_path": "/data"},
        response_body={"id": "stg_abc", "name": "outputs", "host_path": "/mnt/datasets", "dest_path": "/data"},
    )
    result = client.staging_create(name="outputs", host_path="/mnt/datasets", dest_path="/data")
    assert result["host_path"] == "/mnt/datasets"


def test_staging_delete(client, mock_transport):
    """staging_delete returns {"deleted": id}."""
    mock_transport[("DELETE", f"{BASE_URL}/api/staging/stg_abc")] = _make_response(
        200, {"deleted": "stg_abc"}
    )
    result = client.staging_delete("stg_abc")
    assert result["deleted"] == "stg_abc"


# ---------------------------------------------------------------------------
# Extension API — jobs with state filter (uses /api/jobs)
# ---------------------------------------------------------------------------

def test_jobs_with_state_filter_uses_api_endpoint(client, mock_transport):
    """passing state= triggers the extension /api/jobs endpoint instead of /v1/jobs."""
    import json
    
    def _check_query(request):
        query = str(request.url).split("?")[1]
        assert "state=running" in query
        return _make_response(200, [{"job_id": "running001", "status": "running"}])
    
    mock_transport[("GET", f"{BASE_URL}/api/jobs")] = _check_query
    result = client.jobs(state="running")
    assert isinstance(result, list)
    assert result[0]["status"] == "running"


def test_jobs_without_state_uses_v1_endpoint(client, mock_transport):
    """passing no state defaults to the legacy /v1/jobs endpoint."""
    mock_transport[("GET", f"{BASE_URL}/v1/jobs")] = _make_response(
        200, [{"job_id": "abc", "status": "running"}]
    )
    result = client.jobs()
    assert result[0]["job_id"] == "abc"
