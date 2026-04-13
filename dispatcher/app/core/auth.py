"""
dispatcher/app/core/auth.py
────────────────────────────
API key authentication extracted from main.py.
"""
from __future__ import annotations

import os
import typing as t

from fastapi import HTTPException, Header

#: Set ``DISPATCHER_API_KEY`` in the environment to require callers to
#: supply a matching ``X-Api-Key`` header.  Leave unset in development to
#: skip authentication (a warning is logged by ``main.py``'s lifespan handler).
API_KEY: t.Optional[str] = os.getenv("DISPATCHER_API_KEY")


def get_api_key(x_api_key: t.Optional[str] = Header(None)) -> bool:
    """FastAPI dependency that validates the ``X-Api-Key`` request header.

    Returns ``True`` when the key matches (or when no key is configured).
    Raises ``HTTPException(401)`` on mismatch.
    """
    if not API_KEY:
        # Dev mode: no key configured — allow all requests but warn in logs.
        return True
    if not x_api_key or x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API Key")
    return True
