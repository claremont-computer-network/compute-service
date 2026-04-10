"""
Tests for module-level configuration and env-var parsing.
"""
import importlib
import sys
from unittest.mock import patch


def _reload_main(env: dict):
    """Re-import app.main with the given environment variables patched in."""
    for mod_name in list(sys.modules):
        if "app.main" in mod_name:
            del sys.modules[mod_name]
    with patch("docker.from_env"), patch.dict("os.environ", env, clear=False):
        import app.main as m
        return m


def test_max_shm_size_mb_valid_integer():
    """A valid MAX_SHM_SIZE_MB env var is parsed correctly."""
    m = _reload_main({"MAX_SHM_SIZE_MB": "4096"})
    assert m.MAX_SHM_SIZE_MB == 4096


def test_max_shm_size_mb_invalid_falls_back_to_8192(caplog):
    """A non-integer MAX_SHM_SIZE_MB logs a warning and falls back to 8192."""
    import logging
    with caplog.at_level(logging.WARNING, logger="app.main"):
        m = _reload_main({"MAX_SHM_SIZE_MB": "lots"})
    assert m.MAX_SHM_SIZE_MB == 8192
    assert "MAX_SHM_SIZE_MB" in caplog.text


def test_allow_ipc_host_false_by_default():
    """ALLOW_IPC_HOST defaults to False when the env var is absent."""
    env = {k: v for k, v in __import__("os").environ.items() if k != "ALLOW_IPC_HOST"}
    for mod_name in list(sys.modules):
        if "app.main" in mod_name:
            del sys.modules[mod_name]
    with patch("docker.from_env"), patch.dict("os.environ", {}, clear=True):
        import app.main as m
    assert m.ALLOW_IPC_HOST is False


def test_allow_ipc_host_true_when_set():
    """ALLOW_IPC_HOST=true enables the flag."""
    m = _reload_main({"ALLOW_IPC_HOST": "true"})
    assert m.ALLOW_IPC_HOST is True


def test_allow_ipc_host_case_insensitive():
    """ALLOW_IPC_HOST is case-insensitive (TRUE, True, true all work)."""
    for val in ("TRUE", "True", "true"):
        m = _reload_main({"ALLOW_IPC_HOST": val})
        assert m.ALLOW_IPC_HOST is True, f"failed for {val!r}"
