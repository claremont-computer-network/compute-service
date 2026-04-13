"""
dispatcher/app/plugins/__init__.py
────────────────────────────────────
Built-in plugin registration.

``register_default_plugins`` is called at ``app.main`` module import time
(after ``client`` and ``job_store`` are created) so that plugins are active
even when the FastAPI lifespan context manager is not entered — which is the
case during tests that use a bare :class:`starlette.testclient.TestClient`
without a ``with`` block.  The lifespan also logs the active plugin list.

Community plugin authors
------------------------
To add a plugin without modifying this file, call
``registry.register(YourPlugin())`` from your own startup code after
``app.main`` has been imported::

    from app.core.plugin import registry
    from my_package.plugins import InstalledPackageReporterPlugin

    registry.register(InstalledPackageReporterPlugin())
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
    registry.clear()
    registry.register(NvidiaEntrypointPlugin())
    registry.register(ShmIpcPolicyPlugin())
    registry.register(VolumePolicyPlugin())
    registry.register(ResourceSamplerPlugin())
    registry.register(LogRetentionPlugin())
