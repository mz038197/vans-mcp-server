from __future__ import annotations

from unittest.mock import MagicMock, patch

from googleapiclient.errors import HttpError

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

_LABELS = {
    "labels": [
        {"id": "INBOX", "name": "INBOX", "type": "system"},
        {"id": "UNREAD", "name": "UNREAD", "type": "system"},
        {"id": "TRASH", "name": "TRASH", "type": "system"},
        {"id": "Label_1", "name": "作業", "type": "user"},
        {"id": "Label_2", "name": "待辦", "type": "user"},
    ]
}


def _oauth() -> GoogleOAuthService:
    return GoogleOAuthService(
        client_id="cid",
        client_secret="csecret",
        redirect_uri="http://127.0.0.1:8080/connect/google/callback",
        session_secret="session-secret-for-tests",
    )


def _store(scopes: str = _FULL_GMAIL) -> MagicMock:
    store = MagicMock()
    store.get_valid_access_token.return_value = MagicMock(
        access_token="access",
        refresh_token="refresh",
        scopes=scopes,
    )
    return store


def _http_error(status: int = 404) -> HttpError:
    resp = MagicMock()
    resp.status = status
    resp.reason = "Not Found"
    return HttpError(resp, b'{"error": {"message": "Not Found"}}')


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
        message_ids=["m99"],
        confirm=False,
    )
    assert result["error"] == "confirmation_required"
    assert result["trashed"] is False
    assert result["message_ids"] == ["m99"]
    store.get_valid_access_token.assert_not_called()


def test_trash_requires_modify_scope():
    store = _store(_BASE_GMAIL)
    try:
        gmail_tools.trash_message(
            user_id=1,
            store=store,
            oauth=_oauth(),
            message_ids=["m99"],
            confirm=True,
        )
        assert False, "expected PermissionError"
    except PermissionError:
        pass


def test_trash_with_confirm_calls_api():
    store = _store()
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
            oauth=_oauth(),
            message_ids=["m99"],
            confirm=True,
        )
    assert result["trashed"] is True
    assert result["succeeded"] == [{"id": "m99"}]
    assert result["failed"] == []


def test_trash_batch_records_per_message_failure():
    store = _store()
    fake_service = MagicMock()

    def trash_side_effect(*, userId, id):
        mock = MagicMock()
        if id == "bad":
            mock.execute.side_effect = _http_error()
        else:
            mock.execute.return_value = {"id": id, "labelIds": ["TRASH"]}
        return mock

    fake_service.users.return_value.messages.return_value.trash.side_effect = (
        trash_side_effect
    )
    with patch("vans_mcp_server.tools.gmail._gmail_service", return_value=fake_service):
        result = gmail_tools.trash_message(
            user_id=1,
            store=store,
            oauth=_oauth(),
            message_ids=["m1", "bad"],
            confirm=True,
        )
    assert result["succeeded"] == [{"id": "m1"}]
    assert result["failed"][0]["id"] == "bad"
    assert result["trashed"] is False


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


def test_search_returns_unread_and_user_label_names():
    store = _store(_BASE_GMAIL)
    fake_service = MagicMock()
    messages = fake_service.users.return_value.messages.return_value
    messages.list.return_value.execute.return_value = {
        "messages": [{"id": "m1", "threadId": "t1"}]
    }
    messages.get.return_value.execute.return_value = {
        "id": "m1",
        "threadId": "t1",
        "snippet": "hi",
        "labelIds": ["INBOX", "UNREAD", "Label_1"],
        "payload": {
            "headers": [
                {"name": "From", "value": "a@example.com"},
                {"name": "To", "value": "b@example.com"},
                {"name": "Subject", "value": "作業"},
                {"name": "Date", "value": "Thu, 27 Aug 2026"},
            ]
        },
    }
    fake_service.users.return_value.labels.return_value.list.return_value.execute.return_value = (
        _LABELS
    )
    with patch("vans_mcp_server.tools.gmail._gmail_service", return_value=fake_service):
        result = gmail_tools.search_messages(
            user_id=1, store=store, oauth=_oauth(), query="is:unread"
        )
    assert result["messages"][0]["unread"] is True
    assert result["messages"][0]["labels"] == ["作業"]


def test_mark_read_removes_unread_without_confirm():
    store = _store()
    fake_service = MagicMock()
    modify = fake_service.users.return_value.messages.return_value.modify
    modify.return_value.execute.return_value = {"id": "m1", "labelIds": ["INBOX"]}
    with patch("vans_mcp_server.tools.gmail._gmail_service", return_value=fake_service):
        result = gmail_tools.mark_read(
            user_id=1, store=store, oauth=_oauth(), message_ids=["m1"]
        )
    assert result["succeeded"] == [{"id": "m1"}]
    assert result["failed"] == []
    modify.assert_called_once_with(
        userId="me",
        id="m1",
        body={"removeLabelIds": ["UNREAD"]},
    )
    store.get_valid_access_token.assert_called()


def test_mark_read_requires_modify_scope():
    store = _store(_BASE_GMAIL)
    try:
        gmail_tools.mark_read(
            user_id=1, store=store, oauth=_oauth(), message_ids=["m1"]
        )
        assert False, "expected PermissionError"
    except PermissionError:
        pass


def test_mark_read_rejects_empty_and_oversized_batch():
    store = _store()
    empty = gmail_tools.mark_read(
        user_id=1, store=store, oauth=_oauth(), message_ids=[]
    )
    assert empty["error"] == "invalid_message_ids"
    too_many = gmail_tools.mark_read(
        user_id=1,
        store=store,
        oauth=_oauth(),
        message_ids=[f"m{i}" for i in range(26)],
    )
    assert too_many["error"] == "batch_too_large"
    store.get_valid_access_token.assert_not_called()


def test_mark_read_dedupes_and_records_per_message_failure():
    store = _store()
    fake_service = MagicMock()

    def modify_side_effect(*, userId, id, body):
        mock = MagicMock()
        if id == "bad":
            mock.execute.side_effect = _http_error()
        else:
            mock.execute.return_value = {"id": id}
        return mock

    fake_service.users.return_value.messages.return_value.modify.side_effect = (
        modify_side_effect
    )
    with patch("vans_mcp_server.tools.gmail._gmail_service", return_value=fake_service):
        result = gmail_tools.mark_read(
            user_id=1,
            store=store,
            oauth=_oauth(),
            message_ids=["m1", "m1", "bad"],
        )
    assert result["succeeded"] == [{"id": "m1"}]
    assert result["failed"][0]["id"] == "bad"
    assert fake_service.users.return_value.messages.return_value.modify.call_count == 2


def test_mark_unread_adds_unread_label():
    store = _store()
    fake_service = MagicMock()
    modify = fake_service.users.return_value.messages.return_value.modify
    modify.return_value.execute.return_value = {
        "id": "m1",
        "labelIds": ["INBOX", "UNREAD"],
    }
    with patch("vans_mcp_server.tools.gmail._gmail_service", return_value=fake_service):
        result = gmail_tools.mark_unread(
            user_id=1, store=store, oauth=_oauth(), message_ids=["m1"]
        )
    assert result["succeeded"] == [{"id": "m1"}]
    modify.assert_called_once_with(
        userId="me",
        id="m1",
        body={"addLabelIds": ["UNREAD"]},
    )


def test_modify_labels_rejects_empty_add_and_remove():
    store = _store()
    result = gmail_tools.modify_labels(
        user_id=1,
        store=store,
        oauth=_oauth(),
        message_ids=["m1"],
        add_labels=[],
        remove_labels=[],
    )
    assert result["error"] == "labels_required"
    store.get_valid_access_token.assert_not_called()


def test_modify_labels_rejects_overlapping_names():
    store = _store()
    result = gmail_tools.modify_labels(
        user_id=1,
        store=store,
        oauth=_oauth(),
        message_ids=["m1"],
        add_labels=["作業"],
        remove_labels=["作業"],
    )
    assert result["error"] == "overlapping_labels"


def test_modify_labels_rejects_system_and_unknown_names_without_mutating():
    store = _store()
    fake_service = MagicMock()
    fake_service.users.return_value.labels.return_value.list.return_value.execute.return_value = (
        _LABELS
    )
    modify = fake_service.users.return_value.messages.return_value.modify
    with patch("vans_mcp_server.tools.gmail._gmail_service", return_value=fake_service):
        system = gmail_tools.modify_labels(
            user_id=1,
            store=store,
            oauth=_oauth(),
            message_ids=["m1"],
            add_labels=["INBOX"],
            remove_labels=[],
        )
        unread = gmail_tools.modify_labels(
            user_id=1,
            store=store,
            oauth=_oauth(),
            message_ids=["m1"],
            add_labels=["UNREAD"],
            remove_labels=[],
        )
        missing = gmail_tools.modify_labels(
            user_id=1,
            store=store,
            oauth=_oauth(),
            message_ids=["m1"],
            add_labels=["不存在"],
            remove_labels=[],
        )
    assert system["error"] == "system_label_not_allowed"
    assert unread["error"] == "system_label_not_allowed"
    assert missing["error"] == "unknown_label"
    modify.assert_not_called()


def test_modify_labels_trims_and_requires_exact_name():
    store = _store()
    fake_service = MagicMock()
    fake_service.users.return_value.labels.return_value.list.return_value.execute.return_value = (
        _LABELS
    )
    modify = fake_service.users.return_value.messages.return_value.modify
    modify.return_value.execute.return_value = {"id": "m1"}
    with patch("vans_mcp_server.tools.gmail._gmail_service", return_value=fake_service):
        ok = gmail_tools.modify_labels(
            user_id=1,
            store=store,
            oauth=_oauth(),
            message_ids=["m1"],
            add_labels=[" 作業 "],
            remove_labels=["待辦"],
        )
        wrong_case = gmail_tools.modify_labels(
            user_id=1,
            store=store,
            oauth=_oauth(),
            message_ids=["m1"],
            add_labels=["homework"],
            remove_labels=[],
        )
    assert ok["succeeded"] == [{"id": "m1"}]
    modify.assert_called_with(
        userId="me",
        id="m1",
        body={"addLabelIds": ["Label_1"], "removeLabelIds": ["Label_2"]},
    )
    assert wrong_case["error"] == "unknown_label"
