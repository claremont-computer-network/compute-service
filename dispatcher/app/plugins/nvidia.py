"""
dispatcher/app/plugins/nvidia.py
─────────────────────────────────
NvidiaEntrypointPlugin — suppress the NGC container entrypoint banner.

Background
----------
NVIDIA NGC images (``nvcr.io/*``) ship with an ``ENTRYPOINT`` script that
prints a multi-page licensing/version banner before exec-ing the user
command.  When running a notebook cell as ``python -c <code>`` the banner
pollutes the captured stdout and makes parsing output unreliable.

This plugin overrides the entrypoint with an empty string when the caller
sets ``suppress_entrypoint=True`` on the request, which bypasses the NGC
script entirely and hands control directly to the ``CMD`` / ``command``.

Community authors
-----------------
This plugin is intentionally minimal — a worked example of a ``pre_create``
hook.  Fork it to handle other noisy base images (e.g. auto-detect the
registry prefix and suppress unconditionally).
"""
from __future__ import annotations

import typing as t

from app.core.plugin import CaasPlugin

if t.TYPE_CHECKING:
    from app.jobs import JobRecord


class NvidiaEntrypointPlugin(CaasPlugin):
    """Suppress the NGC entrypoint banner for NVIDIA container images.

    Activated when the request carries ``suppress_entrypoint=True``.
    Sets ``entrypoint=""`` in *create_kwargs* so Docker skips the image's
    ``ENTRYPOINT`` script and runs the command directly.

    Priority: 10
    """

    name = "nvidia-entrypoint"
    priority = 10

    def pre_create(self, req: t.Any, create_kwargs: dict) -> None:  # noqa: D401
        """Override the entrypoint if ``req.suppress_entrypoint`` is truthy."""
        suppress = getattr(req, "suppress_entrypoint", False)
        if suppress:
            create_kwargs.setdefault("entrypoint", "")
