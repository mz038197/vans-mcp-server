from __future__ import annotations

import logging
import os
from typing import Any

from fastmcp import FastMCP
from fastmcp.server.dependencies import get_access_token
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route

from vans_mcp_server.auth import VcrApiKeyVerifier
from vans_mcp_server.tools import knowledge
from vans_mcp_server.usage import UsageLogger, timed_tool

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("vans_mcp_server")

usage = UsageLogger.from_env()
auth = VcrApiKeyVerifier.from_env()

mcp = FastMCP(
    "vans_mcp_server",
    auth=auth,
    instructions=(
        "Vans MCP Portal for Agent Dungeon. "
        "Milestone 1 exposes a course mock Knowledge Portal (Notion-style observe tools)."
    ),
)


def _claims() -> dict[str, Any]:
    token = get_access_token()
    if token is None:
        return {}
    return dict(token.claims or {})


def _record(tool_name: str, success: bool, latency_ms: int, error_type: str | None = None) -> None:
    claims = _claims()
    usage.record(
        tool_name=tool_name,
        success=success,
        user_id=claims.get("user_id"),
        api_key_id=claims.get("api_key_id"),
        latency_ms=latency_ms,
        error_type=error_type,
    )


@mcp.tool(
    name="notion_search_pages",
    annotations={
        "title": "Search Notion pages (course mock)",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
def notion_search_pages(query: str, limit: int = 5) -> str:
    """Search course Knowledge Portal pages (mock Notion observe).

    Args:
        query: Free-text search against title, summary, and tags.
        limit: Max pages to return (1-20).
    """
    timer = timed_tool()
    ok = False
    err: str | None = None
    out = ""
    try:
        with timer:
            result = knowledge.search_pages(query, limit=limit)
            out = knowledge.to_json(result)
        ok = True
        return out
    except Exception as exc:
        err = type(exc).__name__
        raise
    finally:
        _record("notion_search_pages", ok, timer.latency_ms, err)


@mcp.tool(
    name="notion_read_page",
    annotations={
        "title": "Read Notion page (course mock)",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
def notion_read_page(page_id: str) -> str:
    """Read one course Knowledge Portal page by id (mock Notion observe).

    Args:
        page_id: Page id from notion_search_pages (e.g. page_hualien_guide).
    """
    timer = timed_tool()
    ok = False
    err: str | None = None
    out = ""
    try:
        with timer:
            result = knowledge.read_page(page_id)
            out = knowledge.to_json(result)
        ok = True
        return out
    except Exception as exc:
        err = type(exc).__name__
        raise
    finally:
        _record("notion_read_page", ok, timer.latency_ms, err)


async def health(_request: Request) -> JSONResponse:
    mode = "neon" if os.environ.get("DATABASE_URL") else "bypass_or_unconfigured"
    return JSONResponse(
        {
            "ok": True,
            "service": "vans-mcp-server",
            "auth": mode,
            "public_url": os.environ.get("PUBLIC_URL", ""),
        }
    )


# Mount prefix /mcp is the public path; http_app path="/" avoids double-prefixing.
mcp_app = mcp.http_app(path="/")

app = Starlette(
    routes=[
        Route("/health", health, methods=["GET"]),
        Mount("/mcp", app=mcp_app),
    ],
    lifespan=mcp_app.lifespan,
)
