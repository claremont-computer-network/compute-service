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

``post_run(record, result)``
    Called after the container exits and logs have been captured.  Mutate
    *result* (the HTTP response dict) in-place, or perform side-effects such
    as persisting logs.

``on_register(record)``
    Called immediately after a :class:`~app.jobs.JobRecord` has been added to
    the job store.  Useful for starting background threads (e.g. resource
    sampling) that need the container ID.
"""
from __future__ import annotations

import typing as t

if t.TYPE_CHECKING:
    from app.jobs import JobRecord


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

    def post_run(self, record: "JobRecord", result: dict) -> None:
        """React after the container exits and logs are captured.

        Args:
            record: The :class:`~app.jobs.JobRecord` for the finished job.
            result: The HTTP response body dict.  Mutate in-place to add or
                modify fields, e.g. to inject a ``"resource_history"`` summary.
        """

    def on_register(self, record: "JobRecord") -> None:
        """React immediately after a job is registered in the job store.

        Args:
            record: The freshly created :class:`~app.jobs.JobRecord`.
        """


class PluginRegistry:
    """Ordered registry of :class:`CaasPlugin` instances.

    Plugins are kept sorted by :attr:`~CaasPlugin.priority` (ascending) so
    each hook fires in a predictable order.
    """

    def __init__(self) -> None:
        self._plugins: list[CaasPlugin] = []

    def register(self, plugin: CaasPlugin) -> None:
        """Add *plugin* to the registry and re-sort by priority."""
        self._plugins.append(plugin)
        self._plugins.sort(key=lambda p: p.priority)

    def pre_create(self, req: t.Any, create_kwargs: dict) -> None:
        """Invoke :meth:`~CaasPlugin.pre_create` on every registered plugin."""
        for plugin in self._plugins:
            plugin.pre_create(req, create_kwargs)

    def post_run(self, record: "JobRecord", result: dict) -> None:
        """Invoke :meth:`~CaasPlugin.post_run` on every registered plugin."""
        for plugin in self._plugins:
            plugin.post_run(record, result)

    def on_register(self, record: "JobRecord") -> None:
        """Invoke :meth:`~CaasPlugin.on_register` on every registered plugin."""
        for plugin in self._plugins:
            plugin.on_register(record)

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
