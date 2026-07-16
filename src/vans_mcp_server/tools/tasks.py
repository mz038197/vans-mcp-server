from __future__ import annotations

import json
from typing import Any

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

from vans_mcp_server.oauth.google import (
    GOOGLE_PORTAL_SCOPES,
    TASKS_SCOPE,
    GoogleOAuthService,
    scopes_include,
)
from vans_mcp_server.oauth.store import OAuthConnectionStore
from vans_mcp_server.tools import calendar as calendar_tools

DEFAULT_TASKLIST_ID = "@default"
TASKS_REQUIRED_SCOPES = (TASKS_SCOPE,)
_ALLOWED_STATUSES = frozenset({"needsAction", "completed"})


def to_json(data: dict[str, Any]) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2)


def missing_scopes_payload(
    *,
    connect_url: str | None,
    granted: str | None,
    required: tuple[str, ...] = TASKS_REQUIRED_SCOPES,
) -> dict[str, Any]:
    return {
        "error": "missing_scopes",
        "message": (
            "Google is connected but required Tasks scopes are missing. "
            "Open connect_url and re-authorize to grant Tasks access."
        ),
        "required_scopes": list(required),
        "granted_scopes": granted,
        "connect_url": connect_url,
    }


def confirmation_required_payload(*, task_id: str, tasklist_id: str) -> dict[str, Any]:
    return {
        "error": "confirmation_required",
        "message": (
            "Refusing to delete task without confirm=true. "
            "Ask the human to confirm, then call again with confirm=true."
        ),
        "action": "delete",
        "task_id": task_id,
        "tasklist_id": tasklist_id,
        "deleted": False,
    }


def _credentials(
    access_token: str, refresh_token: str | None, oauth: GoogleOAuthService
) -> Credentials:
    return Credentials(
        token=access_token,
        refresh_token=refresh_token,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=oauth.client_id,
        client_secret=oauth.client_secret,
        scopes=list(GOOGLE_PORTAL_SCOPES),
    )


def _tasks_service(creds: Credentials):
    return build("tasks", "v1", credentials=creds, cache_discovery=False)


def _normalize_tasklist_id(tasklist_id: str | None) -> str:
    value = (tasklist_id or "").strip()
    return value or DEFAULT_TASKLIST_ID


def _task_summary(task: dict[str, Any], *, tasklist_id: str) -> dict[str, Any]:
    return {
        "id": task.get("id"),
        "title": task.get("title"),
        "status": task.get("status"),
        "due": task.get("due"),
        "notes": task.get("notes"),
        "updated": task.get("updated"),
        "completed": task.get("completed"),
        "parent": task.get("parent"),
        "position": task.get("position"),
        "tasklist_id": tasklist_id,
    }


def ensure_tasks_ready(
    *,
    user_id: int,
    store: OAuthConnectionStore | None,
    oauth: GoogleOAuthService | None,
    public_url: str,
    required_scopes: tuple[str, ...] = TASKS_REQUIRED_SCOPES,
) -> tuple[dict[str, Any] | None, Any]:
    """Return (error_payload, connection) — connection is StoredGoogleConnection when OK."""
    oauth_ok = oauth is not None and oauth.is_configured()
    connect_url = calendar_tools.build_connect_url(
        oauth, public_url=public_url, user_id=user_id
    )
    if store is None or not oauth_ok:
        return (
            calendar_tools.not_connected_payload(
                connect_url=connect_url, oauth_configured=oauth_ok
            ),
            None,
        )
    if not store.is_connected(user_id):
        return (
            calendar_tools.not_connected_payload(
                connect_url=connect_url, oauth_configured=True
            ),
            None,
        )
    conn = store.get_valid_access_token(user_id)
    if conn is None:
        return (
            calendar_tools.not_connected_payload(
                connect_url=connect_url, oauth_configured=True
            ),
            None,
        )
    if not scopes_include(conn.scopes, required_scopes):
        return (
            missing_scopes_payload(
                connect_url=connect_url,
                granted=conn.scopes,
                required=required_scopes,
            ),
            None,
        )
    return None, conn


def list_tasklists(
    *,
    user_id: int,
    store: OAuthConnectionStore,
    oauth: GoogleOAuthService,
    max_results: int = 20,
) -> dict[str, Any]:
    conn = store.get_valid_access_token(user_id)
    if conn is None:
        raise LookupError("not_connected")
    if not scopes_include(conn.scopes, TASKS_REQUIRED_SCOPES):
        raise PermissionError("missing_scopes")

    creds = _credentials(conn.access_token, conn.refresh_token, oauth)
    service = _tasks_service(creds)
    max_results = max(1, min(int(max_results), 100))
    listed = service.tasklists().list(maxResults=max_results).execute()
    items = []
    for entry in listed.get("items") or []:
        items.append(
            {
                "id": entry.get("id"),
                "title": entry.get("title"),
                "updated": entry.get("updated"),
            }
        )
    return {
        "count": len(items),
        "tasklists": items,
        "source": "google_tasks",
    }


def list_tasks(
    *,
    user_id: int,
    store: OAuthConnectionStore,
    oauth: GoogleOAuthService,
    tasklist_id: str = DEFAULT_TASKLIST_ID,
    max_results: int = 20,
    show_completed: bool = False,
) -> dict[str, Any]:
    conn = store.get_valid_access_token(user_id)
    if conn is None:
        raise LookupError("not_connected")
    if not scopes_include(conn.scopes, TASKS_REQUIRED_SCOPES):
        raise PermissionError("missing_scopes")

    list_id = _normalize_tasklist_id(tasklist_id)
    creds = _credentials(conn.access_token, conn.refresh_token, oauth)
    service = _tasks_service(creds)
    max_results = max(1, min(int(max_results), 100))
    listed = (
        service.tasks()
        .list(
            tasklist=list_id,
            maxResults=max_results,
            showCompleted=bool(show_completed),
            showHidden=bool(show_completed),
        )
        .execute()
    )
    items = [
        _task_summary(task, tasklist_id=list_id) for task in (listed.get("items") or [])
    ]
    return {
        "tasklist_id": list_id,
        "show_completed": bool(show_completed),
        "count": len(items),
        "tasks": items,
        "source": "google_tasks",
    }


def create_task(
    *,
    user_id: int,
    store: OAuthConnectionStore,
    oauth: GoogleOAuthService,
    title: str,
    notes: str = "",
    due: str = "",
    tasklist_id: str = DEFAULT_TASKLIST_ID,
) -> dict[str, Any]:
    conn = store.get_valid_access_token(user_id)
    if conn is None:
        raise LookupError("not_connected")
    if not scopes_include(conn.scopes, TASKS_REQUIRED_SCOPES):
        raise PermissionError("missing_scopes")
    cleaned_title = (title or "").strip()
    if not cleaned_title:
        raise ValueError("title is required")

    list_id = _normalize_tasklist_id(tasklist_id)
    body: dict[str, Any] = {"title": cleaned_title}
    if (notes or "").strip():
        body["notes"] = notes.strip()
    if (due or "").strip():
        body["due"] = due.strip()

    creds = _credentials(conn.access_token, conn.refresh_token, oauth)
    service = _tasks_service(creds)
    created = service.tasks().insert(tasklist=list_id, body=body).execute()
    summary = _task_summary(created, tasklist_id=list_id)
    return {
        **summary,
        "created": True,
        "source": "google_tasks",
    }


def update_task(
    *,
    user_id: int,
    store: OAuthConnectionStore,
    oauth: GoogleOAuthService,
    task_id: str,
    tasklist_id: str = DEFAULT_TASKLIST_ID,
    title: str | None = None,
    notes: str | None = None,
    due: str | None = None,
    status: str | None = None,
) -> dict[str, Any]:
    conn = store.get_valid_access_token(user_id)
    if conn is None:
        raise LookupError("not_connected")
    if not scopes_include(conn.scopes, TASKS_REQUIRED_SCOPES):
        raise PermissionError("missing_scopes")

    tid = (task_id or "").strip()
    if not tid:
        raise ValueError("task_id is required")
    list_id = _normalize_tasklist_id(tasklist_id)

    body: dict[str, Any] = {"id": tid}
    if title is not None:
        cleaned = title.strip()
        if not cleaned:
            raise ValueError("title cannot be empty")
        body["title"] = cleaned
    if notes is not None:
        body["notes"] = notes
    if due is not None:
        body["due"] = due.strip() if due.strip() else None
    if status is not None:
        cleaned_status = status.strip()
        if cleaned_status not in _ALLOWED_STATUSES:
            raise ValueError("status must be needsAction or completed")
        body["status"] = cleaned_status

    if len(body) == 1:
        raise ValueError("provide at least one of title, notes, due, status")

    creds = _credentials(conn.access_token, conn.refresh_token, oauth)
    service = _tasks_service(creds)
    updated = (
        service.tasks()
        .patch(tasklist=list_id, task=tid, body=body)
        .execute()
    )
    summary = _task_summary(updated, tasklist_id=list_id)
    return {
        **summary,
        "updated": True,
        "source": "google_tasks",
    }


def delete_task(
    *,
    user_id: int,
    store: OAuthConnectionStore,
    oauth: GoogleOAuthService,
    task_id: str,
    tasklist_id: str = DEFAULT_TASKLIST_ID,
    confirm: bool = False,
) -> dict[str, Any]:
    tid = (task_id or "").strip()
    list_id = _normalize_tasklist_id(tasklist_id)
    if not tid:
        raise ValueError("task_id is required")
    if not confirm:
        return confirmation_required_payload(task_id=tid, tasklist_id=list_id)

    conn = store.get_valid_access_token(user_id)
    if conn is None:
        raise LookupError("not_connected")
    if not scopes_include(conn.scopes, TASKS_REQUIRED_SCOPES):
        raise PermissionError("missing_scopes")

    creds = _credentials(conn.access_token, conn.refresh_token, oauth)
    service = _tasks_service(creds)
    service.tasks().delete(tasklist=list_id, task=tid).execute()
    return {
        "deleted": True,
        "id": tid,
        "tasklist_id": list_id,
        "source": "google_tasks",
    }
