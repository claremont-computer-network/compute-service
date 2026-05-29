"""
caas_mcp.config
───────────────

Centralised configuration for the MCP server.  All values come from
environment variables — no runtime prompts, no config files.

Required environment variables
──────────────────────────────
CAAS_DISPATCHER_URL — base URL of the dispatcher (e.g. *http://192.168.1.50:8000*).

Optional environment variables
──────────────────────────────
CAAS_API_KEY          — X-API-Key header value forwarded to the dispatcher.
CAAS_REMOTE_WORKSPACE — absolute path **on the remote dispatcher's filesystem** to
                        use as the agent workspace for implicit volume mounts.
"""
from __future__ import annotations

import os


class ConfigError(RuntimeError):
    """Raised when a required configuration value is missing."""


def _get_env(key: str, required: bool = True) -> str | None:
    val = os.environ.get(key)
    if required and not val:
        raise ConfigError(
            f"Required environment variable {key!r} is not set. "
            f"Ensure the MCP host sets {key} before spawning the stdio process."
        )
    return val


class Config:
    """Immutable configuration read from environment variables at startup."""

    def __init__(
        self,
        dispatcher_url: str | None = None,
        api_key: str | None = None,
        remote_workspace: str | None = None,
    ) -> None:
        self.dispatcher_url = dispatcher_url or _get_env("CAAS_DISPATCHER_URL")
        self.api_key = api_key or _get_env("CAAS_API_KEY", required=False)
        self.remote_workspace = remote_workspace or _get_env(
            "CAAS_REMOTE_WORKSPACE", required=False
        )

    def __repr__(self) -> str:
        api = f"'{'*' * 8}'" if self.api_key else "None"
        ws = repr(self.remote_workspace)
        return f"Config(dispatcher_url={self.dispatcher_url!r}, api_key={api}, remote_workspace={ws})"
