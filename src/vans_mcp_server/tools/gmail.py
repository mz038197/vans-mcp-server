from __future__ import annotations

import base64
import json
import re
from email.mime.text import MIMEText
from typing import Any

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

from vans_mcp_server.oauth.google import (
    GMAIL_COMPOSE_SCOPE,
    GMAIL_MODIFY_SCOPE,
    GMAIL_READONLY_SCOPE,
    GOOGLE_PORTAL_SCOPES,
    GoogleOAuthService,
    scopes_include,
)
from vans_mcp_server.oauth.store import OAuthConnectionStore
from vans_mcp_server.tools import calendar as calendar_tools

GMAIL_BASE_SCOPES = (
    GMAIL_READONLY_SCOPE,
    GMAIL_COMPOSE_SCOPE,
)
# Trash (messages.trash) needs modify; read/search/draft/send do not.
GMAIL_TRASH_SCOPES = (*GMAIL_BASE_SCOPES, GMAIL_MODIFY_SCOPE)
# Backward-compatible name for base (non-trash) Gmail tools.
GMAIL_REQUIRED_SCOPES = GMAIL_BASE_SCOPES


def to_json(data: dict[str, Any]) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2)


def missing_scopes_payload(
    *,
    connect_url: str | None,
    granted: str | None,
    required: tuple[str, ...] = GMAIL_BASE_SCOPES,
) -> dict[str, Any]:
    return {
        "error": "missing_scopes",
        "message": (
            "Google is connected but required Gmail scopes are missing. "
            "Open connect_url and re-authorize to grant Gmail access."
        ),
        "required_scopes": list(required),
        "granted_scopes": granted,
        "connect_url": connect_url,
    }


def confirmation_required_payload(
    *,
    to: str | None = None,
    subject: str | None = None,
    message_id: str | None = None,
    action: str = "send",
) -> dict[str, Any]:
    if action == "trash":
        message = (
            "Refusing to move message to trash without confirm=true. "
            "Ask the human to confirm, then call again with confirm=true."
        )
    else:
        message = (
            "Refusing to send email without confirm=true. "
            "Ask the human to confirm, then call again with confirm=true."
        )
    payload: dict[str, Any] = {
        "error": "confirmation_required",
        "message": message,
        "action": action,
    }
    if to is not None:
        payload["to"] = to
    if subject is not None:
        payload["subject"] = subject
    if message_id is not None:
        payload["message_id"] = message_id
    if action == "send":
        payload["sent"] = False
    if action == "trash":
        payload["trashed"] = False
    return payload


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


def _gmail_service(creds: Credentials):
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def _header_map(payload: dict[str, Any]) -> dict[str, str]:
    headers = payload.get("headers") or []
    out: dict[str, str] = {}
    for item in headers:
        name = (item.get("name") or "").lower()
        if name:
            out[name] = item.get("value") or ""
    return out


def _decode_body_data(data: str | None) -> str:
    if not data:
        return ""
    padded = data + "=" * (-len(data) % 4)
    try:
        return base64.urlsafe_b64decode(padded.encode("ascii")).decode(
            "utf-8", errors="replace"
        )
    except Exception:
        return ""


def _extract_text(payload: dict[str, Any] | None, *, limit: int = 4000) -> str:
    if not payload:
        return ""
    mime = (payload.get("mimeType") or "").lower()
    body = payload.get("body") or {}
    if mime.startswith("text/plain"):
        text = _decode_body_data(body.get("data"))
        return text[:limit]
    if mime.startswith("text/html"):
        html = _decode_body_data(body.get("data"))
        text = re.sub(r"<[^>]+>", " ", html)
        text = re.sub(r"\s+", " ", text).strip()
        return text[:limit]
    parts = payload.get("parts") or []
    chunks: list[str] = []
    for part in parts:
        chunk = _extract_text(part, limit=limit)
        if chunk:
            chunks.append(chunk)
        if sum(len(c) for c in chunks) >= limit:
            break
    joined = "\n".join(chunks)
    return joined[:limit]


def ensure_gmail_ready(
    *,
    user_id: int,
    store: OAuthConnectionStore | None,
    oauth: GoogleOAuthService | None,
    public_url: str,
    required_scopes: tuple[str, ...] = GMAIL_BASE_SCOPES,
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


def search_messages(
    *,
    user_id: int,
    store: OAuthConnectionStore,
    oauth: GoogleOAuthService,
    query: str,
    max_results: int = 10,
) -> dict[str, Any]:
    conn = store.get_valid_access_token(user_id)
    if conn is None:
        raise LookupError("not_connected")
    if not scopes_include(conn.scopes, GMAIL_BASE_SCOPES):
        raise PermissionError("missing_scopes")

    creds = _credentials(conn.access_token, conn.refresh_token, oauth)
    service = _gmail_service(creds)
    max_results = max(1, min(int(max_results), 25))
    listed = (
        service.users()
        .messages()
        .list(userId="me", q=query or "", maxResults=max_results)
        .execute()
    )
    messages = []
    for item in listed.get("messages") or []:
        msg_id = item.get("id")
        if not msg_id:
            continue
        full = (
            service.users()
            .messages()
            .get(
                userId="me",
                id=msg_id,
                format="metadata",
                metadataHeaders=["From", "To", "Subject", "Date"],
            )
            .execute()
        )
        headers = _header_map(full.get("payload") or {})
        messages.append(
            {
                "id": msg_id,
                "threadId": full.get("threadId") or item.get("threadId"),
                "snippet": full.get("snippet"),
                "from": headers.get("from"),
                "to": headers.get("to"),
                "subject": headers.get("subject"),
                "date": headers.get("date"),
            }
        )
    return {
        "query": query,
        "count": len(messages),
        "messages": messages,
        "source": "gmail",
    }


def summarize_thread(
    *,
    user_id: int,
    store: OAuthConnectionStore,
    oauth: GoogleOAuthService,
    thread_id: str,
    max_messages: int = 10,
) -> dict[str, Any]:
    conn = store.get_valid_access_token(user_id)
    if conn is None:
        raise LookupError("not_connected")
    if not scopes_include(conn.scopes, GMAIL_BASE_SCOPES):
        raise PermissionError("missing_scopes")

    creds = _credentials(conn.access_token, conn.refresh_token, oauth)
    service = _gmail_service(creds)
    max_messages = max(1, min(int(max_messages), 20))
    thread = (
        service.users()
        .threads()
        .get(userId="me", id=thread_id, format="full")
        .execute()
    )
    digest = []
    for msg in (thread.get("messages") or [])[:max_messages]:
        payload = msg.get("payload") or {}
        headers = _header_map(payload)
        digest.append(
            {
                "id": msg.get("id"),
                "from": headers.get("from"),
                "to": headers.get("to"),
                "subject": headers.get("subject"),
                "date": headers.get("date"),
                "snippet": msg.get("snippet"),
                "body_excerpt": _extract_text(payload, limit=1500),
            }
        )
    subjects = [d.get("subject") for d in digest if d.get("subject")]
    return {
        "thread_id": thread_id,
        "message_count": len(digest),
        "subject": subjects[0] if subjects else None,
        "messages": digest,
        "note": (
            "Structured digest only (no LLM). "
            "The agent may further summarize for the student."
        ),
        "source": "gmail",
    }


def _build_raw_message(*, to: str, subject: str, body: str) -> str:
    message = MIMEText(body or "", _charset="utf-8")
    message["to"] = to.strip()
    message["subject"] = (subject or "").strip()
    raw = base64.urlsafe_b64encode(message.as_bytes()).decode("ascii")
    return raw


def create_draft(
    *,
    user_id: int,
    store: OAuthConnectionStore,
    oauth: GoogleOAuthService,
    to: str,
    subject: str,
    body: str = "",
) -> dict[str, Any]:
    conn = store.get_valid_access_token(user_id)
    if conn is None:
        raise LookupError("not_connected")
    if not scopes_include(conn.scopes, GMAIL_BASE_SCOPES):
        raise PermissionError("missing_scopes")
    if not (to or "").strip():
        raise ValueError("to is required")

    creds = _credentials(conn.access_token, conn.refresh_token, oauth)
    service = _gmail_service(creds)
    raw = _build_raw_message(to=to, subject=subject, body=body)
    created = (
        service.users()
        .drafts()
        .create(userId="me", body={"message": {"raw": raw}})
        .execute()
    )
    msg = created.get("message") or {}
    return {
        "created": True,
        "draft_id": created.get("id"),
        "message_id": msg.get("id"),
        "threadId": msg.get("threadId"),
        "to": to,
        "subject": subject,
        "source": "gmail",
    }


def send_email(
    *,
    user_id: int,
    store: OAuthConnectionStore,
    oauth: GoogleOAuthService,
    to: str,
    subject: str,
    body: str = "",
    confirm: bool = False,
) -> dict[str, Any]:
    if not confirm:
        return confirmation_required_payload(to=to, subject=subject, action="send")
    conn = store.get_valid_access_token(user_id)
    if conn is None:
        raise LookupError("not_connected")
    if not scopes_include(conn.scopes, GMAIL_BASE_SCOPES):
        raise PermissionError("missing_scopes")
    if not (to or "").strip():
        raise ValueError("to is required")

    creds = _credentials(conn.access_token, conn.refresh_token, oauth)
    service = _gmail_service(creds)
    raw = _build_raw_message(to=to, subject=subject, body=body)
    sent = service.users().messages().send(userId="me", body={"raw": raw}).execute()
    return {
        "sent": True,
        "id": sent.get("id"),
        "threadId": sent.get("threadId"),
        "labelIds": sent.get("labelIds"),
        "to": to,
        "subject": subject,
        "source": "gmail",
    }


def trash_message(
    *,
    user_id: int,
    store: OAuthConnectionStore,
    oauth: GoogleOAuthService,
    message_id: str,
    confirm: bool = False,
) -> dict[str, Any]:
    """Move a message to Trash (not permanent delete)."""
    mid = (message_id or "").strip()
    if not confirm:
        return confirmation_required_payload(message_id=mid, action="trash")
    conn = store.get_valid_access_token(user_id)
    if conn is None:
        raise LookupError("not_connected")
    if not scopes_include(conn.scopes, GMAIL_TRASH_SCOPES):
        raise PermissionError("missing_scopes")
    if not mid:
        raise ValueError("message_id is required")

    creds = _credentials(conn.access_token, conn.refresh_token, oauth)
    service = _gmail_service(creds)
    result = service.users().messages().trash(userId="me", id=mid).execute()
    return {
        "trashed": True,
        "id": result.get("id") or mid,
        "threadId": result.get("threadId"),
        "labelIds": result.get("labelIds"),
        "source": "gmail",
        "note": "Moved to Trash. Not permanently deleted.",
    }

