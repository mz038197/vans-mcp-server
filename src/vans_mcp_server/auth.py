from __future__ import annotations

import hashlib
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import psycopg
from psycopg.rows import dict_row

from mcp.server.auth.middleware.auth_context import AuthContextMiddleware
from mcp.server.auth.middleware.bearer_auth import (
    AuthenticatedUser,
    BearerAuthBackend,
)
from starlette.middleware import Middleware
from starlette.middleware.authentication import AuthenticationMiddleware
from starlette.types import ASGIApp, Receive, Scope, Send

from fastmcp.server.auth import AccessToken, TokenVerifier

logger = logging.getLogger(__name__)

VCR_KEY_PREFIX = "vcr_sk_"


def normalize_api_key(api_key: str | None) -> str:
    if not api_key:
        return ""
    return api_key.strip()


def hash_api_key(api_key: str) -> str:
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()


def _normalize_database_url(url: str) -> str:
    if url.startswith("postgres://"):
        return "postgresql://" + url[len("postgres://") :]
    return url


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


@dataclass(frozen=True)
class AuthContext:
    user_id: int
    email: str | None
    name: str | None
    api_key_id: int
    key_prefix: str
    role: str | None = None


class ApiKeyStore:
    """Minimal vcr_sk_ verification against the Vans Coding Router Neon schema."""

    def __init__(self, database_url: str) -> None:
        self.database_url = _normalize_database_url(database_url)

    def verify(self, api_key: str) -> AuthContext | None:
        api_key = normalize_api_key(api_key)
        if not api_key:
            return None
        key_hash = hash_api_key(api_key)
        now = datetime.now(timezone.utc)
        with psycopg.connect(self.database_url, row_factory=dict_row) as conn:
            row = conn.execute(
                """
                SELECT k.id AS api_key_id,
                       k.key_prefix,
                       k.enabled,
                       k.expires_at,
                       k.session_id,
                       u.id AS user_id,
                       u.email,
                       u.name,
                       u.role,
                       u.status AS user_status,
                       s.expires_at AS session_expires_at,
                       s.status AS session_status,
                       c.status AS class_status,
                       c.ends_at AS class_ends_at
                FROM api_keys k
                JOIN users u ON u.id = k.user_id
                LEFT JOIN class_sessions s ON s.id = k.session_id
                LEFT JOIN classes c ON c.id = s.class_id
                WHERE k.key_hash = %s
                """,
                (key_hash,),
            ).fetchone()
            if not row:
                return None
            if not bool(row["enabled"]):
                return None
            if row["user_status"] != "active":
                return None
            expires_at = _parse_dt(row["expires_at"])
            if expires_at and now >= expires_at:
                return None
            session_expires_at = _parse_dt(row["session_expires_at"])
            if row["session_id"] and session_expires_at and now >= session_expires_at:
                return None
            class_ends_at = _parse_dt(row["class_ends_at"])
            if row["session_id"] and (
                row["class_status"] != "active"
                or (class_ends_at and now >= class_ends_at)
            ):
                return None
            conn.execute(
                "UPDATE api_keys SET last_used_at = %s WHERE id = %s",
                (now.isoformat(), row["api_key_id"]),
            )
            conn.commit()
            return AuthContext(
                user_id=int(row["user_id"]),
                email=row["email"],
                name=row["name"],
                api_key_id=int(row["api_key_id"]),
                key_prefix=str(row["key_prefix"]),
                role=row["role"],
            )


class VcrApiKeyVerifier(TokenVerifier):
    """FastMCP auth provider: Bearer vcr_sk_ → Neon api_keys (or local bypass).

    Intentionally does not set ``base_url`` / resource metadata. Advertising
    ``resource_metadata`` makes VS Code / Cursor start an OAuth DCR flow and
    ignore static ``Authorization: Bearer vcr_sk_...`` headers.
    """

    def __init__(
        self,
        *,
        database_url: str | None = None,
        bypass_key: str | None = None,
    ) -> None:
        # base_url=None → no RFC 9728 resource_metadata on 401 WWW-Authenticate
        super().__init__(base_url=None)
        self.store = ApiKeyStore(database_url) if database_url else None
        self.bypass_key = normalize_api_key(bypass_key) if bypass_key else ""

    @classmethod
    def from_env(cls) -> VcrApiKeyVerifier:
        return cls(
            database_url=os.environ.get("DATABASE_URL") or None,
            bypass_key=os.environ.get("MCP_DEV_BYPASS_KEY") or None,
        )

    async def verify_token(self, token: str) -> AccessToken | None:
        token = normalize_api_key(token)
        if not token:
            return None

        if self.bypass_key and token == self.bypass_key:
            return AccessToken(
                token=token,
                client_id="dev-bypass",
                scopes=["mcp:read"],
                expires_at=None,
                claims={
                    "user_id": 0,
                    "email": "dev@localhost",
                    "name": "dev-bypass",
                    "api_key_id": 0,
                    "key_prefix": "dev",
                    "auth_mode": "bypass",
                },
            )

        if self.store is None:
            logger.warning("DATABASE_URL unset and token is not MCP_DEV_BYPASS_KEY")
            return None

        try:
            ctx = self.store.verify(token)
        except Exception:
            logger.exception("API key verification failed")
            return None
        if ctx is None:
            return None
        return AccessToken(
            token=token,
            client_id=str(ctx.user_id),
            scopes=["mcp:read"],
            expires_at=None,
            claims={
                "user_id": ctx.user_id,
                "email": ctx.email,
                "name": ctx.name,
                "api_key_id": ctx.api_key_id,
                "key_prefix": ctx.key_prefix,
                "role": ctx.role,
                "auth_mode": "neon",
            },
        )


class RequireApiKeyMiddleware:
    """Reject unauthenticated MCP requests without advertising OAuth.

    FastMCP's built-in RequireAuthMiddleware sends ``WWW-Authenticate: Bearer``,
    which makes VS Code start Dynamic Client Registration. API-key clients only
    need a plain 401 so configured ``Authorization`` headers are used instead.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            user = scope.get("user")
            if not isinstance(user, AuthenticatedUser):
                await send(
                    {
                        "type": "http.response.start",
                        "status": 401,
                        "headers": [
                            (b"content-type", b"application/json"),
                            (b"content-length", b"27"),
                        ],
                    }
                )
                await send(
                    {
                        "type": "http.response.body",
                        "body": b'{"error":"unauthorized"}',
                    }
                )
                return
        await self.app(scope, receive, send)


def api_key_http_middleware(verifier: VcrApiKeyVerifier) -> list[Middleware]:
    """Starlette middleware stack for Bearer vcr_sk_ without OAuth discovery."""
    return [
        Middleware(AuthenticationMiddleware, backend=BearerAuthBackend(verifier)),
        Middleware(AuthContextMiddleware),
        Middleware(RequireApiKeyMiddleware),
    ]
