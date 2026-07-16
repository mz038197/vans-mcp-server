from __future__ import annotations

from unittest.mock import MagicMock, patch

from vans_mcp_server.oauth.google import (
    GMAIL_COMPOSE_SCOPE,
    GMAIL_MODIFY_SCOPE,
    GMAIL_READONLY_SCOPE,
    GoogleOAuthService,
    scopes_include,
)
from vans_mcp_server.tools import gmail as gmail_tools

_BASE_GMAIL = f"{GMAIL_READONLY_SCOPE} {GMAIL_COMPOSE_SCOPE}"
_FULL_GMAIL = f"{_BASE_GMAIL} {GMAIL_MODIFY_SCOPE}"


def test_scopes_include_gmail():
    granted = (
        "openid email profile https://www.googleapis.com/auth/calendar "
        f"{_FULL_GMAIL}"
    )
    assert scopes_include(
        granted,
        (GMAIL_READONLY_SCOPE, GMAIL_COMPOSE_SCOPE),
    )
    assert scopes_include(
        granted,
        (GMAIL_READONLY_SCOPE, GMAIL_COMPOSE_SCOPE, GMAIL_MODIFY_SCOPE),
    )
    assert not scopes_include(
        "openid email https://www.googleapis.com/auth/calendar",
        (GMAIL_READONLY_SCOPE,),
    )


def test_authorize_url_includes_gmail_modify():
    oauth = GoogleOAuthService(
        client_id="cid",
        client_secret="csecret",
        redirect_uri="http://127.0.0.1:8080/connect/google/callback",
        session_secret="session-secret-for-tests",
    )
    url = oauth.authorize_url("abc")
    assert "gmail.readonly" in url
    assert "gmail.compose" in url
    assert "gmail.modify" in url
    assert "calendar" in url
    assert "auth%2Ftasks" in url


def test_send_email_requires_confirm():
    store = MagicMock()
    oauth = GoogleOAuthService(
        client_id="cid",
        client_secret="csecret",
        redirect_uri="http://127.0.0.1:8080/connect/google/callback",
        session_secret="session-secret-for-tests",
    )
    result = gmail_tools.send_email(
        user_id=1,
        store=store,
        oauth=oauth,
        to="a@example.com",
        subject="hi",
        body="body",
        confirm=False,
    )
    assert result["error"] == "confirmation_required"
    assert result["sent"] is False
    store.get_valid_access_token.assert_not_called()


def test_send_email_works_without_modify_scope():
    """Read/compose users must not be blocked by missing gmail.modify."""
    store = MagicMock()
    store.get_valid_access_token.return_value = MagicMock(
        access_token="access",
        refresh_token="refresh",
        scopes=_BASE_GMAIL,
    )
    oauth = GoogleOAuthService(
        client_id="cid",
        client_secret="csecret",
        redirect_uri="http://127.0.0.1:8080/connect/google/callback",
        session_secret="session-secret-for-tests",
    )
    fake_service = MagicMock()
    fake_service.users.return_value.messages.return_value.send.return_value.execute.return_value = {
        "id": "m1",
        "threadId": "t1",
        "labelIds": ["SENT"],
    }
    with patch("vans_mcp_server.tools.gmail._gmail_service", return_value=fake_service):
        result = gmail_tools.send_email(
            user_id=1,
            store=store,
            oauth=oauth,
            to="a@example.com",
            subject="hi",
            body="body",
            confirm=True,
        )
    assert result["sent"] is True
    assert result["id"] == "m1"


def test_create_draft_calls_api():
    store = MagicMock()
    store.get_valid_access_token.return_value = MagicMock(
        access_token="access",
        refresh_token="refresh",
        scopes=_BASE_GMAIL,
    )
    oauth = GoogleOAuthService(
        client_id="cid",
        client_secret="csecret",
        redirect_uri="http://127.0.0.1:8080/connect/google/callback",
        session_secret="session-secret-for-tests",
    )
    fake_service = MagicMock()
    fake_service.users.return_value.drafts.return_value.create.return_value.execute.return_value = {
        "id": "d1",
        "message": {"id": "m1", "threadId": "t1"},
    }
    with patch("vans_mcp_server.tools.gmail._gmail_service", return_value=fake_service):
        result = gmail_tools.create_draft(
            user_id=1,
            store=store,
            oauth=oauth,
            to="a@example.com",
            subject="draft",
            body="hello",
        )
    assert result["created"] is True
    assert result["draft_id"] == "d1"


def test_trash_requires_confirm():
    store = MagicMock()
    oauth = GoogleOAuthService(
        client_id="cid",
        client_secret="csecret",
        redirect_uri="http://127.0.0.1:8080/connect/google/callback",
        session_secret="session-secret-for-tests",
    )
    result = gmail_tools.trash_message(
        user_id=1,
        store=store,
        oauth=oauth,
        message_id="m99",
        confirm=False,
    )
    assert result["error"] == "confirmation_required"
    assert result["trashed"] is False
    store.get_valid_access_token.assert_not_called()


def test_trash_requires_modify_scope():
    store = MagicMock()
    store.get_valid_access_token.return_value = MagicMock(
        access_token="access",
        refresh_token="refresh",
        scopes=_BASE_GMAIL,
    )
    oauth = GoogleOAuthService(
        client_id="cid",
        client_secret="csecret",
        redirect_uri="http://127.0.0.1:8080/connect/google/callback",
        session_secret="session-secret-for-tests",
    )
    try:
        gmail_tools.trash_message(
            user_id=1,
            store=store,
            oauth=oauth,
            message_id="m99",
            confirm=True,
        )
        assert False, "expected PermissionError"
    except PermissionError:
        pass


def test_trash_with_confirm_calls_api():
    store = MagicMock()
    store.get_valid_access_token.return_value = MagicMock(
        access_token="access",
        refresh_token="refresh",
        scopes=_FULL_GMAIL,
    )
    oauth = GoogleOAuthService(
        client_id="cid",
        client_secret="csecret",
        redirect_uri="http://127.0.0.1:8080/connect/google/callback",
        session_secret="session-secret-for-tests",
    )
    fake_service = MagicMock()
    fake_service.users.return_value.messages.return_value.trash.return_value.execute.return_value = {
        "id": "m99",
        "threadId": "t9",
        "labelIds": ["TRASH"],
    }
    with patch("vans_mcp_server.tools.gmail._gmail_service", return_value=fake_service):
        result = gmail_tools.trash_message(
            user_id=1,
            store=store,
            oauth=oauth,
            message_id="m99",
            confirm=True,
        )
    assert result["trashed"] is True
    assert result["id"] == "m99"


def test_search_missing_scopes_raises():
    store = MagicMock()
    store.get_valid_access_token.return_value = MagicMock(
        access_token="access",
        refresh_token="refresh",
        scopes="https://www.googleapis.com/auth/calendar",
    )
    oauth = GoogleOAuthService(
        client_id="cid",
        client_secret="csecret",
        redirect_uri="http://127.0.0.1:8080/connect/google/callback",
        session_secret="session-secret-for-tests",
    )
    try:
        gmail_tools.search_messages(
            user_id=1, store=store, oauth=oauth, query="in:inbox"
        )
        assert False, "expected PermissionError"
    except PermissionError:
        pass
