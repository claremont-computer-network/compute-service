"""
dispatcher/app/plugins/__init__.py
────────────────────────────────────
Built-in plugin registration.

``register_default_plugins`` is called once during the FastAPI lifespan
startup to wire all built-in plugins into the global registry.  The order
of ``registry.register()`` calls is irrelevant — plugins are sorted by
``priority`` inside :class:`~app.core.plugin.PluginRegistry`.

Community plugin authors
------------------------
To add a plugin without modifying this file, call
``registry.register(YourPlugin())`` from your own startup code (e.g. a
FastAPI lifespan middleware or an environment-variable-controlled loader)::

    from app.core.plugin import registry
    from my_package.plugins import InstalledPackageReporterPlugin

    registry.register(InstalledPackageReporterPlugin(job_store, docker_client))
"""
from app.core.plugin import registry
from app.plugins.nvidia import NvidiaEntrypointPlugin
from app.plugins.shm_ipc import ShmIpcPolicyPlugin
from app.plugins.volumes import VolumePolicyPlugin
from app.plugins.resource_sampler import ResourceSamplerPlugin
from app.plugins.log_retention import LogRetentionPlugin


def register_default_plugins(job_store, docker_client) -> None:
    """Register all built-in :class:`~app.core.plugin.CaasPlugin` instances.

    Clears any previously registered plugins first so that calling this
    function more than once (e.g. during test reloads) does not accumulate
    duplicate entries.

    Args:
        job_store: The active :class:`~app.jobs.JobStore` instance (unused
            directly — plugins read ``app.main.job_store`` at call-time).
        docker_client: The active Docker SDK client (unused directly —
            plugins read ``app.main.client`` at call-time).
    """
    registry._plugins.clear()
    registry.register(NvidiaEntrypointPlugin())
    registry.register(ShmIpcPolicyPlugin())
    registry.register(VolumePolicyPlugin())
    registry.register(ResourceSamplerPlugin())
    registry.register(LogRetentionPlugin())
