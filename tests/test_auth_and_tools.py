from __future__ import annotations

import hashlib
import importlib

import pytest
from starlette.testclient import TestClient

from vans_mcp_server.auth import VcrApiKeyVerifier, hash_api_key, normalize_api_key
from vans_mcp_server.tools import knowledge


def test_normalize_and_hash_key():
    assert normalize_api_key("  vcr_sk_abc  ") == "vcr_sk_abc"
    assert hash_api_key("vcr_sk_abc") == hashlib.sha256(b"vcr_sk_abc").hexdigest()


@pytest.mark.asyncio
async def test_bypass_key_accepted():
    verifier = VcrApiKeyVerifier(bypass_key="vcr_sk_dev_local_only")
    token = await verifier.verify_token("vcr_sk_dev_local_only")
    assert token is not None
    assert token.claims["auth_mode"] == "bypass"


@pytest.mark.asyncio
async def test_invalid_key_rejected_without_db():
    verifier = VcrApiKeyVerifier()
    assert await verifier.verify_token("vcr_sk_nope") is None
    assert await verifier.verify_token("") is None


def test_knowledge_search_and_read():
    found = knowledge.search_pages("portal")
    assert found["count"] >= 1
    page_id = found["pages"][0]["id"]
    page = knowledge.read_page(page_id)
    assert page["found"] is True
    missing = knowledge.read_page("no_such_page")
    assert missing["found"] is False
    assert "hint" in missing


def _reload_app(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("MCP_DEV_BYPASS_KEY", "vcr_sk_dev_local_only")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    import vans_mcp_server.app as app_module

    importlib.reload(app_module)
    return app_module


def test_health_endpoint(monkeypatch):
    app_module = _reload_app(monkeypatch)
    with TestClient(app_module.app) as client:
        res = client.get("/health")
        assert res.status_code == 200
        body = res.json()
        assert body["ok"] is True
        assert body["service"] == "vans-mcp-server"


def test_mcp_rejects_bad_key(monkeypatch):
    app_module = _reload_app(monkeypatch)
    with TestClient(app_module.app) as client:
        auth = client.post(
            "/mcp/",
            headers={
                "Authorization": "Bearer vcr_sk_wrong",
                "Accept": "application/json, text/event-stream",
                "Content-Type": "application/json",
            },
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "test", "version": "0"},
                },
            },
        )
        assert auth.status_code in (401, 403)


def test_mcp_401_does_not_advertise_oauth_metadata(monkeypatch):
    """Clients must use Bearer API keys, not OAuth DCR discovery."""
    monkeypatch.setenv("PUBLIC_URL", "https://mcp.vanscoding.com")
    app_module = _reload_app(monkeypatch)
    with TestClient(app_module.app) as client:
        res = client.post(
            "/mcp/",
            headers={
                "Accept": "application/json, text/event-stream",
                "Content-Type": "application/json",
            },
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "test", "version": "0"},
                },
            },
        )
        assert res.status_code == 401
        www = res.headers.get("www-authenticate", "")
        assert "resource_metadata" not in www.lower()
        assert "oauth" not in www.lower()
