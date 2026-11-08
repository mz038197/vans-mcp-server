"""Against a real Postgres: the service's own database and the one-off move (#100).

Skipped unless these are set (roles set up as in docs/mcp-database-move-rehearsal.md):

- VANS_MCP_TEST_TARGET_URL: an empty database, connecting as a non-superuser app role
- VANS_MCP_TEST_SOURCE_URL: a database standing in for the router's neondb, as its owner

Each test drops mcp_usage and mcp_oauth_connections in both before it starts.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import psycopg
import pytest

from vans_mcp_server.oauth.crypto import TokenEncryptor
from vans_mcp_server.oauth.google import GoogleTokenBundle
from vans_mcp_server.oauth.store import OAuthConnectionStore
from vans_mcp_server.usage import UsageLogger

TARGET_URL = os.environ.get("VANS_MCP_TEST_TARGET_URL", "")
SOURCE_URL = os.environ.get("VANS_MCP_TEST_SOURCE_URL", "")

pytestmark = pytest.mark.skipif(
    not (TARGET_URL and SOURCE_URL),
    reason="VANS_MCP_TEST_TARGET_URL and VANS_MCP_TEST_SOURCE_URL not set",
)

TABLES = ("mcp_usage", "mcp_oauth_connections")


def _drop_tables(url: str) -> None:
    with psycopg.connect(url) as conn:
        for table in TABLES:
            conn.execute(f"DROP TABLE IF EXISTS {table}")


@pytest.fixture(autouse=True)
def empty_databases():
    _drop_tables(TARGET_URL)
    _drop_tables(SOURCE_URL)
    yield


def _owner(url: str, table: str) -> str:
    with psycopg.connect(url) as conn:
        return conn.execute(
            "SELECT tableowner FROM pg_tables WHERE schemaname = 'public' AND tablename = %s",
            (table,),
        ).fetchone()[0]


def _current_user(url: str) -> str:
    with psycopg.connect(url) as conn:
        return conn.execute("SELECT current_user").fetchone()[0]


def _bundle(refresh: str | None = "refresh-1", access: str = "access-1") -> GoogleTokenBundle:
    return GoogleTokenBundle(
        access_token=access,
        refresh_token=refresh,
        expires_in=3600,
        scope="https://www.googleapis.com/auth/calendar",
        google_sub="sub-1234",
        email="s@example.com",
    )


def test_service_creates_its_tables_owned_by_its_own_role(monkeypatch):
    monkeypatch.setenv("VANS_MCP_DATABASE_URL", TARGET_URL)
    monkeypatch.setenv("OAUTH_TOKEN_ENCRYPTION_KEY", TokenEncryptor.generate_key())

    usage = UsageLogger.from_env()
    store = OAuthConnectionStore.from_env()
    assert store is not None

    app_role = _current_user(TARGET_URL)
    assert _owner(TARGET_URL, "mcp_usage") == app_role
    assert _owner(TARGET_URL, "mcp_oauth_connections") == app_role

    store.upsert_google_tokens(user_id=1234, bundle=_bundle())
    usage.record(tool_name="calendar_list_events", success=True, user_id=1234, latency_ms=5)

    assert store.is_connected(1234)
    assert store.get_valid_access_token(1234).refresh_token == "refresh-1"
    with psycopg.connect(TARGET_URL) as conn:
        assert conn.execute(
            "SELECT user_id, tool_name, api_key_id FROM mcp_usage"
        ).fetchall() == [(1234, "calendar_list_events", None)]


# --- the one-off move: neondb -> vans_mcp_server -------------------------------------


def _seed_source(key: str) -> OAuthConnectionStore:
    """The router's neondb as it is today: both tables written by the old service."""
    store = OAuthConnectionStore(SOURCE_URL, TokenEncryptor(key))
    store.upsert_google_tokens(user_id=1234, bundle=_bundle("refresh-1234"))
    store.upsert_discord_bot_token(
        user_id=42, bot_token="bot-token-42", application_id="app-42", bot_user_id="b-42"
    )
    usage = UsageLogger(SOURCE_URL)
    with psycopg.connect(SOURCE_URL) as conn:
        # A row from before Key Check still carries api_key_id.
        conn.execute(
            "INSERT INTO mcp_usage (user_id, api_key_id, tool_name, success, latency_ms, created_at)"
            " VALUES (1234, 77, 'calendar_list_events', true, 12, '2026-10-01T00:00:00+00:00')"
        )
    usage.record(tool_name="discord_send_message", success=False, user_id=42, error_type="X")
    return store


def _rows(url: str, table: str) -> list[tuple]:
    with psycopg.connect(url) as conn:
        return conn.execute(f"SELECT * FROM {table} ORDER BY id").fetchall()


def test_copy_moves_both_tables_unchanged_and_the_service_reads_them():
    from vans_mcp_server import table_move

    key = TokenEncryptor.generate_key()
    _seed_source(key)

    summary = table_move.copy(SOURCE_URL, TARGET_URL)

    for table in TABLES:
        assert _rows(TARGET_URL, table) == _rows(SOURCE_URL, table)
        assert summary.tables[table].source_rows == summary.tables[table].target_rows
    assert summary.tables["mcp_usage"].source_rows == 2
    assert summary.tables["mcp_oauth_connections"].source_rows == 2
    app_role = _current_user(TARGET_URL)
    assert _owner(TARGET_URL, "mcp_usage") == app_role
    assert _owner(TARGET_URL, "mcp_oauth_connections") == app_role

    moved = OAuthConnectionStore(TARGET_URL, TokenEncryptor(key))
    assert moved.get_valid_access_token(1234).refresh_token == "refresh-1234"
    assert moved.get_discord_bot_connection(42).bot_token == "bot-token-42"

    # New rows after the switch continue the ids instead of colliding with copied ones.
    UsageLogger(TARGET_URL).record(tool_name="tasks_list_tasks", success=True, user_id=1234)
    moved.upsert_google_tokens(user_id=7, bundle=_bundle("refresh-7"))
    assert [r[0] for r in _rows(TARGET_URL, "mcp_usage")] == [1, 2, 3]
    assert [r[0] for r in _rows(TARGET_URL, "mcp_oauth_connections")] == [1, 2, 3]


def test_copy_again_brings_over_the_gap_and_nothing_twice():
    from vans_mcp_server import table_move

    key = TokenEncryptor.generate_key()
    source_store = _seed_source(key)
    table_move.copy(SOURCE_URL, TARGET_URL)

    # Writes that land on neondb between the first copy and the pause.
    UsageLogger(SOURCE_URL).record(tool_name="gmail_list_labels", success=True, user_id=1234)
    source_store.upsert_google_tokens(user_id=1234, bundle=_bundle("refresh-1234-new", "a2"))
    source_store.upsert_google_tokens(user_id=555, bundle=_bundle("refresh-555"))

    again = table_move.copy(SOURCE_URL, TARGET_URL)
    assert again.tables["mcp_usage"].copied == 1
    assert again.all_match
    for table in TABLES:
        assert _rows(TARGET_URL, table) == _rows(SOURCE_URL, table)
    moved = OAuthConnectionStore(TARGET_URL, TokenEncryptor(key))
    assert moved.get_valid_access_token(1234).refresh_token == "refresh-1234-new"
    assert moved.get_valid_access_token(555).refresh_token == "refresh-555"

    third = table_move.copy(SOURCE_URL, TARGET_URL)
    assert third.tables["mcp_usage"].copied == 0
    assert third.tables["mcp_usage"].target_rows == 3
    assert third.all_match


def test_copy_refuses_once_the_service_writes_the_target():
    from vans_mcp_server import table_move

    key = TokenEncryptor.generate_key()
    _seed_source(key)
    table_move.copy(SOURCE_URL, TARGET_URL)

    # After the switch the service writes only the target.
    moved = OAuthConnectionStore(TARGET_URL, TokenEncryptor(key))
    moved.upsert_google_tokens(user_id=1234, bundle=_bundle("refresh-after-switch"))
    before = _rows(TARGET_URL, "mcp_oauth_connections")

    with pytest.raises(table_move.MoveError, match="already switched"):
        table_move.copy(SOURCE_URL, TARGET_URL)
    assert _rows(TARGET_URL, "mcp_oauth_connections") == before

    UsageLogger(TARGET_URL).record(tool_name="tasks_list_tasks", success=True, user_id=1)
    with pytest.raises(table_move.MoveError, match="already switched"):
        table_move.copy(SOURCE_URL, TARGET_URL)


def test_pause_stops_writes_on_neondb_and_resume_restores_them():
    from vans_mcp_server import table_move

    key = TokenEncryptor.generate_key()
    source_store = _seed_source(key)

    table_move.pause(SOURCE_URL)
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        source_store.upsert_google_tokens(user_id=9, bundle=_bundle("refresh-9"))
    # Reads keep working during the pause: existing connections still answer.
    assert source_store.get_valid_access_token(1234).refresh_token == "refresh-1234"
    assert table_move.status(SOURCE_URL, TARGET_URL).source_paused

    table_move.resume(SOURCE_URL)
    source_store.upsert_google_tokens(user_id=9, bundle=_bundle("refresh-9"))
    assert not table_move.status(SOURCE_URL, TARGET_URL).source_paused


SUPERUSER_URL = os.environ.get("VANS_MCP_TEST_SUPERUSER_URL", "")


@pytest.mark.skipif(not SUPERUSER_URL, reason="VANS_MCP_TEST_SUPERUSER_URL not set")
def test_pause_holds_when_the_owner_can_write_all_data():
    # On Neon, neondb_owner is in neon_superuser, which holds pg_write_all_data: giving up
    # its own table privileges does not stop it writing.
    from vans_mcp_server import table_move

    owner = _current_user(SOURCE_URL)
    with psycopg.connect(SUPERUSER_URL, autocommit=True) as admin:
        admin.execute(f'GRANT pg_write_all_data TO "{owner}"')
    try:
        source_store = _seed_source(TokenEncryptor.generate_key())
        table_move.pause(SOURCE_URL)
        try:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                source_store.upsert_google_tokens(user_id=9, bundle=_bundle("refresh-9"))
            with psycopg.connect(SOURCE_URL) as conn, pytest.raises(
                psycopg.errors.InsufficientPrivilege
            ):
                conn.execute(
                    "INSERT INTO mcp_usage (tool_name, success, created_at) VALUES ('t', true, now()::text)"
                )
            assert source_store.get_valid_access_token(1234).refresh_token == "refresh-1234"
            assert table_move.status(SOURCE_URL, TARGET_URL).source_paused
        finally:
            table_move.resume(SOURCE_URL)
        source_store.upsert_google_tokens(user_id=9, bundle=_bundle("refresh-9"))
        assert not table_move.status(SOURCE_URL, TARGET_URL).source_paused
    finally:
        with psycopg.connect(SUPERUSER_URL, autocommit=True) as admin:
            admin.execute(f'REVOKE pg_write_all_data FROM "{owner}"')


def test_final_copy_needs_the_pause_and_ends_with_an_exact_match():
    from vans_mcp_server import table_move

    key = TokenEncryptor.generate_key()
    _seed_source(key)
    table_move.copy(SOURCE_URL, TARGET_URL)
    UsageLogger(SOURCE_URL).record(tool_name="gmail_list_labels", success=True, user_id=1234)

    with pytest.raises(table_move.MoveError, match="pause"):
        table_move.final_copy(SOURCE_URL, TARGET_URL)

    table_move.pause(SOURCE_URL)
    try:
        final = table_move.final_copy(SOURCE_URL, TARGET_URL)
    finally:
        table_move.resume(SOURCE_URL)
    assert final.source_paused
    assert final.all_match
    assert final.tables["mcp_usage"].copied == 1
    for table in TABLES:
        assert _rows(TARGET_URL, table) == _rows(SOURCE_URL, table)


def _secrets_in(url: str) -> list[str]:
    from urllib.parse import urlsplit

    return [urlsplit(url).password or "missing-password"]


def test_command_line_prints_counts_and_no_secrets(monkeypatch, capsys):
    from vans_mcp_server import table_move

    key = TokenEncryptor.generate_key()
    _seed_source(key)
    monkeypatch.setenv("VANS_MCP_MOVE_SOURCE_URL", SOURCE_URL)
    monkeypatch.setenv("VANS_MCP_DATABASE_URL", TARGET_URL)

    assert table_move.main(["copy"]) == 0
    assert table_move.main(["pause"]) == 0
    assert table_move.main(["final-copy"]) == 0
    assert table_move.main(["resume"]) == 0
    out = capsys.readouterr().out

    assert "mcp_oauth_connections" in out and "READY" in out
    ciphertext = _rows(SOURCE_URL, "mcp_oauth_connections")[0][3]
    for secret in [*_secrets_in(SOURCE_URL), *_secrets_in(TARGET_URL), key, ciphertext,
                   "refresh-1234", "bot-token-42"]:
        assert secret not in out


def test_command_line_failure_does_not_print_the_password(monkeypatch, capsys):
    from urllib.parse import urlsplit, urlunsplit

    from vans_mcp_server import table_move

    parts = urlsplit(TARGET_URL)
    wrong = "wrong-pw-marker-31337"
    bad_target = urlunsplit(
        parts._replace(netloc=f"{parts.username}:{wrong}@{parts.hostname}:{parts.port}")
    )
    monkeypatch.setenv("VANS_MCP_MOVE_SOURCE_URL", SOURCE_URL)
    monkeypatch.setenv("VANS_MCP_DATABASE_URL", bad_target)

    assert table_move.main(["status"]) == 1
    out = capsys.readouterr().out
    assert "FAILED" in out
    assert wrong not in out
    assert _secrets_in(SOURCE_URL)[0] not in out
