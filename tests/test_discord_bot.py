from __future__ import annotations

from unittest.mock import MagicMock, patch

import httpx
import pytest

from vans_mcp_server.oauth.crypto import TokenEncryptor
from vans_mcp_server.oauth.discord_connect import DiscordConnectState
from vans_mcp_server.oauth.store import (
    PROVIDER_DISCORD_BOT,
    OAuthConnectionStore,
    StoredDiscordBotConnection,
)
from vans_mcp_server.tools import discord as discord_tools


def test_discord_connect_state_roundtrip():
    svc = DiscordConnectState(session_secret="session-secret-for-tests")
    state = svc.create_connect_state(42)
    assert svc.verify_connect_state(state) == 42
    assert svc.verify_connect_state("bogus") is None


def test_build_invite_url_includes_guild():
    url = discord_tools.build_invite_url(
        application_id="app123", guild_id="guild999"
    )
    assert "client_id=app123" in url
    assert "guild_id=guild999" in url
    assert "disable_guild_select=true" in url
    assert "scope=bot" in url
    assert f"permissions={discord_tools.BOT_PERMISSIONS}" in url


def test_send_message_requires_confirm(monkeypatch):
    monkeypatch.setenv("DISCORD_GUILD_ID", "guild1")
    store = MagicMock()
    result = discord_tools.send_message(
        user_id=1,
        store=store,
        channel_id="ch1",
        content="hello",
        confirm=False,
    )
    assert result["error"] == "confirmation_required"
    assert result["sent"] is False
    store.get_discord_bot_connection.assert_not_called()


def test_send_message_rejects_empty_channel_before_confirm(monkeypatch):
    monkeypatch.setenv("DISCORD_GUILD_ID", "guild1")
    store = MagicMock()
    with pytest.raises(ValueError, match="channel_id"):
        discord_tools.send_message(
            user_id=1,
            store=store,
            channel_id="   ",
            content="hello",
            confirm=False,
        )


def test_list_channels_uses_bot_auth(monkeypatch):
    monkeypatch.setenv("DISCORD_GUILD_ID", "guild1")
    store = MagicMock()
    store.get_discord_bot_connection.return_value = StoredDiscordBotConnection(
        user_id=1,
        bot_token="bot-token-secret",
        application_id="app1",
        bot_user_id="botuser1",
    )

    guilds_resp = MagicMock()
    guilds_resp.status_code = 200
    guilds_resp.json.return_value = [{"id": "guild1", "name": "Class"}]

    channels_resp = MagicMock()
    channels_resp.status_code = 200
    channels_resp.raise_for_status = MagicMock()
    channels_resp.json.return_value = [
        {"id": "c1", "name": "general", "type": 0, "position": 0},
        {"id": "c2", "name": "voice", "type": 2, "position": 1},
    ]

    fake_client = MagicMock()
    fake_client.__enter__.return_value = fake_client
    fake_client.__exit__.return_value = None

    def get_side_effect(url, **kwargs):
        if url.endswith("/users/@me/guilds"):
            return guilds_resp
        if url.endswith("/guilds/guild1/channels"):
            return channels_resp
        raise AssertionError(f"unexpected GET {url}")

    fake_client.get.side_effect = get_side_effect

    with patch("vans_mcp_server.tools.discord.httpx.Client", return_value=fake_client):
        result = discord_tools.list_channels(user_id=1, store=store)

    assert result["guild_id"] == "guild1"
    assert len(result["channels"]) == 1
    assert result["channels"][0]["id"] == "c1"
    auth = fake_client.get.call_args_list[-1].kwargs["headers"]["Authorization"]
    assert auth == "Bot bot-token-secret"


def test_read_messages_rejects_channel_outside_guild(monkeypatch):
    monkeypatch.setenv("DISCORD_GUILD_ID", "guild1")
    store = MagicMock()
    store.get_discord_bot_connection.return_value = StoredDiscordBotConnection(
        user_id=1,
        bot_token="bot-token-secret",
        application_id="app1",
        bot_user_id="botuser1",
    )

    guilds_resp = MagicMock()
    guilds_resp.status_code = 200
    guilds_resp.json.return_value = [{"id": "guild1"}]

    channel_resp = MagicMock()
    channel_resp.status_code = 200
    channel_resp.raise_for_status = MagicMock()
    channel_resp.json.return_value = {"id": "ch-other", "guild_id": "other-guild"}

    fake_client = MagicMock()
    fake_client.__enter__.return_value = fake_client
    fake_client.__exit__.return_value = None

    def get_side_effect(url, **kwargs):
        if url.endswith("/users/@me/guilds"):
            return guilds_resp
        if url.endswith("/channels/ch-other"):
            return channel_resp
        raise AssertionError(f"unexpected GET {url}")

    fake_client.get.side_effect = get_side_effect

    with patch("vans_mcp_server.tools.discord.httpx.Client", return_value=fake_client):
        result = discord_tools.read_messages(
            user_id=1, store=store, channel_id="ch-other", limit=5
        )

    assert result["error"] == "channel_not_in_guild"
    assert result["channel_id"] == "ch-other"


def test_send_message_with_confirm(monkeypatch):
    monkeypatch.setenv("DISCORD_GUILD_ID", "guild1")
    store = MagicMock()
    store.get_discord_bot_connection.return_value = StoredDiscordBotConnection(
        user_id=1,
        bot_token="bot-token-secret",
        application_id="app1",
        bot_user_id="botuser1",
    )

    guilds_resp = MagicMock()
    guilds_resp.status_code = 200
    guilds_resp.json.return_value = [{"id": "guild1"}]

    channel_resp = MagicMock()
    channel_resp.status_code = 200
    channel_resp.raise_for_status = MagicMock()
    channel_resp.json.return_value = {"id": "ch1", "guild_id": "guild1"}

    post_resp = MagicMock()
    post_resp.status_code = 200
    post_resp.raise_for_status = MagicMock()
    post_resp.json.return_value = {
        "id": "msg1",
        "content": "hello class",
        "channel_id": "ch1",
    }

    fake_client = MagicMock()
    fake_client.__enter__.return_value = fake_client
    fake_client.__exit__.return_value = None

    def get_side_effect(url, **kwargs):
        if url.endswith("/users/@me/guilds"):
            return guilds_resp
        if url.endswith("/channels/ch1"):
            return channel_resp
        raise AssertionError(f"unexpected GET {url}")

    fake_client.get.side_effect = get_side_effect
    fake_client.post.return_value = post_resp

    with patch("vans_mcp_server.tools.discord.httpx.Client", return_value=fake_client):
        result = discord_tools.send_message(
            user_id=1,
            store=store,
            channel_id="ch1",
            content="hello class",
            confirm=True,
        )

    assert result["sent"] is True
    assert result["id"] == "msg1"
    kwargs = fake_client.post.call_args.kwargs
    assert kwargs["headers"]["Authorization"] == "Bot bot-token-secret"
    assert kwargs["json"] == {"content": "hello class"}


def test_list_channels_not_in_guild(monkeypatch):
    monkeypatch.setenv("DISCORD_GUILD_ID", "guild1")
    store = MagicMock()
    store.get_discord_bot_connection.return_value = StoredDiscordBotConnection(
        user_id=1,
        bot_token="bot-token-secret",
        application_id="app1",
        bot_user_id="botuser1",
    )

    guilds_resp = MagicMock()
    guilds_resp.status_code = 200
    guilds_resp.json.return_value = [{"id": "other"}]

    fake_client = MagicMock()
    fake_client.__enter__.return_value = fake_client
    fake_client.__exit__.return_value = None
    fake_client.get.return_value = guilds_resp

    with patch("vans_mcp_server.tools.discord.httpx.Client", return_value=fake_client):
        result = discord_tools.list_channels(user_id=1, store=store)

    assert result["error"] == "not_in_guild"
    assert "invite_url" in result
    assert "guild_id=guild1" in result["invite_url"]


def test_upsert_discord_bot_token_encrypts(monkeypatch):
    key = TokenEncryptor.generate_key()
    encryptor = TokenEncryptor(key)
    store = OAuthConnectionStore.__new__(OAuthConnectionStore)
    store.database_url = "postgresql://unused"
    store.encryptor = encryptor
    store.oauth = None

    executed = {}

    class FakeConn:
        def execute(self, sql, params=None):
            executed["sql"] = sql
            executed["params"] = params
            return self

        def commit(self):
            executed["committed"] = True

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

    with patch("vans_mcp_server.oauth.store.psycopg.connect", return_value=FakeConn()):
        store.upsert_discord_bot_token(
            user_id=7,
            bot_token="my-bot-token",
            application_id="app99",
            bot_user_id="bot99",
        )

    assert executed["committed"] is True
    params = executed["params"]
    assert params[0] == 7
    assert params[1] == PROVIDER_DISCORD_BOT
    assert params[2] != "my-bot-token"
    assert encryptor.decrypt(params[2]) == "my-bot-token"
    assert params[3] == "app99"
    assert params[4] == "bot99"


def test_verify_bot_token_raises_on_401():
    resp = MagicMock()
    resp.status_code = 401
    resp.raise_for_status.side_effect = httpx.HTTPStatusError(
        "401", request=MagicMock(), response=resp
    )
    fake_client = MagicMock()
    fake_client.__enter__.return_value = fake_client
    fake_client.__exit__.return_value = None
    fake_client.get.return_value = resp

    with patch("vans_mcp_server.tools.discord.httpx.Client", return_value=fake_client):
        with pytest.raises(httpx.HTTPStatusError):
            discord_tools.verify_bot_token("bad-token")
