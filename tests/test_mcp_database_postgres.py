"""Against a real Postgres: the service's own database (#100).

Skipped unless VANS_MCP_TEST_TARGET_URL is set: an empty database, connecting as a
non-superuser app role (set up as in docs/mcp-database-move-rehearsal.md).

Each test drops mcp_usage and mcp_oauth_connections before it starts.
"""

from __future__ import annotations

import os

import psycopg
import pytest

from vans_mcp_server.oauth.crypto import TokenEncryptor
from vans_mcp_server.oauth.google import GoogleTokenBundle
from vans_mcp_server.oauth.store import OAuthConnectionStore
from vans_mcp_server.usage import UsageLogger

TARGET_URL = os.environ.get("VANS_MCP_TEST_TARGET_URL", "")

pytestmark = pytest.mark.skipif(not TARGET_URL, reason="VANS_MCP_TEST_TARGET_URL not set")

TABLES = ("mcp_usage", "mcp_oauth_connections")


def _drop_tables(url: str) -> None:
    with psycopg.connect(url) as conn:
        for table in TABLES:
            conn.execute(f"DROP TABLE IF EXISTS {table}")


@pytest.fixture(autouse=True)
def empty_databases():
    _drop_tables(TARGET_URL)
    yield


def _owner(url: str, table: str) -> str:
    with psycopg.connect(url) as conn:
        return conn.execute(
            "SELECT tableowner FROM pg_tables WHERE schemaname = 'public' AND tablename = %s",
            (table,),
        ).fetchone()[0]


def _current_user(url: str) -> str:
    with psycopg.connect(url) as conn:
        return conn.execute("SELECT current_user").fetchone()[0]


def _bundle(refresh: str | None = "refresh-1", access: str = "access-1") -> GoogleTokenBundle:
    return GoogleTokenBundle(
        access_token=access,
        refresh_token=refresh,
        expires_in=3600,
        scope="https://www.googleapis.com/auth/calendar",
        google_sub="sub-1234",
        email="s@example.com",
    )


def test_service_creates_its_tables_owned_by_its_own_role(monkeypatch):
    monkeypatch.setenv("VANS_MCP_DATABASE_URL", TARGET_URL)
    monkeypatch.setenv("OAUTH_TOKEN_ENCRYPTION_KEY", TokenEncryptor.generate_key())

    usage = UsageLogger.from_env()
    store = OAuthConnectionStore.from_env()
    assert store is not None

    app_role = _current_user(TARGET_URL)
    assert _owner(TARGET_URL, "mcp_usage") == app_role
    assert _owner(TARGET_URL, "mcp_oauth_connections") == app_role

    store.upsert_google_tokens(user_id=1234, bundle=_bundle())
    usage.record(tool_name="calendar_list_events", success=True, user_id=1234, latency_ms=5)

    assert store.is_connected(1234)
    assert store.get_valid_access_token(1234).refresh_token == "refresh-1"
    with psycopg.connect(TARGET_URL) as conn:
        assert conn.execute(
            "SELECT user_id, tool_name, api_key_id FROM mcp_usage"
        ).fetchall() == [(1234, "calendar_list_events", None)]

