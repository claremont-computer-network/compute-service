"""
dispatcher/app/core/plugin.py
─────────────────────────────
CaasPlugin base class, PluginRegistry, and the global ``registry`` singleton.

Community authors
-----------------
Subclass :class:`CaasPlugin`, implement whichever hooks you need, set a
unique :attr:`name` and a :attr:`priority`, then call
``registry.register(MyPlugin())``.

Hook execution order
--------------------
Plugins are sorted by ``priority`` (ascending) at registration time.
Lower numbers run first.  Built-in plugins occupy 0–99; leave 100+ for
third-party / community plugins.

Extension hooks
---------------
``pre_create(req, create_kwargs)``
    Called after ``_prepare_run()`` assembles *create_kwargs* but **before**
    ``containers.create()`` (cell endpoint) or ``containers.run()`` (execute
    endpoint).  Mutate *create_kwargs* in-place to add, remove, or modify
    Docker API kwargs.

``on_register(record)``
    Called immediately after a :class:`~app.jobs.JobRecord` has been added to
    the job store.  Useful for starting background threads (e.g. resource
    sampling) that need the container ID.

``post_run(record, result)``
    Called after the container exits and logs have been captured (cell jobs
    only).  Mutate *result* (the HTTP response dict) in-place, or perform
    side-effects such as persisting logs.

``on_job_complete(record, exit_code)``
    Called when **any** job reaches a terminal state — both cell jobs
    (``execute_cell``) and detached jobs (``/v1/execute``).  Fired from
    ``_enrich_job_data`` when Docker reports a terminal container state, and
    from ``stop_job`` when a job is cancelled.  Use this hook for any
    behaviour that must run regardless of how the job was submitted.

``on_enrich(record, data)``
    Called at the end of ``_enrich_job_data`` so plugins can inject custom
    fields into the job-detail and job-list HTTP responses (e.g. training
    metrics, GPU utilisation summaries, custom status labels).

Plugin services
---------------
``CaasPlugin.services`` is set by :meth:`PluginRegistry.configure_services`
before any hooks fire.  It is a :class:`PluginServices` instance that exposes
shared dispatcher objects (job store, Docker client) without requiring plugins
to import ``app.main`` directly.
"""
from __future__ import annotations

import logging
import typing as t

logger = logging.getLogger("caas.dispatcher")

if t.TYPE_CHECKING:
    from app.jobs import JobRecord, JobStore


class PluginServices:
    """Shared dispatcher objects exposed to plugins.

    Assigned to each plugin's ``services`` attribute by
    :meth:`PluginRegistry.configure_services` before any hooks are invoked.
    Plugins should access the job store and Docker client through this
    object rather than importing ``app.main`` directly, which makes them
    independently testable.

    Attributes:
        job_store: The active :class:`~app.jobs.JobStore` instance.
        docker_client: The active Docker SDK client.
    """

    def __init__(self, job_store: "JobStore", docker_client: t.Any) -> None:
        self.job_store = job_store
        self.docker_client = docker_client


class CaasPlugin:
    """Abstract base for all compute-service dispatcher plugins.

    Subclass this, override the hooks you need, and register an instance
    via :data:`registry`.
    """

    #: Human-readable identifier shown in ``GET /health`` and log lines.
    name: str = "unnamed"

    #: Execution priority (ascending — lower runs first).
    #: Built-in plugins use 0–99; community plugins should use 100+.
    priority: int = 0

    #: Injected by PluginRegistry.configure_services() before any hook fires.
    #: Provides access to job_store and docker_client without importing app.main.
    services: t.Optional["PluginServices"] = None

    # ------------------------------------------------------------------
    # Hooks — all default to no-ops so subclasses only override what they need
    # ------------------------------------------------------------------

    def pre_create(self, req: t.Any, create_kwargs: dict) -> None:  # noqa: D401
        """Mutate *create_kwargs* before ``containers.create()`` / ``containers.run()``.

        Args:
            req: The original :class:`~app.main.CellRequest` or
                :class:`~app.main.ExecuteRequest` object.
            create_kwargs: The dict of Docker API kwargs assembled by
                ``_prepare_run()``.  Mutate it in-place.
        """

    def on_register(self, record: "JobRecord") -> None:
        """React immediately after a job is registered in the job store.

        Args:
            record: The freshly created :class:`~app.jobs.JobRecord`.
        """

    def post_run(self, record: "JobRecord", result: dict) -> None:
        """React after a cell job's container exits and logs are captured.

        This hook fires only for ``execute_cell`` (synchronous cell) jobs.
        For behaviour that must run for **all** job types use
        :meth:`on_job_complete` instead.

        Args:
            record: The :class:`~app.jobs.JobRecord` for the finished job.
            result: The HTTP response body dict.  Mutate in-place to add or
                modify fields, e.g. to inject a ``"resource_history"`` summary.
        """

    def on_job_complete(self, record: "JobRecord", exit_code: t.Optional[int]) -> None:
        """React when any job reaches a terminal state.

        Fired for both cell jobs and detached ``/v1/execute`` jobs — whenever
        Docker reports a terminal container state or ``DELETE /v1/jobs/{id}``
        is called.  Use this hook for side-effects that should happen
        regardless of how the job was submitted (e.g. persisting results,
        sending pipeline callbacks, archiving logs).

        Args:
            record: The :class:`~app.jobs.JobRecord` at the moment the job
                was detected as complete.  ``record.exit_code`` may be ``None``
                if the exit code is not yet available.
            exit_code: The container exit code, or ``None`` if unavailable
                (e.g. the container was removed before the code could be read).
        """

    def on_enrich(self, record: "JobRecord", data: dict) -> None:
        """Inject custom fields into a job-detail or job-list HTTP response.

        Called at the end of ``_enrich_job_data`` after live Docker
        reconciliation has updated ``data``.  Mutate *data* in-place to add
        extra fields (e.g. training metrics, GPU utilisation summaries, custom
        status labels).

        Args:
            record: A snapshot of the :class:`~app.jobs.JobRecord`.
            data: The ``model_dump()`` dict that will be serialised to JSON.
                Mutate in-place.
        """


class PluginRegistry:
    """Ordered registry of :class:`CaasPlugin` instances.

    Plugins are kept sorted by :attr:`~CaasPlugin.priority` (ascending) so
    each hook fires in a predictable order.
    """

    def __init__(self) -> None:
        self._plugins: list[CaasPlugin] = []
        self._services: t.Optional[PluginServices] = None

    def configure_services(self, job_store: "JobStore", docker_client: t.Any) -> None:
        """Create a :class:`PluginServices` instance and inject it into all
        currently registered plugins, and into any plugins registered later.

        Call this once after the job store and Docker client are created.
        """
        self._services = PluginServices(job_store=job_store, docker_client=docker_client)
        for plugin in self._plugins:
            plugin.services = self._services

    def register(self, plugin: CaasPlugin) -> None:
        """Add *plugin* to the registry and re-sort by priority.

        If :meth:`configure_services` has already been called, the new plugin
        receives the services instance immediately.

        Raises:
            ValueError: If a plugin with the same :attr:`~CaasPlugin.name`
                is already registered.  Names must be unique so that
                ``/health`` output is unambiguous and double-execution of
                hooks is impossible.
        """
        if any(existing.name == plugin.name for existing in self._plugins):
            raise ValueError(
                f"A plugin named {plugin.name!r} is already registered. "
                "Use registry.clear() before re-registering or choose a unique name."
            )
        if self._services is not None:
            plugin.services = self._services
        self._plugins.append(plugin)
        self._plugins.sort(key=lambda p: p.priority)

    def clear(self) -> None:
        """Remove all registered plugins (services configuration is preserved).

        Call this before ``register_default_plugins()`` to ensure a clean
        slate (e.g. during test reloads or service restarts).
        """
        self._plugins.clear()

    def pre_create(self, req: t.Any, create_kwargs: dict) -> None:
        """Invoke :meth:`~CaasPlugin.pre_create` on every registered plugin.

        Exceptions propagate — this hook is where validation plugins raise
        ``HTTPException`` to reject invalid requests, so errors must not be
        swallowed.
        """
        for plugin in self._plugins:
            plugin.pre_create(req, create_kwargs)

    def on_register(self, record: "JobRecord") -> None:
        """Invoke :meth:`~CaasPlugin.on_register` on every registered plugin.

        Exceptions from individual plugins are caught, logged, and skipped so
        that a buggy third-party plugin cannot prevent job registration from
        completing.
        """
        for plugin in self._plugins:
            try:
                plugin.on_register(record)
            except Exception:  # noqa: BLE001
                logger.exception(
                    "Plugin %r raised an unhandled exception in on_register(); skipping.",
                    plugin.name,
                )

    def post_run(self, record: "JobRecord", result: dict) -> None:
        """Invoke :meth:`~CaasPlugin.post_run` on every registered plugin.

        Exceptions from individual plugins are caught, logged, and skipped so
        that a buggy third-party plugin cannot turn a successful run into a 500.
        """
        for plugin in self._plugins:
            try:
                plugin.post_run(record, result)
            except Exception:  # noqa: BLE001
                logger.exception(
                    "Plugin %r raised an unhandled exception in post_run(); skipping.",
                    plugin.name,
                )

    def on_job_complete(self, record: "JobRecord", exit_code: t.Optional[int]) -> None:
        """Invoke :meth:`~CaasPlugin.on_job_complete` on every registered plugin.

        Exceptions from individual plugins are caught, logged, and skipped.
        """
        for plugin in self._plugins:
            try:
                plugin.on_job_complete(record, exit_code)
            except Exception:  # noqa: BLE001
                logger.exception(
                    "Plugin %r raised an unhandled exception in on_job_complete(); skipping.",
                    plugin.name,
                )

    def on_enrich(self, record: "JobRecord", data: dict) -> None:
        """Invoke :meth:`~CaasPlugin.on_enrich` on every registered plugin.

        Exceptions from individual plugins are caught, logged, and skipped so
        that a buggy plugin cannot break job-list or job-detail responses.
        """
        for plugin in self._plugins:
            try:
                plugin.on_enrich(record, data)
            except Exception:  # noqa: BLE001
                logger.exception(
                    "Plugin %r raised an unhandled exception in on_enrich(); skipping.",
                    plugin.name,
                )

    def names(self) -> list[str]:
        """Return plugin names in priority order (useful for ``/health``)."""
        return [p.name for p in self._plugins]

    def __len__(self) -> int:
        return len(self._plugins)

    def __repr__(self) -> str:
        return f"PluginRegistry({self.names()!r})"


#: Global plugin registry.  Import this singleton wherever you need to
#: register or invoke plugins.
registry = PluginRegistry()
