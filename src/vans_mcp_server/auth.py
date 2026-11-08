from __future__ import annotations

import json
import logging
import os

from mcp.server.auth.middleware.auth_context import AuthContextMiddleware
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
from starlette.authentication import (
    AuthCredentials,
    AuthenticationBackend,
    AuthenticationError,
)
from starlette.middleware import Middleware
from starlette.middleware.authentication import AuthenticationMiddleware
from starlette.requests import HTTPConnection
from starlette.responses import Response
from starlette.types import ASGIApp, Receive, Scope, Send

from fastmcp.server.auth import AccessToken, TokenVerifier

from vans_mcp_server.key_check import (
    KeyCheckClient,
    KeyRefusal,
    key_prefix,
)

logger = logging.getLogger(__name__)

DEFAULT_VCR_URL = "https://ai.vanscoding.com"
SCOPES = ["mcp:read"]


def normalize_api_key(api_key: str | None) -> str:
    if not api_key:
        return ""
    return api_key.strip()


def _access_token(
    token: str,
    *,
    client_id: str,
    user_id: int,
    kind: str,
    class_id: int | None,
    auth_mode: str,
) -> AccessToken:
    """The one place the claims the tools read are built."""
    return AccessToken(
        token=token,
        client_id=client_id,
        scopes=SCOPES,
        expires_at=None,
        claims={
            "user_id": user_id,
            "kind": kind,
            "class_id": class_id,
            "auth_mode": auth_mode,
        },
    )


class VcrApiKeyVerifier(TokenVerifier):
    """FastMCP token verifier: Bearer vcr_sk_ -> router Key Check (or local bypass).

    Intentionally does not set ``base_url`` / resource metadata. Advertising
    ``resource_metadata`` makes VS Code / Cursor start an OAuth DCR flow and
    ignore static ``Authorization: Bearer vcr_sk_...`` headers.
    """

    def __init__(
        self,
        *,
        key_check: KeyCheckClient,
        bypass_key: str | None = None,
    ) -> None:
        # base_url=None -> no RFC 9728 resource_metadata on 401 WWW-Authenticate
        super().__init__(base_url=None)
        self.key_check = key_check
        self.bypass_key = normalize_api_key(bypass_key) if bypass_key else ""

    @classmethod
    def from_env(cls) -> VcrApiKeyVerifier:
        bypass_key = os.environ.get("MCP_DEV_BYPASS_KEY") or None
        if bypass_key and os.environ.get("FLY_APP_NAME"):
            raise RuntimeError(
                "MCP_DEV_BYPASS_KEY is for local development only; "
                "unset it on Fly (FLY_APP_NAME is set)."
            )
        return cls(
            key_check=KeyCheckClient(
                vcr_url=os.environ.get("VCR_URL") or DEFAULT_VCR_URL,
                service_credential=os.environ.get("VCR_SERVICE_CREDENTIAL") or "",
            ),
            bypass_key=bypass_key,
        )

    async def authenticate(self, token: str | None) -> AccessToken | KeyRefusal:
        token = normalize_api_key(token)
        if self.bypass_key and token == self.bypass_key:
            return _access_token(
                token,
                client_id="dev-bypass",
                user_id=0,
                kind="personal",
                class_id=None,
                auth_mode="bypass",
            )
        answer = await self.key_check.check(token)
        if isinstance(answer, KeyRefusal):
            logger.info(
                "key refused (%s) for key %s", answer.reason, key_prefix(token)
            )
            return answer
        return _access_token(
            token,
            client_id=str(answer.user_id),
            user_id=answer.user_id,
            kind=answer.kind,
            class_id=answer.class_id,
            auth_mode="key_check",
        )

    async def verify_token(self, token: str) -> AccessToken | None:
        result = await self.authenticate(token)
        return result if isinstance(result, AccessToken) else None


class KeyRefused(AuthenticationError):
    """Carries the router's message to the 401 body."""


class KeyCheckAuthBackend(AuthenticationBackend):
    def __init__(self, verifier: VcrApiKeyVerifier) -> None:
        self.verifier = verifier

    async def authenticate(self, conn: HTTPConnection):
        header = conn.headers.get("authorization")
        if not header or not header.lower().startswith("bearer "):
            return None
        result = await self.verifier.authenticate(header[len("bearer ") :])
        if isinstance(result, KeyRefusal):
            raise KeyRefused(result.message)
        return AuthCredentials(result.scopes), AuthenticatedUser(result)


def _plain_401(message: str | None = None) -> Response:
    """401 with no ``WWW-Authenticate``, so VS Code keeps the configured header."""
    body: dict = {"error": "unauthorized"}
    if message:
        body["message"] = message
    return Response(
        json.dumps(body, ensure_ascii=False),
        status_code=401,
        media_type="application/json",
    )


def _refusal_response(_conn: HTTPConnection, exc: AuthenticationError) -> Response:
    return _plain_401(str(exc))


class RequireApiKeyMiddleware:
    """Reject unauthenticated MCP requests without advertising OAuth.

    FastMCP's built-in RequireAuthMiddleware sends ``WWW-Authenticate: Bearer``,
    which makes VS Code start Dynamic Client Registration. API-key clients only
    need a plain 401 so configured ``Authorization`` headers are used instead.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and not isinstance(
            scope.get("user"), AuthenticatedUser
        ):
            await _plain_401()(scope, receive, send)
            return
        await self.app(scope, receive, send)


def api_key_http_middleware(verifier: VcrApiKeyVerifier) -> list[Middleware]:
    """Starlette middleware stack for Bearer vcr_sk_ without OAuth discovery."""
    return [
        Middleware(
            AuthenticationMiddleware,
            backend=KeyCheckAuthBackend(verifier),
            on_error=_refusal_response,
        ),
        Middleware(AuthContextMiddleware),
        Middleware(RequireApiKeyMiddleware),
    ]
