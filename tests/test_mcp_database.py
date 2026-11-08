"""vans-mcp-server keeps its tables in its own database, VANS_MCP_DATABASE_URL (#100).

The router's DATABASE_URL is not read any more (router ADR 0069, 0071, 0073).
"""

from __future__ import annotations

import importlib
from unittest.mock import MagicMock

import psycopg
import pytest
from starlette.testclient import TestClient

from vans_mcp_server.oauth.crypto import TokenEncryptor

ROUTER_URL = "postgresql://neondb_owner:pw@router.example/neondb"
MCP_URL = "postgresql://vans_mcp_server_app:pw@router.example/vans_mcp_server"


@pytest.fixture
def reload_app(monkeypatch):
    """Reload the app with the given database settings; no real Postgres is opened."""

    connect = MagicMock()
    monkeypatch.setattr(psycopg, "connect", connect)

    def build(*, database_url: str | None = None, mcp_database_url: str | None = None):
        monkeypatch.setenv("MCP_DEV_BYPASS_KEY", "vcr_sk_dev_local_only")
        monkeypatch.delenv("FLY_APP_NAME", raising=False)
        monkeypatch.setenv("SESSION_SECRET", "session-secret-for-tests")
        monkeypatch.setenv("GOOGLE_CLIENT_ID", "cid")
        monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "csecret")
        monkeypatch.setenv("DISCORD_GUILD_ID", "guild-1")
        monkeypatch.setenv("OAUTH_TOKEN_ENCRYPTION_KEY", TokenEncryptor.generate_key())
        for name, value in (
            ("DATABASE_URL", database_url),
            ("VANS_MCP_DATABASE_URL", mcp_database_url),
        ):
            if value is None:
                monkeypatch.delenv(name, raising=False)
            else:
                monkeypatch.setenv(name, value)
        import vans_mcp_server.app as app_module

        importlib.reload(app_module)
        return app_module

    build.connect = connect
    return build


def test_health_reports_the_mcp_database(reload_app):
    app_module = reload_app(mcp_database_url=MCP_URL)
    with TestClient(app_module.app) as client:
        body = client.get("/health").json()
    assert body["database_configured"] is True
    assert body["oauth_store_configured"] is True


def test_router_database_url_alone_is_not_a_database(reload_app):
    app_module = reload_app(database_url=ROUTER_URL)
    with TestClient(app_module.app) as client:
        body = client.get("/health").json()
    assert body["database_configured"] is False
    assert body["oauth_store_configured"] is False
    reload_app.connect.assert_not_called()


def test_google_connect_page_needs_the_mcp_database(reload_app):
    app_module = reload_app(database_url=ROUTER_URL)
    state = app_module.google_oauth.create_connect_state(1234)
    with TestClient(app_module.app) as client:
        res = client.get(f"/connect/google/callback?state={state}&code=abc")
    assert res.status_code == 503
    assert "VANS_MCP_DATABASE_URL and OAUTH_TOKEN_ENCRYPTION_KEY are required" in res.text


def test_discord_connect_pages_need_the_mcp_database(reload_app):
    app_module = reload_app(database_url=ROUTER_URL)
    state = app_module.discord_connect.create_connect_state(42)
    with TestClient(app_module.app) as client:
        start = client.get(f"/connect/discord/start?state={state}")
        submit = client.post(
            "/connect/discord/submit",
            data={"state": state, "application_id": "1", "bot_token": "t"},
        )
    for res in (start, submit):
        assert res.status_code == 503
        assert "VANS_MCP_DATABASE_URL and OAUTH_TOKEN_ENCRYPTION_KEY are required" in res.text


def test_leftover_router_database_url_is_ignored_with_a_warning(reload_app, caplog):
    with caplog.at_level("WARNING", logger="vans_mcp_server"):
        app_module = reload_app(database_url=ROUTER_URL, mcp_database_url=MCP_URL)
    assert app_module.usage.database_url == MCP_URL
    assert app_module.oauth_store.database_url == MCP_URL
    warnings = [r for r in caplog.records if "DATABASE_URL" in r.getMessage()]
    assert len(warnings) == 1
    assert warnings[0].levelname == "WARNING"
    assert "not read" in warnings[0].getMessage()
    assert "pw" not in warnings[0].getMessage()


def test_no_warning_without_the_router_database_url(reload_app, caplog):
    with caplog.at_level("WARNING", logger="vans_mcp_server"):
        reload_app(mcp_database_url=MCP_URL)
    assert not [r for r in caplog.records if "DATABASE_URL" in r.getMessage()]
