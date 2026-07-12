from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

from vans_mcp_server.oauth.google import GOOGLE_PORTAL_SCOPES, GoogleOAuthService
from vans_mcp_server.oauth.store import OAuthConnectionStore

DEFAULT_TIMEZONE = "Asia/Taipei"


def to_json(data: dict[str, Any]) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2)


def not_connected_payload(
    *,
    connect_url: str | None,
    oauth_configured: bool,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "error": "not_connected",
        "message": (
            "Google Portal is not connected for this user. "
            "Open connect_url in a browser to authorize Calendar/Gmail "
            "(separate from portal login)."
        ),
        "oauth_configured": oauth_configured,
    }
    if connect_url:
        payload["connect_url"] = connect_url
    else:
        payload["hint"] = (
            "Set GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, SESSION_SECRET, "
            "OAUTH_TOKEN_ENCRYPTION_KEY, and DATABASE_URL on the server."
        )
    return payload


def build_connect_url(
    oauth: GoogleOAuthService | None,
    *,
    public_url: str,
    user_id: int,
) -> str | None:
    if oauth is None or not oauth.is_configured():
        return None
    state = oauth.create_connect_state(user_id)
    base = public_url.rstrip("/")
    return f"{base}/connect/google/start?state={state}"


def connection_status(
    *,
    user_id: int,
    store: OAuthConnectionStore | None,
    oauth: GoogleOAuthService | None,
    public_url: str,
) -> dict[str, Any]:
    oauth_configured = oauth is not None and oauth.is_configured()
    connect_url = build_connect_url(oauth, public_url=public_url, user_id=user_id)
    if store is None:
        return {
            "connected": False,
            "oauth_configured": oauth_configured,
            "store_configured": False,
            "connect_url": connect_url,
            "error": "store_unconfigured",
            "hint": "DATABASE_URL and OAUTH_TOKEN_ENCRYPTION_KEY are required.",
        }
    connected = store.is_connected(user_id)
    return {
        "connected": connected,
        "oauth_configured": oauth_configured,
        "store_configured": True,
        "connect_url": connect_url if not connected else None,
        "message": (
            "Google Portal already connected."
            if connected
            else "Open connect_url to authorize Google Calendar and Gmail."
        ),
    }


def _credentials(access_token: str, refresh_token: str | None, oauth: GoogleOAuthService) -> Credentials:
    return Credentials(
        token=access_token,
        refresh_token=refresh_token,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=oauth.client_id,
        client_secret=oauth.client_secret,
        scopes=list(GOOGLE_PORTAL_SCOPES),
    )


def _calendar_service(creds: Credentials):
    return build("calendar", "v3", credentials=creds, cache_discovery=False)


def _parse_bound(text: str, tz_name: str) -> datetime:
    """Parse ISO datetime to UTC (for API query bounds like timeMin/timeMax)."""
    return _parse_in_timezone(text, tz_name).astimezone(timezone.utc)


def _parse_in_timezone(text: str, tz_name: str) -> datetime:
    """Parse ISO datetime as an aware datetime in ``tz_name``.

    Naive values are treated as local wall time in ``tz_name``.
    Aware values are converted into ``tz_name`` (preserving the instant).
    """
    raw = (text or "").strip()
    if not raw:
        raise ValueError("empty datetime")
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    dt = datetime.fromisoformat(raw)
    tz = ZoneInfo(tz_name)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=tz)
    return dt.astimezone(tz)


def _event_datetime_payload(dt: datetime, tz_name: str) -> dict[str, str]:
    """Google EventDateTime: local wall clock + IANA timeZone (no offset in dateTime)."""
    local = dt.astimezone(ZoneInfo(tz_name)) if dt.tzinfo else dt.replace(tzinfo=ZoneInfo(tz_name))
    return {
        "dateTime": local.replace(tzinfo=None).isoformat(timespec="seconds"),
        "timeZone": tz_name,
    }


def list_events(
    *,
    user_id: int,
    store: OAuthConnectionStore,
    oauth: GoogleOAuthService,
    time_min: str,
    time_max: str,
    timezone_name: str = DEFAULT_TIMEZONE,
    max_results: int = 20,
) -> dict[str, Any]:
    conn = store.get_valid_access_token(user_id)
    if conn is None:
        raise LookupError("not_connected")
    creds = _credentials(conn.access_token, conn.refresh_token, oauth)
    service = _calendar_service(creds)
    t_min = _parse_bound(time_min, timezone_name)
    t_max = _parse_bound(time_max, timezone_name)
    max_results = max(1, min(int(max_results), 50))
    result = (
        service.events()
        .list(
            calendarId="primary",
            timeMin=t_min.isoformat().replace("+00:00", "Z"),
            timeMax=t_max.isoformat().replace("+00:00", "Z"),
            singleEvents=True,
            orderBy="startTime",
            maxResults=max_results,
        )
        .execute()
    )
    events = []
    for item in result.get("items", []):
        events.append(
            {
                "id": item.get("id"),
                "summary": item.get("summary"),
                "start": item.get("start"),
                "end": item.get("end"),
                "htmlLink": item.get("htmlLink"),
                "status": item.get("status"),
            }
        )
    return {
        "count": len(events),
        "timezone": timezone_name,
        "time_min": t_min.isoformat(),
        "time_max": t_max.isoformat(),
        "events": events,
        "source": "google_calendar",
    }


def find_free_time(
    *,
    user_id: int,
    store: OAuthConnectionStore,
    oauth: GoogleOAuthService,
    time_min: str,
    time_max: str,
    duration_minutes: int = 30,
    timezone_name: str = DEFAULT_TIMEZONE,
) -> dict[str, Any]:
    conn = store.get_valid_access_token(user_id)
    if conn is None:
        raise LookupError("not_connected")
    creds = _credentials(conn.access_token, conn.refresh_token, oauth)
    service = _calendar_service(creds)
    t_min = _parse_bound(time_min, timezone_name)
    t_max = _parse_bound(time_max, timezone_name)
    duration = timedelta(minutes=max(5, min(int(duration_minutes), 480)))

    body = {
        "timeMin": t_min.isoformat().replace("+00:00", "Z"),
        "timeMax": t_max.isoformat().replace("+00:00", "Z"),
        "timeZone": timezone_name,
        "items": [{"id": "primary"}],
    }
    fb = service.freebusy().query(body=body).execute()
    busy_raw = fb.get("calendars", {}).get("primary", {}).get("busy", [])
    busy = []
    for slot in busy_raw:
        busy.append(
            (
                _parse_bound(slot["start"], timezone_name),
                _parse_bound(slot["end"], timezone_name),
            )
        )
    busy.sort(key=lambda x: x[0])

    free_slots: list[dict[str, str]] = []
    cursor = t_min
    for b_start, b_end in busy:
        if b_start > cursor and (b_start - cursor) >= duration:
            free_slots.append(
                {
                    "start": cursor.astimezone(ZoneInfo(timezone_name)).isoformat(),
                    "end": b_start.astimezone(ZoneInfo(timezone_name)).isoformat(),
                }
            )
        if b_end > cursor:
            cursor = b_end
    if t_max > cursor and (t_max - cursor) >= duration:
        free_slots.append(
            {
                "start": cursor.astimezone(ZoneInfo(timezone_name)).isoformat(),
                "end": t_max.astimezone(ZoneInfo(timezone_name)).isoformat(),
            }
        )

    return {
        "timezone": timezone_name,
        "duration_minutes": int(duration.total_seconds() // 60),
        "time_min": t_min.isoformat(),
        "time_max": t_max.isoformat(),
        "busy_count": len(busy),
        "free_slots": free_slots,
        "source": "google_calendar",
    }


def create_event(
    *,
    user_id: int,
    store: OAuthConnectionStore,
    oauth: GoogleOAuthService,
    summary: str,
    start: str,
    end: str,
    description: str = "",
    timezone_name: str = DEFAULT_TIMEZONE,
) -> dict[str, Any]:
    conn = store.get_valid_access_token(user_id)
    if conn is None:
        raise LookupError("not_connected")
    if not (summary or "").strip():
        raise ValueError("summary is required")
    creds = _credentials(conn.access_token, conn.refresh_token, oauth)
    service = _calendar_service(creds)
    start_dt = _parse_in_timezone(start, timezone_name)
    end_dt = _parse_in_timezone(end, timezone_name)
    body: dict[str, Any] = {
        "summary": summary.strip(),
        "description": description or "",
        "start": _event_datetime_payload(start_dt, timezone_name),
        "end": _event_datetime_payload(end_dt, timezone_name),
    }
    created = service.events().insert(calendarId="primary", body=body).execute()
    return {
        "created": True,
        "id": created.get("id"),
        "summary": created.get("summary"),
        "htmlLink": created.get("htmlLink"),
        "start": created.get("start"),
        "end": created.get("end"),
        "source": "google_calendar",
    }
