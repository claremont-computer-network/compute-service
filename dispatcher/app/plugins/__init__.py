"""
dispatcher/app/plugins/__init__.py
────────────────────────────────────
Built-in plugin registration and ``CAAS_PLUGINS`` env-var loader.

``register_default_plugins`` is called at ``app.main`` module import time
(after ``client`` and ``job_store`` are created) so that plugins are active
even when the FastAPI lifespan context manager is not entered — which is the
case during tests that use a bare :class:`starlette.testclient.TestClient`
without a ``with`` block.  The lifespan also logs the active plugin list.

Community plugin authors
------------------------
To load a third-party plugin **without modifying this file**, set the
``CAAS_PLUGINS`` environment variable to a comma-separated list of
fully-qualified class paths and the dispatcher will import and register them
automatically at startup::

    CAAS_PLUGINS=my_package.plugins.AuditPlugin,other_pkg.MetricsPlugin

Each entry must be a dotted path ending in a :class:`~app.core.plugin.CaasPlugin`
subclass.  The class is instantiated with no arguments, so any configuration
should be read from environment variables inside the plugin's hooks.

Alternatively, you can still call ``registry.register(YourPlugin())`` from
your own startup code after ``app.main`` has been imported.
"""
import importlib
import inspect
import logging
import os

from app.core.plugin import CaasPlugin, registry
from app.plugins.nvidia import NvidiaEntrypointPlugin
from app.plugins.shm_ipc import ShmIpcPolicyPlugin
from app.plugins.volumes import VolumePolicyPlugin
from app.plugins.resource_sampler import ResourceSamplerPlugin
from app.plugins.log_retention import LogRetentionPlugin

logger = logging.getLogger("caas.dispatcher")


def _load_env_plugins() -> None:
    """Import and register plugins listed in the ``CAAS_PLUGINS`` env var.

    Each entry in the comma-separated list must be a fully-qualified class
    path, e.g. ``my_package.plugins.AuditPlugin``.  The class is instantiated
    with no arguments.

    Malformed entries and import errors are logged as ``ERROR`` and skipped
    so that a misconfigured ``CAAS_PLUGINS`` value does not prevent the
    dispatcher from starting.
    """
    raw = os.getenv("CAAS_PLUGINS", "").strip()
    if not raw:
        return

    for entry in (e.strip() for e in raw.split(",") if e.strip()):
        # Split "some.module.ClassName" into ("some.module", "ClassName")
        if "." not in entry:
            logger.error(
                "CAAS_PLUGINS entry %r is not a fully-qualified class path "
                "(expected 'module.ClassName') — skipping.",
                entry,
            )
            continue
        module_path, class_name = entry.rsplit(".", 1)
        try:
            module = importlib.import_module(module_path)
        except Exception:  # noqa: BLE001 — catches SyntaxError, RuntimeError, etc.
            logger.exception(
                "CAAS_PLUGINS: could not import module %r (entry %r) — skipping.",
                module_path,
                entry,
            )
            continue
        try:
            cls = getattr(module, class_name, None)
        except Exception:  # noqa: BLE001 — PEP 562 __getattr__ or other dynamic access
            logger.exception(
                "CAAS_PLUGINS: error resolving attribute %r from module %r (entry %r) — skipping.",
                class_name,
                module_path,
                entry,
            )
            continue
        if cls is None:
            logger.error(
                "CAAS_PLUGINS: module %r has no attribute %r (entry %r) — skipping.",
                module_path,
                class_name,
                entry,
            )
            continue
        if not inspect.isclass(cls):
            logger.error(
                "CAAS_PLUGINS: %r is not a class (entry %r) — skipping.",
                class_name,
                entry,
            )
            continue
        if not issubclass(cls, CaasPlugin):
            logger.error(
                "CAAS_PLUGINS: %r is a class but not a CaasPlugin subclass (entry %r) — skipping.",
                class_name,
                entry,
            )
            continue
        try:
            plugin = cls()
            registry.register(plugin)
            plugin_name = getattr(plugin, "name", entry)
            plugin_priority = getattr(plugin, "priority", None)
            logger.info(
                "CAAS_PLUGINS: registered %r as %r (priority %r).",
                entry,
                plugin_name,
                plugin_priority,
            )
        except Exception:  # noqa: BLE001
            logger.exception(
                "CAAS_PLUGINS: failed to instantiate or register %r — skipping.",
                entry,
            )
            continue


def register_default_plugins(job_store, docker_client) -> None:
    """Register all built-in :class:`~app.core.plugin.CaasPlugin` instances,
    configure shared services, then load any additional plugins from the
    ``CAAS_PLUGINS`` env var.

    Clears any previously registered plugins first so that calling this
    function more than once (e.g. during test reloads) does not accumulate
    duplicate entries.  Services configuration is applied after registration
    so both built-in and env-loaded plugins receive the same services object.

    Args:
        job_store: The active :class:`~app.jobs.JobStore` instance.
        docker_client: The active Docker SDK client.
    """
    registry.clear()
    registry.register(NvidiaEntrypointPlugin())
    registry.register(ShmIpcPolicyPlugin())
    registry.register(VolumePolicyPlugin())
    registry.register(ResourceSamplerPlugin())
    registry.register(LogRetentionPlugin())
    _load_env_plugins()
    registry.configure_services(job_store, docker_client)
