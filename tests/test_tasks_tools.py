from __future__ import annotations

from unittest.mock import MagicMock, patch

from vans_mcp_server.oauth.google import TASKS_SCOPE, GoogleOAuthService, scopes_include
from vans_mcp_server.tools import tasks as tasks_tools

_TASKS_SCOPES = TASKS_SCOPE
_CALENDAR_ONLY = "https://www.googleapis.com/auth/calendar"


def _oauth() -> GoogleOAuthService:
    return GoogleOAuthService(
        client_id="cid",
        client_secret="csecret",
        redirect_uri="http://127.0.0.1:8080/connect/google/callback",
        session_secret="session-secret-for-tests",
    )


def test_scopes_include_tasks():
    granted = (
        "openid email profile https://www.googleapis.com/auth/calendar "
        f"{_TASKS_SCOPES}"
    )
    assert scopes_include(granted, (TASKS_SCOPE,))
    assert not scopes_include(_CALENDAR_ONLY, (TASKS_SCOPE,))


def test_authorize_url_includes_tasks():
    url = _oauth().authorize_url("abc")
    assert "auth%2Ftasks" in url
    assert "calendar" in url
    assert "gmail.readonly" in url


def test_delete_task_requires_confirm():
    store = MagicMock()
    result = tasks_tools.delete_task(
        user_id=1,
        store=store,
        oauth=_oauth(),
        task_id="t1",
        confirm=False,
    )
    assert result["error"] == "confirmation_required"
    assert result["deleted"] is False
    store.get_valid_access_token.assert_not_called()


def test_list_tasks_missing_scopes_raises():
    store = MagicMock()
    store.get_valid_access_token.return_value = MagicMock(
        access_token="access",
        refresh_token="refresh",
        scopes=_CALENDAR_ONLY,
    )
    try:
        tasks_tools.list_tasks(user_id=1, store=store, oauth=_oauth())
        assert False, "expected PermissionError"
    except PermissionError:
        pass


def test_list_tasklists_calls_api():
    store = MagicMock()
    store.get_valid_access_token.return_value = MagicMock(
        access_token="access",
        refresh_token="refresh",
        scopes=_TASKS_SCOPES,
    )
    fake_service = MagicMock()
    fake_service.tasklists.return_value.list.return_value.execute.return_value = {
        "items": [
            {"id": "L1", "title": "My Tasks", "updated": "2026-07-16T00:00:00.000Z"}
        ]
    }
    with patch("vans_mcp_server.tools.tasks._tasks_service", return_value=fake_service):
        result = tasks_tools.list_tasklists(user_id=1, store=store, oauth=_oauth())
    assert result["count"] == 1
    assert result["tasklists"][0]["id"] == "L1"


def test_list_tasks_calls_api():
    store = MagicMock()
    store.get_valid_access_token.return_value = MagicMock(
        access_token="access",
        refresh_token="refresh",
        scopes=_TASKS_SCOPES,
    )
    fake_service = MagicMock()
    fake_service.tasks.return_value.list.return_value.execute.return_value = {
        "items": [
            {
                "id": "t1",
                "title": "Buy milk",
                "status": "needsAction",
                "notes": "2%",
            }
        ]
    }
    with patch("vans_mcp_server.tools.tasks._tasks_service", return_value=fake_service):
        result = tasks_tools.list_tasks(
            user_id=1, store=store, oauth=_oauth(), show_completed=True
        )
    assert result["count"] == 1
    assert result["tasks"][0]["id"] == "t1"
    assert result["tasklist_id"] == "@default"
    kwargs = fake_service.tasks.return_value.list.call_args.kwargs
    assert kwargs["showCompleted"] is True
    assert kwargs["showHidden"] is True


def test_create_task_calls_api():
    store = MagicMock()
    store.get_valid_access_token.return_value = MagicMock(
        access_token="access",
        refresh_token="refresh",
        scopes=_TASKS_SCOPES,
    )
    fake_service = MagicMock()
    fake_service.tasks.return_value.insert.return_value.execute.return_value = {
        "id": "t2",
        "title": "Ship Tasks",
        "status": "needsAction",
        "notes": "MVP",
        "due": "2026-07-20T00:00:00.000Z",
    }
    with patch("vans_mcp_server.tools.tasks._tasks_service", return_value=fake_service):
        result = tasks_tools.create_task(
            user_id=1,
            store=store,
            oauth=_oauth(),
            title="Ship Tasks",
            notes="MVP",
            due="2026-07-20T00:00:00.000Z",
        )
    assert result["created"] is True
    assert result["id"] == "t2"
    body = fake_service.tasks.return_value.insert.call_args.kwargs["body"]
    assert body["title"] == "Ship Tasks"
    assert body["notes"] == "MVP"


def test_update_task_complete():
    store = MagicMock()
    store.get_valid_access_token.return_value = MagicMock(
        access_token="access",
        refresh_token="refresh",
        scopes=_TASKS_SCOPES,
    )
    fake_service = MagicMock()
    fake_service.tasks.return_value.patch.return_value.execute.return_value = {
        "id": "t2",
        "title": "Ship Tasks",
        "status": "completed",
    }
    with patch("vans_mcp_server.tools.tasks._tasks_service", return_value=fake_service):
        result = tasks_tools.update_task(
            user_id=1,
            store=store,
            oauth=_oauth(),
            task_id="t2",
            status="completed",
        )
    assert result["updated"] is True
    assert result["status"] == "completed"
    body = fake_service.tasks.return_value.patch.call_args.kwargs["body"]
    assert body["status"] == "completed"


def test_delete_task_with_confirm_calls_api():
    store = MagicMock()
    store.get_valid_access_token.return_value = MagicMock(
        access_token="access",
        refresh_token="refresh",
        scopes=_TASKS_SCOPES,
    )
    fake_service = MagicMock()
    fake_service.tasks.return_value.delete.return_value.execute.return_value = None
    with patch("vans_mcp_server.tools.tasks._tasks_service", return_value=fake_service):
        result = tasks_tools.delete_task(
            user_id=1,
            store=store,
            oauth=_oauth(),
            task_id="t2",
            confirm=True,
        )
    assert result["deleted"] is True
    assert result["id"] == "t2"
    kwargs = fake_service.tasks.return_value.delete.call_args.kwargs
    assert kwargs["tasklist"] == "@default"
    assert kwargs["task"] == "t2"
