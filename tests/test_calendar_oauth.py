from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from vans_mcp_server.oauth.crypto import TokenEncryptor
from vans_mcp_server.oauth.google import GoogleOAuthService, GoogleTokenBundle
from vans_mcp_server.tools import calendar as calendar_tools


def test_token_encryptor_roundtrip():
    key = TokenEncryptor.generate_key()
    enc = TokenEncryptor(key)
    token = "ya29.refresh-or-access"
    assert enc.decrypt(enc.encrypt(token)) == token


def test_connect_state_roundtrip():
    oauth = GoogleOAuthService(
        client_id="cid",
        client_secret="csecret",
        redirect_uri="http://127.0.0.1:8080/connect/google/callback",
        session_secret="session-secret-for-tests",
    )
    state = oauth.create_connect_state(42)
    assert oauth.verify_connect_state(state) == 42
    assert oauth.verify_connect_state("tampered") is None


def test_authorize_url_is_offline_consent():
    oauth = GoogleOAuthService(
        client_id="cid",
        client_secret="csecret",
        redirect_uri="http://127.0.0.1:8080/connect/google/callback",
        session_secret="session-secret-for-tests",
    )
    url = oauth.authorize_url("abc")
    assert "access_type=offline" in url
    assert "prompt=consent" in url
    assert "calendar" in url


def test_not_connected_payload_includes_connect_url():
    payload = calendar_tools.not_connected_payload(
        connect_url="http://127.0.0.1:8080/connect/google/start?state=x",
        oauth_configured=True,
    )
    assert payload["error"] == "not_connected"
    assert "connect_url" in payload


def test_build_connect_url():
    oauth = GoogleOAuthService(
        client_id="cid",
        client_secret="csecret",
        redirect_uri="http://127.0.0.1:8080/connect/google/callback",
        session_secret="session-secret-for-tests",
    )
    url = calendar_tools.build_connect_url(
        oauth, public_url="https://mcp.vanscoding.com", user_id=7
    )
    assert url is not None
    assert url.startswith("https://mcp.vanscoding.com/connect/google/start?state=")


def test_parse_in_timezone_naive_and_aware():
    naive = calendar_tools._parse_in_timezone("2026-07-12T15:00:00", "Asia/Taipei")
    assert naive.tzinfo is not None
    assert naive.hour == 15
    assert naive.utcoffset() == timedelta(hours=8)

    utc_aware = calendar_tools._parse_in_timezone(
        "2026-07-12T07:00:00+00:00", "Asia/Taipei"
    )
    assert utc_aware.hour == 15
    assert utc_aware.utcoffset() == timedelta(hours=8)


def test_create_event_sends_local_wall_time_with_timezone():
    """create_event must not UTC-roundtrip then re-attach offset in dateTime."""
    oauth = GoogleOAuthService(
        client_id="cid",
        client_secret="csecret",
        redirect_uri="http://127.0.0.1:8080/connect/google/callback",
        session_secret="session-secret-for-tests",
    )
    store = MagicMock()
    store.get_valid_access_token.return_value = MagicMock(
        access_token="access",
        refresh_token="refresh",
    )

    fake_service = MagicMock()
    fake_service.events.return_value.insert.return_value.execute.return_value = {
        "id": "evt1",
        "summary": "Study",
        "htmlLink": "https://calendar.google.com/event?eid=1",
        "start": {"dateTime": "2026-07-12T15:00:00", "timeZone": "Asia/Taipei"},
        "end": {"dateTime": "2026-07-12T16:00:00", "timeZone": "Asia/Taipei"},
    }

    with patch("vans_mcp_server.tools.calendar._calendar_service", return_value=fake_service):
        result = calendar_tools.create_event(
            user_id=1,
            store=store,
            oauth=oauth,
            summary="Study",
            start="2026-07-12T15:00:00",
            end="2026-07-12T16:00:00",
            timezone_name="Asia/Taipei",
        )

    assert result["created"] is True
    body = fake_service.events.return_value.insert.call_args.kwargs["body"]
    assert body["start"] == {
        "dateTime": "2026-07-12T15:00:00",
        "timeZone": "Asia/Taipei",
    }
    assert body["end"] == {
        "dateTime": "2026-07-12T16:00:00",
        "timeZone": "Asia/Taipei",
    }
    # Aware UTC input must convert to Taipei wall time, not send UTC digits.
    with patch("vans_mcp_server.tools.calendar._calendar_service", return_value=fake_service):
        calendar_tools.create_event(
            user_id=1,
            store=store,
            oauth=oauth,
            summary="Study",
            start="2026-07-12T07:00:00Z",
            end="2026-07-12T08:00:00Z",
            timezone_name="Asia/Taipei",
        )
    body2 = fake_service.events.return_value.insert.call_args.kwargs["body"]
    assert body2["start"]["dateTime"] == "2026-07-12T15:00:00"
    assert body2["start"]["timeZone"] == "Asia/Taipei"


def test_get_valid_access_token_expires_at_uses_post_refresh_now(monkeypatch):
    from vans_mcp_server.oauth.store import OAuthConnectionStore

    encryptor = TokenEncryptor(TokenEncryptor.generate_key())
    oauth = MagicMock()
    oauth.refresh_access_token.return_value = GoogleTokenBundle(
        access_token="new-access",
        refresh_token=None,
        expires_in=3600,
        scope="https://www.googleapis.com/auth/calendar",
        google_sub="sub",
        email="a@example.com",
    )

    store = OAuthConnectionStore.__new__(OAuthConnectionStore)
    store.database_url = "postgresql://unused"
    store.encryptor = encryptor
    store.oauth = oauth

    expired = datetime(2020, 1, 1, tzinfo=timezone.utc)
    row = {
        "refresh_token_enc": encryptor.encrypt("refresh"),
        "access_token_enc": encryptor.encrypt("old-access"),
        "expires_at": expired.isoformat(),
        "scopes": "calendar",
        "google_sub": "sub",
    }

    t_before = datetime(2026, 7, 12, 10, 0, 0, tzinfo=timezone.utc)
    t_after = datetime(2026, 7, 12, 10, 0, 5, tzinfo=timezone.utc)
    clock = {"n": 0}

    def fake_now(tz=None):
        clock["n"] += 1
        # 1st: pre-refresh expiry check; 2nd+: post-refresh expires_at (+ upsert).
        return t_before if clock["n"] == 1 else t_after

    monkeypatch.setattr(
        "vans_mcp_server.oauth.store.datetime",
        type(
            "DT",
            (),
            {
                "now": staticmethod(fake_now),
                "fromisoformat": datetime.fromisoformat,
            },
        ),
    )

    with (
        patch.object(store, "_load_row", return_value=row),
        patch.object(store, "upsert_google_tokens") as upsert,
    ):
        conn = store.get_valid_access_token(1)

    assert conn is not None
    assert conn.access_token == "new-access"
    assert conn.expires_at == t_after + timedelta(seconds=3600)
    upsert.assert_called_once()


def test_update_event_patches_summary_and_time():
    oauth = GoogleOAuthService(
        client_id="cid",
        client_secret="csecret",
        redirect_uri="http://127.0.0.1:8080/connect/google/callback",
        session_secret="session-secret-for-tests",
    )
    store = MagicMock()
    store.get_valid_access_token.return_value = MagicMock(
        access_token="access",
        refresh_token="refresh",
    )
    fake_service = MagicMock()
    fake_service.events.return_value.patch.return_value.execute.return_value = {
        "id": "evt1",
        "summary": "Revised",
        "htmlLink": "https://calendar.google.com/event?eid=1",
        "start": {"dateTime": "2026-07-12T16:00:00", "timeZone": "Asia/Taipei"},
        "end": {"dateTime": "2026-07-12T17:00:00", "timeZone": "Asia/Taipei"},
        "status": "confirmed",
    }

    with patch("vans_mcp_server.tools.calendar._calendar_service", return_value=fake_service):
        result = calendar_tools.update_event(
            user_id=1,
            store=store,
            oauth=oauth,
            event_id="evt1",
            summary="Revised",
            start="2026-07-12T16:00:00",
            end="2026-07-12T17:00:00",
            timezone_name="Asia/Taipei",
        )

    assert result["updated"] is True
    assert result["id"] == "evt1"
    kwargs = fake_service.events.return_value.patch.call_args.kwargs
    assert kwargs["eventId"] == "evt1"
    assert kwargs["body"]["summary"] == "Revised"
    assert kwargs["body"]["start"] == {
        "dateTime": "2026-07-12T16:00:00",
        "timeZone": "Asia/Taipei",
    }
    assert "sendUpdates" not in kwargs


def test_create_event_with_attendees_sends_updates():
    oauth = GoogleOAuthService(
        client_id="cid",
        client_secret="csecret",
        redirect_uri="http://127.0.0.1:8080/connect/google/callback",
        session_secret="session-secret-for-tests",
    )
    store = MagicMock()
    store.get_valid_access_token.return_value = MagicMock(
        access_token="access",
        refresh_token="refresh",
    )
    fake_service = MagicMock()
    fake_service.events.return_value.insert.return_value.execute.return_value = {
        "id": "evt2",
        "summary": "Meetup",
        "htmlLink": "https://calendar.google.com/event?eid=2",
        "start": {"dateTime": "2026-07-12T15:00:00", "timeZone": "Asia/Taipei"},
        "end": {"dateTime": "2026-07-12T16:00:00", "timeZone": "Asia/Taipei"},
        "attendees": [
            {"email": "a@example.com", "responseStatus": "needsAction"},
            {"email": "b@example.com", "responseStatus": "needsAction"},
        ],
    }

    with patch("vans_mcp_server.tools.calendar._calendar_service", return_value=fake_service):
        result = calendar_tools.create_event(
            user_id=1,
            store=store,
            oauth=oauth,
            summary="Meetup",
            start="2026-07-12T15:00:00",
            end="2026-07-12T16:00:00",
            attendees=["a@example.com", " b@example.com ", "a@example.com"],
        )

    assert result["created"] is True
    assert result["attendees"] == [
        {"email": "a@example.com", "responseStatus": "needsAction"},
        {"email": "b@example.com", "responseStatus": "needsAction"},
    ]
    kwargs = fake_service.events.return_value.insert.call_args.kwargs
    assert kwargs["sendUpdates"] == "all"
    assert kwargs["body"]["attendees"] == [
        {"email": "a@example.com"},
        {"email": "b@example.com"},
    ]


def test_create_event_without_attendees_omits_send_updates():
    oauth = GoogleOAuthService(
        client_id="cid",
        client_secret="csecret",
        redirect_uri="http://127.0.0.1:8080/connect/google/callback",
        session_secret="session-secret-for-tests",
    )
    store = MagicMock()
    store.get_valid_access_token.return_value = MagicMock(
        access_token="access",
        refresh_token="refresh",
    )
    fake_service = MagicMock()
    fake_service.events.return_value.insert.return_value.execute.return_value = {
        "id": "evt3",
        "summary": "Solo",
        "htmlLink": "https://calendar.google.com/event?eid=3",
        "start": {"dateTime": "2026-07-12T15:00:00", "timeZone": "Asia/Taipei"},
        "end": {"dateTime": "2026-07-12T16:00:00", "timeZone": "Asia/Taipei"},
    }

    with patch("vans_mcp_server.tools.calendar._calendar_service", return_value=fake_service):
        calendar_tools.create_event(
            user_id=1,
            store=store,
            oauth=oauth,
            summary="Solo",
            start="2026-07-12T15:00:00",
            end="2026-07-12T16:00:00",
        )

    kwargs = fake_service.events.return_value.insert.call_args.kwargs
    assert "sendUpdates" not in kwargs
    assert "attendees" not in kwargs["body"]


def test_empty_attendees_list_sets_send_updates_on_create_and_update():
    """attendees=[] is explicit (not omit); both create and update use sendUpdates=all."""
    oauth = GoogleOAuthService(
        client_id="cid",
        client_secret="csecret",
        redirect_uri="http://127.0.0.1:8080/connect/google/callback",
        session_secret="session-secret-for-tests",
    )
    store = MagicMock()
    store.get_valid_access_token.return_value = MagicMock(
        access_token="access",
        refresh_token="refresh",
    )
    fake_service = MagicMock()
    fake_service.events.return_value.insert.return_value.execute.return_value = {
        "id": "evt-empty",
        "summary": "Empty guests",
        "htmlLink": "https://calendar.google.com/event?eid=e",
        "start": {"dateTime": "2026-07-12T15:00:00", "timeZone": "Asia/Taipei"},
        "end": {"dateTime": "2026-07-12T16:00:00", "timeZone": "Asia/Taipei"},
        "attendees": [],
    }
    fake_service.events.return_value.patch.return_value.execute.return_value = {
        "id": "evt1",
        "summary": "Cleared",
        "htmlLink": "https://calendar.google.com/event?eid=1",
        "attendees": [],
        "status": "confirmed",
    }

    with patch("vans_mcp_server.tools.calendar._calendar_service", return_value=fake_service):
        calendar_tools.create_event(
            user_id=1,
            store=store,
            oauth=oauth,
            summary="Empty guests",
            start="2026-07-12T15:00:00",
            end="2026-07-12T16:00:00",
            attendees=[],
        )
        calendar_tools.update_event(
            user_id=1,
            store=store,
            oauth=oauth,
            event_id="evt1",
            attendees=[],
        )

    insert_kwargs = fake_service.events.return_value.insert.call_args.kwargs
    assert insert_kwargs["sendUpdates"] == "all"
    assert insert_kwargs["body"]["attendees"] == []

    patch_kwargs = fake_service.events.return_value.patch.call_args.kwargs
    assert patch_kwargs["sendUpdates"] == "all"
    assert patch_kwargs["body"] == {"attendees": []}


def test_update_event_attendees_only_sends_updates():
    oauth = GoogleOAuthService(
        client_id="cid",
        client_secret="csecret",
        redirect_uri="http://127.0.0.1:8080/connect/google/callback",
        session_secret="session-secret-for-tests",
    )
    store = MagicMock()
    store.get_valid_access_token.return_value = MagicMock(
        access_token="access",
        refresh_token="refresh",
    )
    fake_service = MagicMock()
    fake_service.events.return_value.patch.return_value.execute.return_value = {
        "id": "evt1",
        "summary": "Study",
        "htmlLink": "https://calendar.google.com/event?eid=1",
        "attendees": [{"email": "guest@example.com", "responseStatus": "needsAction"}],
        "status": "confirmed",
    }

    with patch("vans_mcp_server.tools.calendar._calendar_service", return_value=fake_service):
        result = calendar_tools.update_event(
            user_id=1,
            store=store,
            oauth=oauth,
            event_id="evt1",
            attendees=["guest@example.com"],
        )

    assert result["updated"] is True
    assert result["attendees"] == [
        {"email": "guest@example.com", "responseStatus": "needsAction"}
    ]
    kwargs = fake_service.events.return_value.patch.call_args.kwargs
    assert kwargs["sendUpdates"] == "all"
    assert kwargs["body"] == {"attendees": [{"email": "guest@example.com"}]}


def test_attendee_payloads_rejects_invalid_email():
    with pytest.raises(ValueError, match="invalid attendee email"):
        calendar_tools._attendee_payloads(["not-an-email"])
    with pytest.raises(ValueError, match="invalid attendee email"):
        calendar_tools.create_event(
            user_id=1,
            store=MagicMock(),
            oauth=GoogleOAuthService(
                client_id="cid",
                client_secret="csecret",
                redirect_uri="http://127.0.0.1:8080/connect/google/callback",
                session_secret="session-secret-for-tests",
            ),
            summary="Bad",
            start="2026-07-12T15:00:00",
            end="2026-07-12T16:00:00",
            attendees=["bad-email"],
        )


def test_update_event_rejects_partial_time_and_empty_body():
    oauth = GoogleOAuthService(
        client_id="cid",
        client_secret="csecret",
        redirect_uri="http://127.0.0.1:8080/connect/google/callback",
        session_secret="session-secret-for-tests",
    )
    store = MagicMock()
    with pytest.raises(ValueError, match="start and end"):
        calendar_tools.update_event(
            user_id=1,
            store=store,
            oauth=oauth,
            event_id="evt1",
            start="2026-07-12T16:00:00",
        )
    with pytest.raises(ValueError, match="at least one"):
        calendar_tools.update_event(
            user_id=1,
            store=store,
            oauth=oauth,
            event_id="evt1",
        )
    store.get_valid_access_token.assert_not_called()


def test_delete_event_requires_confirm():
    store = MagicMock()
    oauth = GoogleOAuthService(
        client_id="cid",
        client_secret="csecret",
        redirect_uri="http://127.0.0.1:8080/connect/google/callback",
        session_secret="session-secret-for-tests",
    )
    result = calendar_tools.delete_event(
        user_id=1,
        store=store,
        oauth=oauth,
        event_id="evt1",
        confirm=False,
    )
    assert result["error"] == "confirmation_required"
    assert result["deleted"] is False
    assert result["event_id"] == "evt1"
    store.get_valid_access_token.assert_not_called()


def test_delete_event_rejects_empty_id_before_confirm():
    store = MagicMock()
    oauth = GoogleOAuthService(
        client_id="cid",
        client_secret="csecret",
        redirect_uri="http://127.0.0.1:8080/connect/google/callback",
        session_secret="session-secret-for-tests",
    )
    with pytest.raises(ValueError, match="event_id is required"):
        calendar_tools.delete_event(
            user_id=1,
            store=store,
            oauth=oauth,
            event_id="   ",
            confirm=False,
        )
    store.get_valid_access_token.assert_not_called()


def test_delete_event_with_confirm():
    oauth = GoogleOAuthService(
        client_id="cid",
        client_secret="csecret",
        redirect_uri="http://127.0.0.1:8080/connect/google/callback",
        session_secret="session-secret-for-tests",
    )
    store = MagicMock()
    store.get_valid_access_token.return_value = MagicMock(
        access_token="access",
        refresh_token="refresh",
    )
    fake_service = MagicMock()
    fake_service.events.return_value.delete.return_value.execute.return_value = None

    with patch("vans_mcp_server.tools.calendar._calendar_service", return_value=fake_service):
        result = calendar_tools.delete_event(
            user_id=1,
            store=store,
            oauth=oauth,
            event_id="evt1",
            confirm=True,
        )

    assert result["deleted"] is True
    assert result["id"] == "evt1"
    kwargs = fake_service.events.return_value.delete.call_args.kwargs
    assert kwargs["calendarId"] == "primary"
    assert kwargs["eventId"] == "evt1"


def test_find_free_time_gap_logic():
    oauth = GoogleOAuthService(
        client_id="cid",
        client_secret="csecret",
        redirect_uri="http://127.0.0.1:8080/connect/google/callback",
        session_secret="session-secret-for-tests",
    )
    store = MagicMock()
    store.get_valid_access_token.return_value = MagicMock(
        access_token="access",
        refresh_token="refresh",
    )

    t0 = datetime(2026, 7, 12, 1, 0, tzinfo=timezone.utc)
    t1 = datetime(2026, 7, 12, 5, 0, tzinfo=timezone.utc)
    busy_start = datetime(2026, 7, 12, 2, 0, tzinfo=timezone.utc)
    busy_end = datetime(2026, 7, 12, 3, 0, tzinfo=timezone.utc)

    fake_service = MagicMock()
    fake_service.freebusy.return_value.query.return_value.execute.return_value = {
        "calendars": {
            "primary": {
                "busy": [
                    {
                        "start": busy_start.isoformat().replace("+00:00", "Z"),
                        "end": busy_end.isoformat().replace("+00:00", "Z"),
                    }
                ]
            }
        }
    }

    with patch("vans_mcp_server.tools.calendar._calendar_service", return_value=fake_service):
        result = calendar_tools.find_free_time(
            user_id=1,
            store=store,
            oauth=oauth,
            time_min=t0.isoformat(),
            time_max=t1.isoformat(),
            duration_minutes=30,
            timezone_name="UTC",
        )

    assert result["busy_count"] == 1
    assert len(result["free_slots"]) >= 2


def test_connect_routes_and_not_connected_tool(monkeypatch):
    monkeypatch.setenv("MCP_DEV_BYPASS_KEY", "vcr_sk_dev_local_only")
    monkeypatch.setenv("PUBLIC_URL", "http://127.0.0.1:8080")
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "cid")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "csecret")
    monkeypatch.setenv("SESSION_SECRET", "session-secret-for-tests")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("OAUTH_TOKEN_ENCRYPTION_KEY", raising=False)

    import importlib

    import vans_mcp_server.app as app_module

    importlib.reload(app_module)

    from starlette.testclient import TestClient

    with TestClient(app_module.app) as client:
        health = client.get("/health")
        assert health.status_code == 200
        body = health.json()
        assert body["google_oauth_configured"] is True
        assert body["oauth_store_configured"] is False

        oauth = app_module.google_oauth
        assert oauth is not None
        state = oauth.create_connect_state(0)
        start = client.get(f"/connect/google/start?state={state}", follow_redirects=False)
        assert start.status_code == 302
        assert "accounts.google.com" in start.headers["location"]

        bad = client.get("/connect/google/start?state=bad", follow_redirects=False)
        assert bad.status_code == 400


def test_callback_saves_tokens(monkeypatch):
    key = TokenEncryptor.generate_key()
    monkeypatch.setenv("MCP_DEV_BYPASS_KEY", "vcr_sk_dev_local_only")
    monkeypatch.setenv("PUBLIC_URL", "http://127.0.0.1:8080")
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "cid")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "csecret")
    monkeypatch.setenv("SESSION_SECRET", "session-secret-for-tests")
    monkeypatch.setenv("OAUTH_TOKEN_ENCRYPTION_KEY", key)
    monkeypatch.delenv("DATABASE_URL", raising=False)

    import importlib

    import vans_mcp_server.app as app_module

    fake_store = MagicMock()
    importlib.reload(app_module)
    oauth = app_module.google_oauth
    assert oauth is not None
    app_module.oauth_store = fake_store
    state = oauth.create_connect_state(99)
    bundle = GoogleTokenBundle(
        access_token="access",
        refresh_token="refresh",
        expires_in=3600,
        scope="https://www.googleapis.com/auth/calendar",
        google_sub="sub99",
        email="a@example.com",
    )
    with patch.object(oauth, "exchange_code", return_value=bundle):
        from starlette.testclient import TestClient

        with TestClient(app_module.app) as client:
            res = client.get(
                f"/connect/google/callback?state={state}&code=abc",
            )
            assert res.status_code == 200
            assert "connected" in res.text.lower()
            fake_store.upsert_google_tokens.assert_called_once()
            kwargs = fake_store.upsert_google_tokens.call_args.kwargs
            assert kwargs["user_id"] == 99
            assert kwargs["bundle"].refresh_token == "refresh"
