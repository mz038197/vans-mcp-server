from __future__ import annotations

import logging
import os
from typing import Any

from fastmcp import FastMCP
from fastmcp.server.dependencies import get_access_token
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse
from starlette.routing import Mount, Route

from vans_mcp_server.auth import VcrApiKeyVerifier, api_key_http_middleware
from vans_mcp_server.oauth.google import GoogleOAuthService
from vans_mcp_server.oauth.store import OAuthConnectionStore
from vans_mcp_server.tools import calendar as calendar_tools
from vans_mcp_server.tools import gmail as gmail_tools
from vans_mcp_server.tools import knowledge
from vans_mcp_server.usage import UsageLogger, timed_tool

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("vans_mcp_server")

usage = UsageLogger.from_env()
auth = VcrApiKeyVerifier.from_env()
google_oauth = GoogleOAuthService.from_env()
oauth_store = OAuthConnectionStore.from_env(oauth=google_oauth)


def _public_url() -> str:
    return (os.environ.get("PUBLIC_URL") or "http://127.0.0.1:8080").rstrip("/")


# Do not pass auth= to FastMCP: its RequireAuthMiddleware advertises OAuth via
# WWW-Authenticate and VS Code enters Dynamic Client Registration.
mcp = FastMCP(
    "vans_mcp_server",
    instructions=(
        "Vans MCP Portal for Agent Dungeon. "
        "Knowledge (mock Notion), Planning (Google Calendar), and Communication "
        "(Gmail search/draft/send). Google tools require /connect/google authorization "
        "separate from portal login. gmail_send_email and gmail_trash_message "
        "require confirm=true."
    ),
)


def _claims() -> dict[str, Any]:
    token = get_access_token()
    if token is None:
        return {}
    return dict(token.claims or {})


def _user_id() -> int | None:
    claims = _claims()
    raw = claims.get("user_id")
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


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


def _require_user_id() -> int:
    user_id = _user_id()
    if user_id is None:
        raise RuntimeError("authenticated user_id missing")
    return user_id


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


@mcp.tool(
    name="google_get_connect_url",
    annotations={
        "title": "Get Google Portal connect URL",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
def google_get_connect_url() -> str:
    """Return the browser URL to connect Google Calendar and Gmail for this student.

    Separate from portal/dungeon Google login. Re-open after scope upgrades
    (e.g. adding Gmail after a Calendar-only connect).
    """
    timer = timed_tool()
    ok = False
    err: str | None = None
    out = ""
    try:
        with timer:
            user_id = _require_user_id()
            result = calendar_tools.connection_status(
                user_id=user_id,
                store=oauth_store,
                oauth=google_oauth,
                public_url=_public_url(),
            )
            out = calendar_tools.to_json(result)
        ok = True
        return out
    except Exception as exc:
        err = type(exc).__name__
        raise
    finally:
        _record("google_get_connect_url", ok, timer.latency_ms, err)


def _calendar_guard_or_payload(user_id: int) -> str | None:
    """Return JSON error string when Calendar cannot be used; else None."""
    oauth_ok = google_oauth is not None and google_oauth.is_configured()
    if oauth_store is None or not oauth_ok:
        return calendar_tools.to_json(
            calendar_tools.not_connected_payload(
                connect_url=calendar_tools.build_connect_url(
                    google_oauth, public_url=_public_url(), user_id=user_id
                ),
                oauth_configured=oauth_ok,
            )
        )
    if not oauth_store.is_connected(user_id):
        return calendar_tools.to_json(
            calendar_tools.not_connected_payload(
                connect_url=calendar_tools.build_connect_url(
                    google_oauth, public_url=_public_url(), user_id=user_id
                ),
                oauth_configured=True,
            )
        )
    return None


@mcp.tool(
    name="calendar_list_events",
    annotations={
        "title": "List Google Calendar events",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
def calendar_list_events(
    time_min: str,
    time_max: str,
    timezone_name: str = "Asia/Taipei",
    max_results: int = 20,
) -> str:
    """List events on the student's primary Google Calendar.

    Args:
        time_min: Range start (ISO-8601; naive times use timezone_name).
        time_max: Range end (ISO-8601).
        timezone_name: IANA timezone (default Asia/Taipei).
        max_results: Max events to return (1-50).
    """
    timer = timed_tool()
    ok = False
    err: str | None = None
    out = ""
    try:
        with timer:
            user_id = _require_user_id()
            blocked = _calendar_guard_or_payload(user_id)
            if blocked is not None:
                out = blocked
            else:
                assert oauth_store is not None and google_oauth is not None
                result = calendar_tools.list_events(
                    user_id=user_id,
                    store=oauth_store,
                    oauth=google_oauth,
                    time_min=time_min,
                    time_max=time_max,
                    timezone_name=timezone_name,
                    max_results=max_results,
                )
                out = calendar_tools.to_json(result)
        ok = True
        if '"error": "not_connected"' in out or '"error":"not_connected"' in out:
            err = "not_connected"
        return out
    except LookupError:
        err = "not_connected"
        user_id = _user_id() or 0
        out = calendar_tools.to_json(
            calendar_tools.not_connected_payload(
                connect_url=calendar_tools.build_connect_url(
                    google_oauth, public_url=_public_url(), user_id=user_id
                ),
                oauth_configured=google_oauth is not None,
            )
        )
        ok = True
        return out
    except Exception as exc:
        err = type(exc).__name__
        raise
    finally:
        _record("calendar_list_events", ok, timer.latency_ms, err)


@mcp.tool(
    name="calendar_find_free_time",
    annotations={
        "title": "Find free time on Google Calendar",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
def calendar_find_free_time(
    time_min: str,
    time_max: str,
    duration_minutes: int = 30,
    timezone_name: str = "Asia/Taipei",
) -> str:
    """Find free slots on the student's primary calendar via FreeBusy.

    Args:
        time_min: Range start (ISO-8601).
        time_max: Range end (ISO-8601).
        duration_minutes: Minimum slot length in minutes.
        timezone_name: IANA timezone (default Asia/Taipei).
    """
    timer = timed_tool()
    ok = False
    err: str | None = None
    out = ""
    try:
        with timer:
            user_id = _require_user_id()
            blocked = _calendar_guard_or_payload(user_id)
            if blocked is not None:
                out = blocked
            else:
                assert oauth_store is not None and google_oauth is not None
                result = calendar_tools.find_free_time(
                    user_id=user_id,
                    store=oauth_store,
                    oauth=google_oauth,
                    time_min=time_min,
                    time_max=time_max,
                    duration_minutes=duration_minutes,
                    timezone_name=timezone_name,
                )
                out = calendar_tools.to_json(result)
        ok = True
        return out
    except LookupError:
        err = "not_connected"
        user_id = _user_id() or 0
        out = calendar_tools.to_json(
            calendar_tools.not_connected_payload(
                connect_url=calendar_tools.build_connect_url(
                    google_oauth, public_url=_public_url(), user_id=user_id
                ),
                oauth_configured=google_oauth is not None,
            )
        )
        ok = True
        return out
    except Exception as exc:
        err = type(exc).__name__
        raise
    finally:
        _record("calendar_find_free_time", ok, timer.latency_ms, err)


@mcp.tool(
    name="calendar_create_event",
    annotations={
        "title": "Create Google Calendar event",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    },
)
def calendar_create_event(
    summary: str,
    start: str,
    end: str,
    description: str = "",
    timezone_name: str = "Asia/Taipei",
) -> str:
    """Create an event on the student's primary Google Calendar.

    Args:
        summary: Event title.
        start: Start datetime (ISO-8601).
        end: End datetime (ISO-8601).
        description: Optional description.
        timezone_name: IANA timezone (default Asia/Taipei).
    """
    timer = timed_tool()
    ok = False
    err: str | None = None
    out = ""
    try:
        with timer:
            user_id = _require_user_id()
            blocked = _calendar_guard_or_payload(user_id)
            if blocked is not None:
                out = blocked
            else:
                assert oauth_store is not None and google_oauth is not None
                result = calendar_tools.create_event(
                    user_id=user_id,
                    store=oauth_store,
                    oauth=google_oauth,
                    summary=summary,
                    start=start,
                    end=end,
                    description=description,
                    timezone_name=timezone_name,
                )
                out = calendar_tools.to_json(result)
        ok = True
        return out
    except LookupError:
        err = "not_connected"
        user_id = _user_id() or 0
        out = calendar_tools.to_json(
            calendar_tools.not_connected_payload(
                connect_url=calendar_tools.build_connect_url(
                    google_oauth, public_url=_public_url(), user_id=user_id
                ),
                oauth_configured=google_oauth is not None,
            )
        )
        ok = True
        return out
    except Exception as exc:
        err = type(exc).__name__
        raise
    finally:
        _record("calendar_create_event", ok, timer.latency_ms, err)


def _gmail_error_payload(user_id: int, exc: Exception) -> str:
    connect_url = calendar_tools.build_connect_url(
        google_oauth, public_url=_public_url(), user_id=user_id
    )
    if isinstance(exc, LookupError):
        return gmail_tools.to_json(
            calendar_tools.not_connected_payload(
                connect_url=connect_url,
                oauth_configured=google_oauth is not None,
            )
        )
    if isinstance(exc, PermissionError):
        granted = None
        if oauth_store is not None:
            granted = oauth_store.get_granted_scopes(user_id)
        return gmail_tools.to_json(
            gmail_tools.missing_scopes_payload(
                connect_url=connect_url, granted=granted
            )
        )
    raise exc


@mcp.tool(
    name="gmail_search_messages",
    annotations={
        "title": "Search Gmail messages",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
def gmail_search_messages(query: str, max_results: int = 10) -> str:
    """Search the student's Gmail inbox with a Gmail query string.

    Args:
        query: Gmail search query (e.g. from:alice newer_than:7d).
        max_results: Max messages to return (1-25).
    """
    timer = timed_tool()
    ok = False
    err: str | None = None
    out = ""
    try:
        with timer:
            user_id = _require_user_id()
            error_payload, conn = gmail_tools.ensure_gmail_ready(
                user_id=user_id,
                store=oauth_store,
                oauth=google_oauth,
                public_url=_public_url(),
            )
            if error_payload is not None:
                out = gmail_tools.to_json(error_payload)
                err = error_payload.get("error")
            else:
                assert oauth_store is not None and google_oauth is not None and conn
                result = gmail_tools.search_messages(
                    user_id=user_id,
                    store=oauth_store,
                    oauth=google_oauth,
                    query=query,
                    max_results=max_results,
                )
                out = gmail_tools.to_json(result)
        ok = True
        return out
    except (LookupError, PermissionError) as exc:
        user_id = _user_id() or 0
        out = _gmail_error_payload(user_id, exc)
        err = type(exc).__name__
        ok = True
        return out
    except Exception as exc:
        err = type(exc).__name__
        raise
    finally:
        _record("gmail_search_messages", ok, timer.latency_ms, err)


@mcp.tool(
    name="gmail_summarize_thread",
    annotations={
        "title": "Summarize Gmail thread (structured digest)",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
def gmail_summarize_thread(thread_id: str, max_messages: int = 10) -> str:
    """Return a structured digest of a Gmail thread (no server-side LLM).

    Args:
        thread_id: Gmail thread id (from search results).
        max_messages: Max messages to include (1-20).
    """
    timer = timed_tool()
    ok = False
    err: str | None = None
    out = ""
    try:
        with timer:
            user_id = _require_user_id()
            error_payload, conn = gmail_tools.ensure_gmail_ready(
                user_id=user_id,
                store=oauth_store,
                oauth=google_oauth,
                public_url=_public_url(),
            )
            if error_payload is not None:
                out = gmail_tools.to_json(error_payload)
                err = error_payload.get("error")
            else:
                assert oauth_store is not None and google_oauth is not None and conn
                result = gmail_tools.summarize_thread(
                    user_id=user_id,
                    store=oauth_store,
                    oauth=google_oauth,
                    thread_id=thread_id,
                    max_messages=max_messages,
                )
                out = gmail_tools.to_json(result)
        ok = True
        return out
    except (LookupError, PermissionError) as exc:
        user_id = _user_id() or 0
        out = _gmail_error_payload(user_id, exc)
        err = type(exc).__name__
        ok = True
        return out
    except Exception as exc:
        err = type(exc).__name__
        raise
    finally:
        _record("gmail_summarize_thread", ok, timer.latency_ms, err)


@mcp.tool(
    name="gmail_create_draft",
    annotations={
        "title": "Create Gmail draft",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    },
)
def gmail_create_draft(to: str, subject: str, body: str = "") -> str:
    """Create a Gmail draft (does not send).

    Args:
        to: Recipient email address.
        subject: Email subject.
        body: Plain-text body.
    """
    timer = timed_tool()
    ok = False
    err: str | None = None
    out = ""
    try:
        with timer:
            user_id = _require_user_id()
            error_payload, conn = gmail_tools.ensure_gmail_ready(
                user_id=user_id,
                store=oauth_store,
                oauth=google_oauth,
                public_url=_public_url(),
            )
            if error_payload is not None:
                out = gmail_tools.to_json(error_payload)
                err = error_payload.get("error")
            else:
                assert oauth_store is not None and google_oauth is not None and conn
                result = gmail_tools.create_draft(
                    user_id=user_id,
                    store=oauth_store,
                    oauth=google_oauth,
                    to=to,
                    subject=subject,
                    body=body,
                )
                out = gmail_tools.to_json(result)
        ok = True
        return out
    except (LookupError, PermissionError) as exc:
        user_id = _user_id() or 0
        out = _gmail_error_payload(user_id, exc)
        err = type(exc).__name__
        ok = True
        return out
    except Exception as exc:
        err = type(exc).__name__
        raise
    finally:
        _record("gmail_create_draft", ok, timer.latency_ms, err)


@mcp.tool(
    name="gmail_send_email",
    annotations={
        "title": "Send Gmail message",
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": False,
        "openWorldHint": True,
    },
)
def gmail_send_email(
    to: str,
    subject: str,
    body: str = "",
    confirm: bool = False,
) -> str:
    """Send an email from the student's Gmail account.

    Requires confirm=true after human approval. Without confirm, returns
    confirmation_required and does not send.

    Args:
        to: Recipient email address.
        subject: Email subject.
        body: Plain-text body.
        confirm: Must be true to actually send.
    """
    timer = timed_tool()
    ok = False
    err: str | None = None
    out = ""
    try:
        with timer:
            user_id = _require_user_id()
            if not confirm:
                out = gmail_tools.to_json(
                    gmail_tools.confirmation_required_payload(to=to, subject=subject)
                )
                err = "confirmation_required"
            else:
                error_payload, conn = gmail_tools.ensure_gmail_ready(
                    user_id=user_id,
                    store=oauth_store,
                    oauth=google_oauth,
                    public_url=_public_url(),
                )
                if error_payload is not None:
                    out = gmail_tools.to_json(error_payload)
                    err = error_payload.get("error")
                else:
                    assert oauth_store is not None and google_oauth is not None and conn
                    result = gmail_tools.send_email(
                        user_id=user_id,
                        store=oauth_store,
                        oauth=google_oauth,
                        to=to,
                        subject=subject,
                        body=body,
                        confirm=True,
                    )
                    out = gmail_tools.to_json(result)
        ok = True
        return out
    except (LookupError, PermissionError) as exc:
        user_id = _user_id() or 0
        out = _gmail_error_payload(user_id, exc)
        err = type(exc).__name__
        ok = True
        return out
    except Exception as exc:
        err = type(exc).__name__
        raise
    finally:
        _record("gmail_send_email", ok, timer.latency_ms, err)


@mcp.tool(
    name="gmail_trash_message",
    annotations={
        "title": "Move Gmail message to Trash",
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": False,
        "openWorldHint": True,
    },
)
def gmail_trash_message(message_id: str, confirm: bool = False) -> str:
    """Move a Gmail message to Trash (not permanent delete).

    Typical flow: gmail_search_messages to find ids, then trash with confirm=true
    after human approval.

    Args:
        message_id: Gmail message id from search results.
        confirm: Must be true to actually move to Trash.
    """
    timer = timed_tool()
    ok = False
    err: str | None = None
    out = ""
    try:
        with timer:
            user_id = _require_user_id()
            if not confirm:
                out = gmail_tools.to_json(
                    gmail_tools.confirmation_required_payload(
                        message_id=message_id, action="trash"
                    )
                )
                err = "confirmation_required"
            else:
                error_payload, conn = gmail_tools.ensure_gmail_ready(
                    user_id=user_id,
                    store=oauth_store,
                    oauth=google_oauth,
                    public_url=_public_url(),
                    required_scopes=gmail_tools.GMAIL_TRASH_SCOPES,
                )
                if error_payload is not None:
                    out = gmail_tools.to_json(error_payload)
                    err = error_payload.get("error")
                else:
                    assert oauth_store is not None and google_oauth is not None and conn
                    result = gmail_tools.trash_message(
                        user_id=user_id,
                        store=oauth_store,
                        oauth=google_oauth,
                        message_id=message_id,
                        confirm=True,
                    )
                    out = gmail_tools.to_json(result)
        ok = True
        return out
    except (LookupError, PermissionError) as exc:
        user_id = _user_id() or 0
        out = _gmail_error_payload(user_id, exc)
        err = type(exc).__name__
        ok = True
        return out
    except Exception as exc:
        err = type(exc).__name__
        raise
    finally:
        _record("gmail_trash_message", ok, timer.latency_ms, err)


async def health(_request: Request) -> JSONResponse:
    mode = "neon" if os.environ.get("DATABASE_URL") else "bypass_or_unconfigured"
    return JSONResponse(
        {
            "ok": True,
            "service": "vans-mcp-server",
            "auth": mode,
            "public_url": os.environ.get("PUBLIC_URL", ""),
            "google_oauth_configured": bool(
                google_oauth is not None and google_oauth.is_configured()
            ),
            "oauth_store_configured": oauth_store is not None,
        }
    )


async def connect_google_start(request: Request) -> RedirectResponse | HTMLResponse:
    if google_oauth is None or not google_oauth.is_configured():
        return HTMLResponse(
            "<h1>Google OAuth not configured</h1>"
            "<p>Set GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, and SESSION_SECRET.</p>",
            status_code=503,
        )
    state = request.query_params.get("state") or ""
    user_id = google_oauth.verify_connect_state(state)
    if user_id is None:
        return HTMLResponse(
            "<h1>Invalid or expired connect link</h1>"
            "<p>Ask your agent for a new google_get_connect_url.</p>",
            status_code=400,
        )
    # Re-sign so callback state is fresh and still bound to the same user.
    fresh_state = google_oauth.create_connect_state(user_id)
    return RedirectResponse(google_oauth.authorize_url(fresh_state), status_code=302)


async def connect_google_callback(request: Request) -> HTMLResponse:
    if google_oauth is None or not google_oauth.is_configured():
        return HTMLResponse(
            "<h1>Google OAuth not configured</h1>",
            status_code=503,
        )
    if oauth_store is None:
        return HTMLResponse(
            "<h1>OAuth store not configured</h1>"
            "<p>DATABASE_URL and OAUTH_TOKEN_ENCRYPTION_KEY are required.</p>",
            status_code=503,
        )

    error = request.query_params.get("error")
    if error:
        return HTMLResponse(
            f"<h1>Google authorization failed</h1><p>{error}</p>",
            status_code=400,
        )

    state = request.query_params.get("state") or ""
    code = request.query_params.get("code") or ""
    user_id = google_oauth.verify_connect_state(state)
    if user_id is None:
        return HTMLResponse(
            "<h1>Invalid or expired state</h1>"
            "<p>Please request a new connect URL from your agent.</p>",
            status_code=400,
        )
    if not code:
        return HTMLResponse("<h1>Missing authorization code</h1>", status_code=400)

    try:
        bundle = google_oauth.exchange_code(code)
        if not bundle.refresh_token:
            # May happen if user previously authorized without offline; force re-consent.
            return HTMLResponse(
                "<h1>No refresh token returned</h1>"
                "<p>Revoke app access at "
                "<a href='https://myaccount.google.com/permissions'>Google Account permissions</a> "
                "and connect again.</p>",
                status_code=400,
            )
        oauth_store.upsert_google_tokens(user_id=user_id, bundle=bundle)
    except Exception:
        logger.exception("google connect callback failed user_id=%s", user_id)
        return HTMLResponse(
            "<h1>Failed to save Google connection</h1>"
            "<p>Check server logs and try again.</p>",
            status_code=500,
        )

    return HTMLResponse(
        """
        <html><body style="font-family: system-ui; max-width: 40rem; margin: 2rem auto;">
        <h1>Google Portal connected</h1>
        <p>You can close this tab and return to your agent.
        Calendar and Gmail tools can now use your Google account.</p>
        </body></html>
        """,
        status_code=200,
    )


# Mount prefix /mcp is the public path; http_app path="/" avoids double-prefixing.
mcp_app = mcp.http_app(path="/")
for mw in reversed(api_key_http_middleware(auth)):
    # Starlette add_middleware: last added runs first on the request.
    mcp_app.add_middleware(mw.cls, *mw.args, **mw.kwargs)

app = Starlette(
    routes=[
        Route("/health", health, methods=["GET"]),
        Route("/connect/google/start", connect_google_start, methods=["GET"]),
        Route("/connect/google/callback", connect_google_callback, methods=["GET"]),
        Mount("/mcp", app=mcp_app),
    ],
    lifespan=mcp_app.lifespan,
)
