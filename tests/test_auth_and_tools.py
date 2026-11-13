from __future__ import annotations

import importlib
import json
from pathlib import Path

import httpx

import pytest
from starlette.testclient import TestClient

from vans_mcp_server.auth import VcrApiKeyVerifier, normalize_api_key
from vans_mcp_server.key_check import KeyCheckClient
from vans_mcp_server.tools import knowledge

CONTRACTS = Path(__file__).parent / "fixtures" / "key_check"


def _key_check_answering(contract_file: str) -> KeyCheckClient:
    body = json.loads((CONTRACTS / contract_file).read_text(encoding="utf-8"))
    return KeyCheckClient(
        vcr_url="https://router.test",
        service_credential="svc",
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=body)),
    )


def _verifier_answering(contract_file: str) -> VcrApiKeyVerifier:
    return VcrApiKeyVerifier(key_check=_key_check_answering(contract_file))


def test_normalize_key():
    assert normalize_api_key("  vcr_sk_abc  ") == "vcr_sk_abc"


@pytest.mark.asyncio
async def test_classroom_claims_carry_the_student_id_kind_and_class():
    token = await _verifier_answering("classroom-accepted.json").verify_token(
        "vcr_sk_student"
    )
    assert token is not None
    assert token.claims["user_id"] == 1234
    assert token.claims["kind"] == "classroom"
    assert token.claims["class_id"] == 56
    for gone in ("email", "name", "api_key_id", "role"):
        assert gone not in token.claims


@pytest.mark.asyncio
async def test_personal_claims_carry_the_user_id_and_no_class():
    token = await _verifier_answering("personal-accepted.json").verify_token(
        "vcr_sk_teacher"
    )
    assert token is not None
    assert token.claims["user_id"] == 42
    assert token.claims["kind"] == "personal"
    assert token.claims["class_id"] is None


def test_bypass_key_on_fly_refuses_to_start(monkeypatch):
    monkeypatch.setenv("MCP_DEV_BYPASS_KEY", "vcr_sk_dev_local_only")
    monkeypatch.setenv("FLY_APP_NAME", "vans-mcp-server")

    with pytest.raises(RuntimeError, match="MCP_DEV_BYPASS_KEY"):
        _load_app()
    monkeypatch.delenv("FLY_APP_NAME")
    _load_app()


def _load_app():
    import sys

    loaded = sys.modules.get("vans_mcp_server.app")
    if loaded is not None:
        return importlib.reload(loaded)
    return importlib.import_module("vans_mcp_server.app")


def test_key_check_settings_come_from_vcr_url_and_vcr_service_credential(monkeypatch):
    monkeypatch.delenv("MCP_DEV_BYPASS_KEY", raising=False)
    monkeypatch.setenv("FLY_APP_NAME", "vans-mcp-server")
    monkeypatch.setenv("VCR_URL", "https://router.example/")
    monkeypatch.setenv("VCR_SERVICE_CREDENTIAL", "cred-123")
    verifier = VcrApiKeyVerifier.from_env()
    assert verifier.key_check.vcr_url == "https://router.example"
    assert verifier.key_check.service_credential == "cred-123"

    monkeypatch.delenv("VCR_URL")
    assert VcrApiKeyVerifier.from_env().key_check.vcr_url == "https://ai.vanscoding.com"


@pytest.mark.asyncio
async def test_bypass_key_accepted():
    verifier = VcrApiKeyVerifier(
        key_check=_key_check_answering("refused.json"),
        bypass_key="vcr_sk_dev_local_only",
    )
    token = await verifier.verify_token("vcr_sk_dev_local_only")
    assert token is not None
    assert token.claims["auth_mode"] == "bypass"


@pytest.mark.asyncio
async def test_invalid_key_rejected_without_db():
    verifier = _verifier_answering("refused.json")
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
    monkeypatch.delenv("FLY_APP_NAME", raising=False)
    monkeypatch.delenv("VANS_MCP_DATABASE_URL", raising=False)
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
    app_module.auth.key_check = _verifier_answering("refused.json").key_check
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
        assert "www-authenticate" not in {k.lower() for k in res.headers.keys()}


_INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2024-11-05",
        "capabilities": {},
        "clientInfo": {"name": "test", "version": "0"},
    },
}
_MCP_HEADERS = {
    "Accept": "application/json, text/event-stream",
    "Content-Type": "application/json",
}


def test_mcp_oauth_401_challenges_with_resource_metadata(monkeypatch):
    """Agent Builder entry: 401 points at RFC 9728 metadata (ADR 0004)."""
    monkeypatch.setenv("PUBLIC_URL", "https://mcp.vanscoding.com")
    app_module = _reload_app(monkeypatch)
    with TestClient(app_module.app) as client:
        res = client.post("/mcp-oauth/", headers=_MCP_HEADERS, json=_INITIALIZE)
        assert res.status_code == 401
        assert res.headers["www-authenticate"] == (
            'Bearer resource_metadata="https://mcp.vanscoding.com'
            '/.well-known/oauth-protected-resource/mcp-oauth"'
        )


def test_mcp_oauth_refused_key_also_challenges(monkeypatch):
    """An expired Classroom API Key must send Agent Builder back to linking."""
    app_module = _reload_app(monkeypatch)
    app_module.auth.key_check = _verifier_answering("refused.json").key_check
    with TestClient(app_module.app) as client:
        res = client.post(
            "/mcp-oauth/",
            headers={**_MCP_HEADERS, "Authorization": "Bearer vcr_sk_expired"},
            json=_INITIALIZE,
        )
        assert res.status_code == 401
        assert "resource_metadata=" in res.headers["www-authenticate"]


def test_mcp_oauth_metadata_names_the_router(monkeypatch):
    monkeypatch.setenv("PUBLIC_URL", "https://mcp.vanscoding.com")
    monkeypatch.setenv("VCR_URL", "https://ai.vanscoding.com")
    app_module = _reload_app(monkeypatch)
    with TestClient(app_module.app) as client:
        res = client.get("/.well-known/oauth-protected-resource/mcp-oauth")
        assert res.status_code == 200
        assert res.json() == {
            "resource": "https://mcp.vanscoding.com/mcp-oauth",
            "authorization_servers": ["https://ai.vanscoding.com"],
            "scopes_supported": ["classroom"],
            "bearer_methods_supported": ["header"],
        }


def test_mcp_has_no_resource_metadata(monkeypatch):
    """/mcp must stay undiscoverable so VS Code keeps its static key."""
    app_module = _reload_app(monkeypatch)
    with TestClient(app_module.app) as client:
        assert client.get("/.well-known/oauth-protected-resource/mcp").status_code == 404
        assert client.get("/.well-known/oauth-protected-resource").status_code == 404


def test_mcp_oauth_accepts_the_same_keys_as_mcp(monkeypatch):
    app_module = _reload_app(monkeypatch)
    with TestClient(app_module.app) as client:
        res = client.post(
            "/mcp-oauth/",
            headers={**_MCP_HEADERS, "Authorization": "Bearer vcr_sk_dev_local_only"},
            json=_INITIALIZE,
        )
        assert res.status_code == 200
        assert "www-authenticate" not in {k.lower() for k in res.headers.keys()}


def test_mcp_accepts_bypass_key(monkeypatch):
    app_module = _reload_app(monkeypatch)
    with TestClient(app_module.app) as client:
        res = client.post(
            "/mcp/",
            headers={
                "Authorization": "Bearer vcr_sk_dev_local_only",
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
        assert res.status_code == 200


@pytest.mark.asyncio
async def test_calendar_attendees_schema_is_plain_array(monkeypatch):
    """Cursor/VS Code rejects anyOf [array, null]; attendees must be a plain array."""
    app_module = _reload_app(monkeypatch)
    create = await app_module.mcp.get_tool("calendar_create_event")
    update = await app_module.mcp.get_tool("calendar_update_event")
    for tool in (create, update):
        attendees = tool.parameters["properties"]["attendees"]
        assert attendees.get("type") == "array"
        assert attendees.get("items") == {"type": "string"}
        assert "anyOf" not in attendees
    clear = update.parameters["properties"]["clear_attendees"]
    assert clear.get("type") == "boolean"
    assert "anyOf" not in clear
