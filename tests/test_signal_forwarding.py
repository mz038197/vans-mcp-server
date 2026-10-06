"""Production ERROR logs under vans_mcp_server are posted once to vans-signals."""

from __future__ import annotations

import importlib
import json
import logging
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
import pytest
from google.auth.exceptions import RefreshError, TransportError
from googleapiclient.errors import HttpError
from starlette.testclient import TestClient

from vans_mcp_server.oauth.crypto import TokenEncryptor
from vans_mcp_server.oauth.google import (
    GMAIL_COMPOSE_SCOPE,
    GMAIL_MODIFY_SCOPE,
    GMAIL_READONLY_SCOPE,
)
from vans_mcp_server.oauth.store import StoredDiscordBotConnection

MCP_SOURCE = "vans-mcp-server"
FULL_GMAIL = f"{GMAIL_READONLY_SCOPE} {GMAIL_COMPOSE_SCOPE} {GMAIL_MODIFY_SCOPE}"


class _RecordingSignals(ThreadingHTTPServer):
    def __init__(self):
        super().__init__(("127.0.0.1", 0), _SignalsHandler)
        self.posts: list[dict] = []
        self.hold_response = threading.Event()
        self.release_response = threading.Event()
        self.status = 200


class _SignalsHandler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length)
        server: _RecordingSignals = self.server  # type: ignore[assignment]
        server.posts.append(
            {
                "path": self.path,
                "authorization": self.headers.get("Authorization"),
                "body": json.loads(raw.decode("utf-8")),
            }
        )
        if server.hold_response.is_set():
            server.release_response.wait(timeout=5)
        self.send_response(server.status)
        self.end_headers()

    def log_message(self, fmt: str, *args) -> None:
        return


class _SignalsReceiver:
    def __init__(self) -> None:
        self._server = _RecordingSignals()
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    @property
    def url(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    @property
    def posts(self) -> list[dict]:
        return self._server.posts

    def close(self) -> None:
        self._server.release_response.set()
        self._server.shutdown()
        self._thread.join(timeout=2)
        self._server.server_close()


def _wait_until(predicate, timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("timed out")


def _example() -> dict:
    return json.loads(
        Path("tests/fixtures/signal_body.example.json").read_text(encoding="utf-8")
    )


def _assert_signal_body(body: dict) -> None:
    example = _example()
    assert body.keys() == example.keys()
    assert len(body) == 5
    assert body["level"] == "ERROR"
    assert body["source"] == MCP_SOURCE
    logged_at = datetime.fromisoformat(body["log_time"])
    assert logged_at.tzinfo is not None and logged_at.utcoffset() is not None


def _close_forwarder(app_module) -> None:
    forwarder = getattr(app_module, "_signal_forwarder", None)
    if forwarder is not None:
        forwarder.close()


def _reload_with_signals(monkeypatch: pytest.MonkeyPatch, receiver: _SignalsReceiver, **extra: str):
    monkeypatch.setenv("VANS_SIGNALS_URL", receiver.url)
    monkeypatch.setenv("VANS_SIGNALS_TOKEN", "mcp-token")
    monkeypatch.setenv("VANS_SIGNALS_SOURCE", MCP_SOURCE)
    monkeypatch.setenv("MCP_DEV_BYPASS_KEY", "vcr_sk_dev_local_only")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    for key, value in extra.items():
        monkeypatch.setenv(key, value)
    import vans_mcp_server.app as app_module

    importlib.reload(app_module)
    return app_module


@pytest.fixture
def signals(monkeypatch):
    receiver = _SignalsReceiver()
    app_module = None
    try:
        app_module = _reload_with_signals(monkeypatch, receiver)
        yield app_module, receiver
    finally:
        if app_module is not None:
            _close_forwarder(app_module)
        receiver.close()


def _bypass_auth(app_module, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(app_module, "_require_user_id", lambda: 1)
    monkeypatch.setattr(app_module, "_user_id", lambda: 1)


def _google_ready(app_module, *, scopes: str = FULL_GMAIL) -> MagicMock:
    store = MagicMock()
    store.is_connected.return_value = True
    store.get_granted_scopes.return_value = scopes
    store.get_valid_access_token.return_value = MagicMock(
        access_token="access",
        refresh_token="refresh",
        scopes=scopes,
    )
    app_module.oauth_store = store
    oauth = MagicMock()
    oauth.is_configured.return_value = True
    oauth.client_id = "cid"
    oauth.client_secret = "csecret"
    oauth.create_connect_state.return_value = "state"
    app_module.google_oauth = oauth
    return store


def _http_error(status: int, body: bytes = b'{"error": {"message": "boom"}}') -> HttpError:
    resp = MagicMock()
    resp.status = status
    resp.reason = "error"
    return HttpError(resp, body)


def _httpx_status_error(
    status: int,
    body: str,
    url: str = "https://oauth2.googleapis.com/token",
) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", url)
    response = httpx.Response(status, request=request, content=body.encode("utf-8"))
    return httpx.HTTPStatusError(f"{status}", request=request, response=response)


def _assert_no_signal(receiver: _SignalsReceiver) -> None:
    time.sleep(0.2)
    assert receiver.posts == []


def test_posted_json_matches_the_example_keys(signals):
    _app_module, receiver = signals
    logging.getLogger("vans_mcp_server").error("upstream openrouter did not update")
    _wait_until(lambda: len(receiver.posts) >= 1)
    post = receiver.posts[0]
    assert post["path"] == "/signals"
    assert post["authorization"] == "Bearer mcp-token"
    _assert_signal_body(post["body"])
    assert post["body"]["logger_name"] == "vans_mcp_server"
    assert post["body"]["message"] == "upstream openrouter did not update"


def test_blank_or_missing_source_posts_nothing(monkeypatch):
    receiver = _SignalsReceiver()
    try:
        monkeypatch.setenv("VANS_SIGNALS_URL", receiver.url)
        monkeypatch.setenv("VANS_SIGNALS_TOKEN", "mcp-token")
        monkeypatch.setenv("VANS_SIGNALS_SOURCE", "   ")
        monkeypatch.setenv("MCP_DEV_BYPASS_KEY", "vcr_sk_dev_local_only")
        monkeypatch.delenv("DATABASE_URL", raising=False)
        import vans_mcp_server.app as app_module

        importlib.reload(app_module)
        logging.getLogger("vans_mcp_server").error("should stay local")
        _assert_no_signal(receiver)
        _close_forwarder(app_module)
    finally:
        receiver.close()


def test_google_5xx_on_calendar_list_posts_a_signal(signals, monkeypatch):
    app_module, receiver = signals
    _bypass_auth(app_module, monkeypatch)
    _google_ready(app_module)
    with patch(
        "vans_mcp_server.tools.calendar.list_events",
        side_effect=_http_error(503),
    ):
        with pytest.raises(HttpError):
            app_module.calendar_list_events(
                "2026-01-01T00:00:00Z", "2026-01-02T00:00:00Z"
            )
    _wait_until(lambda: len(receiver.posts) >= 1)
    assert len(receiver.posts) == 1
    _assert_signal_body(receiver.posts[0]["body"])


def test_connection_failure_on_calendar_list_posts_a_signal(signals, monkeypatch):
    app_module, receiver = signals
    _bypass_auth(app_module, monkeypatch)
    _google_ready(app_module)
    with patch(
        "vans_mcp_server.tools.calendar.list_events",
        side_effect=httpx.ConnectError("google unreachable"),
    ):
        with pytest.raises(httpx.ConnectError):
            app_module.calendar_list_events(
                "2026-01-01T00:00:00Z", "2026-01-02T00:00:00Z"
            )
    _wait_until(lambda: len(receiver.posts) >= 1)
    _assert_signal_body(receiver.posts[0]["body"])


def test_google_auth_transport_error_posts_a_signal(signals, monkeypatch):
    app_module, receiver = signals
    _bypass_auth(app_module, monkeypatch)
    _google_ready(app_module)
    with patch(
        "vans_mcp_server.tools.calendar.list_events",
        side_effect=TransportError("token transport failed"),
    ):
        with pytest.raises(TransportError):
            app_module.calendar_list_events(
                "2026-01-01T00:00:00Z", "2026-01-02T00:00:00Z"
            )
    _wait_until(lambda: len(receiver.posts) >= 1)
    _assert_signal_body(receiver.posts[0]["body"])


def test_google_401_invalid_client_posts_a_signal(signals, monkeypatch):
    app_module, receiver = signals
    _bypass_auth(app_module, monkeypatch)
    _google_ready(app_module)
    err = _httpx_status_error(401, '{"error":"invalid_client"}')
    with patch("vans_mcp_server.tools.calendar.list_events", side_effect=err):
        with pytest.raises(httpx.HTTPStatusError):
            app_module.calendar_list_events(
                "2026-01-01T00:00:00Z", "2026-01-02T00:00:00Z"
            )
    _wait_until(lambda: len(receiver.posts) >= 1)
    _assert_signal_body(receiver.posts[0]["body"])


def test_google_429_posts_a_signal(signals, monkeypatch):
    app_module, receiver = signals
    _bypass_auth(app_module, monkeypatch)
    _google_ready(app_module)
    with patch(
        "vans_mcp_server.tools.calendar.list_events",
        side_effect=_http_error(429, b'{"error":{"message":"rateLimitExceeded"}}'),
    ):
        with pytest.raises(HttpError):
            app_module.calendar_list_events(
                "2026-01-01T00:00:00Z", "2026-01-02T00:00:00Z"
            )
    _wait_until(lambda: len(receiver.posts) >= 1)
    _assert_signal_body(receiver.posts[0]["body"])


def test_unexpected_tool_exception_posts_a_signal(signals, monkeypatch):
    app_module, receiver = signals
    _bypass_auth(app_module, monkeypatch)
    _google_ready(app_module)
    with patch(
        "vans_mcp_server.tools.calendar.list_events",
        side_effect=RuntimeError("calendar helper exploded"),
    ):
        with pytest.raises(RuntimeError, match="calendar helper exploded"):
            app_module.calendar_list_events(
                "2026-01-01T00:00:00Z", "2026-01-02T00:00:00Z"
            )
    _wait_until(lambda: len(receiver.posts) >= 1)
    message = receiver.posts[0]["body"]["message"]
    assert "calendar helper exploded" in message
    _assert_signal_body(receiver.posts[0]["body"])


@pytest.mark.parametrize("status", [400, 403, 404, 409])
def test_other_google_4xx_posts_no_signal(signals, monkeypatch, status):
    app_module, receiver = signals
    _bypass_auth(app_module, monkeypatch)
    _google_ready(app_module)
    with patch(
        "vans_mcp_server.tools.calendar.list_events",
        side_effect=_http_error(status),
    ):
        with pytest.raises(HttpError):
            app_module.calendar_list_events(
                "2026-01-01T00:00:00Z", "2026-01-02T00:00:00Z"
            )
    _assert_no_signal(receiver)


def test_refresh_invalid_grant_posts_no_signal(signals, monkeypatch):
    app_module, receiver = signals
    _bypass_auth(app_module, monkeypatch)
    _google_ready(app_module)
    grant = _httpx_status_error(400, '{"error":"invalid_grant"}')
    with patch("vans_mcp_server.tools.calendar.list_events", side_effect=grant):
        with pytest.raises(httpx.HTTPStatusError):
            app_module.calendar_list_events(
                "2026-01-01T00:00:00Z", "2026-01-02T00:00:00Z"
            )
    _assert_no_signal(receiver)

    with patch(
        "vans_mcp_server.tools.calendar.list_events",
        side_effect=RefreshError("invalid_grant"),
    ):
        with pytest.raises(RefreshError):
            app_module.calendar_list_events(
                "2026-01-01T00:00:00Z", "2026-01-02T00:00:00Z"
            )
    _assert_no_signal(receiver)


def test_not_connected_and_missing_args_post_no_signal(signals, monkeypatch):
    app_module, receiver = signals
    _bypass_auth(app_module, monkeypatch)
    out = app_module.calendar_list_events(
        "2026-01-01T00:00:00Z", "2026-01-02T00:00:00Z"
    )
    payload = json.loads(out)
    assert payload["error"] == "not_connected"
    with pytest.raises(ValueError, match="event_id"):
        app_module.calendar_delete_event("  ", confirm=True)
    _assert_no_signal(receiver)


def test_gmail_missing_scopes_posts_no_signal(signals, monkeypatch):
    app_module, receiver = signals
    _bypass_auth(app_module, monkeypatch)
    _google_ready(app_module, scopes="https://www.googleapis.com/auth/calendar")
    out = app_module.gmail_search_messages("in:inbox")
    payload = json.loads(out)
    assert payload["error"] == "missing_scopes"
    _assert_no_signal(receiver)


def test_gmail_batch_inner_5xx_stays_in_result_with_no_signal(signals, monkeypatch):
    app_module, receiver = signals
    _bypass_auth(app_module, monkeypatch)
    _google_ready(app_module)
    fake_service = MagicMock()

    def trash_side_effect(*, userId, id):
        mock = MagicMock()
        mock.execute.side_effect = _http_error(503)
        return mock

    fake_service.users.return_value.messages.return_value.trash.side_effect = (
        trash_side_effect
    )
    with patch("vans_mcp_server.tools.gmail._gmail_service", return_value=fake_service):
        out = app_module.gmail_trash_message(["m1"], confirm=True)
    payload = json.loads(out)
    assert payload["failed"][0]["id"] == "m1"
    assert payload["trashed"] is False
    _assert_no_signal(receiver)


def test_invalid_api_key_posts_no_signal(signals):
    app_module, receiver = signals
    with TestClient(app_module.app) as client:
        res = client.post(
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
        assert res.status_code in (401, 403)
    _assert_no_signal(receiver)


def test_student_denying_google_consent_posts_no_signal(monkeypatch):
    receiver = _SignalsReceiver()
    app_module = None
    try:
        app_module = _reload_with_signals(
            monkeypatch,
            receiver,
            PUBLIC_URL="http://127.0.0.1:8080",
            GOOGLE_CLIENT_ID="cid",
            GOOGLE_CLIENT_SECRET="csecret",
            SESSION_SECRET="session-secret-for-tests",
        )
        app_module.oauth_store = MagicMock()
        oauth = app_module.google_oauth
        assert oauth is not None
        state = oauth.create_connect_state(1)
        with TestClient(app_module.app) as client:
            res = client.get(
                f"/connect/google/callback?state={state}&error=access_denied"
            )
        assert res.status_code == 400
        assert "access_denied" in res.text
        _assert_no_signal(receiver)
    finally:
        if app_module is not None:
            _close_forwarder(app_module)
        receiver.close()


def test_google_callback_invalid_grant_posts_no_signal(monkeypatch):
    receiver = _SignalsReceiver()
    app_module = None
    key = TokenEncryptor.generate_key()
    try:
        app_module = _reload_with_signals(
            monkeypatch,
            receiver,
            PUBLIC_URL="http://127.0.0.1:8080",
            GOOGLE_CLIENT_ID="cid",
            GOOGLE_CLIENT_SECRET="csecret",
            SESSION_SECRET="session-secret-for-tests",
            OAUTH_TOKEN_ENCRYPTION_KEY=key,
        )
        app_module.oauth_store = MagicMock()
        oauth = app_module.google_oauth
        assert oauth is not None
        state = oauth.create_connect_state(7)
        grant = _httpx_status_error(400, '{"error":"invalid_grant"}')
        with patch.object(oauth, "exchange_code", side_effect=grant):
            with TestClient(app_module.app) as client:
                res = client.get(
                    f"/connect/google/callback?state={state}&code=expired"
                )
        assert res.status_code == 500
        _assert_no_signal(receiver)
    finally:
        if app_module is not None:
            _close_forwarder(app_module)
        receiver.close()


def _discord_connect_ready(app_module) -> None:
    app_module.oauth_store = MagicMock()


def test_discord_invalid_bot_token_posts_no_signal(monkeypatch):
    receiver = _SignalsReceiver()
    app_module = None
    key = TokenEncryptor.generate_key()
    try:
        app_module = _reload_with_signals(
            monkeypatch,
            receiver,
            PUBLIC_URL="http://127.0.0.1:8080",
            SESSION_SECRET="session-secret-for-tests",
            DISCORD_GUILD_ID="guild1",
            OAUTH_TOKEN_ENCRYPTION_KEY=key,
        )
        _discord_connect_ready(app_module)
        state = app_module.discord_connect.create_connect_state(3)
        err = _httpx_status_error(
            401,
            '{"message":"401: Unauthorized"}',
            url="https://discord.com/api/v10/users/@me",
        )
        with patch(
            "vans_mcp_server.tools.discord.verify_bot_token",
            side_effect=err,
        ):
            with TestClient(app_module.app) as client:
                res = client.post(
                    "/connect/discord/submit",
                    data={
                        "state": state,
                        "application_id": "app1",
                        "bot_token": "bad-token",
                    },
                )
        assert res.status_code == 400
        assert "Invalid Bot Token" in res.text
        _assert_no_signal(receiver)
    finally:
        if app_module is not None:
            _close_forwarder(app_module)
        receiver.close()


def test_discord_connect_5xx_posts_a_signal_and_tells_the_student(monkeypatch):
    receiver = _SignalsReceiver()
    app_module = None
    key = TokenEncryptor.generate_key()
    try:
        app_module = _reload_with_signals(
            monkeypatch,
            receiver,
            PUBLIC_URL="http://127.0.0.1:8080",
            SESSION_SECRET="session-secret-for-tests",
            DISCORD_GUILD_ID="guild1",
            OAUTH_TOKEN_ENCRYPTION_KEY=key,
        )
        _discord_connect_ready(app_module)
        state = app_module.discord_connect.create_connect_state(3)
        err = _httpx_status_error(
            503,
            '{"message":"service unavailable"}',
            url="https://discord.com/api/v10/users/@me",
        )
        with patch(
            "vans_mcp_server.tools.discord.verify_bot_token",
            side_effect=err,
        ):
            with TestClient(app_module.app) as client:
                res = client.post(
                    "/connect/discord/submit",
                    data={
                        "state": state,
                        "application_id": "app1",
                        "bot_token": "token",
                    },
                )
        assert "Discord 暫時有問題" in res.text
        assert "Invalid Bot Token" not in res.text
        _wait_until(lambda: len(receiver.posts) >= 1)
        _assert_signal_body(receiver.posts[0]["body"])
    finally:
        if app_module is not None:
            _close_forwarder(app_module)
        receiver.close()


def test_bot_in_guild_5xx_posts_a_signal_and_is_not_not_in_guild(signals, monkeypatch):
    app_module, receiver = signals
    _bypass_auth(app_module, monkeypatch)
    monkeypatch.setenv("DISCORD_GUILD_ID", "guild1")
    store = MagicMock()
    store.get_discord_bot_connection.return_value = StoredDiscordBotConnection(
        user_id=1,
        bot_token="bot-token-secret",
        application_id="app1",
        bot_user_id="botuser1",
    )
    app_module.oauth_store = store

    guilds_resp = MagicMock()
    guilds_resp.status_code = 503
    guilds_resp.raise_for_status.side_effect = _httpx_status_error(
        503,
        '{"message":"unavailable"}',
        url="https://discord.com/api/v10/users/@me/guilds",
    )

    fake_client = MagicMock()
    fake_client.__enter__.return_value = fake_client
    fake_client.__exit__.return_value = None
    fake_client.get.return_value = guilds_resp

    with patch("vans_mcp_server.tools.discord.httpx.Client", return_value=fake_client):
        with pytest.raises(httpx.HTTPStatusError):
            app_module.discord_list_channels()
    _wait_until(lambda: len(receiver.posts) >= 1)
    _assert_signal_body(receiver.posts[0]["body"])
    assert all(
        "not_in_guild" not in (post["body"]["message"] or "") for post in receiver.posts
    )
