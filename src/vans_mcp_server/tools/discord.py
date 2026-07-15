from __future__ import annotations

import json
import os
from typing import Any
from urllib.parse import urlencode

import httpx

from vans_mcp_server.oauth.discord_connect import DiscordConnectState
from vans_mcp_server.oauth.store import OAuthConnectionStore

DISCORD_API = "https://discord.com/api/v10"
# VIEW_CHANNEL | SEND_MESSAGES | READ_MESSAGE_HISTORY
BOT_PERMISSIONS = (1 << 10) | (1 << 11) | (1 << 16)


def to_json(data: dict[str, Any]) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2)


def classroom_guild_id() -> str | None:
    gid = (os.environ.get("DISCORD_GUILD_ID") or "").strip()
    return gid or None


def guild_configured() -> bool:
    return classroom_guild_id() is not None


def build_invite_url(*, application_id: str, guild_id: str | None = None) -> str:
    gid = guild_id or classroom_guild_id() or ""
    params: dict[str, str] = {
        "client_id": application_id.strip(),
        "permissions": str(BOT_PERMISSIONS),
        "scope": "bot",
    }
    if gid:
        params["guild_id"] = gid
        params["disable_guild_select"] = "true"
    return f"https://discord.com/oauth2/authorize?{urlencode(params)}"


def build_connect_url(
    state_svc: DiscordConnectState | None,
    *,
    public_url: str,
    user_id: int,
) -> str | None:
    if state_svc is None or not state_svc.is_configured():
        return None
    state = state_svc.create_connect_state(user_id)
    base = public_url.rstrip("/")
    return f"{base}/connect/discord/start?state={state}"


def not_configured_payload() -> dict[str, Any]:
    return {
        "error": "not_configured",
        "message": (
            "Discord classroom guild is not configured. "
            "Set DISCORD_GUILD_ID on the server."
        ),
        "discord_guild_configured": False,
    }


def not_connected_payload(
    *,
    connect_url: str | None,
    store_configured: bool,
    state_configured: bool,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "error": "not_connected",
        "message": (
            "Discord bot is not connected for this user. "
            "Open connect_url in a browser, paste your Bot Token and Application ID "
            "(do not paste the token into chat)."
        ),
        "store_configured": store_configured,
        "state_configured": state_configured,
    }
    if connect_url:
        payload["connect_url"] = connect_url
    else:
        payload["hint"] = (
            "Set SESSION_SECRET, DATABASE_URL, OAUTH_TOKEN_ENCRYPTION_KEY, "
            "and DISCORD_GUILD_ID on the server."
        )
    return payload


def not_in_guild_payload(
    *,
    invite_url: str | None,
    guild_id: str,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "error": "not_in_guild",
        "message": (
            "Your bot is connected but not in the classroom Discord server. "
            "Open invite_url to add the bot, then try again."
        ),
        "guild_id": guild_id,
    }
    if invite_url:
        payload["invite_url"] = invite_url
    return payload


def confirmation_required_payload(
    *,
    channel_id: str,
    content: str,
) -> dict[str, Any]:
    return {
        "error": "confirmation_required",
        "message": (
            "Refusing to send Discord message without confirm=true. "
            "Ask the human to confirm, then call again with confirm=true."
        ),
        "action": "send",
        "channel_id": channel_id,
        "content_preview": content[:200],
        "sent": False,
    }


def forbidden_payload(*, detail: str | None = None) -> dict[str, Any]:
    return {
        "error": "forbidden",
        "message": detail
        or "Discord API returned 403. Check bot channel permissions in the classroom server.",
    }


def connection_status(
    *,
    user_id: int,
    store: OAuthConnectionStore | None,
    state_svc: DiscordConnectState | None,
    public_url: str,
) -> dict[str, Any]:
    guild_id = classroom_guild_id()
    store_ok = store is not None
    state_ok = state_svc is not None and state_svc.is_configured()
    connect_url = build_connect_url(state_svc, public_url=public_url, user_id=user_id)
    connected = bool(store and store.is_discord_bot_connected(user_id))
    result: dict[str, Any] = {
        "connected": connected,
        "discord_guild_configured": bool(guild_id),
        "guild_id": guild_id,
        "connect_url": connect_url,
        "instructions": (
            "1) Create an Application + Bot in Discord Developer Portal. "
            "2) Open connect_url and paste Application ID + Bot Token (never in chat). "
            "3) Open the invite_url shown after connect to add the bot to the classroom server. "
            "4) Enable Message Content Intent if you need to read message text."
        ),
    }
    if connected and store is not None:
        conn = store.get_discord_bot_connection(user_id)
        if conn and conn.application_id and guild_id:
            result["invite_url"] = build_invite_url(
                application_id=conn.application_id, guild_id=guild_id
            )
            result["application_id"] = conn.application_id
            result["bot_user_id"] = conn.bot_user_id
    if not guild_id:
        result["error"] = "not_configured"
    elif not connected:
        result.update(
            not_connected_payload(
                connect_url=connect_url,
                store_configured=store_ok,
                state_configured=state_ok,
            )
        )
        # Keep connected=false and connect_url from above; error already set.
        result["connected"] = False
    return result


def verify_bot_token(bot_token: str) -> dict[str, Any]:
    """Call GET /users/@me with Bot token. Raises httpx.HTTPStatusError on failure."""
    token = (bot_token or "").strip()
    if not token:
        raise ValueError("bot_token is required")
    with httpx.Client(timeout=20.0) as client:
        resp = client.get(
            f"{DISCORD_API}/users/@me",
            headers={"Authorization": f"Bot {token}"},
        )
        resp.raise_for_status()
        return resp.json()


def _auth_headers(bot_token: str) -> dict[str, str]:
    return {"Authorization": f"Bot {bot_token}", "Content-Type": "application/json"}


def _require_ready(
    *,
    user_id: int,
    store: OAuthConnectionStore | None,
) -> tuple[str, str]:
    """Return (bot_token, guild_id) or raise LookupError / RuntimeError."""
    guild_id = classroom_guild_id()
    if not guild_id:
        raise RuntimeError("not_configured")
    if store is None:
        raise LookupError("not_connected")
    conn = store.get_discord_bot_connection(user_id)
    if conn is None:
        raise LookupError("not_connected")
    return conn.bot_token, guild_id


def _bot_in_guild(client: httpx.Client, *, bot_token: str, guild_id: str) -> bool:
    resp = client.get(
        f"{DISCORD_API}/users/@me/guilds",
        headers=_auth_headers(bot_token),
    )
    if resp.status_code == 403:
        # Some bots cannot list guilds; fall back to guild fetch.
        g = client.get(
            f"{DISCORD_API}/guilds/{guild_id}",
            headers=_auth_headers(bot_token),
        )
        return g.status_code == 200
    if resp.status_code != 200:
        return False
    guilds = resp.json()
    return any(str(g.get("id")) == str(guild_id) for g in guilds)


def _ensure_in_guild(
    *,
    user_id: int,
    store: OAuthConnectionStore,
    bot_token: str,
    guild_id: str,
) -> dict[str, Any] | None:
    """Return error payload if bot not in guild; else None."""
    with httpx.Client(timeout=20.0) as client:
        if _bot_in_guild(client, bot_token=bot_token, guild_id=guild_id):
            return None
    conn = store.get_discord_bot_connection(user_id)
    invite = None
    if conn and conn.application_id:
        invite = build_invite_url(application_id=conn.application_id, guild_id=guild_id)
    return not_in_guild_payload(invite_url=invite, guild_id=guild_id)


def list_channels(
    *,
    user_id: int,
    store: OAuthConnectionStore | None,
) -> dict[str, Any]:
    bot_token, guild_id = _require_ready(user_id=user_id, store=store)
    assert store is not None
    blocked = _ensure_in_guild(
        user_id=user_id, store=store, bot_token=bot_token, guild_id=guild_id
    )
    if blocked is not None:
        return blocked

    with httpx.Client(timeout=20.0) as client:
        resp = client.get(
            f"{DISCORD_API}/guilds/{guild_id}/channels",
            headers=_auth_headers(bot_token),
        )
        if resp.status_code == 403:
            return forbidden_payload()
        resp.raise_for_status()
        channels = resp.json()

    # type 0 = GUILD_TEXT, 5 = GUILD_ANNOUNCEMENT, 15 = GUILD_FORUM (skip for MVP list clarity)
    textish = []
    for ch in channels:
        if ch.get("type") not in (0, 5):
            continue
        textish.append(
            {
                "id": ch.get("id"),
                "name": ch.get("name"),
                "type": ch.get("type"),
                "position": ch.get("position"),
            }
        )
    textish.sort(key=lambda c: (c.get("position") is None, c.get("position") or 0))
    return {
        "guild_id": guild_id,
        "channels": textish,
        "source": "discord",
    }


def _channel_in_guild(
    client: httpx.Client, *, bot_token: str, channel_id: str, guild_id: str
) -> bool:
    resp = client.get(
        f"{DISCORD_API}/channels/{channel_id}",
        headers=_auth_headers(bot_token),
    )
    if resp.status_code == 404:
        return False
    if resp.status_code == 403:
        raise PermissionError("forbidden")
    resp.raise_for_status()
    data = resp.json()
    return str(data.get("guild_id") or "") == str(guild_id)


def read_messages(
    *,
    user_id: int,
    store: OAuthConnectionStore | None,
    channel_id: str,
    limit: int = 20,
) -> dict[str, Any]:
    cid = (channel_id or "").strip()
    if not cid:
        raise ValueError("channel_id is required")
    lim = max(1, min(int(limit), 100))
    bot_token, guild_id = _require_ready(user_id=user_id, store=store)
    assert store is not None
    blocked = _ensure_in_guild(
        user_id=user_id, store=store, bot_token=bot_token, guild_id=guild_id
    )
    if blocked is not None:
        return blocked

    with httpx.Client(timeout=20.0) as client:
        try:
            if not _channel_in_guild(
                client, bot_token=bot_token, channel_id=cid, guild_id=guild_id
            ):
                return {
                    "error": "channel_not_in_guild",
                    "message": "Channel is not in the classroom Discord server.",
                    "guild_id": guild_id,
                    "channel_id": cid,
                }
        except PermissionError:
            return forbidden_payload()
        resp = client.get(
            f"{DISCORD_API}/channels/{cid}/messages",
            params={"limit": lim},
            headers=_auth_headers(bot_token),
        )
        if resp.status_code == 403:
            return forbidden_payload(
                detail=(
                    "Cannot read messages. Check bot permissions and "
                    "Message Content Intent in the Developer Portal."
                )
            )
        resp.raise_for_status()
        messages = resp.json()

    out_msgs = []
    for m in messages:
        author = m.get("author") or {}
        out_msgs.append(
            {
                "id": m.get("id"),
                "content": m.get("content"),
                "timestamp": m.get("timestamp"),
                "author": {
                    "id": author.get("id"),
                    "username": author.get("username"),
                    "bot": author.get("bot"),
                },
            }
        )
    return {
        "guild_id": guild_id,
        "channel_id": cid,
        "messages": out_msgs,
        "source": "discord",
    }


def send_message(
    *,
    user_id: int,
    store: OAuthConnectionStore | None,
    channel_id: str,
    content: str,
    confirm: bool = False,
) -> dict[str, Any]:
    cid = (channel_id or "").strip()
    body = content if content is not None else ""
    if not cid:
        raise ValueError("channel_id is required")
    if not str(body).strip():
        raise ValueError("content is required")
    if not confirm:
        return confirmation_required_payload(channel_id=cid, content=str(body))

    bot_token, guild_id = _require_ready(user_id=user_id, store=store)
    assert store is not None
    blocked = _ensure_in_guild(
        user_id=user_id, store=store, bot_token=bot_token, guild_id=guild_id
    )
    if blocked is not None:
        return blocked

    with httpx.Client(timeout=20.0) as client:
        try:
            if not _channel_in_guild(
                client, bot_token=bot_token, channel_id=cid, guild_id=guild_id
            ):
                return {
                    "error": "channel_not_in_guild",
                    "message": "Channel is not in the classroom Discord server.",
                    "guild_id": guild_id,
                    "channel_id": cid,
                    "sent": False,
                }
        except PermissionError:
            return forbidden_payload()
        resp = client.post(
            f"{DISCORD_API}/channels/{cid}/messages",
            headers=_auth_headers(bot_token),
            json={"content": str(body)},
        )
        if resp.status_code == 403:
            return forbidden_payload(detail="Cannot send message in this channel.")
        resp.raise_for_status()
        created = resp.json()

    return {
        "sent": True,
        "id": created.get("id"),
        "channel_id": cid,
        "guild_id": guild_id,
        "content": created.get("content"),
        "source": "discord",
    }
