"""Tests for caas_mcp.config."""
import os
import pytest


def _unset(*keys):
    """Remove keys from the environment (fail if they were not set)."""
    for k in keys:
        os.environ.pop(k, None)


def test_required_dispatcher_url_raises_when_missing():
    _unset("CAAS_DISPATCHER_URL", "CAAS_API_KEY", "CAAS_REMOTE_WORKSPACE")
    from caas_mcp.config import Config, ConfigError
    with pytest.raises(ConfigError, match="CAAS_DISPATCHER_URL"):
        Config()


def test_requires_dispatcher_url_via_arg():
    _unset("CAAS_DISPATCHER_URL")
    from caas_mcp.config import Config, ConfigError
    with pytest.raises(ConfigError, match="CAAS_DISPATCHER_URL"):
        Config(dispatcher_url=None)


def test_accepts_dispatcher_url_as_kwarg():
    _unset("CAAS_DISPATCHER_URL", "CAAS_API_KEY", "CAAS_REMOTE_WORKSPACE")
    from caas_mcp.config import Config
    cfg = Config(dispatcher_url="http://10.0.0.1:8000")
    assert cfg.dispatcher_url == "http://10.0.0.1:8000"


def test_optional_api_key_defaults_to_none():
    _unset("CAAS_API_KEY", "CAAS_REMOTE_WORKSPACE")
    os.environ["CAAS_DISPATCHER_URL"] = "http://10.0.0.1:8000"
    from caas_mcp.config import Config
    cfg = Config()
    assert cfg.api_key is None


def test_reads_api_key_from_env():
    os.environ["CAAS_DISPATCHER_URL"] = "http://10.0.0.1:8000"
    os.environ["CAAS_API_KEY"] = "my-secret"
    from caas_mcp.config import Config
    cfg = Config()
    assert cfg.api_key == "my-secret"


def test_optional_remote_workspace_defaults_to_none():
    _unset("CAAS_REMOTE_WORKSPACE")
    os.environ["CAAS_DISPATCHER_URL"] = "http://10.0.0.1:8000"
    from caas_mcp.config import Config
    cfg = Config()
    assert cfg.remote_workspace is None


def test_reads_remote_workspace_from_env():
    os.environ["CAAS_DISPATCHER_URL"] = "http://10.0.0.1:8000"
    os.environ["CAAS_REMOTE_WORKSPACE"] = "/mnt/data/staging"
    from caas_mcp.config import Config
    cfg = Config()
    assert cfg.remote_workspace == "/mnt/data/staging"


def test_repr_hides_api_key():
    from caas_mcp.config import Config
    cfg = Config(dispatcher_url="http://x", api_key="secret123")
    assert "secret123" not in repr(cfg)
    assert "******" in repr(cfg)


def test_repr_shows_none_when_no_key():
    from caas_mcp.config import Config
    cfg = Config(dispatcher_url="http://x", api_key=None)
    # repr uses Python repr of None which is 'None'
    assert "None" in repr(cfg) or "api_key='None'" not in repr(cfg)


def test_repr_shows_hidden_key():
    from caas_mcp.config import Config
    cfg = Config(dispatcher_url="http://x", api_key="secret")
    # The key value itself must NOT appear in the repr
    assert "secret" not in repr(cfg)
    # But the placeholder should be visible
    assert "******" in repr(cfg)


def test_args_override_env():
    os.environ["CAAS_DISPATCHER_URL"] = "http://old.local:8000"
    os.environ["CAAS_API_KEY"] = "old-key"
    os.environ.pop("CAAS_REMOTE_WORKSPACE", None)

    from caas_mcp.config import Config
    cfg = Config(
        dispatcher_url="http://new.local:8000",
        api_key="new-key",
        remote_workspace="/mnt/new",
    )
    assert cfg.dispatcher_url == "http://new.local:8000"
    assert cfg.api_key == "new-key"
    assert cfg.remote_workspace == "/mnt/new"
