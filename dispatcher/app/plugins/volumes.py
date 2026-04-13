"""
dispatcher/app/plugins/volumes.py
───────────────────────────────────
VolumePolicyPlugin — validate and resolve bind-mount volume specs.

Background
----------
Allowing arbitrary bind-mounts from the host is a significant security
risk on a shared server.  The ``ALLOWED_HOST_DIRS`` environment variable
defines a comma-separated allow-list of host paths.  Any ``host_path``
that is not equal to, or a sub-path of, one of the allowed directories is
rejected with HTTP 400.

This plugin also resolves each ``host_path`` to its canonical form via
:func:`pathlib.Path.resolve` (``strict=False``), which follows symlinks and
normalises the path, preventing symlink-escape attacks and trailing-slash
mismatches.  The resolved bindings are injected into *create_kwargs* in-place.

Environment variables
---------------------
``ALLOWED_HOST_DIRS``
    Comma-separated list of host paths that callers may bind-mount.
    Empty (the default) means **no mounts are permitted**.

Community authors
-----------------
Fork this plugin to add per-user or per-team allow-lists, read-only
enforcement for sensitive directories, or path canonicalisation for
symlinked NAS mount points.
"""
from __future__ import annotations

import typing as t
from pathlib import Path

from fastapi import HTTPException

from app.core.plugin import CaasPlugin

if t.TYPE_CHECKING:
    from app.jobs import JobRecord


class VolumePolicyPlugin(CaasPlugin):
    """Validate bind-mount volume paths against the server allow-list.

    Reads ``ALLOWED_HOST_DIRS`` from ``app.main`` at call time so that
    tests can override the list without re-importing.

    Priority: 30
    """

    name = "volume-policy"
    priority = 30

    def pre_create(self, req: t.Any, create_kwargs: dict) -> None:  # noqa: D401
        """Validate ``req.volumes`` and inject resolved bindings into *create_kwargs*.

        If ``req.volumes`` is ``None`` or empty this is a no-op.

        Raises:
            HTTPException(400): when a host path is not in the allow-list.
        """
        volumes = getattr(req, "volumes", None)
        if not volumes:
            return

        # Import main at call-time so tests can monkey-patch module attrs.
        import app.main as _main  # pylint: disable=import-outside-toplevel

        # Pre-compute canonicalized allow-list roots once per request so we
        # don't call Path.resolve() O(n*m) times across volumes × allowed dirs.
        allowed_root_paths = [
            Path(p).resolve(strict=False)
            for p in _main.ALLOWED_HOST_DIRS
            if p
        ]

        bindings: dict[str, dict] = {}
        for v in volumes:
            # resolve() follows symlinks and normalises the path, preventing
            # symlink-escape attacks and trailing-slash mismatches.
            # strict=False means the path need not exist yet (e.g. a newly
            # created output dir that hasn't been written to yet).
            try:
                hp_path = Path(v.host_path).resolve(strict=False)
            except OSError as exc:
                raise HTTPException(
                    status_code=400,
                    detail=f"Cannot resolve host path {v.host_path!r}: {exc}",
                )
            hp = str(hp_path)
            # Use Path.is_relative_to() for containment so that edge cases like
            # root='/' work correctly (root + os.sep would give '//' which no
            # path starts with).
            allowed = any(
                hp_path == root or hp_path.is_relative_to(root)
                for root in allowed_root_paths
            )
            if not allowed:
                raise HTTPException(
                    status_code=400,
                    detail=f"Host path not allowed: {hp}",
                )
            if hp in bindings:
                raise HTTPException(
                    status_code=400,
                    detail=f"Multiple requested volumes resolve to the same host path: {hp}",
                )
            bindings[hp] = {"bind": v.container_path, "mode": v.mode}

        create_kwargs["volumes"] = bindings
