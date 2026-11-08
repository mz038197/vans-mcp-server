"""MCP over HTTP with keys checked by the router's Key Check (contract examples)."""

from __future__ import annotations

import importlib
import json
import logging
from pathlib import Path

import httpx
import pytest
from starlette.testclient import TestClient

from vans_mcp_server.key_check import KeyCheckClient
from vans_mcp_server.oauth.store import StoredDiscordBotConnection

CONTRACTS = Path(__file__).parent / "fixtures" / "key_check"
STUDENT_KEY = "vcr_sk_student_secret_value"
TEACHER_KEY = "vcr_sk_teacher_secret_value"
ACCEPT = "application/json, text/event-stream"
INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2024-11-05",
        "capabilities": {},
        "clientInfo": {"name": "test", "version": "0"},
    },
}


def contract(name: str) -> dict:
    return json.loads((CONTRACTS / name).read_text(encoding="utf-8"))


class FakeRouter:
    def __init__(self, answers: dict[str, tuple[int, dict]]) -> None:
        self.answers = answers
        self.asked: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        key = json.loads(request.content)["api_key"]
        self.asked.append(key)
        status, body = self.answers.get(key, (200, contract("refused.json")))
        return httpx.Response(status, json=body)


class ConnectedStore:
    """OAuth store holding connections saved earlier under router user ids."""

    def __init__(self, user_ids: set[int]) -> None:
        self.user_ids = user_ids
        self.asked: list[int] = []

    def is_connected(self, user_id: int, provider: str = "google") -> bool:
        self.asked.append(user_id)
        return user_id in self.user_ids

    def is_discord_bot_connected(self, user_id: int) -> bool:
        self.asked.append(user_id)
        return user_id in self.user_ids

    def get_discord_bot_connection(self, user_id: int):
        if user_id not in self.user_ids:
            return None
        return StoredDiscordBotConnection(
            user_id=user_id, bot_token="t", application_id="app-1", bot_user_id="b-1"
        )


@pytest.fixture
def app_with_router(monkeypatch):
    """Reload the app with Key Check pointed at a fake router."""

    def build(router: FakeRouter):
        monkeypatch.delenv("MCP_DEV_BYPASS_KEY", raising=False)
        monkeypatch.delenv("FLY_APP_NAME", raising=False)
        monkeypatch.delenv("DATABASE_URL", raising=False)
        monkeypatch.setenv("DISCORD_GUILD_ID", "guild-1")
        import vans_mcp_server.app as app_module

        importlib.reload(app_module)
        app_module.auth.key_check = KeyCheckClient(
            vcr_url="https://router.test",
            service_credential="svc",
            transport=httpx.MockTransport(router),
        )
        return app_module

    return build


def _rpc_result(response: httpx.Response) -> dict:
    if response.headers.get("content-type", "").startswith("text/event-stream"):
        for line in response.text.splitlines():
            if line.startswith("data: "):
                return json.loads(line[len("data: ") :])
        raise AssertionError(response.text)
    return response.json()


def _call_tool(
    client: TestClient, key: str, tool: str, arguments: dict | None = None
) -> dict:
    headers = {
        "Authorization": f"Bearer {key}",
        "Accept": ACCEPT,
        "Content-Type": "application/json",
    }
    init = client.post("/mcp/", headers=headers, json=INITIALIZE)
    assert init.status_code == 200, init.text
    session = init.headers.get("mcp-session-id")
    if session:
        headers["mcp-session-id"] = session
    client.post(
        "/mcp/",
        headers=headers,
        json={"jsonrpc": "2.0", "method": "notifications/initialized"},
    )
    res = client.post(
        "/mcp/",
        headers=headers,
        json={
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": tool, "arguments": arguments or {}},
        },
    )
    assert res.status_code == 200, res.text
    result = _rpc_result(res)["result"]
    return json.loads(result["content"][0]["text"])


def test_classroom_key_reads_the_students_existing_google_connection(app_with_router):
    router = FakeRouter({STUDENT_KEY: (200, contract("classroom-accepted.json"))})
    app_module = app_with_router(router)
    app_module.oauth_store = ConnectedStore({1234})

    with TestClient(app_module.app) as client:
        status = _call_tool(client, STUDENT_KEY, "google_get_connect_url")

    assert status["connected"] is True
    assert app_module.oauth_store.asked == [1234]


def test_personal_key_reads_the_teachers_existing_discord_connection(app_with_router):
    router = FakeRouter({TEACHER_KEY: (200, contract("personal-accepted.json"))})
    app_module = app_with_router(router)
    app_module.oauth_store = ConnectedStore({42})

    with TestClient(app_module.app) as client:
        status = _call_tool(client, TEACHER_KEY, "discord_get_connect_url")

    assert status["connected"] is True
    assert status["application_id"] == "app-1"


def _initialize(client: TestClient, key: str | None) -> httpx.Response:
    headers = {"Accept": ACCEPT, "Content-Type": "application/json"}
    if key is not None:
        headers["Authorization"] = f"Bearer {key}"
    return client.post("/mcp/", headers=headers, json=INITIALIZE)


def _assert_plain_401(res: httpx.Response, message: str) -> None:
    assert res.status_code == 401
    assert "www-authenticate" not in {k.lower() for k in res.headers.keys()}
    assert res.json()["message"] == message
    assert int(res.headers["content-length"]) == len(res.content)


def test_refused_key_gets_a_plain_401_with_the_router_message(app_with_router):
    router = FakeRouter({STUDENT_KEY: (200, contract("refused.json"))})
    app_module = app_with_router(router)

    with TestClient(app_module.app) as client:
        res = _initialize(client, STUDENT_KEY)

    _assert_plain_401(res, "無效的 API 金鑰")


def test_class_not_started_is_refused_with_the_router_wording(app_with_router):
    not_started = dict(
        contract("refused.json"),
        reason="not_started",
        message="課堂尚未開始，請在開始時間後再使用",
    )
    app_module = app_with_router(FakeRouter({STUDENT_KEY: (200, not_started)}))

    with TestClient(app_module.app) as client:
        res = _initialize(client, STUDENT_KEY)

    _assert_plain_401(res, "課堂尚未開始，請在開始時間後再使用")


def test_router_down_gets_a_plain_401_saying_try_again_later(app_with_router):
    app_module = app_with_router(FakeRouter({STUDENT_KEY: (503, {"detail": "x"})}))

    with TestClient(app_module.app) as client:
        res = _initialize(client, STUDENT_KEY)

    _assert_plain_401(res, "驗證服務暫時無法使用，請稍後再試")


def test_missing_authorization_gets_a_plain_401(app_with_router):
    router = FakeRouter({})
    app_module = app_with_router(router)

    with TestClient(app_module.app) as client:
        res = _initialize(client, None)

    assert res.status_code == 401
    assert "www-authenticate" not in {k.lower() for k in res.headers.keys()}
    assert int(res.headers["content-length"]) == len(res.content)
    assert router.asked == []


def test_tool_usage_is_recorded_without_an_api_key_id(app_with_router, caplog):
    router = FakeRouter({STUDENT_KEY: (200, contract("classroom-accepted.json"))})
    app_module = app_with_router(router)
    caplog.set_level(logging.INFO, logger="vans_mcp_server.usage")

    with TestClient(app_module.app) as client:
        _call_tool(client, STUDENT_KEY, "notion_search_pages", {"query": "portal"})

    rows = [
        r.args
        for r in caplog.records
        if r.name == "vans_mcp_server.usage" and r.msg == "mcp_usage %s"
    ]
    assert rows, "the tool call was not recorded"
    row = rows[-1] if isinstance(rows[-1], dict) else rows[-1][0]
    assert row["tool_name"] == "notion_search_pages"
    assert row["user_id"] == 1234
    assert "api_key_id" not in row
