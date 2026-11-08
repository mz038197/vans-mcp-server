"""Key Check client: ask the router whether a vcr_sk_ key may be used (ADR 0079, 0082).

One call returns a KeyIdentity or a KeyRefusal. The router's contract is the only
standard; this module has no shared package behind it.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from dataclasses import dataclass
from typing import Callable

import httpx

KEY_CHECK_PATH = "/internal/key-check"
VCR_KEY_PREFIX = "vcr_sk_"
INVALID_NOTICE = "無效的 API 金鑰"
UNAVAILABLE_NOTICE = "驗證服務暫時無法使用，請稍後再試"
PASS_CACHE_SECONDS = 60.0
TIMEOUT_SECONDS = 3.0
LOGGED_KEY_CHARS = 12

logger = logging.getLogger(__name__)


def key_prefix(api_key: str) -> str:
    """The only part of a key that may reach a log, exception, or Signal."""
    return api_key[:LOGGED_KEY_CHARS]


@dataclass(frozen=True)
class KeyIdentity:
    """Who a key belongs to.

    ``user_id`` is the Classroom answer's ``student_id`` or the Personal
    answer's ``user_id``: the integer the tools and tables key on.
    """

    kind: str
    user_id: int
    class_id: int | None = None
    class_session_id: int | None = None
    is_admin: bool = False
    classes: tuple[tuple[int, str], ...] = ()


@dataclass(frozen=True)
class KeyRefusal:
    reason: str
    message: str


class KeyCheckClient:
    def __init__(
        self,
        *,
        vcr_url: str,
        service_credential: str,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.vcr_url = vcr_url.rstrip("/")
        self.service_credential = service_credential
        self.transport = transport
        self.clock = clock
        # SHA-256 of the key -> (identity, expiry on self.clock). No key text.
        self._passes: dict[str, tuple[KeyIdentity, float]] = {}

    async def check(self, api_key: str | None) -> KeyIdentity | KeyRefusal:
        api_key = (api_key or "").strip()
        if not api_key.startswith(VCR_KEY_PREFIX):
            return KeyRefusal(reason="invalid", message=INVALID_NOTICE)
        digest = hashlib.sha256(api_key.encode("utf-8")).hexdigest()
        now = self.clock()
        cached = self._passes.get(digest)
        if cached is not None and now < cached[1]:
            return cached[0]
        answer = await self._ask_router(api_key)
        if isinstance(answer, KeyIdentity):
            self._forget_expired(now)
            self._passes[digest] = (answer, now + PASS_CACHE_SECONDS)
        return answer

    def _forget_expired(self, now: float) -> None:
        for digest in [d for d, (_, until) in self._passes.items() if now >= until]:
            del self._passes[digest]

    async def _ask_router(self, api_key: str) -> KeyIdentity | KeyRefusal:
        try:
            # The whole call, not each httpx phase, must end within 3 seconds.
            async with asyncio.timeout(TIMEOUT_SECONDS):
                async with httpx.AsyncClient(
                    transport=self.transport, timeout=TIMEOUT_SECONDS
                ) as http:
                    response = await http.post(
                        self.vcr_url + KEY_CHECK_PATH,
                        json={"api_key": api_key},
                        headers={
                            "Authorization": f"Bearer {self.service_credential}"
                        },
                    )
        except TimeoutError:
            return self._unavailable(api_key, "timed out after 3 s")
        except httpx.HTTPError as exc:
            return self._unavailable(api_key, type(exc).__name__)
        if response.status_code != 200:
            return self._unavailable(api_key, f"HTTP {response.status_code}")
        try:
            return _answer_from(response.json())
        except (ValueError, KeyError, TypeError):
            return self._unavailable(api_key, "unreadable answer")

    def _unavailable(self, api_key: str, cause: str) -> KeyRefusal:
        logger.error(
            "Key Check unavailable (%s) for key %s", cause, key_prefix(api_key)
        )
        return KeyRefusal(reason="unavailable", message=UNAVAILABLE_NOTICE)


def _answer_from(body: dict) -> KeyIdentity | KeyRefusal:
    if body["accepted"] is not True:
        return KeyRefusal(reason=str(body["reason"]), message=str(body["message"]))
    if body["kind"] == "personal":
        return KeyIdentity(
            kind="personal",
            user_id=int(body["user_id"]),
            is_admin=bool(body["is_admin"]),
            classes=tuple(
                (int(item["id"]), str(item["name"])) for item in body["classes"]
            ),
        )
    if body["kind"] == "classroom":
        return KeyIdentity(
            kind="classroom",
            user_id=int(body["student_id"]),
            class_id=int(body["class_id"]),
            class_session_id=int(body["class_session_id"]),
        )
    raise ValueError("unknown kind")
