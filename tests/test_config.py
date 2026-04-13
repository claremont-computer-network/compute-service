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


# ── QUEUE_TIMEOUT_SECS ────────────────────────────────────────────────────────

def test_queue_timeout_valid_integer():
    """A valid QUEUE_TIMEOUT_SECS is parsed correctly."""
    m = _reload_main({"QUEUE_TIMEOUT_SECS": "120"})
    assert m.QUEUE_TIMEOUT == 120


def test_queue_timeout_default():
    """QUEUE_TIMEOUT defaults to 300 when the env var is absent."""
    for mod_name in list(sys.modules):
        if "app.main" in mod_name:
            del sys.modules[mod_name]
    with patch("docker.from_env"), patch.dict("os.environ", {}, clear=True):
        import app.main as m
    assert m.QUEUE_TIMEOUT == 300


def test_queue_timeout_invalid_falls_back_to_300(caplog):
    """A non-integer QUEUE_TIMEOUT_SECS logs a warning and falls back to 300."""
    import logging
    with caplog.at_level(logging.WARNING, logger="caas.dispatcher"):
        m = _reload_main({"QUEUE_TIMEOUT_SECS": "infinity"})
    assert m.QUEUE_TIMEOUT == 300
    assert "QUEUE_TIMEOUT_SECS" in caplog.text


def test_queue_timeout_negative_clamps_to_zero(caplog):
    """A negative QUEUE_TIMEOUT_SECS logs a warning and clamps to 0 (fail-fast)."""
    import logging
    with caplog.at_level(logging.WARNING, logger="caas.dispatcher"):
        m = _reload_main({"QUEUE_TIMEOUT_SECS": "-1"})
    assert m.QUEUE_TIMEOUT == 0
    assert "QUEUE_TIMEOUT_SECS" in caplog.text


# ── MAX_CONCURRENT_GPU_JOBS ───────────────────────────────────────────────────

def test_max_concurrent_gpu_jobs_valid_integer():
    """A valid MAX_CONCURRENT_GPU_JOBS is reflected in the GPU semaphore."""
    m = _reload_main({"MAX_CONCURRENT_GPU_JOBS": "3"})
    assert m.resource_slots._slots["gpu"]._value == 3


def test_max_concurrent_gpu_jobs_default():
    """MAX_CONCURRENT_GPU_JOBS defaults to 1 when the env var is absent."""
    for mod_name in list(sys.modules):
        if "app.main" in mod_name:
            del sys.modules[mod_name]
    with patch("docker.from_env"), patch.dict("os.environ", {}, clear=True):
        import app.main as m
    assert m.resource_slots._slots["gpu"]._value == 1


def test_max_concurrent_gpu_jobs_invalid_falls_back_to_default(caplog):
    """A non-integer MAX_CONCURRENT_GPU_JOBS logs a warning and falls back to 1."""
    import logging
    with caplog.at_level(logging.WARNING, logger="caas.dispatcher"):
        m = _reload_main({"MAX_CONCURRENT_GPU_JOBS": "two"})
    assert m.resource_slots._slots["gpu"]._value == 1
    assert "MAX_CONCURRENT_GPU_JOBS" in caplog.text


def test_max_concurrent_gpu_jobs_negative_clamps_to_zero(caplog):
    """A negative MAX_CONCURRENT_GPU_JOBS logs a warning and clamps to 0."""
    import logging
    with caplog.at_level(logging.WARNING, logger="caas.dispatcher"):
        m = _reload_main({"MAX_CONCURRENT_GPU_JOBS": "-5"})
    assert m.resource_slots._slots["gpu"]._value == 0
    assert "MAX_CONCURRENT_GPU_JOBS" in caplog.text


# ── MAX_CONCURRENT_CPU_JOBS ───────────────────────────────────────────────────

def test_max_concurrent_cpu_jobs_valid_integer():
    """A valid MAX_CONCURRENT_CPU_JOBS is reflected in the CPU semaphore."""
    m = _reload_main({"MAX_CONCURRENT_CPU_JOBS": "8"})
    assert m.resource_slots._slots["cpu"]._value == 8


def test_max_concurrent_cpu_jobs_default():
    """MAX_CONCURRENT_CPU_JOBS defaults to 4 when the env var is absent."""
    for mod_name in list(sys.modules):
        if "app.main" in mod_name:
            del sys.modules[mod_name]
    with patch("docker.from_env"), patch.dict("os.environ", {}, clear=True):
        import app.main as m
    assert m.resource_slots._slots["cpu"]._value == 4


def test_max_concurrent_cpu_jobs_invalid_falls_back_to_default(caplog):
    """A non-integer MAX_CONCURRENT_CPU_JOBS logs a warning and falls back to 4."""
    import logging
    with caplog.at_level(logging.WARNING, logger="caas.dispatcher"):
        m = _reload_main({"MAX_CONCURRENT_CPU_JOBS": "many"})
    assert m.resource_slots._slots["cpu"]._value == 4
    assert "MAX_CONCURRENT_CPU_JOBS" in caplog.text


def test_max_concurrent_cpu_jobs_negative_clamps_to_zero(caplog):
    """A negative MAX_CONCURRENT_CPU_JOBS logs a warning and clamps to 0."""
    import logging
    with caplog.at_level(logging.WARNING, logger="caas.dispatcher"):
        m = _reload_main({"MAX_CONCURRENT_CPU_JOBS": "-2"})
    assert m.resource_slots._slots["cpu"]._value == 0
    assert "MAX_CONCURRENT_CPU_JOBS" in caplog.text
