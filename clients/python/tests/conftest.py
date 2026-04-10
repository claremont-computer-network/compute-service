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
