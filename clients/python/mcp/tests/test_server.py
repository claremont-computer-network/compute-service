"""Tests for caas_mcp.server helpers and server construction."""
import json as _json

import httpx
import pytest


BASE_URL = "http://compute-node:8000"
API_KEY  = "test-key"


def _make_response(status_code: int, body: dict) -> httpx.Response:
    return httpx.Response(status_code, json=body)


def _assert_payload(
    mock_transport: dict,
    method: str,
    url: str,
    expected: dict,
    response_body: dict | None = None,
    response_status: int = 200,
) -> None:
    body = response_body if response_body is not None else {}

    def _handler(request: httpx.Request) -> httpx.Response:
        actual = _json.loads(request.content)
        for key, value in expected.items():
            assert actual.get(key) == value, (
                f"Payload mismatch for {key!r}: expected {value!r}, got {actual.get(key)!r}"
            )
        return _make_response(response_status, body)

    mock_transport[(method, url)] = _handler


# ---------------------------------------------------------------------------
# helpers in server.py
# ---------------------------------------------------------------------------

from caas_mcp.server import _parse_env, _parse_gpu, _to_json


class TestParseEnv:

    def test_none_becomes_none(self):
        assert _parse_env(None) is None

    def test_empty_string_becomes_none(self):
        assert _parse_env("") is None

    def test_single_pair(self):
        result = _parse_env("FOO=1")
        assert result == {"FOO": "1"}

    def test_multiple_pairs(self):
        result = _parse_env("A=1,B=2")
        assert result == {"A": "1", "B": "2"}

    def test_whitespace_is_stripped(self):
        result = _parse_env(" A = 1 , B=2 ")
        assert result == {"A": "1", "B": "2"}

    def test_malformed_pair_is_skipped(self):
        assert _parse_env("FOO,BAR=2") == {"BAR": "2"}


class TestParseGpu:

    def test_device_ids_list(self):
        result = _parse_gpu("0,1")
        assert result == {"device_ids": ["0", "1"], "capabilities": ["gpu"]}

    def test_single_device(self):
        result = _parse_gpu("3")
        assert result == {"device_ids": ["3"], "capabilities": ["gpu"]}

    def test_legacy_prefix_stripped(self):
        """Legacy 'gpu:N' prefix is stripped and converted to device_ids list."""
        result = _parse_gpu("gpu:2")
        assert result == {"device_ids": ["2"], "capabilities": ["gpu"]}

    def test_all_devices(self):
        result = _parse_gpu("all")
        assert result == {"device_ids": "all", "capabilities": ["gpu"]}

    def test_empty_returns_none(self):
        assert _parse_gpu("") is None
        assert _parse_gpu("   ") is None

    def test_none_returns_none(self):
        assert _parse_gpu(None) is None


class TestToJson:

    def test_dict(self):
        result = _to_json({"key": "val"})
        parsed = _json.loads(result)
        assert parsed == {"key": "val"}

    def test_list(self):
        result = _to_json([1, 2, 3])
        assert _json.loads(result) == [1, 2, 3]

    def test_special_chars(self):
        result = _to_json({"emoji": "\u2764"})
        assert "\u2764" in result


# ---------------------------------------------------------------------------
# make_server — structural tests (async)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_make_server_returns_server():
    """make_server() returns a FastMCP instance (no real HTTP call)."""
    import os
    os.environ["CAAS_DISPATCHER_URL"] = "http://test:9999"
    os.environ.pop("CAAS_API_KEY", None)
    os.environ.pop("CAAS_REMOTE_WORKSPACE", None)

    from caas_mcp.server import make_server
    from mcp.server.fastmcp import FastMCP

    server = make_server()
    assert isinstance(server, FastMCP)


@pytest.mark.asyncio
async def test_make_server_exposes_health_resource():
    """The server registers health and GPU resources."""
    import os
    os.environ["CAAS_DISPATCHER_URL"] = "http://test:9999"
    os.environ.pop("CAAS_API_KEY", None)
    os.environ.pop("CAAS_REMOTE_WORKSPACE", None)

    from caas_mcp.server import make_server
    server = make_server()

    tools = await server.list_tools()
    names = {t.name for t in tools}
    assert "list_jobs" in names

    resources = await server.list_resources()
    uris = [str(r.uri) for r in resources]
    assert "system://health" in uris
    assert "system://gpu" in uris

    # sandbox resource is registered as a template
    templates = await server.list_resource_templates()
    template_uris = [str(t.uriTemplate) for t in templates]
    assert "sandbox://{sandbox_id}/status" in template_uris


@pytest.mark.asyncio
async def test_make_server_tool_count():
    """The server registers all currently supported tools."""
    import os
    os.environ["CAAS_DISPATCHER_URL"] = "http://test:9999"
    os.environ.pop("CAAS_API_KEY", None)
    os.environ.pop("CAAS_REMOTE_WORKSPACE", None)

    from caas_mcp.server import make_server
    server = make_server()

    tools = await server.list_tools()
    names = sorted(t.name for t in tools)
    assert names == [
        "browse_files",
        "cancel_schedule",
        "check_image",
        "create_sandbox",
        "create_schedule",
        "delete_template",
        "execute",
        "execute_cell",
        "get_job_by_id",
        "get_logs",
        "list_images",
        "list_jobs",
        "list_schedules",
        "list_templates",
        "read_file",
        "sandbox_exec",
        "sandbox_stop",
        "staging_create",
        "staging_delete",
        "staging_list",
        "stop_job",
        "upsert_template",
        "write_file",
    ]


# ---------------------------------------------------------------------------
# Integration-style: pass a pre-built Config with injected client
# ---------------------------------------------------------------------------

def _make_mock_transport(responses):
    """Create an httpx mock transport from a dict of (method, url) -> response.

    Supports ``transport[(METHOD, url)] = response_or_callable`` syntax.
    """
    _storage = dict(responses) if not isinstance(responses, dict) else dict(responses)

    class _Transport(httpx.BaseTransport):
        def __setitem__(self, key, value):
            _storage[key] = value

        def __getitem__(self, key):
            return _storage[key]

        def keys(self):
            return _storage.keys()

        def handle_request(self, request):
            key = (request.method, str(request.url).split("?")[0])
            if key not in _storage:
                raise AssertionError(f"Unexpected request: {key}")
            resp = _storage[key]
            if callable(resp):
                return resp(request)
            return resp

    return _Transport()


def _make_client(responses):
    """Create a CaasClient wired to a mock transport."""
    from caas.client import CaasClient
    return CaasClient(
        host=BASE_URL,
        api_key=API_KEY,
        http_client=httpx.Client(transport=_make_mock_transport(responses)),
    )


@pytest.mark.asyncio
async def test_list_jobs_with_state_filter():
    """list_jobs(state='running') uses the extension /api/jobs endpoint."""
    mt = _make_mock_transport({})
    mt[("GET", f"{BASE_URL}/api/jobs")] = _make_response(
        200, [{"job_id": "run001", "status": "running"}]
    )

    from caas_mcp.config import Config
    from caas_mcp.server import make_server

    cfg = Config(dispatcher_url=BASE_URL)
    cfg._mock_http = httpx.Client(transport=mt)

    server = make_server(cfg)

    tools = await server.list_tools()
    assert len([t for t in tools if t.name == "list_jobs"]) == 1

    result = await server.call_tool("list_jobs", {"state": "running"})
    content_text = result[0][0].text if hasattr(result[0][0], "text") else result[0][0]
    parsed = _json.loads(content_text)
    assert isinstance(parsed, list)
    assert parsed[0]["status"] == "running"


@pytest.mark.asyncio
async def test_list_jobs_returns_error_json_on_caas_error():
    """list_jobs returns structured JSON error when CaasClient raises."""
    mt = _make_mock_transport({})
    mt[("GET", f"{BASE_URL}/v1/jobs")] = _make_response(
        500, {"detail": "Docker unreachable"}
    )

    from caas_mcp.config import Config
    from caas_mcp.server import make_server

    cfg = Config(dispatcher_url=BASE_URL)
    cfg._mock_http = httpx.Client(transport=mt)

    server = make_server(cfg)

    tools = await server.list_tools()
    assert len([t for t in tools if t.name == "list_jobs"]) == 1

    result = await server.call_tool("list_jobs", {})
    content_text = result[0][0].text if hasattr(result[0][0], "text") else result[0][0]
    parsed = _json.loads(content_text)
    assert "error" in parsed
    assert "Docker unreachable" in parsed["error"]


@pytest.mark.asyncio
async def test_execute_cell_injects_workspace_volume():
    """When CAAS_REMOTE_WORKSPACE is set, execute_cell injects the volume mount."""
    captured_request = {}

    def _check(request):
        body = _json.loads(request.content)
        captured_request["volumes"] = body.get("volumes", [])
        return _make_response(
            200, {"status": "exited", "exit_code": 0, "stdout": "ok\n", "logs": "ok\n"}
        )

    mt = _make_mock_transport({("POST", f"{BASE_URL}/v1/execute/cell"): _check})

    import os
    os.environ["CAAS_DISPATCHER_URL"] = BASE_URL
    os.environ["CAAS_REMOTE_WORKSPACE"] = "/mnt/data/staging"

    from caas_mcp.config import Config
    from caas_mcp.server import make_server
    cfg = Config()  # reads from env
    cfg._mock_http = httpx.Client(transport=mt)

    server = make_server(cfg)

    tools = await server.list_tools()
    assert len([t for t in tools if t.name == "execute_cell"]) == 1

    await server.call_tool("execute_cell", {"code": "print('hello')", "image": "python:3.11-slim"})
    volumes = captured_request["volumes"]
    assert len(volumes) == 1
    assert volumes[0]["host_path"] == "/mnt/data/staging"
    assert volumes[0]["container_path"] == "/workspace"
    assert volumes[0]["mode"] == "rw"


@pytest.mark.asyncio
async def test_upsert_template_invalid_volumes_returns_error_json():
    """upsert_template returns structured JSON for malformed volumes input."""
    import os
    os.environ["CAAS_DISPATCHER_URL"] = BASE_URL
    os.environ.pop("CAAS_API_KEY", None)
    os.environ.pop("CAAS_REMOTE_WORKSPACE", None)

    from caas_mcp.server import make_server
    server = make_server()

    result = await server.call_tool("upsert_template", {"name": "tmpl", "volumes": "{bad"})
    content_text = result[0][0].text if hasattr(result[0][0], "text") else result[0][0]
    parsed = _json.loads(content_text)
    assert "error" in parsed
    assert "Invalid volumes JSON" in parsed["error"]


@pytest.mark.asyncio
async def test_create_schedule_invalid_volumes_returns_error_json():
    """create_schedule returns structured JSON for malformed volumes input."""
    import os
    os.environ["CAAS_DISPATCHER_URL"] = BASE_URL
    os.environ.pop("CAAS_API_KEY", None)
    os.environ.pop("CAAS_REMOTE_WORKSPACE", None)

    from caas_mcp.server import make_server
    server = make_server()

    result = await server.call_tool("create_schedule", {"template_id": "t1", "volumes": "{bad"})
    content_text = result[0][0].text if hasattr(result[0][0], "text") else result[0][0]
    parsed = _json.loads(content_text)
    assert "error" in parsed
    assert "Invalid volumes JSON" in parsed["error"]


# ---------------------------------------------------------------------------
# Sandbox integration tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_create_sandbox_includes_workspace_volume():
    """create_sandbox injects CAAS_REMOTE_WORKSPACE as a volume mount."""
    mt = _make_mock_transport({})
    mt[("POST", f"{BASE_URL}/v1/sandbox")] = _make_response(
        200, {"sandbox_id": "sbox001", "status": "running"}
    )

    import os
    os.environ["CAAS_DISPATCHER_URL"] = BASE_URL
    os.environ["CAAS_REMOTE_WORKSPACE"] = "/mnt/data/staging"

    from caas_mcp.config import Config
    from caas_mcp.server import make_server

    cfg = Config()
    cfg._mock_http = httpx.Client(transport=mt)

    server = make_server(cfg)

    tools = await server.list_tools()
    assert len([t for t in tools if t.name == "create_sandbox"]) == 1

    result = await server.call_tool("create_sandbox", {"image": "python:3.11-slim"})
    content_text = result[0][0].text if hasattr(result[0][0], "text") else result[0][0]
    parsed = _json.loads(content_text)
    assert parsed["sandbox_id"] == "sbox001"
    assert parsed["status"] == "running"


@pytest.mark.asyncio
async def test_create_sandbox_parses_gpu_string():
    """create_sandbox parses 'gpu: all' into correct device_ids shape."""
    mt = _make_mock_transport({})

    captured_payload = {}

    def _check(request):
        captured_payload["gpu"] = _json.loads(request.content).get("gpu", {})
        return _make_response(200, {"sandbox_id": "s001", "status": "running"})

    mt[("POST", f"{BASE_URL}/v1/sandbox")] = _check

    import os
    os.environ["CAAS_DISPATCHER_URL"] = BASE_URL
    os.environ.pop("CAAS_REMOTE_WORKSPACE", None)

    from caas_mcp.config import Config
    from caas_mcp.server import make_server

    cfg = Config(dispatcher_url=BASE_URL)
    cfg._mock_http = httpx.Client(transport=mt)

    server = make_server(cfg)

    await server.call_tool("create_sandbox", {"image": "pytorch:latest", "gpu": "all"})

    assert captured_payload["gpu"] == {"device_ids": "all", "capabilities": ["gpu"]}


@pytest.mark.asyncio
async def test_sandbox_exec_tool():
    """sandbox_exec forwards sandbox_id and cmd, returns stdout/stderr."""
    mt = _make_mock_transport({})
    mt[("POST", f"{BASE_URL}/v1/jobs/sbox001/exec")] = _make_response(
        200, {"stdout": "all green\n", "stderr": "", "exit_code": 0}
    )

    from caas_mcp.config import Config
    from caas_mcp.server import make_server

    cfg = Config(dispatcher_url=BASE_URL)
    cfg._mock_http = httpx.Client(transport=mt)

    server = make_server(cfg)

    tools = await server.list_tools()
    assert len([t for t in tools if t.name == "sandbox_exec"]) == 1

    result = await server.call_tool("sandbox_exec", {"sandbox_id": "sbox001", "cmd": "echo ok"})
    content_text = result[0][0].text if hasattr(result[0][0], "text") else result[0][0]
    parsed = _json.loads(content_text)
    assert parsed["stdout"] == "all green\n"
    assert parsed["exit_code"] == 0


@pytest.mark.asyncio
async def test_sandbox_stop_tool():
    """sandbox_stop calls DELETE /v1/jobs/{id} and returns stopped status."""
    mt = _make_mock_transport({})
    mt[("DELETE", f"{BASE_URL}/v1/jobs/sbox001")] = _make_response(
        200, {"job_id": "sbox001", "status": "stopped"}
    )

    from caas_mcp.config import Config
    from caas_mcp.server import make_server

    cfg = Config(dispatcher_url=BASE_URL)
    cfg._mock_http = httpx.Client(transport=mt)

    server = make_server(cfg)

    await server.call_tool("sandbox_stop", {"sandbox_id": "sbox001"})


@pytest.mark.asyncio
async def test_sandbox_status_resource():
    """sandbox://sandbox_id/status resource returns job metadata."""
    mt = _make_mock_transport({})
    mt[("GET", f"{BASE_URL}/v1/jobs/sbox001")] = _make_response(
        200, {"status": "running", "job_type": "sandbox", "image": "python:3.11-slim"}
    )

    from caas_mcp.config import Config
    from caas_mcp.server import make_server

    cfg = Config(dispatcher_url=BASE_URL)
    cfg._mock_http = httpx.Client(transport=mt)

    server = make_server(cfg)

    # sandbox resource is registered as a template, not a concrete resource
    templates = await server.list_resource_templates()
    template_uris = [str(t.uriTemplate) for t in templates]
    matching = [u for u in template_uris if "sandbox://{sandbox_id}/status" in u]
    assert len(matching) == 1


@pytest.mark.asyncio
async def test_sandbox_exec_truncates_large_output():
    """sandbox_exec truncates stdout/stderr to 8000 chars to protect context window."""
    mt = _make_mock_transport({})
    large_output = "x" * 10000
    mt[("POST", f"{BASE_URL}/v1/jobs/sbox001/exec")] = _make_response(
        200, {"stdout": large_output, "stderr": large_output, "exit_code": 0}
    )

    from caas_mcp.config import Config
    from caas_mcp.server import make_server

    cfg = Config(dispatcher_url=BASE_URL)
    cfg._mock_http = httpx.Client(transport=mt)

    server = make_server(cfg)

    result = await server.call_tool("sandbox_exec", {"sandbox_id": "sbox001", "cmd": "run"})
    content_text = result[0][0].text if hasattr(result[0][0], "text") else result[0][0]
    parsed = _json.loads(content_text)

    assert len(parsed["stdout"]) <= 8000 + 46  # 8000 + "[System Note: stdout truncated.]"
    assert len(parsed["stderr"]) <= 8000 + 46  # 8000 + "[System Note: stderr truncated.]"
    assert "truncated" in parsed["stdout"]
    assert "truncated" in parsed["stderr"]
