"""Google OAuth connect (Calendar + Gmail) — separate from portal login."""

from vans_mcp_server.oauth.google import (
    CALENDAR_SCOPES,
    GMAIL_COMPOSE_SCOPE,
    GMAIL_READONLY_SCOPE,
    GOOGLE_PORTAL_SCOPES,
    GoogleOAuthService,
    GoogleTokenBundle,
    scopes_include,
)
from vans_mcp_server.oauth.store import OAuthConnectionStore

__all__ = [
    "CALENDAR_SCOPES",
    "GMAIL_COMPOSE_SCOPE",
    "GMAIL_READONLY_SCOPE",
    "GOOGLE_PORTAL_SCOPES",
    "GoogleOAuthService",
    "GoogleTokenBundle",
    "OAuthConnectionStore",
    "scopes_include",
]
