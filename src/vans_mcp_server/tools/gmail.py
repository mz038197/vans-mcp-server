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
# messages.modify / trash need gmail.modify; read/search/draft/send do not.
GMAIL_MODIFY_SCOPES = (*GMAIL_BASE_SCOPES, GMAIL_MODIFY_SCOPE)
# Backward-compatible name for base (non-modify) Gmail tools.
GMAIL_REQUIRED_SCOPES = GMAIL_BASE_SCOPES
BATCH_MAX = 25


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
    message_ids: list[str] | None = None,
    label: str | None = None,
    action: str = "send",
) -> dict[str, Any]:
    if action == "trash":
        message = (
            "Refusing to move messages to trash without confirm=true. "
            "Ask the human to confirm, then call again with confirm=true."
        )
    elif action == "delete_label":
        message = (
            "Refusing to delete User Label without confirm=true. "
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
    if message_ids is not None:
        payload["message_ids"] = message_ids
    if label is not None:
        payload["label"] = label
    if action == "send":
        payload["sent"] = False
    if action == "trash":
        payload["trashed"] = False
    if action == "delete_label":
        payload["deleted"] = False
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


def _label_catalog(service) -> dict[str, dict[str, Any]]:
    """Map Gmail label id to the label resource (id, name, type)."""
    listed = service.users().labels().list(userId="me").execute()
    by_id: dict[str, dict[str, Any]] = {}
    for item in listed.get("labels") or []:
        label_id = item.get("id")
        if not label_id:
            continue
        name = (item.get("name") or "").strip()
        if name:
            item = {**item, "name": name}
        by_id[label_id] = item
    return by_id


def _user_label_names(
    label_ids: list[str], catalog: dict[str, dict[str, Any]]
) -> list[str]:
    names: list[str] = []
    for lid in label_ids:
        item = catalog.get(lid)
        if item and (item.get("type") or "") == "user" and item.get("name"):
            names.append(item["name"])
    return names


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
    catalog = _label_catalog(service)
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
        label_ids = list(full.get("labelIds") or [])
        messages.append(
            {
                "id": msg_id,
                "threadId": full.get("threadId") or item.get("threadId"),
                "snippet": full.get("snippet"),
                "from": headers.get("from"),
                "to": headers.get("to"),
                "subject": headers.get("subject"),
                "date": headers.get("date"),
                "unread": "UNREAD" in label_ids,
                "labels": _user_label_names(label_ids, catalog),
            }
        )
    return {
        "query": query,
        "count": len(messages),
        "messages": messages,
        "source": "gmail",
    }


def list_user_labels(
    *,
    user_id: int,
    store: OAuthConnectionStore,
    oauth: GoogleOAuthService,
) -> dict[str, Any]:
    conn = store.get_valid_access_token(user_id)
    if conn is None:
        raise LookupError("not_connected")
    if not scopes_include(conn.scopes, GMAIL_BASE_SCOPES):
        raise PermissionError("missing_scopes")
    creds = _credentials(conn.access_token, conn.refresh_token, oauth)
    service = _gmail_service(creds)
    catalog = _label_catalog(service)
    names = sorted(
        item["name"]
        for item in catalog.values()
        if (item.get("type") or "") == "user" and item.get("name")
    )
    return {"labels": names, "source": "gmail"}


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
    message_ids: list[str],
    confirm: bool = False,
) -> dict[str, Any]:
    """Move messages to Trash (not permanent delete)."""
    normalized = _normalize_message_ids(message_ids)
    if isinstance(normalized, dict):
        return normalized
    if not confirm:
        return confirmation_required_payload(message_ids=normalized, action="trash")
    conn = _require_modify_conn(user_id=user_id, store=store, oauth=oauth)
    creds = _credentials(conn.access_token, conn.refresh_token, oauth)
    service = _gmail_service(creds)

    def _trash_one(mid: str) -> dict[str, str]:
        result = service.users().messages().trash(userId="me", id=mid).execute()
        return {"id": result.get("id") or mid}

    out = _try_each_message(normalized, _trash_one)
    out["trashed"] = bool(out["succeeded"]) and not out["failed"]
    out["note"] = "Moved to Trash. Not permanently deleted."
    return out


def _unique_stripped(values: list[str] | None) -> list[str]:
    seen: list[str] = []
    seen_set: set[str] = set()
    for item in values or []:
        value = (item or "").strip()
        if not value or value in seen_set:
            continue
        seen_set.add(value)
        seen.append(value)
    return seen


def _normalize_message_ids(message_ids: list[str] | None) -> dict[str, Any] | list[str]:
    seen = _unique_stripped(message_ids)
    if not seen:
        return {
            "error": "invalid_message_ids",
            "message": "message_ids must contain at least one id.",
        }
    if len(seen) > BATCH_MAX:
        return {
            "error": "batch_too_large",
            "message": f"At most {BATCH_MAX} message ids per call.",
            "max": BATCH_MAX,
            "count": len(seen),
        }
    return seen


def _require_modify_conn(
    *,
    user_id: int,
    store: OAuthConnectionStore,
    oauth: GoogleOAuthService,
) -> Any:
    conn = store.get_valid_access_token(user_id)
    if conn is None:
        raise LookupError("not_connected")
    if not scopes_include(conn.scopes, GMAIL_MODIFY_SCOPES):
        raise PermissionError("missing_scopes")
    return conn


def _try_each_message(message_ids: list[str], fn) -> dict[str, Any]:
    succeeded: list[dict[str, str]] = []
    failed: list[dict[str, str]] = []
    for mid in message_ids:
        try:
            # ponytail: one HTTP call per message. Gmail has no transactional
            # batch; switch to BatchHttpRequest if 25 serial calls get too slow.
            succeeded.append(fn(mid))
        except Exception as exc:
            failed.append({"id": mid, "error": str(exc)})
    return {
        "succeeded": succeeded,
        "failed": failed,
        "source": "gmail",
    }


def _run_modify_batch(
    *,
    user_id: int,
    store: OAuthConnectionStore,
    oauth: GoogleOAuthService,
    message_ids: list[str],
    body: dict[str, Any],
) -> dict[str, Any]:
    normalized = _normalize_message_ids(message_ids)
    if isinstance(normalized, dict):
        return normalized
    conn = _require_modify_conn(user_id=user_id, store=store, oauth=oauth)
    creds = _credentials(conn.access_token, conn.refresh_token, oauth)
    service = _gmail_service(creds)

    def _modify_one(mid: str) -> dict[str, str]:
        result = (
            service.users()
            .messages()
            .modify(userId="me", id=mid, body=body)
            .execute()
        )
        return {"id": result.get("id") or mid}

    return _try_each_message(normalized, _modify_one)


def mark_read(
    *,
    user_id: int,
    store: OAuthConnectionStore,
    oauth: GoogleOAuthService,
    message_ids: list[str],
) -> dict[str, Any]:
    return _run_modify_batch(
        user_id=user_id,
        store=store,
        oauth=oauth,
        message_ids=message_ids,
        body={"removeLabelIds": ["UNREAD"]},
    )


def mark_unread(
    *,
    user_id: int,
    store: OAuthConnectionStore,
    oauth: GoogleOAuthService,
    message_ids: list[str],
) -> dict[str, Any]:
    return _run_modify_batch(
        user_id=user_id,
        store=store,
        oauth=oauth,
        message_ids=message_ids,
        body={"addLabelIds": ["UNREAD"]},
    )


def _system_label_hint(name: str) -> str:
    if name == "UNREAD":
        return (
            "UNREAD is a System Label. "
            "Use gmail_mark_read or gmail_mark_unread."
        )
    if name in {"TRASH", "SPAM"}:
        return (
            f"{name} is a System Label. "
            "Use gmail_trash_message to move messages to Trash."
        )
    return (
        f"{name} is a System Label. "
        "The portal does not allow System Label changes."
    )


def _label_by_name(catalog: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {item["name"]: item for item in catalog.values() if item.get("name")}


def _unknown_label(name: str) -> dict[str, Any]:
    return {
        "error": "unknown_label",
        "label": name,
        "message": f"No User Label named {name!r}.",
    }


def _system_label_error(name: str) -> dict[str, Any]:
    return {
        "error": "system_label_not_allowed",
        "label": name,
        "message": _system_label_hint(name),
    }


def _reject_system_names(
    catalog: dict[str, dict[str, Any]], names: list[str]
) -> dict[str, Any] | None:
    by_name = _label_by_name(catalog)
    for name in names:
        item = by_name.get(name)
        if item is not None and (item.get("type") or "") != "user":
            return _system_label_error(name)
    return None


def _resolve_user_label_ids(
    catalog: dict[str, dict[str, Any]], names: list[str]
) -> dict[str, Any] | list[str]:
    by_name = _label_by_name(catalog)
    ids: list[str] = []
    for name in names:
        item = by_name.get(name)
        if item is None:
            return _unknown_label(name)
        if (item.get("type") or "") != "user":
            return _system_label_error(name)
        ids.append(item["id"])
    return ids


def _ids_for_add(
    service,
    catalog: dict[str, dict[str, Any]],
    names: list[str],
) -> dict[str, Any] | tuple[list[str], list[str]]:
    by_name = _label_by_name(catalog)
    ids: list[str] = []
    created: list[str] = []
    for name in names:
        item = by_name.get(name)
        if item is None:
            created_item = (
                service.users()
                .labels()
                .create(userId="me", body={"name": name})
                .execute()
            )
            label_id = created_item.get("id")
            if not label_id:
                return _unknown_label(name)
            created_name = (created_item.get("name") or name).strip()
            item = {**created_item, "id": label_id, "name": created_name}
            catalog[label_id] = item
            by_name[created_name] = item
            created.append(name)
            ids.append(label_id)
            continue
        ids.append(item["id"])
    return ids, created


def modify_labels(
    *,
    user_id: int,
    store: OAuthConnectionStore,
    oauth: GoogleOAuthService,
    message_ids: list[str],
    add_labels: list[str] | None = None,
    remove_labels: list[str] | None = None,
) -> dict[str, Any]:
    normalized = _normalize_message_ids(message_ids)
    if isinstance(normalized, dict):
        return normalized
    add_names = _unique_stripped(add_labels)
    remove_names = _unique_stripped(remove_labels)
    if not add_names and not remove_names:
        return {
            "error": "labels_required",
            "message": "Provide add_labels and/or remove_labels.",
        }
    overlap = sorted(set(add_names) & set(remove_names))
    if overlap:
        return {
            "error": "overlapping_labels",
            "labels": overlap,
            "message": "A name cannot be added and removed in the same call.",
        }
    conn = _require_modify_conn(user_id=user_id, store=store, oauth=oauth)
    creds = _credentials(conn.access_token, conn.refresh_token, oauth)
    service = _gmail_service(creds)
    catalog = _label_catalog(service)
    blocked = _reject_system_names(catalog, add_names + remove_names)
    if blocked is not None:
        return blocked
    remove_ids = (
        _resolve_user_label_ids(catalog, remove_names) if remove_names else []
    )
    if isinstance(remove_ids, dict):
        return remove_ids
    add_result: dict[str, Any] | tuple[list[str], list[str]] = (
        _ids_for_add(service, catalog, add_names) if add_names else ([], [])
    )
    if isinstance(add_result, dict):
        return add_result
    add_ids, created_labels = add_result
    body: dict[str, Any] = {}
    if add_ids:
        body["addLabelIds"] = add_ids
    if remove_ids:
        body["removeLabelIds"] = remove_ids

    def _one(mid: str) -> dict[str, str]:
        result = (
            service.users()
            .messages()
            .modify(userId="me", id=mid, body=body)
            .execute()
        )
        return {"id": result.get("id") or mid}

    out = _try_each_message(normalized, _one)
    out["add_labels"] = add_names
    out["remove_labels"] = remove_names
    out["created_labels"] = created_labels
    return out


def delete_label(
    *,
    user_id: int,
    store: OAuthConnectionStore,
    oauth: GoogleOAuthService,
    name: str,
    confirm: bool = False,
) -> dict[str, Any]:
    trimmed = (name or "").strip()
    if not trimmed:
        return {
            "error": "label_required",
            "message": "Provide a User Label name.",
        }
    conn = _require_modify_conn(user_id=user_id, store=store, oauth=oauth)
    creds = _credentials(conn.access_token, conn.refresh_token, oauth)
    service = _gmail_service(creds)
    catalog = _label_catalog(service)
    resolved = _resolve_user_label_ids(catalog, [trimmed])
    if isinstance(resolved, dict):
        return resolved
    if not confirm:
        return confirmation_required_payload(
            label=trimmed, action="delete_label"
        )
    service.users().labels().delete(userId="me", id=resolved[0]).execute()
    return {
        "deleted": True,
        "label": trimmed,
        "source": "gmail",
    }

