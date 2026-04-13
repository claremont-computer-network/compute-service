"""
Tests for the CAAS_PLUGINS env-var loader in app.plugins._load_env_plugins.
"""
import pytest
from unittest.mock import patch
from app.core.plugin import CaasPlugin, PluginRegistry


class _BrokenPlugin(CaasPlugin):
    name = "broken"
    priority = 200

    def __init__(self):
        raise RuntimeError("intentional init failure")


_BROKEN_PATH = f"{__name__}._BrokenPlugin"


# ---------------------------------------------------------------------------
# A minimal in-process plugin we can reference by dotted path
# ---------------------------------------------------------------------------

class _DummyPlugin(CaasPlugin):
    name = "dummy-env-loaded"
    priority = 200


class _DummyPlugin2(CaasPlugin):
    name = "dummy-env-loaded-2"
    priority = 210


# Dotted paths used in tests
_DUMMY_PATH = f"{__name__}._DummyPlugin"
_DUMMY2_PATH = f"{__name__}._DummyPlugin2"


# ---------------------------------------------------------------------------
# Fixture: isolated registry so tests don't interfere with the global one
# ---------------------------------------------------------------------------

@pytest.fixture()
def isolated_loader():
    """Return _load_env_plugins bound to a fresh, empty PluginRegistry."""
    from app.plugins import _load_env_plugins
    import app.plugins as _mod

    fresh_registry = PluginRegistry()
    with patch.object(_mod, "registry", fresh_registry):
        yield fresh_registry, _load_env_plugins


# ---------------------------------------------------------------------------
# Happy-path tests
# ---------------------------------------------------------------------------

def test_single_plugin_loaded(isolated_loader):
    """A single valid entry is imported and registered."""
    registry, loader = isolated_loader
    with patch.dict("os.environ", {"CAAS_PLUGINS": _DUMMY_PATH}):
        loader()
    assert registry.names() == ["dummy-env-loaded"]


def test_multiple_plugins_loaded(isolated_loader):
    """Multiple comma-separated entries are all registered."""
    registry, loader = isolated_loader
    with patch.dict("os.environ", {"CAAS_PLUGINS": f"{_DUMMY_PATH},{_DUMMY2_PATH}"}):
        loader()
    assert set(registry.names()) == {"dummy-env-loaded", "dummy-env-loaded-2"}


def test_empty_env_var_is_noop(isolated_loader):
    """An empty CAAS_PLUGINS leaves the registry unchanged."""
    registry, loader = isolated_loader
    with patch.dict("os.environ", {"CAAS_PLUGINS": ""}):
        loader()
    assert registry.names() == []


def test_whitespace_only_entries_ignored(isolated_loader):
    """Entries that are whitespace only after stripping are silently ignored."""
    registry, loader = isolated_loader
    with patch.dict("os.environ", {"CAAS_PLUGINS": f"  ,  ,{_DUMMY_PATH},  "}):
        loader()
    assert registry.names() == ["dummy-env-loaded"]


def test_plugins_sorted_by_priority(isolated_loader):
    """Plugins are sorted by priority regardless of the order listed in the env var."""
    registry, loader = isolated_loader
    # _DummyPlugin2 has priority 210, _DummyPlugin has 200 — list them reversed
    with patch.dict("os.environ", {"CAAS_PLUGINS": f"{_DUMMY2_PATH},{_DUMMY_PATH}"}):
        loader()
    assert registry.names() == ["dummy-env-loaded", "dummy-env-loaded-2"]


# ---------------------------------------------------------------------------
# Error-handling tests — bad entries must be skipped, not crash the loader
# ---------------------------------------------------------------------------

def test_no_dot_in_entry_is_skipped(isolated_loader, caplog):
    """An entry without a dot logs ERROR and is skipped."""
    registry, loader = isolated_loader
    with patch.dict("os.environ", {"CAAS_PLUGINS": f"NoDotHere,{_DUMMY_PATH}"}):
        with caplog.at_level("ERROR", logger="caas.dispatcher"):
            loader()
    assert "NoDotHere" in caplog.text
    # The valid entry still loads
    assert registry.names() == ["dummy-env-loaded"]


def test_missing_module_is_skipped(isolated_loader, caplog):
    """An entry whose module does not exist logs ERROR and is skipped."""
    registry, loader = isolated_loader
    with patch.dict("os.environ", {"CAAS_PLUGINS": f"no_such_module.FakePlugin,{_DUMMY_PATH}"}):
        with caplog.at_level("ERROR", logger="caas.dispatcher"):
            loader()
    assert "no_such_module" in caplog.text
    assert registry.names() == ["dummy-env-loaded"]


def test_missing_class_is_skipped(isolated_loader, caplog):
    """An entry whose class does not exist in the module logs ERROR and is skipped."""
    registry, loader = isolated_loader
    bad = f"{__name__}.NonExistentClass"
    with patch.dict("os.environ", {"CAAS_PLUGINS": f"{bad},{_DUMMY_PATH}"}):
        with caplog.at_level("ERROR", logger="caas.dispatcher"):
            loader()
    assert "NonExistentClass" in caplog.text
    assert registry.names() == ["dummy-env-loaded"]


def test_instantiation_error_is_skipped(isolated_loader, caplog):
    """A plugin whose __init__ raises logs ERROR and is skipped."""
    registry, loader = isolated_loader
    with patch.dict("os.environ", {"CAAS_PLUGINS": f"{_BROKEN_PATH},{_DUMMY_PATH}"}):
        with caplog.at_level("ERROR", logger="caas.dispatcher"):
            loader()
    assert "broken" in caplog.text.lower() or "intentional" in caplog.text
    assert registry.names() == ["dummy-env-loaded"]


def test_duplicate_name_is_skipped(isolated_loader, caplog):
    """A plugin whose name collides with an already-registered plugin is skipped."""
    registry, loader = isolated_loader
    # Register _DummyPlugin once manually, then try to load it again via env var
    registry.register(_DummyPlugin())
    with patch.dict("os.environ", {"CAAS_PLUGINS": _DUMMY_PATH}):
        with caplog.at_level("ERROR", logger="caas.dispatcher"):
            loader()
    # Still only one entry
    assert registry.names() == ["dummy-env-loaded"]
