"""
dispatcher/app/plugins/shm_ipc.py
───────────────────────────────────
ShmIpcPolicyPlugin — enforce server-side shm_size and ipc_mode policy.

Background
----------
PyTorch multi-GPU jobs and DataLoader workers communicate through shared
memory.  The Docker default (64 MiB) is far too small for real workloads.
Callers can request a larger ``shm_size`` or ``ipc_mode="host"`` (which
gives the container unlimited shared memory), but both carry security
implications on a shared host.

This plugin enforces two environment-variable-controlled limits:

``ALLOW_IPC_HOST`` (default ``false``)
    Set to ``true`` to permit ``ipc_mode="host"``.  Only ``"host"`` is
    accepted; any other value is rejected with HTTP 400.

``MAX_SHM_SIZE_MB`` (default ``8192``)
    Maximum shared-memory segment in MiB.  Requests above this are rejected
    with HTTP 400.

Community authors
-----------------
This is a worked example of a *validation* ``pre_create`` hook — one that
raises ``HTTPException`` to reject the request rather than mutating kwargs.
Fork it to add per-team quota logic, or to allow different limits based on
the image or environment.
"""
from __future__ import annotations

import typing as t

from fastapi import HTTPException

from app.core.plugin import CaasPlugin

if t.TYPE_CHECKING:
    from app.jobs import JobRecord

_SHM_SUFFIXES: dict[str, int] = {"b": 1, "k": 1024, "m": 1024**2, "g": 1024**3}


class ShmIpcPolicyPlugin(CaasPlugin):
    """Enforce ``shm_size`` and ``ipc_mode`` server-side policy.

    Reads ``ALLOW_IPC_HOST`` and ``MAX_SHM_SIZE_MB`` from ``app.main`` at
    call time so that tests can override the values without re-importing.

    Priority: 20
    """

    name = "shm-ipc-policy"
    priority = 20

    def pre_create(self, req: t.Any, create_kwargs: dict) -> None:  # noqa: D401
        """Validate ``req.ipc_mode`` and ``req.shm_size`` against server policy.

        Raises:
            HTTPException(400): on policy violation.
        """
        # Import main at call-time so tests can monkey-patch the module attrs.
        import app.main as _main  # pylint: disable=import-outside-toplevel

        ipc_mode = getattr(req, "ipc_mode", None)
        shm_size = getattr(req, "shm_size", None)

        if ipc_mode is not None:
            if not _main.ALLOW_IPC_HOST:
                raise HTTPException(
                    status_code=400,
                    detail="ipc_mode requires ALLOW_IPC_HOST=true on the dispatcher",
                )
            if ipc_mode != "host":
                raise HTTPException(
                    status_code=400,
                    detail=f"Unsupported ipc_mode: {ipc_mode!r}. Only 'host' is permitted.",
                )

        if shm_size is not None:
            raw = shm_size.strip().lower()
            if not raw:
                raise HTTPException(
                    status_code=400,
                    detail=f"Cannot parse shm_size: {shm_size!r}. Expected a value like '512m' or '2g'.",
                )
            suffix = raw[-1] if raw[-1] in _SHM_SUFFIXES else "b"
            number_part = raw[:-1] if raw[-1] in _SHM_SUFFIXES else raw
            try:
                size_value = float(number_part)
            except ValueError:
                raise HTTPException(
                    status_code=400,
                    detail=f"Cannot parse shm_size: {shm_size!r}. Expected a value like '512m' or '2g'.",
                )
            if size_value <= 0:
                raise HTTPException(
                    status_code=400,
                    detail=f"shm_size must be a positive value, got {shm_size!r}.",
                )
            size_bytes = size_value * _SHM_SUFFIXES[suffix]
            size_mb = size_bytes / (1024**2)
            if size_mb > _main.MAX_SHM_SIZE_MB:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"shm_size {shm_size!r} exceeds the server limit of "
                        f"{_main.MAX_SHM_SIZE_MB} MiB. "
                        f"Reduce the value or ask the administrator to raise MAX_SHM_SIZE_MB."
                    ),
                )
