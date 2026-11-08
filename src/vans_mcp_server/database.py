"""Where vans-mcp-server keeps mcp_usage and mcp_oauth_connections.

`VANS_MCP_DATABASE_URL` points at the `vans_mcp_server` database, role
`vans_mcp_server_app` (router ADR 0071, 0073). The router's `DATABASE_URL` is not
read: the service no longer opens the router database (router ADR 0069).
"""

from __future__ import annotations

import logging
import os

SETTING = "VANS_MCP_DATABASE_URL"


def normalize_database_url(url: str) -> str:
    if url.startswith("postgres://"):
        return "postgresql://" + url[len("postgres://") :]
    return url


def database_url_from_env() -> str | None:
    """The service database URL, or None when unset (usage then goes to the log)."""
    url = (os.environ.get(SETTING) or "").strip()
    return normalize_database_url(url) if url else None


def warn_if_router_database_url(logger: logging.Logger) -> None:
    """The router's DATABASE_URL is a leftover from before #100; say so once at startup."""
    if (os.environ.get("DATABASE_URL") or "").strip():
        logger.warning(
            "DATABASE_URL is set but not read; this service uses %s. "
            "Remove the DATABASE_URL Fly secret (router ADR 0069).",
            SETTING,
        )
