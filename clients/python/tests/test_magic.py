"""
Tests for caas.magic – the %%dispatch IPython cell magic.
IPython is mocked so these tests run without a live kernel.
"""
import pytest
from unittest.mock import MagicMock, patch, call


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _make_ip(registered=None):
    """Return a minimal IPython shell mock."""
    ip = MagicMock()
    ip.register_magic_function = MagicMock()
    return ip


def _run_magic(line, cell, host="http://compute-node:8000", api_key="key",
               default_image="python:3.11-slim", extra_env=None):
    """
    Invoke the dispatch magic function directly, bypassing IPython registration.
    Returns (printed_output, client_mock).
    """
    from caas.magic import _dispatch_magic, _config

    _config.update({
        "host": host,
        "api_key": api_key,
        "default_image": default_image,
        "default_gpu": None,
    })
    if extra_env:
        _config.update(extra_env)

    mock_client = MagicMock()
    mock_client.execute_cell.return_value = "remote output\n"

    printed = []
    with patch("caas.magic._make_client", return_value=mock_client):
        with patch("builtins.print", side_effect=lambda *a, **k: printed.append(" ".join(str(x) for x in a))):
            _dispatch_magic(line, cell)

    return printed, mock_client


# ---------------------------------------------------------------------------
# basic dispatch
# ---------------------------------------------------------------------------

def test_dispatch_sends_cell_body(mock_transport):
    printed, mock_client = _run_magic(line="", cell="print('hello')")
    mock_client.execute_cell.assert_called_once()
    call_kwargs = mock_client.execute_cell.call_args
    assert call_kwargs.kwargs["code"] == "print('hello')"


def test_dispatch_uses_default_image(mock_transport):
    _, mock_client = _run_magic(line="", cell="x=1")
    call_kwargs = mock_client.execute_cell.call_args
    assert call_kwargs.kwargs["image"] == "python:3.11-slim"


def test_dispatch_prints_remote_output(mock_transport):
    printed, _ = _run_magic(line="", cell="print('hello')")
    assert any("remote output" in p for p in printed)


# ---------------------------------------------------------------------------
# line argument parsing
# ---------------------------------------------------------------------------

def test_dispatch_image_override(mock_transport):
    _, mock_client = _run_magic(line="--image pytorch/pytorch:latest", cell="import torch")
    call_kwargs = mock_client.execute_cell.call_args
    assert call_kwargs.kwargs["image"] == "pytorch/pytorch:latest"


def test_dispatch_gpu_all(mock_transport):
    _, mock_client = _run_magic(line="--gpu all", cell="import torch")
    call_kwargs = mock_client.execute_cell.call_args
    assert call_kwargs.kwargs["gpu"] == {"device_ids": "all", "capabilities": ["gpu"]}


def test_dispatch_gpu_specific_devices(mock_transport):
    _, mock_client = _run_magic(line="--gpu 0,1", cell="import torch")
    call_kwargs = mock_client.execute_cell.call_args
    assert call_kwargs.kwargs["gpu"] == {"device_ids": ["0", "1"], "capabilities": ["gpu"]}


def test_dispatch_no_gpu_by_default(mock_transport):
    _, mock_client = _run_magic(line="", cell="x=1")
    call_kwargs = mock_client.execute_cell.call_args
    assert call_kwargs.kwargs.get("gpu") is None


# ---------------------------------------------------------------------------
# misconfiguration
# ---------------------------------------------------------------------------

def test_dispatch_raises_when_host_not_set():
    from caas.magic import _dispatch_magic, _config, CaasMagicError
    _config["host"] = None
    with pytest.raises(CaasMagicError, match="CAAS_HOST"):
        _dispatch_magic("", "print('x')")


def test_dispatch_raises_when_image_not_set():
    from caas.magic import _dispatch_magic, _config, CaasMagicError
    _config.update({"host": "http://host:8000", "api_key": None, "default_image": None, "default_gpu": None})
    with pytest.raises(CaasMagicError, match="image"):
        _dispatch_magic("", "print('x')")


# ---------------------------------------------------------------------------
# register_magic
# ---------------------------------------------------------------------------

def test_register_magic_loads_from_env():
    """register_magic reads CAAS_HOST and DISPATCHER_API_KEY from environment."""
    import os
    from caas.magic import register_magic, _config

    env = {
        "CAAS_HOST": "http://mynode:8000",
        "DISPATCHER_API_KEY": "secretkey",
        "CAAS_DEFAULT_IMAGE": "python:3.11-slim",
    }
    ip = _make_ip()
    with patch.dict(os.environ, env, clear=False):
        with patch("caas.magic._get_ipython", return_value=ip):
            register_magic()

    assert _config["host"] == "http://mynode:8000"
    assert _config["api_key"] == "secretkey"
    assert _config["default_image"] == "python:3.11-slim"
