from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os
import secrets
import time
from dataclasses import dataclass
from urllib.parse import urlencode

import httpx

logger = logging.getLogger(__name__)

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
STATE_MAX_AGE_SECONDS = 600

CALENDAR_SCOPE = "https://www.googleapis.com/auth/calendar"
GMAIL_READONLY_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
GMAIL_COMPOSE_SCOPE = "https://www.googleapis.com/auth/gmail.compose"
GMAIL_MODIFY_SCOPE = "https://www.googleapis.com/auth/gmail.modify"
TASKS_SCOPE = "https://www.googleapis.com/auth/tasks"

GOOGLE_PORTAL_SCOPES = (
    "openid",
    "email",
    "profile",
    CALENDAR_SCOPE,
    GMAIL_READONLY_SCOPE,
    GMAIL_COMPOSE_SCOPE,
    GMAIL_MODIFY_SCOPE,
    TASKS_SCOPE,
)

# Backward-compatible alias (Calendar + Gmail + Tasks portal scopes).
CALENDAR_SCOPES = GOOGLE_PORTAL_SCOPES


@dataclass(frozen=True)
class GoogleTokenBundle:
    access_token: str
    refresh_token: str | None
    expires_in: int | None
    scope: str | None
    google_sub: str | None
    email: str | None


def scopes_include(granted: str | None, required: tuple[str, ...]) -> bool:
    if not granted:
        return False
    have = set(granted.split())
    return all(scope in have for scope in required)


class GoogleOAuthService:
    """Offline Google OAuth for Calendar/Gmail/Tasks connect (not portal login)."""

    def __init__(
        self,
        *,
        client_id: str,
        client_secret: str,
        redirect_uri: str,
        session_secret: str,
        scopes: tuple[str, ...] = GOOGLE_PORTAL_SCOPES,
    ) -> None:
        self.client_id = client_id.strip()
        self.client_secret = client_secret.strip()
        self.redirect_uri = redirect_uri
        self.session_secret = session_secret.strip()
        self.scopes = scopes

    @classmethod
    def from_env(cls) -> GoogleOAuthService | None:
        client_id = os.environ.get("GOOGLE_CLIENT_ID") or ""
        client_secret = os.environ.get("GOOGLE_CLIENT_SECRET") or ""
        session_secret = os.environ.get("SESSION_SECRET") or ""
        public_url = (os.environ.get("PUBLIC_URL") or "http://127.0.0.1:8080").rstrip(
            "/"
        )
        redirect_uri = (
            os.environ.get("GOOGLE_OAUTH_REDIRECT_URI")
            or f"{public_url}/connect/google/callback"
        )
        if not (client_id.strip() and client_secret.strip() and session_secret.strip()):
            return None
        return cls(
            client_id=client_id,
            client_secret=client_secret,
            redirect_uri=redirect_uri,
            session_secret=session_secret,
        )

    def is_configured(self) -> bool:
        return bool(self.client_id and self.client_secret and self.session_secret)

    def _encode_state(self, raw: str) -> str:
        return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii").rstrip("=")

    def _decode_state_candidates(self, state: str) -> list[str]:
        state = state.strip()
        candidates = [state]
        try:
            padded = state + "=" * (-len(state) % 4)
            decoded = base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8")
            if decoded and decoded not in candidates:
                candidates.append(decoded)
        except (ValueError, UnicodeDecodeError):
            pass
        return candidates

    def create_connect_state(self, user_id: int) -> str:
        nonce = secrets.token_urlsafe(24)
        ts = str(int(time.time()))
        payload = f"{int(user_id)}:{nonce}:{ts}"
        sig = hmac.new(
            self.session_secret.encode("utf-8"),
            payload.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        return self._encode_state(f"{payload}:{sig}")

    def verify_connect_state(self, state: str) -> int | None:
        for candidate in self._decode_state_candidates(state):
            user_id = self._verify_connect_state_raw(candidate)
            if user_id is not None:
                return user_id
        return None

    def _verify_connect_state_raw(self, state: str) -> int | None:
        parts = state.split(":")
        if len(parts) != 4:
            return None
        user_text, nonce, ts_text, sig = parts
        if not user_text or not nonce or not ts_text or not sig:
            return None
        try:
            user_id = int(user_text)
            ts = int(ts_text)
        except ValueError:
            return None
        if time.time() - ts > STATE_MAX_AGE_SECONDS:
            return None
        payload = f"{user_id}:{nonce}:{ts_text}"
        expected = hmac.new(
            self.session_secret.encode("utf-8"),
            payload.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        if not hmac.compare_digest(expected.lower(), sig.strip().lower()):
            return None
        return user_id

    def authorize_url(self, state: str) -> str:
        params = {
            "client_id": self.client_id,
            "redirect_uri": self.redirect_uri,
            "response_type": "code",
            "scope": " ".join(self.scopes),
            "state": state,
            "access_type": "offline",
            "prompt": "consent",
            "include_granted_scopes": "true",
        }
        return f"{GOOGLE_AUTH_URL}?{urlencode(params)}"

    def exchange_code(self, code: str) -> GoogleTokenBundle:
        with httpx.Client(timeout=20.0) as client:
            response = client.post(
                GOOGLE_TOKEN_URL,
                data={
                    "code": code,
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                    "redirect_uri": self.redirect_uri,
                    "grant_type": "authorization_code",
                },
            )
            response.raise_for_status()
            data = response.json()

        access_token = data.get("access_token")
        if not access_token:
            raise ValueError("google token response missing access_token")

        google_sub = None
        email = None
        id_token_jwt = data.get("id_token")
        if id_token_jwt:
            google_sub, email = _peek_id_token_claims(id_token_jwt)

        return GoogleTokenBundle(
            access_token=str(access_token),
            refresh_token=data.get("refresh_token"),
            expires_in=int(data["expires_in"]) if data.get("expires_in") else None,
            scope=data.get("scope"),
            google_sub=google_sub,
            email=email,
        )

    def refresh_access_token(self, refresh_token: str) -> GoogleTokenBundle:
        with httpx.Client(timeout=20.0) as client:
            response = client.post(
                GOOGLE_TOKEN_URL,
                data={
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                    "refresh_token": refresh_token,
                    "grant_type": "refresh_token",
                },
            )
            response.raise_for_status()
            data = response.json()

        access_token = data.get("access_token")
        if not access_token:
            raise ValueError("google refresh response missing access_token")

        return GoogleTokenBundle(
            access_token=str(access_token),
            refresh_token=data.get("refresh_token") or refresh_token,
            expires_in=int(data["expires_in"]) if data.get("expires_in") else None,
            scope=data.get("scope"),
            google_sub=None,
            email=None,
        )


def _peek_id_token_claims(id_token_jwt: str) -> tuple[str | None, str | None]:
    """Decode JWT payload without verification (identity already gated by token endpoint)."""
    try:
        parts = id_token_jwt.split(".")
        if len(parts) < 2:
            return None, None
        padded = parts[1] + "=" * (-len(parts[1]) % 4)
        import json

        payload = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")))
        sub = payload.get("sub")
        email = payload.get("email")
        return (str(sub) if sub else None, str(email) if email else None)
    except Exception:
        logger.debug("failed to peek id_token claims", exc_info=True)
        return None, None
