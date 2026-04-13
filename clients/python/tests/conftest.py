"""
Shared fixtures for caas client tests.
All HTTP calls are intercepted by httpx's MockTransport — no real server needed.
"""
import pytest
import httpx
from unittest.mock import MagicMock


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
    """Register a transport handler that asserts *expected* is a subset of the request body.

    Every key in *expected* must be present in the decoded JSON body with an equal value.
    Keys not in *expected* are ignored. The handler returns a 200 response with
    *response_body* (defaulting to an empty dict) unless overridden.

    Usage::

        _assert_payload(
            mock_transport,
            "POST", f"{BASE_URL}/v1/execute",
            {"image": "alpine:3.18", "detach": True},
            response_body={"container_id": "abc", "status": "running"},
        )
        client.execute(image="alpine:3.18")
    """
    import json as _json

    body = response_body if response_body is not None else {}

    def _handler(request: httpx.Request) -> httpx.Response:
        actual = _json.loads(request.content)
        for key, value in expected.items():
            assert actual.get(key) == value, (
                f"Payload mismatch for {key!r}: expected {value!r}, got {actual.get(key)!r}"
            )
        return _make_response(response_status, body)

    mock_transport[(method, url)] = _handler


@pytest.fixture()
def mock_transport():
    """Returns a dict-based mock transport you can configure per test."""
    return {}


@pytest.fixture()
def client(mock_transport):
    """A CaasClient wired to a controllable httpx mock transport."""
    from caas.client import CaasClient

    responses = mock_transport  # filled in by individual tests

    class _DictTransport(httpx.BaseTransport):
        def handle_request(self, request):
            key = (request.method, str(request.url).split("?")[0])
            if key not in responses:
                raise AssertionError(f"Unexpected request: {key}")
            resp = responses[key]
            if callable(resp):
                return resp(request)
            return resp

    return CaasClient(
        host=BASE_URL,
        api_key=API_KEY,
        http_client=httpx.Client(transport=_DictTransport()),
    )
