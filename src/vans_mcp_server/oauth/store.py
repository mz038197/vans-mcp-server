from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import psycopg
from psycopg.rows import dict_row

from vans_mcp_server.oauth.crypto import TokenEncryptor
from vans_mcp_server.oauth.google import GoogleOAuthService, GoogleTokenBundle

logger = logging.getLogger(__name__)

PROVIDER_GOOGLE = "google"


def _normalize_database_url(url: str) -> str:
    if url.startswith("postgres://"):
        return "postgresql://" + url[len("postgres://") :]
    return url


@dataclass
class StoredGoogleConnection:
    user_id: int
    access_token: str
    refresh_token: str | None
    expires_at: datetime | None
    scopes: str | None
    google_sub: str | None


class OAuthConnectionStore:
    """Neon-backed per-user Google OAuth tokens (encrypted at rest)."""

    def __init__(
        self,
        database_url: str,
        encryptor: TokenEncryptor,
        oauth: GoogleOAuthService | None = None,
    ) -> None:
        self.database_url = _normalize_database_url(database_url)
        self.encryptor = encryptor
        self.oauth = oauth
        self._ensure_table()

    @classmethod
    def from_env(cls, oauth: GoogleOAuthService | None = None) -> OAuthConnectionStore | None:
        database_url = os.environ.get("DATABASE_URL") or ""
        encryptor = TokenEncryptor.from_env()
        if not database_url.strip() or encryptor is None:
            return None
        return cls(database_url, encryptor, oauth=oauth)

    def _ensure_table(self) -> None:
        with psycopg.connect(self.database_url) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS mcp_oauth_connections (
                    id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                    user_id INTEGER NOT NULL,
                    provider TEXT NOT NULL,
                    refresh_token_enc TEXT,
                    access_token_enc TEXT,
                    expires_at TEXT,
                    scopes TEXT,
                    google_sub TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE (user_id, provider)
                )
                """
            )
            conn.commit()

    def is_connected(self, user_id: int, provider: str = PROVIDER_GOOGLE) -> bool:
        with psycopg.connect(self.database_url, row_factory=dict_row) as conn:
            row = conn.execute(
                """
                SELECT 1 AS ok
                FROM mcp_oauth_connections
                WHERE user_id = %s AND provider = %s
                  AND refresh_token_enc IS NOT NULL
                """,
                (user_id, provider),
            ).fetchone()
            return row is not None

    def upsert_google_tokens(
        self,
        *,
        user_id: int,
        bundle: GoogleTokenBundle,
        keep_existing_refresh: bool = True,
    ) -> None:
        now = datetime.now(timezone.utc)
        expires_at = None
        if bundle.expires_in:
            expires_at = (now + timedelta(seconds=int(bundle.expires_in))).isoformat()

        existing_refresh = None
        if keep_existing_refresh and not bundle.refresh_token:
            existing = self._load_row(user_id)
            if existing and existing.get("refresh_token_enc"):
                existing_refresh = existing["refresh_token_enc"]

        refresh_enc = (
            self.encryptor.encrypt(bundle.refresh_token)
            if bundle.refresh_token
            else existing_refresh
        )
        if not refresh_enc:
            raise ValueError("refresh_token required for first Google connect")

        access_enc = self.encryptor.encrypt(bundle.access_token)
        scopes = bundle.scope
        google_sub = bundle.google_sub
        now_iso = now.isoformat()

        with psycopg.connect(self.database_url) as conn:
            conn.execute(
                """
                INSERT INTO mcp_oauth_connections
                    (user_id, provider, refresh_token_enc, access_token_enc,
                     expires_at, scopes, google_sub, created_at, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (user_id, provider) DO UPDATE SET
                    refresh_token_enc = EXCLUDED.refresh_token_enc,
                    access_token_enc = EXCLUDED.access_token_enc,
                    expires_at = EXCLUDED.expires_at,
                    scopes = COALESCE(EXCLUDED.scopes, mcp_oauth_connections.scopes),
                    google_sub = COALESCE(EXCLUDED.google_sub, mcp_oauth_connections.google_sub),
                    updated_at = EXCLUDED.updated_at
                """,
                (
                    user_id,
                    PROVIDER_GOOGLE,
                    refresh_enc,
                    access_enc,
                    expires_at,
                    scopes,
                    google_sub,
                    now_iso,
                    now_iso,
                ),
            )
            conn.commit()

    def _load_row(self, user_id: int) -> dict[str, Any] | None:
        with psycopg.connect(self.database_url, row_factory=dict_row) as conn:
            return conn.execute(
                """
                SELECT *
                FROM mcp_oauth_connections
                WHERE user_id = %s AND provider = %s
                """,
                (user_id, PROVIDER_GOOGLE),
            ).fetchone()

    def get_granted_scopes(
        self, user_id: int, provider: str = PROVIDER_GOOGLE
    ) -> str | None:
        row = self._load_row(user_id) if provider == PROVIDER_GOOGLE else None
        if not row:
            return None
        return row.get("scopes")

    def get_valid_access_token(self, user_id: int) -> StoredGoogleConnection | None:
        row = self._load_row(user_id)
        if not row or not row.get("refresh_token_enc"):
            return None

        refresh_token = self.encryptor.decrypt(row["refresh_token_enc"])
        access_token = (
            self.encryptor.decrypt(row["access_token_enc"])
            if row.get("access_token_enc")
            else ""
        )
        expires_at = _parse_dt(row.get("expires_at"))
        now = datetime.now(timezone.utc)
        needs_refresh = (
            not access_token
            or expires_at is None
            or now >= (expires_at - timedelta(seconds=60))
        )

        if needs_refresh:
            if self.oauth is None:
                raise RuntimeError("Google OAuth not configured; cannot refresh token")
            bundle = self.oauth.refresh_access_token(refresh_token)
            self.upsert_google_tokens(
                user_id=user_id, bundle=bundle, keep_existing_refresh=True
            )
            access_token = bundle.access_token
            refresh_token = bundle.refresh_token or refresh_token
            # Capture time after the refresh API call so expires_at is not
            # underestimated by network/API latency.
            now = datetime.now(timezone.utc)
            expires_at = (
                now + timedelta(seconds=int(bundle.expires_in))
                if bundle.expires_in
                else None
            )

        return StoredGoogleConnection(
            user_id=user_id,
            access_token=access_token,
            refresh_token=refresh_token,
            expires_at=expires_at,
            scopes=row.get("scopes"),
            google_sub=row.get("google_sub"),
        )


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
