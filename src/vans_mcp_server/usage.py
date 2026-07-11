from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timezone
from typing import Any

import psycopg

logger = logging.getLogger(__name__)


def _normalize_database_url(url: str) -> str:
    if url.startswith("postgres://"):
        return "postgresql://" + url[len("postgres://") :]
    return url


class UsageLogger:
    """Record MCP tool calls. Uses Neon when DATABASE_URL is set, else stdout."""

    def __init__(self, database_url: str | None = None) -> None:
        self.database_url = (
            _normalize_database_url(database_url) if database_url else None
        )
        if self.database_url:
            self._ensure_table()

    @classmethod
    def from_env(cls) -> UsageLogger:
        return cls(os.environ.get("DATABASE_URL") or None)

    def _ensure_table(self) -> None:
        assert self.database_url
        with psycopg.connect(self.database_url) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS mcp_usage (
                    id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                    user_id INTEGER,
                    api_key_id INTEGER,
                    tool_name TEXT NOT NULL,
                    success BOOLEAN NOT NULL,
                    latency_ms INTEGER,
                    error_type TEXT,
                    created_at TEXT NOT NULL
                )
                """
            )
            conn.commit()

    def record(
        self,
        *,
        tool_name: str,
        success: bool,
        user_id: int | None = None,
        api_key_id: int | None = None,
        latency_ms: int | None = None,
        error_type: str | None = None,
    ) -> None:
        created_at = datetime.now(timezone.utc).isoformat()
        payload: dict[str, Any] = {
            "user_id": user_id,
            "api_key_id": api_key_id,
            "tool_name": tool_name,
            "success": success,
            "latency_ms": latency_ms,
            "error_type": error_type,
            "created_at": created_at,
        }
        if not self.database_url:
            logger.info("mcp_usage %s", payload)
            return
        try:
            with psycopg.connect(self.database_url) as conn:
                conn.execute(
                    """
                    INSERT INTO mcp_usage
                        (user_id, api_key_id, tool_name, success, latency_ms, error_type, created_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        user_id,
                        api_key_id,
                        tool_name,
                        success,
                        latency_ms,
                        error_type,
                        created_at,
                    ),
                )
                conn.commit()
        except Exception:
            logger.exception("failed to write mcp_usage; falling back to log")
            logger.info("mcp_usage %s", payload)


class timed_tool:
    """Context helper to measure latency for usage logging."""

    def __init__(self) -> None:
        self.start = 0.0
        self.latency_ms = 0

    def __enter__(self) -> timed_tool:
        self.start = time.perf_counter()
        return self

    def __exit__(self, *args: object) -> None:
        self.latency_ms = int((time.perf_counter() - self.start) * 1000)
